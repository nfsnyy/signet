import os
from pathlib import Path

from .. import codeflow
from ..collaboration import process_collaboration_sync, validate_report_collaboration
from ..domain import (
    apply_block,
    apply_claim,
    apply_enhancement,
    apply_submit,
    event_path,
    rejection,
    task_path,
)
from ..errors import OperationalError, PlatformError
from ..gitrepo import commit_paths, ensure_remote_syncable, ensure_repository
from ..jsonio import append_jsonl, read_json, read_jsonl
from ..taskfile import read_task, write_task
from .claims import collect_expired_leases
from .evidence import (
    evidence_basenames,
    materialize_submit_evidence,
    materialize_submit_evidence_bytes,
)
from .packages import stamp_events


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
