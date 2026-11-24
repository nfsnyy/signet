from pathlib import Path

from .. import codeflow
from ..collaboration import validate_report_collaboration
from ..domain import (
    REPORT_SECTIONS,
    SPEC_FILES,
    VOIDABLE_BY_SPEC_INVALIDATION,
    apply_submit,
    event_path,
    invalid_marker_path,
    lease_is_active,
    make_event,
    proposal_base,
    same_pin,
    task_path,
)
from ..errors import PlatformError
from ..gitrepo import commit_paths, ensure_repository, initialize_repository
from ..jsonio import append_jsonl, new_id, read_jsonl, sha256_file, utc_now, write_json
from ..taskfile import read_task, write_task
from .evidence import authoritative_evidence_limits, evidence_manifest, materialize_submit_evidence
from .packages import normalize_req_items, stamp_events
from .requirements import validate_req_items_against_requirement
from .submissions import read_report


def create_trace_review_task(repository, source_metadata, changed_tasks, events):
    task_id = new_id("task")
    spec_id = new_id("spec")
    now = utc_now()
    metadata = {
        "task_id": task_id,
        "title": f"追溯审查：{source_metadata['task_id']}",
        "requirement": "specification-traceability-review",
        "owner": source_metadata["owner"],
        "state": "Draft",
        "spec_id": spec_id,
        "spec_pin": None,
        "lease": None,
        "created_at": now,
        "updated_at": now,
    }
    body = f"# 追溯审查：{source_metadata['task_id']}\n\n原任务已完成，但其钉扎规格后来被作废。请审查交付是否需要召回、补丁或补充说明。\n"
    changed_tasks[task_id] = (metadata, body)
    events.append(make_event("task.initialized", task_id, "operator", source_metadata["owner"], None, "Draft", payload={"trace_review_for": source_metadata["task_id"], "spec_id": spec_id}))


def propagate_pin_invalidation(repository, pin, actor, reason, source_task_id, changed_tasks, events, extra_paths, supersedes=None, veto_id=None):
    spec_id = pin["spec_id"]
    version = pin["acceptance_card"]["version"]
    marker = {
        "spec_id": spec_id,
        "version": version,
        "invalidated_at": utc_now(),
        "invalidated_by": actor,
        "reason": reason,
        "supersedes": supersedes,
    }
    if veto_id:
        marker["veto_id"] = veto_id
    marker_path = invalid_marker_path(repository, spec_id, version)
    if marker_path.exists():
        raise PlatformError("spec.already_invalid", "specification version is already invalidated")
    write_json(marker_path, marker)
    extra_paths.append(marker_path.relative_to(repository))
    tasks_directory = Path(repository) / "tasks"
    if tasks_directory.is_dir():
        for task_file in sorted(tasks_directory.glob("*/task.md")):
            task_id = task_file.parent.name
            if task_id == source_task_id:
                continue
            if task_id in changed_tasks:
                metadata, body = changed_tasks[task_id]
            else:
                metadata, body = read_task(task_file)
            if not same_pin(metadata.get("spec_pin"), pin):
                continue
            context = {"source_task_id": source_task_id, "spec_id": spec_id, "version": version}
            if veto_id:
                context["veto_id"] = veto_id
            if metadata.get("state") == "Done":
                events.append(make_event("traceability.review_required", task_id, actor, metadata["owner"], "Done", "Done", payload=context))
                create_trace_review_task(repository, metadata, changed_tasks, events)
            elif metadata.get("state") in VOIDABLE_BY_SPEC_INVALIDATION:
                before = metadata["state"]
                metadata["state"] = "Void"
                metadata["lease"] = None
                metadata["void_reason"] = "specification-invalidated"
                metadata["updated_at"] = utc_now()
                changed_tasks[task_id] = (metadata, body)
                events.append(make_event("task.void", task_id, actor, actor, before, "Void", payload={"reason": "specification-invalidated", **context}))


def init_repository(repository, actor, name=None, owner=None):
    repository = initialize_repository(repository)
    if (Path(repository) / "project.json").is_file():
        raise PlatformError("repository.already_initialized", "state repository is already initialized")
    project = {
        "project_id": new_id("project"),
        "name": name or Path(repository).resolve().name,
        "owner": owner or actor,
        "created_at": utc_now(),
    }
    write_json(Path(repository) / "project.json", project)
    events = event_path(repository)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch()
    initialized = stamp_events(repository, [make_event("repository.initialized", None, actor, actor, None, None, payload={"project": project})])
    append_jsonl(events, initialized)
    commit = commit_paths(repository, ["project.json", "events/events.jsonl"], "Initialize AI development platform")
    return {"repository": str(repository), "project": project, "commit": commit}


