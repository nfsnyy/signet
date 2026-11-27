import hashlib
import json
import os
from pathlib import Path

from . import codeflow
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
from .runner import frozen_contract
from .service import (
    create_trace_review_task,
    propagate_pin_invalidation,
    stamp_events,
    task_log,
    verify_frozen_integrity,
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


def _evidence_reference_items(document):
    """results 文档中全部 filename 形态证据引用（含 safety-veto manual_rerun 段）。"""
    items = []
    for result in document.get("results", []):
        if not isinstance(result, dict):
            continue
        for item in result.get("evidence", []):
            if isinstance(item, dict) and item.get("filename"):
                items.append(item)
        rerun = result.get("manual_rerun")
        if isinstance(rerun, dict):
            for item in rerun.get("evidence", []):
                if isinstance(item, dict) and item.get("filename"):
                    items.append(item)
    return items


def materialize_uploaded_evidence(repository, task_id, document, evidence_uploads):
    """T24/REQ-6（AC-03）：verify 结果上传的证据字节通道。

    filename 形态引用按名匹配 multipart 随件字节：引用未上传 → 400
    verify.evidence_missing；字节 sha256 与引用不符 → 400
    verify.evidence_hash_mismatch——全部核验通过后才落盘（拒绝零副作用）。
    落盘 tasks/<id>/verification/ 并把引用重写为落账形态（path+sha256，无
    content/filename 键）；path 形态引用（既有账本文件）原样通过。返回落盘
    文件的仓内相对路径列表。"""
    references = _evidence_reference_items(document)
    uploads = {Path(str(name)).name: content for name, content in (evidence_uploads or {}).items()}
    for item in references:
        name = Path(str(item["filename"])).name
        if name not in uploads:
            raise PlatformError(
                "verify.evidence_missing",
                f"results 引用的证据未随件上传: {name}",
                details={"filename": name},
            )
        actual = hashlib.sha256(uploads[name]).hexdigest()
        if actual != item.get("sha256"):
            raise PlatformError(
                "verify.evidence_hash_mismatch",
                f"上传证据字节 sha256 与 results 引用不符: {name}",
                details={"filename": name, "expected": item.get("sha256"), "actual": actual},
            )
    evidence_directory = task_path(repository, task_id).parent / "verification"
    extra_paths = []
    written = {}
    for item in references:
        name = Path(str(item["filename"])).name
        if name not in written:
            evidence_directory.mkdir(parents=True, exist_ok=True)
            target = evidence_directory / name
            target.write_bytes(uploads[name])
            written[name] = target.relative_to(repository).as_posix()
            extra_paths.append(written[name])
        sha256 = item["sha256"]
        item.clear()
        item.update({"path": written[name], "sha256": sha256})
    return extra_paths


def evaluate_ai_review_report(repository, metadata, review_report_file):
    """ai_review 阶段（AC-02~AC-06）：评审报告结构校验、逐条覆盖对账、判定质量
    门槛（fail/doubt 必引证据；无引用 pass 降级 doubt）与 fail 权重裁定。

    未附带报告时零输出跳过（返回 None，无记录、不影响其余阶段）。通过校验返回
    verification.initial payload 的 ai_review 片段；片段附带 "_report_bytes"
    键承载报告原文字节，由 verify_task 落证落盘后剔除。
    """
    if not review_report_file:
        return None
    invalid = "verify.ai_review_invalid_report"
    report = load_json_document(review_report_file, invalid)
    identity = report.get("model_identity")
    if not isinstance(identity, dict):
        raise PlatformError(invalid, "review report model_identity must be an object")
    if not isinstance(identity.get("model"), str) or not identity.get("model"):
        raise PlatformError(invalid, "review report model_identity.model must be a non-empty string")
    if not isinstance(identity.get("harness"), str) or not identity.get("harness"):
        raise PlatformError(invalid, "review report model_identity.harness must be a non-empty string")
    subject = report.get("subject")
    if not isinstance(subject, dict):
        raise PlatformError(invalid, "review report subject must be an object")
    if subject.get("task_id") != metadata.get("task_id") or subject.get("spec_pin") != metadata.get("spec_pin"):
        raise PlatformError(invalid, "review report subject does not match the task under verification")
    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list):
        raise PlatformError(invalid, "review report verdicts must be a list")
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise PlatformError(invalid, "each review verdict must be an object")
        if not isinstance(verdict.get("criterion_id"), str) or not verdict.get("criterion_id"):
            raise PlatformError(invalid, "review verdict criterion_id must be a non-empty string")
        if verdict.get("verdict") not in {"pass", "fail", "doubt"}:
            raise PlatformError(invalid, f"review verdict must be pass, fail, or doubt: {verdict.get('criterion_id')}")
        if not isinstance(verdict.get("rationale"), str) or not verdict.get("rationale"):
            raise PlatformError(invalid, "review verdict rationale must be a non-empty string")
        evidence_refs = verdict.get("evidence_refs")
        if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) for ref in evidence_refs):
            raise PlatformError(invalid, "review verdict evidence_refs must be a list of strings")
    # AC-04 质量门槛：fail/doubt 判定必须引用具体证据，空引用拒签该报告。
    for verdict in verdicts:
        if verdict["verdict"] in {"fail", "doubt"} and not verdict["evidence_refs"]:
            raise PlatformError(invalid, f"{verdict['verdict']} verdict requires evidence references: {verdict['criterion_id']}")
    # AC-03 逐条覆盖对账：criterion_id 集合须恰好等于冻结卡全集且不重复。
    spec_directory, _contract, _mapping = frozen_contract(repository, metadata)
    card = read_json(spec_directory / "acceptance-card.json")
    expected_ids = {item.get("criterion_id") for item in card.get("criteria", []) if isinstance(item, dict)}
    actual_ids = [verdict["criterion_id"] for verdict in verdicts]
    unknown = [criterion_id for criterion_id in actual_ids if criterion_id not in expected_ids]
    if unknown:
        raise PlatformError("verify.ai_review_unknown_criterion", f"review verdict covers unknown criteria: {sorted(set(unknown))}")
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
        raise PlatformError("verify.ai_review_incomplete", "review verdicts must cover every criterion exactly once")
    # AC-04 降级：无证据引用的 pass 一律降级为 doubt（防评审橡皮图章），统计按降级后口径。
    degraded = []
    summary = {"pass": 0, "fail": 0, "doubt": 0}
    for verdict in verdicts:
        effective = verdict["verdict"]
        if effective == "pass" and not verdict["evidence_refs"]:
            effective = "doubt"
            degraded.append({"criterion_id": verdict["criterion_id"], "from": "pass"})
        summary[effective] += 1
    # AC-06 权重分档：fail 阻断 verify（任务保持 Implemented，details 含 fail 清单）。
    failed_verdicts = [verdict for verdict in verdicts if verdict["verdict"] == "fail"]
    if failed_verdicts:
        raise PlatformError("verify.ai_review_failed", "review report contains fail verdicts", details={"verdicts": failed_verdicts})
    report_bytes = Path(review_report_file).read_bytes()
    relative_report_path = f"tasks/{metadata['task_id']}/verification/ai-review-report.json"
    return {
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "report_path": relative_report_path,
        "model_identity": dict(identity),
        "summary": summary,
        "degraded": degraded,
        "_report_bytes": report_bytes,
    }


