import json
import os
from pathlib import Path

from .domain import (
    VOIDABLE_BY_SPEC_INVALIDATION,
    event_path,
    invalid_marker_path,
    lease_is_active,
    make_event,
    same_pin,
    task_path,
)
from .errors import AuthorizationError, OperationalError, PlatformError
from .fingerprint import workspace_fingerprint
from .gitrepo import commit_paths, ensure_repository
from .jsonio import append_jsonl, read_json, read_jsonl, utc_now
from .runner import execute_contract_cases, frozen_contract, machine_criterion_results
from .service import (
    create_trace_review_task,
    propagate_pin_invalidation,
    refresh_workspace,
    stamp_events,
    task_log,
)
from .specs import repository_lock
from .taskfile import read_task, write_task

CANCELABLE_STATES = {"Ready", "In Progress", "Implemented"}


def lifecycle_lock(repository):
    lock_path = Path(repository) / ".aidev-lifecycle.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise OperationalError("lifecycle.already_running", "another lifecycle operation holds the repository lock") from error
    os.close(descriptor)
    return lock_path


def persist(repository, changed_tasks, events, extra_paths, message, push):
    paths = []
    for task_id, (metadata, body) in changed_tasks.items():
        path = task_path(repository, task_id)
        write_task(path, metadata, body)
        paths.append(path.relative_to(repository))
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, events))
    paths.append(events_file.relative_to(repository))
    paths.extend(extra_paths)
    commit = commit_paths(repository, paths, message, push=push)
    return {"commit": commit, "pushed": bool(push and commit)}


def latest_claim_implementer(repository, task_id):
    """实现者判定（AC-16）：最近 claim 事件为 sigt 委托接管（无提案号）时取 on_behalf_of
    （执行 agent 别名），否则取 actor（经典提案流实现者即 claim actor）。"""
    for event in reversed(read_jsonl(event_path(repository))):
        if event.get("task_id") == task_id and event.get("event_type") == "task.claimed":
            if not event.get("proposal_id"):
                delegate = event.get("on_behalf_of")
                if delegate:
                    return delegate
            return event.get("actor")
    return None