def init_task(repository, workspace, title, requirement, owner, actor, req_items=None, validate_req_items=True):
    repository = ensure_repository(repository)
    # api-remote-ready（REQ-3/AC-07）：workspace 参数语义移除——服务端不创建、
    # 不写 client 工作区；工作区由客户端在 init-task 成功后经任务包拉取自行落地。
    cleaned_req_items = normalize_req_items(req_items)
    if validate_req_items and cleaned_req_items:
        validate_req_items_against_requirement(repository, requirement, cleaned_req_items)
    task_id = new_id("task")
    spec_id = new_id("spec")
    now = utc_now()
    metadata = {
        "task_id": task_id,
        "title": title,
        "requirement": requirement,
        "owner": owner,
        "state": "Draft",
        "spec_id": spec_id,
        "spec_pin": None,
        "lease": None,
        "req_items": cleaned_req_items,
        "created_at": now,
        "updated_at": now,
    }
    body = f"# {title}\n\n## 任务说明\n\n待补充。\n"
    authoritative_task = task_path(repository, task_id)
    write_task(authoritative_task, metadata, body)
    event = make_event("task.initialized", task_id, actor, actor, None, "Draft", payload={"spec_id": spec_id})
    append_jsonl(event_path(repository), stamp_events(repository, [event]))
    commit = commit_paths(
        repository,
        [authoritative_task.relative_to(repository), event_path(repository).relative_to(repository)],
        f"Initialize {task_id}",
    )
    return {"task_id": task_id, "spec_id": spec_id, "state": "Draft", "commit": commit}


def verify_frozen_integrity(repository, metadata, actor, entrypoint="operator"):
    """AC-03（api-remote-ready）：claim 双通道与 verify --execute 的业务前置校验——
    重算 specs/{spec_id}/{v}/ 钉扎三件套字节 sha256 对 spec_pin；任一不符 → 拒绝
    （c7.spec_tampered），并落 specification.tamper_detected 安全事件
    （payload 恰五键：spec_id/version/file/expected/actual）。未钉扎任务零输出跳过。"""
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict):
        return
    spec_id = metadata.get("spec_id")
    version = (pin.get("acceptance_card") or {}).get("version")
    if not spec_id or not version:
        return
    directory = Path(repository) / "specs" / spec_id / str(version)
    for key, filename in SPEC_FILES.items():
        expected = (pin.get(key) or {}).get("sha256")
        if not expected:
            continue
        path = directory / filename
        actual = sha256_file(path) if path.is_file() else ""
        if actual != expected:
            payload = {
                "spec_id": spec_id,
                "version": str(version),
                "file": filename,
                "expected": expected,
                "actual": actual,
            }
            event = make_event(
                "specification.tamper_detected",
                metadata.get("task_id"),
                actor,
                actor,
                metadata.get("state"),
                metadata.get("state"),
                payload=payload,
                entrypoint=entrypoint,
            )
            events_file = event_path(repository)
            append_jsonl(events_file, stamp_events(repository, [event]))
            commit_paths(repository, [events_file.relative_to(repository)], f"Tamper detected {spec_id}@{version}")
            raise PlatformError(
                "c7.spec_tampered",
                f"frozen specification file hash mismatch: {filename}",
                details=payload,
            )


def status(workspace):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    inbox = workspace / "inbox"
    outbox_collab = workspace / "outbox-collab"
    return {
        "task": metadata,
        "inbox": sorted(path.name for path in inbox.glob("*.json")) if inbox.is_dir() else [],
        "outbox_collab": sorted(path.name for path in outbox_collab.glob("*.json")) if outbox_collab.is_dir() else [],
        "authoritative": False,
    }


def task_log(repository, task_id):
    repository = ensure_repository(repository)
    return [event for event in read_jsonl(event_path(repository)) if event.get("task_id") == task_id]


