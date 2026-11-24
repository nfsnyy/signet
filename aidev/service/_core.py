import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import distribution
from .. import codeflow
from ..authn import mint_sigt
from ..collaboration import collaboration_overview, process_collaboration_sync, validate_report_collaboration
from .. import projects as project_store
from ..domain import (
    REPORT_SECTIONS,
    SPEC_FILES,
    VOIDABLE_BY_SPEC_INVALIDATION,
    apply_block,
    apply_claim,
    apply_enhancement,
    apply_submit,
    event_path,
    invalid_marker_path,
    lease_is_active,
    make_event,
    proposal_base,
    rejection,
    same_pin,
    spec_path,
    task_path,
    workspace_agents,
)
from ..errors import AuthorizationError, NotFoundError, OperationalError, PlatformError
from ..readmodel import parse_requirement_text, pinned_acceptance_card
from ..gitrepo import commit_paths, ensure_remote_syncable, ensure_repository, initialize_repository
from ..jsonio import append_jsonl, new_id, read_json, read_jsonl, sha256_file, utc_now, write_json
from ..taskfile import read_task, write_task
from .evidence import (
    authoritative_evidence_limits,
    evidence_basenames,
    evidence_manifest,
    materialize_submit_evidence,
    materialize_submit_evidence_bytes,
)
from .packages import normalize_req_items, stamp_events, validate_workspace_pin
from .requirements import read_requirement
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
    return marker


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


def validate_req_items_against_requirement(repository, requirement, req_items):
    """条目引用校验（会话路径，AC-01）：派生闸门之后逐编号命中需求「判据条目」节。

    需求不存在 → requirement.not_found；未确认 → requirement.not_confirmed；
    未知编号 → requirement.entry_not_found（details 列全部非法编号）。全部
    在任何落盘动作之前执行，拒绝形态零副作用。
    """
    requirement_obj = read_requirement(repository, requirement)
    if requirement_obj is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement}")
    fields, _body, entries = requirement_obj
    if fields.get("status") != "confirmed":
        raise PlatformError(
            "requirement.not_confirmed",
            f"requirement {requirement} is not confirmed (status={fields.get('status') or '-'})",
            details={"requirement_id": requirement, "status": fields.get("status")},
        )
    known = {item["id"] for item in entries}
    unknown = [item for item in req_items if item not in known]
    if unknown:
        raise PlatformError(
            "requirement.entry_not_found",
            f"requirement entries not found: {unknown}",
            details={"requirement_id": requirement, "unknown": unknown},
        )


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