def load_json_document(path, code):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError(code, f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise PlatformError(code, f"JSON document must be an object: {path}")
    return value


def validate_evidence_list(value, code):
    if not isinstance(value, list) or not value:
        raise PlatformError(code, "evidence must be a non-empty list")
    for item in value:
        if not isinstance(item, dict) or not item.get("sha256") or not (item.get("path") or item.get("filename")):
            raise PlatformError(code, "each evidence item must contain sha256 and path or filename")


def validate_verification_results(repository, metadata, document):
    if not isinstance(document.get("results"), list):
        raise PlatformError("verify.invalid_results", "results must be a list")
    if document.get("spec_pin") != metadata.get("spec_pin"):
        raise PlatformError("verify.spec_pin_mismatch", "verification results do not match task spec_pin")
    spec_id = metadata["spec_id"]
    version = metadata["spec_pin"]["acceptance_card"]["version"]
    card = read_json(repository / "specs" / spec_id / version / "acceptance-card.json")
    mapping = read_json(repository / "specs" / spec_id / version / "mapping.json")
    criteria = card.get("criteria")
    rows = mapping.get("rows")
    if not isinstance(criteria, list) or not isinstance(rows, list):
        raise PlatformError("verify.invalid_spec", "frozen card or mapping is invalid")
    expected_ids = {item.get("criterion_id") for item in criteria if isinstance(item, dict)}
    manual_ids = {row.get("criterion_id") for row in rows if isinstance(row, dict) and row.get("manual") is True}
    safety_ids = {item.get("criterion_id") for item in criteria if isinstance(item, dict) and item.get("strength") == "safety-veto"}
    actual_ids = []
    for result in document["results"]:
        if not isinstance(result, dict):
            raise PlatformError("verify.invalid_results", "each result must be an object")
        criterion_id = result.get("criterion_id")
        if criterion_id not in expected_ids:
            raise PlatformError("verify.unknown_criterion", f"unknown criterion: {criterion_id}")
        if not isinstance(result.get("passed"), bool):
            raise PlatformError("verify.invalid_results", "passed must be a boolean")
        validate_evidence_list(result.get("evidence"), "verify.invalid_evidence")
        actual_ids.append(criterion_id)
        if criterion_id in safety_ids:
            rerun = result.get("manual_rerun")
            if not isinstance(rerun, dict):
                raise PlatformError("verify.safety_rerun_required", f"safety-veto result requires manual rerun: {criterion_id}")
            command = rerun.get("command")
            if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
                raise PlatformError("verify.safety_rerun_required", f"manual rerun command is invalid: {criterion_id}")
            validate_evidence_list(rerun.get("evidence"), "verify.safety_rerun_required")
    if len(actual_ids) != len(set(actual_ids)):
        raise PlatformError("verify.duplicate_criterion", "criterion appears more than once")
    if set(actual_ids) != expected_ids:
        raise PlatformError("verify.incomplete_results", "results must cover every criterion exactly once")
    if safety_ids & manual_ids:
        raise PlatformError("verify.safety_veto_manual", "safety-veto criterion cannot be manual-only")


def build_executed_document(repository, metadata, verifier, manual_results_file):
    spec_directory, _, mapping = frozen_contract(repository, metadata)
    card = read_json(spec_directory / "acceptance-card.json")
    safety_ids = {item.get("criterion_id") for item in card.get("criteria", []) if isinstance(item, dict) and item.get("strength") == "safety-veto"}
    manual_rows = [row for row in mapping.get("rows", []) if isinstance(row, dict) and row.get("manual") is True]
    manual_results = {}
    if safety_ids or manual_rows:
        if not manual_results_file:
            raise PlatformError("verify.manual_results_required", "manual criteria or safety-veto rerun results are required")
        manual_document = load_json_document(manual_results_file, "verify.invalid_manual_results")
        if manual_document.get("verifier") != verifier:
            raise PlatformError("verify.actor_mismatch", "manual results verifier must match the verify actor")
        for result in manual_document.get("results", []):
            if isinstance(result, dict) and result.get("criterion_id"):
                manual_results[result["criterion_id"]] = result
    case_results = execute_contract_cases(repository, metadata)
    results = machine_criterion_results(case_results, mapping)
    for result in results:
        criterion_id = result["criterion_id"]
        if criterion_id in safety_ids:
            manual_result = manual_results.get(criterion_id)
            if not manual_result or not isinstance(manual_result.get("manual_rerun"), dict):
                raise PlatformError("verify.safety_rerun_required", f"safety-veto manual rerun is required: {criterion_id}")
            result["manual_rerun"] = manual_result["manual_rerun"]
    for row in manual_rows:
        criterion_id = row["criterion_id"]
        if criterion_id not in manual_results:
            raise PlatformError("verify.manual_results_required", f"manual criterion result is required: {criterion_id}")
        results.append(manual_results[criterion_id])
    return {"verifier": verifier, "spec_pin": metadata["spec_pin"], "results": results}


def materialize_machine_evidence(repository, task_id, document):
    evidence_directory = task_path(repository, task_id).parent / "verification"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    extra_paths = []
    for result in document["results"]:
        evidence = result.get("evidence", [])
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or "content" not in item:
                continue
            target = evidence_directory / item["filename"]
            target.write_bytes(item["content"])
            relative = target.relative_to(repository).as_posix()
            extra_paths.append(relative)
            evidence[index] = {"path": relative, "sha256": item["sha256"]}
    return extra_paths


def verify_task(repository, task_id, verifier, confirmed_by, results_file=None, execute=False, manual_results_file=None, push=False):
    repository = ensure_repository(repository)
    if (results_file is None) == (execute is False):
        raise PlatformError("verify.invalid_mode", "provide exactly one of results_file or execute")
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        if metadata.get("state") != "Implemented":
            raise PlatformError("verify.invalid_state", "only an Implemented task can be verified", details={"state": metadata.get("state")})
        if execute:
            document = build_executed_document(repository, metadata, verifier, manual_results_file)
        else:
            document = load_json_document(results_file, "verify.invalid_results")
        if document.get("verifier") != verifier:
            raise PlatformError("verify.actor_mismatch", "results verifier must match the verify actor")
        validate_verification_results(repository, metadata, document)
        failed = [item.get("criterion_id") for item in document["results"] if item.get("passed") is not True]
        if failed:
            raise PlatformError("verify.failed_criterion", "verification results contain failed criteria", details={"criteria": failed})
        if not verifier or verifier == confirmed_by:
            raise PlatformError("verify.invalid_roles", "initial verifier and human confirmer must be different identities")
        implementer = latest_claim_implementer(repository, task_id)
        if implementer and (verifier == implementer or confirmed_by == implementer):
            raise PlatformError("verify.self_review", "implementer cannot verify or confirm their own work")
        extra_paths = materialize_machine_evidence(repository, task_id, document) if execute else []
        metadata["state"] = "Verified"
        metadata["updated_at"] = utc_now()
        # AC-06：verify 落账时记录工作区指纹（算法与 ship 比对共用 aidev.fingerprint）。
        recorded_fingerprint = workspace_fingerprint()
        events = [
            make_event("verification.initial", task_id, verifier, verifier, "Implemented", "Implemented", payload={"results": document, "executed": execute, "workspace_fingerprint": recorded_fingerprint}),
            make_event("verification.confirm", task_id, confirmed_by, confirmed_by, "Implemented", "Verified", payload={"confirmed_by": confirmed_by, "workspace_fingerprint": recorded_fingerprint}),
            make_event("task.verified", task_id, confirmed_by, confirmed_by, "Implemented", "Verified"),
        ]
        return persist(repository, {task_id: (metadata, body)}, events, extra_paths, f"Verify {task_id}", push)
    finally:
        lock.unlink(missing_ok=True)


def reject_task(repository, task_id, category, reason, actor, workspace=None, push=False):
    repository = ensure_repository(repository)
    if category not in {"implementation", "input"}:
        raise PlatformError("reject.invalid_category", "category must be implementation or input")
    if not reason or not reason.strip():
        raise PlatformError("reject.reason_required", "reject reason is required")
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        before = metadata.get("state")
        if category == "implementation":
            if before != "Implemented":
                raise PlatformError("reject.invalid_state", "implementation rejection requires an Implemented task", details={"state": before})
            metadata["state"] = "Ready"
            metadata["lease"] = None
            metadata["void_reason"] = None
            event = make_event("task.rejected", task_id, actor, actor, before, "Ready", payload={"category": category, "reason": reason})
        else:
            if before not in {"Ready", "In Progress", "Implemented"}:
                raise PlatformError("reject.invalid_state", "input rejection requires Ready, In Progress, or Implemented", details={"state": before})
            metadata["state"] = "Void"
            metadata["lease"] = None
            metadata["void_reason"] = "input-defect"
            event = make_event("task.void", task_id, actor, actor, before, "Void", payload={"category": category, "reason": reason})
        metadata["updated_at"] = utc_now()
        result = persist(repository, {task_id: (metadata, body)}, [event], [], f"Reject {task_id}", push)
        if workspace:
            refresh_workspace(repository, workspace, task_id)
            result["workspace_refreshed"] = True
        return result
    finally:
        lock.unlink(missing_ok=True)


def close_task(repository, task_id, decision, actor, reason=None, push=False, ship=False, ship_commit=None, ship_fingerprint=None):
    repository = ensure_repository(repository)
    if decision not in {"accept", "reject"}:
        raise PlatformError("close.invalid_decision", "decision must be accept or reject")
    if ship and decision != "accept":
        raise PlatformError("close.ship_requires_accept", "--ship is only meaningful with --decision accept")
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        state = metadata.get("state")
        # close --ship 允许从 Implemented 一步关闭（AC-07：编排直通流无独立 verify 站）；
        # 常规 close 仍要求 Verified。
        allowed_states = {"Verified", "Implemented"} if ship else {"Verified"}
        if state not in allowed_states:
            raise PlatformError("close.invalid_state", "task state cannot be closed", details={"state": state})
        if decision == "accept":
            metadata["state"] = "Done"
            events = [make_event("task.closed", task_id, actor, actor, state, "Done", payload={"decision": decision})]
            if ship:
                fingerprint = ship_fingerprint or workspace_fingerprint()
                events.append(make_event("task.shipped", task_id, actor, actor, "Done", "Done", payload={"commit_hash": ship_commit, "workspace_fingerprint": fingerprint, "via": "close --ship"}))
        else:
            metadata["state"] = "Verified"
            events = [make_event("close.rejected", task_id, actor, actor, "Verified", "Verified", payload={"decision": decision, "reason": reason or ""})]
        metadata["updated_at"] = utc_now()
        result = persist(repository, {task_id: (metadata, body)}, events, [], f"Close {task_id}", push)
        if ship:
            result["shipped"] = True
        return result
    finally:
        lock.unlink(missing_ok=True)


def latest_verify_fingerprint(events):
    for event in reversed(events):
        if str(event.get("event_type", "")).startswith("verification."):
            fingerprint = (event.get("payload") or {}).get("workspace_fingerprint")
            if fingerprint:
                return fingerprint
    return None


def ship_task(repository, task_id, actor, commit_hash, fingerprint, push=False):
    """Done 任务的 ship 落账（AC-03/AC-04）：指纹比对、同 commit 幂等、异 commit 冲突拒绝。

    git add/commit/push 的本机副作用由 CLI 侧执行（AC-05），本函数只做权威事件记录。
    """
    repository = ensure_repository(repository)
    if not commit_hash or not commit_hash.strip():
        raise PlatformError("ship.invalid_commit", "commit_hash must be a non-empty string")
    commit_hash = commit_hash.strip()
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        state = metadata.get("state")
        if state != "Done":
            # 带活跃租约的任务说明实现者仍在作业，operator 收尾动作与其冲突 → 403；
            # 其余非 Done 状态按状态机拒绝（409）。
            if lease_is_active(metadata.get("lease")):
                raise AuthorizationError("ship.lease_active", f"task {task_id} has an active lease; ship is not allowed while the task is claimed")
            raise PlatformError("ship.invalid_state", "only a Done task can be shipped", details={"state": state})
        events = task_log(repository, task_id)
        expected = latest_verify_fingerprint(events)
        if expected and fingerprint != expected:
            raise PlatformError("ship.fingerprint_mismatch", "workspace_fingerprint does not match the latest verification record", exit_code=3, details={"expected": expected, "provided": fingerprint})
        shipped = [event for event in events if event.get("event_type") == "task.shipped"]
        if shipped:
            last = shipped[-1]
            last_commit = (last.get("payload") or {}).get("commit_hash")
            if last_commit == commit_hash:
                # 同 task + 同 commit 重复 ship 幂等返回，不重复落事件。
                return {"task_id": task_id, "shipped": True, "idempotent": True, "commit_hash": commit_hash, "commit": None}
            closed_after = any(
                event.get("event_type") == "task.closed" and str(event.get("timestamp", "")) > str(last.get("timestamp", ""))
                for event in events
            )
            if not closed_after:
                raise PlatformError("ship.commit_conflict", "a different commit was already shipped and no newer close exists", details={"shipped_commit": last_commit, "provided": commit_hash})
        event = make_event("task.shipped", task_id, actor, actor, "Done", "Done", payload={"commit_hash": commit_hash, "workspace_fingerprint": fingerprint})
        result = persist(repository, {task_id: (metadata, body)}, [event], [], f"Ship {task_id}", push)
        result["shipped"] = True
        result["commit_hash"] = commit_hash
        return result
    finally:
        lock.unlink(missing_ok=True)


def invalidate_specification(repository, source_task_id, actor, reason, supersedes=None, push=False):
    repository = ensure_repository(repository)
    if not reason or not reason.strip():
        raise PlatformError("void.reason_required", "invalidation reason is required")
    lock = lifecycle_lock(repository)
    try:
        source_path = task_path(repository, source_task_id)
        if not source_path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {source_task_id}")
        source_metadata, source_body = read_task(source_path)
        pin = source_metadata.get("spec_pin")
        if not pin:
            raise PlatformError("void.spec_not_pinned", "task has no specification to invalidate")
        spec_id = pin["spec_id"]
        version = pin["acceptance_card"]["version"]
        changed_tasks = {}
        events = []
        extra_paths = []
        marker = propagate_pin_invalidation(repository, pin, actor, reason, source_task_id, changed_tasks, events, extra_paths, supersedes=supersedes)
        events.insert(0, make_event("specification.invalidated", source_task_id, actor, actor, source_metadata["state"], source_metadata["state"], payload=marker))
        source_before = source_metadata["state"]
        source_metadata["state"] = "Void"
        source_metadata["lease"] = None
        source_metadata["void_reason"] = "definition-defect"
        source_metadata["updated_at"] = utc_now()
        changed_tasks[source_task_id] = (source_metadata, source_body)
        events.append(make_event("task.void", source_task_id, actor, actor, source_before, "Void", payload={"reason": "definition-defect", "spec_id": spec_id, "version": version}))
        return persist(repository, changed_tasks, events, extra_paths, f"Invalidate spec {spec_id}@{version}", push)
    finally:
        lock.unlink(missing_ok=True)


def void_task(repository, task_id, reason, actor, push=False):
    repository = ensure_repository(repository)
    if reason not in {"definition-defect", "cancel", "specification-invalidated"}:
        raise PlatformError("void.invalid_reason", "reason must be definition-defect, cancel, or specification-invalidated")
    if reason == "definition-defect":
        return invalidate_specification(repository, task_id, actor, "definition-defect", push=push)
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        allowed = VOIDABLE_BY_SPEC_INVALIDATION if reason == "specification-invalidated" else CANCELABLE_STATES
        if metadata.get("state") not in allowed:
            raise PlatformError("void.invalid_state", "task state cannot be voided for this reason", details={"state": metadata.get("state"), "reason": reason})
        before = metadata["state"]
        metadata["state"] = "Void"
        metadata["lease"] = None
        metadata["void_reason"] = reason
        metadata["updated_at"] = utc_now()
        event = make_event("task.void", task_id, actor, actor, before, "Void", payload={"reason": reason})
        return persist(repository, {task_id: (metadata, body)}, [event], [], f"Void {task_id}", push)
    finally:
        lock.unlink(missing_ok=True)


def adjudicate_task(repository, task_id, category, reason, actor, workspace=None, push=False):
    repository = ensure_repository(repository)
    if category not in {"implementation", "definition"}:
        raise PlatformError("adjudicate.invalid_category", "category must be implementation or definition")
    if not reason or not reason.strip():
        raise PlatformError("adjudicate.reason_required", "adjudication reason is required")
    if category == "definition":
        result = invalidate_specification(repository, task_id, actor, reason, push=push)
        if workspace:
            refresh_workspace(repository, workspace, task_id)
            result["workspace_refreshed"] = True
        return result
    lock = lifecycle_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        if metadata.get("state") != "Blocked":
            raise PlatformError("adjudicate.invalid_state", "only a Blocked task can be adjudicated", details={"state": metadata.get("state")})
        metadata["state"] = "Ready"
        metadata["lease"] = None
        metadata["updated_at"] = utc_now()
        event = make_event("block.adjudicated", task_id, actor, actor, "Blocked", "Ready", payload={"category": category, "reason": reason})
        result = persist(repository, {task_id: (metadata, body)}, [event], [], f"Adjudicate {task_id}", push)
        if workspace:
            refresh_workspace(repository, workspace, task_id)
            result["workspace_refreshed"] = True
        return result
    finally:
        lock.unlink(missing_ok=True)