def submit_authoritative(repository, task_id, actor, report_file, evidence_files, on_behalf_of=None):
    """同步授权模式 submit：直接改写权威账本（In Progress → Implemented）。

    与 claim_authoritative 配套，用于携带 sigt、不带工作区的编排请求
    （如编排 fixture 经 init → freeze → claim → submit 直通 Implemented）；
    经典提案模式（submit）保持不变。AC-16：on_behalf_of 记录执行者（缺省回落 actor）。
    """
    repository = ensure_repository(repository)
    on_behalf_of = on_behalf_of or actor
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    report = read_report(report_file)
    # 编排 fixture 的宽松报告（ran/skipped 段名）归一化为五段式后再过领域校验；
    # 提案通路（submit）仍由 validate_report 严格校验，行为不变。
    if isinstance(report, dict) and set(report) != set(REPORT_SECTIONS):
        report = dict(report)
        if "executed" not in report and "ran" in report:
            ran = report.pop("ran")
            report["executed"] = (
                [{"command": ["orchestration-fixture"], "output": ran}] if isinstance(ran, str) and ran.strip() else []
            )
        if "not_executed" not in report and "skipped" in report:
            skipped = report.pop("skipped")
            report["not_executed"] = (
                [{"item": "orchestration-fixture", "reason": skipped}] if isinstance(skipped, str) and skipped.strip() else []
            )
    evidence = evidence_manifest(evidence_files, limits=authoritative_evidence_limits(repository, metadata))
    code_context = codeflow.code_task_context(repository, metadata)
    code_validator = None
    if code_context:
        code_validator = codeflow.submit_code_validator(code_context[0], task_id)
    proposal = {
        "proposal_id": new_id("proposal"),
        "task_id": task_id,
        "operation": "submit",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {"report": report, "evidence": evidence},
        "workspace": None,
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    accepted_event, outcome = apply_submit(
        proposal,
        metadata,
        collaboration_validator=lambda task_id_, report_: validate_report_collaboration(repository, task_id_, report_),
        code_validator=code_validator,
    )
    if accepted_event is None:
        _rejected_event, result = outcome
        raise PlatformError(result.get("code", "submit.rejected"), result.get("reason", "submit was rejected"))
    evidence_paths, rewritten = materialize_submit_evidence(repository, task_id, evidence)
    accepted_event["payload"]["evidence"] = rewritten
    events_file = event_path(repository)
    write_task(path, metadata, body)
    append_jsonl(events_file, stamp_events(repository, [accepted_event]))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository), *evidence_paths],
        f"Submit {task_id}",
    )
    return {
        "task_id": task_id,
        "state": metadata["state"],
        "authoritative": True,
        "commit": commit,
    }


def block_authoritative(repository, task_id, actor, reason, evidence):
    """同步授权模式 block：立即落事件并提交，不改变任务状态与租约。

    编排场景没有 operator sync 周期，任务保持 In Progress（租约继续由
    持有者持有），void 等后续生命周期动词仍按既有状态机执行。
    """
    repository = ensure_repository(repository)
    if not reason or not reason.strip():
        raise PlatformError("block.reason_required", "block reason is required")
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, _body = read_task(path)
    if metadata.get("state") != "In Progress":
        raise PlatformError("block.invalid_state", "only an In Progress task can be blocked", details={"state": metadata.get("state")})
    lease = metadata.get("lease") or {}
    if not lease_is_active(lease):
        raise PlatformError("block.lease_expired", "task lease is not active")
    if lease.get("claimed_by") != actor:
        raise PlatformError("block.lease_owner_mismatch", "only the lease holder can block this task")
    state = metadata["state"]
    events = [
        make_event(
            "task.blocked",
            task_id,
            actor,
            actor,
            state,
            state,
            payload={"reason": reason, "evidence": evidence},
            entrypoint="agent",
        ),
        make_event(
            "discussion.opened",
            task_id,
            "operator",
            actor,
            state,
            state,
            payload={"topic": "block", "reason": reason},
        ),
    ]
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, events))
    commit = commit_paths(repository, [events_file.relative_to(repository)], f"Block notice {task_id}")
    return {"task_id": task_id, "state": state, "authoritative": True, "commit": commit}


def record_pat_event(repository, event_type, actor, payload):
    """pat.issued / pat.revoked：事件落状态仓账本（git commit）；pats.json 本体在平台侧。"""
    repository = ensure_repository(repository)
    event = make_event(event_type, None, actor, actor, None, None, payload=payload, entrypoint="operator")
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, [event]))
    commit_paths(repository, [events_file.relative_to(repository)], event_type)
    return event