def verify_task(repository, task_id, verifier, confirmed_by, results_file=None, execute=False, manual_results_file=None, push=False, review_report_file=None, reverify=False, evidence_uploads=None):
    repository = ensure_repository(repository)
    # T24/REQ-6（AC-02）：服务端 verify --execute 净室退役——execute=true 一律
    # 400 verify.execute_retired 零副作用（状态/事件/HEAD 不变）；服务端任何模式
    # 不再执行契约用例，机器验收迁至 operator 本机执行+上传（CLI verify --execute
    # 新语义），verify 全路径不依赖预置 SIGNET_PLATFORM_REPO。
    if execute:
        raise PlatformError(
            "verify.execute_retired",
            "服务端净室执行已退役：机器验收由 operator 本机执行并经 results 上传，execute=true 不再接受",
        )
    if results_file is None:
        raise PlatformError("verify.invalid_mode", "results file is required (execute mode is retired)")
    lock = lifecycle_lock(repository)
    code_worktree_env_set = False
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        state = metadata.get("state")
        code_context = codeflow.code_task_context(repository, metadata)
        if state == "Verified":
            # T18（REQ-6）：仅非内置项目任务可显式 reverify（新基点重跑全部门，
            # 源 commit 不变）；未携 reverify 不得静默重验。其余状态口径不变。
            if not (reverify and code_context):
                raise PlatformError("verify.invalid_state", "only an Implemented task can be verified", exit_code=3, details={"state": state})
        elif state != "Implemented":
            raise PlatformError("verify.invalid_state", "only an Implemented task can be verified", exit_code=3, details={"state": state})
        # AC-03（api-remote-ready）：verify 业务前置——重算钉扎三件套
        # 字节对 spec_pin，篡改件不进入验收（c7.spec_tampered + 安全事件）。
        verify_frozen_integrity(repository, metadata, verifier, entrypoint="operator")
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
        # T24/REQ-6（AC-04）+ T27/REQ-6（#55）：code attest 对账（fail-closed，先于
        # 代码对账与任何落账；拒绝保持 Implemented、零验收事件）。required 判定与
        # code_task_context 对齐：:292 的 code_context 非空（闭环）才必索，
        # builtin 无 code_repo 豁免 attest。
        codeflow.check_verify_attestation(metadata, document, code_context=code_context)
        # T18（REQ-6）：源 pin 对账 → 精确源副本 + 预合并。全部既有门（spec
        # integrity/结果校验/角色分离/ai_review/人工）都在对账之后执行。
        code_verification = None
        if code_context:
            project_id, _record = code_context
            pinned = codeflow.reconcile_source_pin(metadata, codeflow.latest_submitted_code(task_log(repository, task_id)))
            code_verification = codeflow.build_code_verification(project_id, task_id, pinned)
            os.environ["SIGNET_CODE_WORKTREE"] = code_verification["worktree"]
            code_worktree_env_set = True
        # AC-01 阶段序：结果校验 → 既有门 → attest 对账 → ai_review → 落证 → Verified。
        ai_review = evaluate_ai_review_report(repository, metadata, review_report_file)
        # T24/REQ-6（AC-03）：证据字节通道——filename 引用逐件核验随件字节后落盘
        # tasks/<id>/verification/（拒绝零副作用）。
        extra_paths = materialize_uploaded_evidence(repository, task_id, document, evidence_uploads)
        if ai_review is not None:
            report_bytes = ai_review.pop("_report_bytes")
            report_target = task_path(repository, task_id).parent / "verification" / "ai-review-report.json"
            report_target.parent.mkdir(parents=True, exist_ok=True)
            report_target.write_bytes(report_bytes)
            extra_paths.append(report_target.relative_to(repository).as_posix())
        metadata["state"] = "Verified"
        metadata["updated_at"] = utc_now()
        # AC-06：verify 落账时记录工作区指纹（算法与 ship 比对共用 aidev.fingerprint）。
        recorded_fingerprint = workspace_fingerprint()
        initial_payload = {"results": document, "executed": False, "workspace_fingerprint": recorded_fingerprint}
        if ai_review is not None:
            initial_payload["ai_review"] = ai_review
        if code_verification is not None:
            initial_payload["code_verification"] = code_verification
        events = [
            make_event("verification.initial", task_id, verifier, verifier, "Implemented", "Implemented", payload=initial_payload),
            make_event("verification.confirm", task_id, confirmed_by, confirmed_by, "Implemented", "Verified", payload={"confirmed_by": confirmed_by, "workspace_fingerprint": recorded_fingerprint}),
            make_event("task.verified", task_id, confirmed_by, confirmed_by, "Implemented", "Verified"),
        ]
        return persist(repository, {task_id: (metadata, body)}, events, extra_paths, f"Verify {task_id}", push)
    finally:
        if code_worktree_env_set:
            os.environ.pop("SIGNET_CODE_WORKTREE", None)
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
        # AC-07（api-remote-ready）：workspace 参数语义移除——服务端零写 client
        # 工作区，workspace_refreshed 键不再出现；投影由客户端拉取。
        return result
    finally:
        lock.unlink(missing_ok=True)