def claim(workspace, actor, on_behalf_of):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    if metadata.get("state") != "Ready":
        raise PlatformError("claim.not_ready", "only a Ready task can be claimed", details={"state": metadata.get("state")})
    if lease_is_active(metadata.get("lease")):
        raise PlatformError("claim.lease_active", "task already has an active lease")
    validate_workspace_pin(workspace, metadata)
    proposal_id = new_id("proposal")
    proposal = {
        "proposal_id": proposal_id,
        "task_id": metadata["task_id"],
        "operation": "claim",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {},
        "workspace": str(workspace),
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    write_json(workspace / "outbox" / f"{proposal_id}.json", proposal)
    return {"proposal_id": proposal_id, "task_id": metadata["task_id"], "authoritative": False}


def collect_expired_leases(repository, changed_tasks, new_events):
    tasks_directory = repository / "tasks"
    if not tasks_directory.is_dir():
        return
    for path in sorted(tasks_directory.glob("*/task.md")):
        task_id = path.parent.name
        if task_id in changed_tasks:
            metadata, body = changed_tasks[task_id]
        else:
            metadata, body = read_task(path)
        lease = metadata.get("lease")
        if lease and not lease_is_active(lease):
            metadata["lease"] = None
            metadata["updated_at"] = utc_now()
            new_events.append(make_event(
                "lease.expired",
                task_id,
                lease["claimed_by"],
                metadata["owner"],
                metadata["state"],
                metadata["state"],
                proposal_id=lease.get("proposal_id"),
                payload={"lease": lease},
                entrypoint="agent",
            ))
            changed_tasks[task_id] = (metadata, body)


PROPOSAL_OPERATIONS = {"claim", "submit", "block", "enhancement"}


def validate_uploaded_proposals(proposals):
    """AC-08：上传提案的结构预检——逐条校验必需键，结构非法的条目以
    request.invalid 拒收（不影响整批其余提案）；合法条目补齐缺省键后返回。"""
    cleaned = []
    rejected = []
    for proposal in proposals:
        valid = isinstance(proposal, dict)
        if valid:
            proposal = dict(proposal)
            proposal_id = proposal.get("proposal_id")
            task_id = proposal.get("task_id")
            operation = proposal.get("operation")
            valid = (
                isinstance(proposal_id, str) and bool(proposal_id.strip())
                and isinstance(task_id, str) and bool(task_id.strip())
                and operation in PROPOSAL_OPERATIONS
                and isinstance(proposal.get("created_at"), str)
            )
        if not valid:
            rejected.append({
                "proposal_id": proposal.get("proposal_id") if isinstance(proposal, dict) else None,
                "accepted": False,
                "code": "request.invalid",
            })
            continue
        if not isinstance(proposal.get("base"), dict):
            proposal["base"] = {}
        if not isinstance(proposal.get("parameters"), dict):
            proposal["parameters"] = {}
        cleaned.append(proposal)
    return cleaned, rejected


def synchronize(repository, workspaces, actor, push=False):
    """经典提案通道（in-proc 直调/单机兼容）：从各工作区 outbox 读提案后走公共
    应用核心；collab 消息维持现行服务端读工作区语义（§8 范围外）。服务端零写
    client 工作区（api-remote-ready/AC-07），投影由客户端拉取。"""
    repository = ensure_repository(repository)
    proposals = []
    for workspace in workspaces:
        outbox = Path(workspace).resolve() / "outbox"
        for path in outbox.glob("*.json"):
            proposal = read_json(path)
            if isinstance(proposal, dict):
                proposals.append(proposal)
    return _synchronize_core(
        repository,
        proposals,
        evidence_lookup=None,
        actor=actor,
        push=push,
        workspace_paths=[Path(workspace).resolve() for workspace in workspaces],
    )


def synchronize_uploaded(repository, proposals, evidence_lookup, actor, push=False, workspace_paths=None):
    """AC-08 上传通道：POST /api/sync 的 proposals 内容上传应用。证据字节经
    multipart 随件（按文件名匹配提案引用），服务端不按 client 路径读证据；
    缺证据的 submit 提案单条拒收 submit.evidence_missing，整批其余提案不受影响。
    提案 actor/on_behalf_of 维持提案自带值（operator 代同步 agent 提案的经典
    通道归因不被凭据覆盖；1.1 覆盖纪律作用于请求体顶层 actor）。"""
    repository = ensure_repository(repository)
    cleaned, rejected = validate_uploaded_proposals(proposals)
    for proposal in cleaned:
        if not isinstance(proposal.get("on_behalf_of"), str) or not proposal["on_behalf_of"].strip():
            proposal["on_behalf_of"] = actor
    result = _synchronize_core(
        repository,
        cleaned,
        evidence_lookup=evidence_lookup,
        actor=actor,
        push=push,
        workspace_paths=[Path(path).resolve() for path in (workspace_paths or [])],
        presorted_results=rejected,
    )
    return result


def _synchronize_core(repository, proposals, evidence_lookup, actor, push, workspace_paths, presorted_results=None):
    lock_path = repository / ".aidev-sync.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise OperationalError("sync.already_running", "another sync holds the repository lock") from error
    os.close(descriptor)
    try:
        ensure_remote_syncable(repository)
        proposals = list(proposals)
        proposals.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("proposal_id") or "")))
        events_file = event_path(repository)
        existing_events = read_jsonl(events_file)
        processed = {item.get("proposal_id") for item in existing_events if item.get("proposal_id")}
        new_events = []
        results = list(presorted_results or [])
        changed_tasks = {}
        submit_extra_paths = []
        collect_expired_leases(repository, changed_tasks, new_events)
        for proposal in proposals:
            proposal_id = proposal["proposal_id"]
            if proposal_id in processed:
                results.append({"proposal_id": proposal_id, "accepted": None, "duplicate": True})
                continue
            current_task = task_path(repository, proposal["task_id"])
            if not current_task.is_file():
                event, result = rejection(proposal, {}, "task.not_found", "authoritative task does not exist")
                new_events.append(event)
                results.append(result)
                continue
            if proposal["task_id"] in changed_tasks:
                metadata, body = changed_tasks[proposal["task_id"]]
            else:
                metadata, body = read_task(current_task)
            operation = proposal.get("operation")
            if operation == "claim":
                accepted_event, outcome = apply_claim(proposal, metadata)
            elif operation == "submit":
                evidence_items = (proposal.get("parameters") or {}).get("evidence") or []
                bytes_by_name = None
                if evidence_lookup is not None:
                    missing = [
                        name for name in evidence_basenames(evidence_items)
                        if name not in evidence_lookup
                    ]
                    if missing:
                        event, result = rejection(
                            proposal, metadata, "submit.evidence_missing",
                            f"evidence bytes were not uploaded with the sync request: {missing}",
                        )
                        new_events.append(event)
                        results.append(result)
                        continue
                    bytes_by_name = {
                        name: evidence_lookup[name] for name in evidence_basenames(evidence_items)
                    }
                else:
                    for item in evidence_items:
                        source = Path(str(item.get("path") or ""))
                        if not source.is_file():
                            raise PlatformError("submit.evidence_missing", f"evidence file no longer exists: {source}")
                code_context = codeflow.code_task_context(repository, metadata)
                code_validator = None
                if code_context:
                    code_validator = codeflow.submit_code_validator(code_context[0], proposal["task_id"])
                accepted_event, outcome = apply_submit(
                    proposal,
                    metadata,
                    collaboration_validator=lambda task_id, report: validate_report_collaboration(repository, task_id, report),
                    code_validator=code_validator,
                )
                if accepted_event is not None:
                    if bytes_by_name is not None:
                        evidence_paths, rewritten = materialize_submit_evidence_bytes(
                            repository, proposal["task_id"], evidence_items, bytes_by_name
                        )
                    else:
                        evidence_paths, rewritten = materialize_submit_evidence(repository, proposal["task_id"], evidence_items)
                    accepted_event["payload"]["evidence"] = rewritten
                    submit_extra_paths.extend(evidence_paths)
            elif operation == "block":
                accepted_event, outcome = apply_block(proposal, metadata)
            elif operation == "enhancement":
                accepted_event, outcome = apply_enhancement(proposal, metadata)
            else:
                event, result = rejection(proposal, metadata, "operation.unsupported", "operation is not implemented")
                new_events.append(event)
                results.append(result)
                continue
            if accepted_event is not None:
                if isinstance(accepted_event, list):
                    new_events.extend(accepted_event)
                else:
                    new_events.append(accepted_event)
                changed_tasks[proposal["task_id"]] = (metadata, body)
            rejected_event, result = outcome
            if rejected_event is not None:
                new_events.append(rejected_event)
            results.append(result)
        collab_events, collab_paths, collab_results = process_collaboration_sync(repository, workspace_paths)
        new_events.extend(collab_events)
        results.extend(collab_results)
        paths = []
        for task_id, (metadata, body) in changed_tasks.items():
            path = task_path(repository, task_id)
            write_task(path, metadata, body)
            paths.append(path.relative_to(repository))
        if new_events:
            append_jsonl(events_file, stamp_events(repository, new_events))
            paths.append(events_file.relative_to(repository))
        paths.extend(collab_paths)
        paths.extend(submit_extra_paths)
        commit = commit_paths(repository, paths, f"Sync {len(proposals)} proposal(s)", push=push) if paths else None
        return {"commit": commit, "results": results, "pushed": bool(push and commit)}
    finally:
        lock_path.unlink(missing_ok=True)


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


def claim_authoritative(repository, task_id, actor):
    """同步授权模式 claim：直接改写权威账本并签发 handoff 令牌。

    用于携带凭据、不带工作区的编排请求；经典提案模式（claim）保持不变。
    允许 Ready 或 Draft（T6 编排预冻结领取：仅无工作区路径，任务进入
    In Progress 并签发 sigt；带工作区的提案式 claim 仍要求 Ready）。
    """
    repository = ensure_repository(repository)
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    if metadata.get("state") not in ("Ready", "Draft"):
        raise PlatformError("claim.not_ready", "only a Ready or Draft task can be claimed", details={"state": metadata.get("state")})
    if lease_is_active(metadata.get("lease")):
        raise PlatformError("claim.lease_active", "task already has an active lease")
    verify_frozen_integrity(repository, metadata, actor, entrypoint="operator")
    proposal = {
        "proposal_id": new_id("proposal"),
        "task_id": task_id,
        "operation": "claim",
        "actor": actor,
        "on_behalf_of": actor,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {},
        "workspace": None,
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    accepted_event, outcome = apply_claim(proposal, metadata, allow_draft=True)
    if accepted_event is None:
        _rejected_event, result = outcome
        raise PlatformError(result.get("code", "claim.rejected"), result.get("reason", "claim was rejected"))
    events_file = event_path(repository)
    write_task(path, metadata, body)
    append_jsonl(events_file, stamp_events(repository, [accepted_event]))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository)],
        f"Claim {task_id}",
    )
    return {
        "task_id": task_id,
        "state": metadata["state"],
        "lease": metadata["lease"],
        "handoff_token": mint_sigt(task_id, actor, sigt_repo_field(repository)),
        "authoritative": True,
        "commit": commit,
    }