def _close_ship_code_task(repository, task_id, metadata, body, actor, state, project_id, push):
    """T18（REQ-7）+ T24（REQ-7）：代码任务 close --ship 的服务端代码入库（全项目同机制，含内置）。

    复核 Verified + 源 pin + 验收 merge_commit → 对账远端 main → 非强制单 ref
    push → 同一状态仓提交落 Done/task.closed/task.shipped。重复 close 幂等返回；
    main 前进 → 409 ship.reverification_required；push 失败 → 409 ship.push_failed
    （保持 Verified、零 closed/shipped）；账本 push 失败由 commit_paths 回滚本地
    半态（恢复后重试只补同一笔账本）。
    """
    events = task_log(repository, task_id)
    if state == "Done":
        shipped = [event for event in events if event.get("event_type") == "task.shipped"]
        if shipped:
            payload = shipped[-1].get("payload") or {}
            return {
                "task_id": task_id,
                "shipped": True,
                "idempotent": True,
                "commit_hash": payload.get("commit_hash"),
                "commit": None,
                "pushed": False,
            }
        raise PlatformError("close.invalid_state", "task was closed without ship; project tasks ship only via close --ship", details={"state": state})
    if state != "Verified":
        raise PlatformError("close.invalid_state", "project close --ship requires a Verified task", details={"state": state})
    verification = codeflow.latest_code_verification(events)
    if not verification:
        raise PlatformError("close.invalid_state", "no code verification record; run verify before close --ship", details={"state": state})
    pinned = metadata.get("code_submission") or {}
    if (
        pinned.get("code_repo_id") != project_id
        or pinned.get("branch") != verification.get("branch")
        or pinned.get("commit_hash") != verification.get("source_commit")
    ):
        raise PlatformError(
            "verify.source_commit_mismatch",
            "任务元数据 code_submission 与验收记录源 pin 不符",
            exit_code=3,
            details={"code_submission": pinned, "code_verification": verification},
        )
    target_base = verification["target_base"]
    merge_commit = verification["merge_commit"]
    # 代码 push 先于账本落账；账本失败重试时远端已等于 merge_commit → 只补账本。
    codeflow.close_push_state(project_id, merge_commit, target_base)
    metadata["state"] = "Done"
    metadata["updated_at"] = utc_now()
    shipped_payload = {
        "project_id": project_id,
        "code_repo_id": project_id,
        "branch": verification["branch"],
        "source_commit": verification["source_commit"],
        "final_commit": merge_commit,
        "commit_hash": merge_commit,
        "via": "server-push",
    }
    events_out = [
        make_event("task.closed", task_id, actor, actor, state, "Done", payload={"decision": "accept"}),
        make_event("task.shipped", task_id, actor, actor, "Done", "Done", payload=shipped_payload),
    ]
    result = persist(repository, {task_id: (metadata, body)}, events_out, [], f"Close {task_id}", push)
    result["shipped"] = True
    result["idempotent"] = False
    return result


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
        # T18（REQ-7）+ T24（REQ-7）：代码任务 close --ship 由服务端推送已验收
        # merge_commit，不调用 local_git_ship、不 git add -A；T24 起 ship_commit/
        # ship_fingerprint 输入全项目禁止（本机 ship 通道整体退役，消除异 commit 重入）。
        if ship and decision == "accept":
            if ship_commit or ship_fingerprint:
                raise PlatformError(
                    "close.ship_input_forbidden",
                    "close --ship 入库由服务端推送已验收 merge_commit；不接受外部 ship_commit/ship_fingerprint 输入",
                )
            code_context = codeflow.code_task_context(repository, metadata)
            if code_context:
                project_id, _record = code_context
                return _close_ship_code_task(repository, task_id, metadata, body, actor, state, project_id, push)
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
    T18（REQ-7）+ T24（REQ-7）：项目任务（含内置）的入库通道只有 close --ship
    （服务端推送已验收 merge_commit），POST /ship → 409 ship.use_close；仅无项目仓
    的遗留编排路径（仓无 project.json/注册表记录）保留本端点旧语义。
    """
    repository = ensure_repository(repository)
    from . import projects as project_store

    ship_project_id = project_store.project_id_for_repository(repository)
    if ship_project_id:
        ship_record = project_store.get_project(ship_project_id)
        if ship_record is not None:
            raise PlatformError(
                "ship.use_close",
                "项目任务（含内置）必须经 close --ship 入库（服务端推送已验收 merge_commit），POST /ship 不适用于项目任务",
                exit_code=3,
            )
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
        # workspace 参数语义移除（AC-07）：服务端零写 client 工作区。
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
        # workspace 参数语义移除（AC-07）：服务端零写 client 工作区。
        return result
    finally:
        lock.unlink(missing_ok=True)