def sigt_repo_field(repository):
    """T17：sigt 的 repo 字段记项目 project_id（逻辑 id）；仓无项目标识时（未初始化
    临时仓/兼容旧形态）回落仓路径字符串。任务解析时校验该字段与任务所属项目一致。"""
    return project_store.project_id_for_repository(repository) or str(Path(repository).resolve())


def requirement_body(repository, requirement_id):
    """requirements/{id}/requirement.md 去 frontmatter 后的原文；无此需求返回 None。
    目录形态优先，缺失回落文件形态 requirements/<id>.md（T17 骨架引导需求）。"""
    if not requirement_id:
        return None
    file_path = Path(repository) / "requirements" / requirement_id / "requirement.md"
    if not file_path.is_file():
        file_path = Path(repository) / "requirements" / f"{requirement_id}.md"
        if not file_path.is_file():
            return None
    try:
        text = file_path.read_text(encoding="utf-8")
        _fields, body = parse_requirement_text(text)
    except (OSError, ValueError):
        return None
    return body


def spec_version_directory(repository, metadata):
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict) or not metadata.get("spec_id"):
        return None
    return Path(repository) / "specs" / metadata["spec_id"] / pin["acceptance_card"]["version"]


def spec_asset_manifest(repository, metadata):
    """钉扎规格版本目录内全部文件的下载清单（name=相对 POSIX 路径, sha256）。"""
    spec_directory = spec_version_directory(repository, metadata)
    if spec_directory is None or not spec_directory.is_dir():
        return []
    manifest = []
    for path in sorted(spec_directory.rglob("*")):
        if path.is_file():
            manifest.append({
                "name": path.relative_to(spec_directory).as_posix(),
                "sha256": sha256_file(path),
            })
    return manifest


def resolve_spec_asset(repository, metadata, name):
    """按名定位钉扎规格目录内的资产文件（拒绝目录穿越），不存在时 404。"""
    spec_directory = spec_version_directory(repository, metadata)
    relative = Path(name or "")
    if spec_directory is None:
        raise NotFoundError("task.asset_not_found", f"task has no frozen spec assets: {name}")
    if relative.is_absolute() or ".." in relative.parts or not str(name).strip():
        raise PlatformError("task.asset_invalid", f"invalid asset name: {name}")
    root = spec_directory.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise PlatformError("task.asset_invalid", f"asset escapes the spec directory: {name}")
    if not path.is_file():
        raise NotFoundError("task.asset_not_found", f"asset does not exist: {name}")
    return path


def task_inbox(repository, task_id):
    """发给本任务的协作消息（package.inbox；无协作面时为空列表）。"""
    try:
        messages = collaboration_overview(repository, task_id).get("messages") or []
    except (OSError, PlatformError):
        return []
    return [message for message in messages if message.get("to_task") == task_id]


def task_materials(repository, metadata):
    """任务包材料清单（REQ-6/AC-08）：[{name, via}]——via="package" 已含于本任务包
    （skill/requirement→requirement_body/acceptance_card/assets/hints→workspace_files），
    via 为绝对路径者经对应读端点匿名获取（AC-07 名册内规范材料）。"""
    materials = [
        {"name": "skill", "via": "package"},
        {"name": "requirement", "via": "package"},
        {"name": "acceptance_card", "via": "package"},
        {"name": "assets", "via": "package"},
    ]
    if (Path(repository) / "hints.md").is_file():
        materials.append({"name": "hints", "via": "package"})
    for norm in distribution.norm_roster():
        name = norm[:-3] if norm.endswith(".md") else norm
        materials.append({"name": name, "via": "/api/norms/" + norm})
    return materials


def build_task_package(repository, metadata, body):
    """sigt 接管响应携带的完整任务包（AC-01）。

    workspace_files 由 CLI 原样落进工作区根：AGENTS.md 恒有；状态仓根存在
    hints.md 时一并注入（不存在则不注入、不报错——防御语义）；T15 起增
    .signet-skill.md（=skill.content，与 /api/skill、任务包 skill 字段三处同源）。
    """
    repository = Path(repository)
    workspace_files = {"AGENTS.md": workspace_agents(metadata)}
    hints_source = repository / "hints.md"
    if hints_source.is_file():
        try:
            workspace_files["hints.md"] = hints_source.read_text(encoding="utf-8")
        except OSError:
            pass
    # REQ-2/AC-02 + REQ-4/AC-04：skill 自举——恰两键 version/content，content 全文
    # 含注入标识行；三处分发同源同字节（read_skill 唯一事实源）。
    skill_document = distribution.read_skill()
    skill = {"version": skill_document["version"], "content": skill_document["content"]}
    workspace_files[".signet-skill.md"] = skill_document["content"]
    return {
        "task_id": metadata["task_id"],
        "title": metadata.get("title"),
        "state": metadata.get("state"),
        "metadata": metadata,
        "task_md": body,
        "acceptance_card": pinned_acceptance_card(repository, metadata),
        "requirement_body": requirement_body(repository, metadata.get("requirement")),
        "assets": spec_asset_manifest(repository, metadata),
        "inbox": task_inbox(repository, metadata["task_id"]),
        "lease_expires_at": (metadata.get("lease") or {}).get("expires_at"),
        "skill": skill,
        "materials": task_materials(repository, metadata),
        "workspace_files": workspace_files,
    }


def last_claim_delegate(repository, task_id):
    """最近一次 task.claimed 的执行者（on_behalf_of 优先，缺省 actor）——sigt 续期判定键。"""
    for event in reversed(read_jsonl(event_path(repository))):
        if event.get("task_id") == task_id and event.get("event_type") == "task.claimed":
            return event.get("on_behalf_of") or event.get("actor")
    return None


def claim_with_sigt(repository, task_id, actor, on_behalf_of=None, sigt_repo=None):
    """sigt 幂等接管 + 续期：直接改写权威账本并提交，响应携带完整任务包。

    - In Progress 且同执行者（actor 且 on_behalf_of 与最近一次 claim 一致）持活跃租约：
      续期（lease.renewed 事件，expires_at 顺延 24h）；
    - Ready（含 refreeze 释放租约后）或执行者变更（委托转交另一 agent）：重 claim
      （task.claimed 事件，状态进入 In Progress）；
    - 他人持活跃租约（actor 不同）：拒绝（claim.lease_active）。
    AC-16 委托流归因：事件 actor=签发人（问责锁定），on_behalf_of=执行者（缺省回落 actor）。
    T17：sigt 的 repo 字段（项目 project_id 或兼容的仓路径）必须与任务所属项目
    一致，不符即拒（403 authz.project_denied，零副作用）。
    """
    repository = ensure_repository(repository)
    on_behalf_of = on_behalf_of or actor
    if sigt_repo is not None:
        expected = {sigt_repo_field(repository), str(Path(repository).resolve())}
        if sigt_repo not in expected:
            raise AuthorizationError(
                "authz.project_denied",
                f"sigt 绑定项目 {sigt_repo} 与任务所属项目不符",
                details={"sigt_project": sigt_repo, "task_project": sigt_repo_field(repository)},
            )
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    state = metadata.get("state")
    lease = metadata.get("lease") or {}
    now = datetime.now(timezone.utc)
    events = []
    takeover = False
    verify_frozen_integrity(repository, metadata, actor, entrypoint="agent")
    if state == "In Progress" and lease_is_active(lease):
        if lease.get("claimed_by") != actor:
            raise PlatformError("claim.lease_active", "task already has an active lease held by another actor")
        if last_claim_delegate(repository, task_id) != on_behalf_of:
            # 委托转交：同签发人令牌换一个执行者接管——落新的 task.claimed 事件（执行者归因）。
            takeover = True
        else:
            previous_expiry = lease.get("expires_at")
            lease["expires_at"] = (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            metadata["lease"] = lease
            metadata["updated_at"] = utc_now()
            events.append(make_event(
                "lease.renewed",
                task_id,
                actor,
                on_behalf_of,
                "In Progress",
                "In Progress",
                payload={"lease": lease, "previous_expires_at": previous_expiry},
                entrypoint="agent",
            ))
    if state in ("Ready", "In Progress") and not events:
        if not takeover and lease_is_active(lease) and lease.get("claimed_by") != actor:
            raise PlatformError("claim.lease_active", "task already has an active lease held by another actor")
        before = "In Progress" if takeover else state
        metadata["lease"] = {
            "claimed_by": actor,
            "claimed_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "proposal_id": None,
        }
        metadata["state"] = "In Progress"
        metadata["updated_at"] = utc_now()
        events.append(make_event(
            "task.claimed",
            task_id,
            actor,
            on_behalf_of,
            before,
            "In Progress",
            payload={"lease": metadata["lease"]},
            entrypoint="agent",
        ))
    elif not events:
        raise PlatformError(
            "claim.not_ready",
            "only a Ready or In Progress task can be re-claimed with a handoff token",
            details={"state": state},
        )
    write_task(path, metadata, body)
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, events))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository)],
        f"Reclaim {task_id}",
    )
    return {
        "task_id": task_id,
        "state": metadata["state"],
        "lease": metadata["lease"],
        "lease_expires_at": metadata["lease"]["expires_at"],
        "package": build_task_package(repository, metadata, body),
        "authoritative": True,
        "commit": commit,
    }


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
