import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .authn import mint_sigt
from .collaboration import collaboration_overview, process_collaboration_sync, validate_report_collaboration
from .domain import (
    REPORT_SECTIONS,
    SPEC_FILES,
    VOIDABLE_BY_SPEC_INVALIDATION,
    WORKSPACE_DIRECTORIES,
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
    validate_actor_alias,
    validate_report,
    workspace_agents,
    resolve_workspace_directory,
)
from .errors import NotFoundError, OperationalError, PlatformError
from .readmodel import parse_requirement_text, pinned_acceptance_card
from .gitrepo import commit_paths, ensure_remote_syncable, ensure_repository, initialize_repository
from .jsonio import append_jsonl, new_id, read_json, read_jsonl, sha256_file, utc_now, write_json
from .taskfile import read_task, write_task


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


def project_metadata(repository):
    path = Path(repository) / "project.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    project_id = data.get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def stamp_events(repository, events):
    project_id = project_metadata(repository)
    if not project_id:
        return events
    for event in events:
        event.setdefault("project_id", project_id)
    return events


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


def init_task(repository, workspace, title, requirement, owner, actor):
    repository = ensure_repository(repository)
    workspace = resolve_workspace_directory(workspace)
    if workspace == repository or repository in workspace.parents or workspace in repository.parents:
        raise PlatformError("workspace.invalid_boundary", "agent workspace and authoritative repository must be separate")
    if workspace.exists() and any(workspace.iterdir()):
        raise PlatformError("workspace.not_empty", "agent workspace must be empty")
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
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in WORKSPACE_DIRECTORIES:
        (workspace / directory).mkdir(exist_ok=True)
    write_task(workspace / "task.md", metadata, body)
    (workspace / "AGENTS.md").write_text(workspace_agents(metadata), encoding="utf-8", newline="\n")
    hints_source = Path(repository) / "hints.md"
    if hints_source.is_file():
        shutil.copyfile(hints_source, workspace / "hints.md")
    return {"task_id": task_id, "spec_id": spec_id, "state": "Draft", "commit": commit}


def validate_workspace_pin(workspace, metadata):
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict):
        raise PlatformError("claim.spec_not_pinned", "task has no spec_pin")
    for key, filename in SPEC_FILES.items():
        expected = pin.get(key, {}).get("sha256")
        path = Path(workspace) / "contract" / filename
        if not expected or not path.is_file() or sha256_file(path) != expected:
            raise PlatformError("claim.spec_hash_mismatch", f"frozen file hash mismatch: {filename}")


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


def read_report(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError("submit.invalid_report", f"report is not valid JSON: {path}") from error


def evidence_manifest(evidence_files):
    manifest = []
    for value in evidence_files:
        path = Path(value).resolve()
        if not path.is_file():
            raise PlatformError("submit.evidence_missing", f"evidence file does not exist: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise PlatformError("submit.evidence_empty", f"evidence file is empty: {path}")
        manifest.append({"path": str(path), "sha256": sha256_file(path), "size": size})
    return manifest


def submit(workspace, actor, on_behalf_of, report_file, evidence_files):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    if metadata.get("state") != "In Progress":
        raise PlatformError("submit.invalid_state", "only an In Progress task can be submitted", details={"state": metadata.get("state")})
    lease = metadata.get("lease") or {}
    if not lease_is_active(lease):
        raise PlatformError("submit.lease_expired", "task lease is not active")
    if lease.get("claimed_by") != actor:
        raise PlatformError("submit.lease_owner_mismatch", "only the lease holder can submit this task")
    validate_workspace_pin(workspace, metadata)
    report = read_report(report_file)
    validate_report(report)
    evidence = evidence_manifest(evidence_files)
    proposal_id = new_id("proposal")
    proposal = {
        "proposal_id": proposal_id,
        "task_id": metadata["task_id"],
        "operation": "submit",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {"report": report, "evidence": evidence},
        "workspace": str(workspace),
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    write_json(workspace / "outbox" / f"{proposal_id}.json", proposal)
    return {"proposal_id": proposal_id, "task_id": metadata["task_id"], "authoritative": False}


def validate_active_lease(metadata, actor, operation):
    lease = metadata.get("lease") or {}
    if not lease_is_active(lease):
        raise PlatformError(f"{operation}.lease_expired", "task lease is not active")
    if lease.get("claimed_by") != actor:
        raise PlatformError(f"{operation}.lease_owner_mismatch", "only the lease holder can perform this operation")


def block(workspace, actor, on_behalf_of, reason, evidence_files):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    if metadata.get("state") != "In Progress":
        raise PlatformError("block.invalid_state", "only an In Progress task can be blocked", details={"state": metadata.get("state")})
    validate_active_lease(metadata, actor, "block")
    if not reason or not reason.strip():
        raise PlatformError("block.reason_required", "block reason is required")
    evidence = evidence_manifest(evidence_files)
    proposal_id = new_id("proposal")
    proposal = {
        "proposal_id": proposal_id,
        "task_id": metadata["task_id"],
        "operation": "block",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {"reason": reason, "evidence": evidence},
        "workspace": str(workspace),
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    write_json(workspace / "outbox" / f"{proposal_id}.json", proposal)
    return {"proposal_id": proposal_id, "task_id": metadata["task_id"], "authoritative": False}


def enhancement(workspace, actor, on_behalf_of, title, details):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    if metadata.get("state") != "In Progress":
        raise PlatformError("enhancement.invalid_state", "only an In Progress task can record an enhancement", details={"state": metadata.get("state")})
    validate_active_lease(metadata, actor, "enhancement")
    if not title or not title.strip() or not details or not details.strip():
        raise PlatformError("enhancement.details_required", "title and details are required")
    proposal_id = new_id("proposal")
    proposal = {
        "proposal_id": proposal_id,
        "task_id": metadata["task_id"],
        "operation": "enhancement",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {"title": title, "details": details},
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


def refresh_workspace(repository, workspace, task_id):
    authoritative = task_path(repository, task_id)
    workspace = Path(workspace).resolve()
    target = workspace / "task.md"
    if not authoritative.is_file() or not target.is_file():
        return
    metadata, body = read_task(authoritative)
    write_task(target, metadata, body)
    (workspace / "AGENTS.md").write_text(workspace_agents(metadata), encoding="utf-8", newline="\n")
    pin = metadata.get("spec_pin")
    if not pin:
        return
    for key, filename in SPEC_FILES.items():
        source = spec_path(repository, metadata, key)
        if source.is_file():
            shutil.copyfile(source, workspace / "contract" / filename)


def synchronize(repository, workspaces, actor, push=False):
    repository = ensure_repository(repository)
    lock_path = repository / ".aidev-sync.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise OperationalError("sync.already_running", "another sync holds the repository lock") from error
    os.close(descriptor)
    try:
        ensure_remote_syncable(repository)
        proposals = []
        for workspace in workspaces:
            outbox = Path(workspace).resolve() / "outbox"
            for path in outbox.glob("*.json"):
                proposal = read_json(path)
                proposal["workspace"] = str(outbox.parent.resolve())
                proposals.append(proposal)
        proposals.sort(key=lambda item: (item["created_at"], item["proposal_id"]))
        events_file = event_path(repository)
        existing_events = read_jsonl(events_file)
        processed = {item.get("proposal_id") for item in existing_events if item.get("proposal_id")}
        new_events = []
        results = []
        changed_tasks = {}
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
                accepted_event, outcome = apply_submit(
                    proposal,
                    metadata,
                    collaboration_validator=lambda task_id, report: validate_report_collaboration(repository, task_id, report),
                )
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
        collab_events, collab_paths, collab_results = process_collaboration_sync(repository, [Path(workspace).resolve() for workspace in workspaces])
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
        commit = commit_paths(repository, paths, f"Sync {len(proposals)} proposal(s)", push=push) if paths else None
        refreshed_workspaces = set()
        for proposal in proposals:
            workspace = proposal.get("workspace")
            if workspace and workspace not in refreshed_workspaces:
                refresh_workspace(repository, workspace, proposal["task_id"])
                refreshed_workspaces.add(workspace)
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
        "handoff_token": mint_sigt(task_id, actor, str(repository)),
        "authoritative": True,
        "commit": commit,
    }


def requirement_body(repository, requirement_id):
    """requirements/{id}/requirement.md 去 frontmatter 后的原文；无此需求返回 None。"""
    if not requirement_id:
        return None
    file_path = Path(repository) / "requirements" / requirement_id / "requirement.md"
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


def build_task_package(repository, metadata, body):
    """sigt 接管响应携带的完整任务包（AC-01）。

    workspace_files 由 CLI 原样落进工作区根：AGENTS.md 恒有；状态仓根存在
    hints.md 时一并注入（不存在则不注入、不报错——防御语义）。
    """
    repository = Path(repository)
    workspace_files = {"AGENTS.md": workspace_agents(metadata)}
    hints_source = repository / "hints.md"
    if hints_source.is_file():
        try:
            workspace_files["hints.md"] = hints_source.read_text(encoding="utf-8")
        except OSError:
            pass
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
        "workspace_files": workspace_files,
    }


def last_claim_delegate(repository, task_id):
    """最近一次 task.claimed 的执行者（on_behalf_of 优先，缺省 actor）——sigt 续期判定键。"""
    for event in reversed(read_jsonl(event_path(repository))):
        if event.get("task_id") == task_id and event.get("event_type") == "task.claimed":
            return event.get("on_behalf_of") or event.get("actor")
    return None


def claim_with_sigt(repository, task_id, actor, on_behalf_of=None):
    """sigt 幂等接管 + 续期：直接改写权威账本并提交，响应携带完整任务包。

    - In Progress 且同执行者（actor 且 on_behalf_of 与最近一次 claim 一致）持活跃租约：
      续期（lease.renewed 事件，expires_at 顺延 24h）；
    - Ready（含 refreeze 释放租约后）或执行者变更（委托转交另一 agent）：重 claim
      （task.claimed 事件，状态进入 In Progress）；
    - 他人持活跃租约（actor 不同）：拒绝（claim.lease_active）。
    AC-16 委托流归因：事件 actor=签发人（问责锁定），on_behalf_of=执行者（缺省回落 actor）。
    """
    repository = ensure_repository(repository)
    on_behalf_of = on_behalf_of or actor
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    state = metadata.get("state")
    lease = metadata.get("lease") or {}
    now = datetime.now(timezone.utc)
    events = []
    takeover = False
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
    evidence = evidence_manifest(evidence_files)
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
    )
    if accepted_event is None:
        _rejected_event, result = outcome
        raise PlatformError(result.get("code", "submit.rejected"), result.get("reason", "submit was rejected"))
    events_file = event_path(repository)
    write_task(path, metadata, body)
    append_jsonl(events_file, stamp_events(repository, [accepted_event]))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository)],
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


# ---------------------------------------------------------------------------
# T9 需求起草流：需求动词（create/update/submit/confirm）与派生闸门。
# 需求存储约定：requirements/<id>/requirement.md，frontmatter 为「key: value」
# 行（readmodel.parse_requirement_text 口径），判据条目在正文「判据条目」节以
# 「- REQ-<n>：<text>」列表项定义；每个动词落 git commit + 账本事件。

ENTRY_ID_RE = re.compile(r"^REQ-\d+$")


def requirement_file_path(repository, requirement_id):
    return Path(repository) / "requirements" / requirement_id / "requirement.md"


def read_requirement(repository, requirement_id):
    """读需求 frontmatter + 正文 + 条目（含文本）；需求不存在返回 None。"""
    file_path = requirement_file_path(repository, requirement_id)
    if not file_path.is_file():
        return None
    try:
        fields, body = parse_requirement_text(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return fields, body, requirement_entries(body)


def requirement_entries(body):
    """正文「判据条目」节的条目列表（- REQ-<n>：…；节外条目不计）。

    返回 [{id, text}]；text 为编号后文本（可空串——确认态重写不丢原文）。
    """
    entries = []
    in_section = False
    for line in str(body or "").splitlines():
        if line.startswith("## "):
            in_section = "判据条目" in line
            continue
        if in_section and line.startswith("- "):
            token = line[2:].strip()
            entry_id = re.split(r"[：:\s]", token, maxsplit=1)[0].strip()
            if entry_id:
                text = token[len(entry_id):].lstrip("：: ").strip()
                entries.append({"id": entry_id, "text": text})
    return entries


def _validate_entries(entries):
    """条目集合法性：REQ-<n> 编号、同需求内不重复。"""
    cleaned = []
    for item in entries or []:
        if not isinstance(item, dict):
            raise PlatformError("requirement.invalid_entries", "each entry must be an object with id and text")
        entry_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not ENTRY_ID_RE.fullmatch(entry_id):
            raise PlatformError("requirement.invalid_entries", f"entry id must look like REQ-<n>: {entry_id!r}")
        if not text:
            raise PlatformError("requirement.invalid_entries", f"entry text must be non-empty: {entry_id}")
        if entry_id in cleaned:
            raise PlatformError("requirement.duplicate_entry", f"duplicate entry id in one requirement: {entry_id}")
        cleaned.append({"id": entry_id, "text": text})
    return cleaned


def _strip_entries_section(body):
    """剥掉正文里的「判据条目」节（重写时随 entries 参数重建，避免重复追加）。"""
    kept = []
    in_section = False
    for line in str(body or "").splitlines():
        if line.startswith("## "):
            in_section = "判据条目" in line
            if in_section:
                continue
        if not in_section:
            kept.append(line)
    return "\n".join(kept).rstrip("\n")


def _compose_requirement_text(fields, body, entries):
    lines = ["---"]
    for key in ("requirement_id", "title", "status", "priority", "owner", "created_at", "updated_at"):
        if fields.get(key) is not None:
            lines.append(f"{key}: {fields[key]}")
    lines.append("---")
    lines.append("")
    main_body = _strip_entries_section(body).strip("\n")
    if main_body.strip():
        lines.append(main_body)
        lines.append("")
    if entries:
        lines.append("## 判据条目")
        lines.append("")
        for item in entries:
            lines.append(f"- {item['id']}：{item['text']}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _commit_requirement_change(repository, requirement_id, events, message):
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, events))
    changed = [
        requirement_file_path(repository, requirement_id).relative_to(Path(repository)),
        events_file.relative_to(Path(repository)),
    ]
    return commit_paths(repository, changed, message)


def validate_requirement_derive(repository, requirement_id):
    """派生闸门（门禁①）：需求引用必须存在且 status=confirmed。

    页面表单与 cookie 会话 API 同一条创建路径（AC-05）；operator CLI
    （Bearer 用户令牌）保留自由引用能力，兼容 T1–T8 既有工作流。
    """
    repository = ensure_repository(repository)
    requirement_id = (requirement_id or "").strip()
    if not requirement_id:
        raise PlatformError("request.invalid", "requirement is required and must be non-empty")
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    fields, _body, _entries = requirement
    if fields.get("status") != "confirmed":
        raise PlatformError(
            "requirement.not_confirmed",
            f"requirement {requirement_id} is not confirmed (status={fields.get('status') or '-'})",
            details={"requirement_id": requirement_id, "status": fields.get("status")},
        )


def create_requirement(repository, requirement_id, title, body, entries, actor, on_behalf_of):
    repository = ensure_repository(repository)
    validate_actor_alias(requirement_id)
    title = (title or "").strip()
    if not title:
        raise PlatformError("requirement.title_required", "title is required and must be non-empty")
    cleaned_entries = _validate_entries(entries)
    target = requirement_file_path(repository, requirement_id)
    if target.is_file():
        raise PlatformError("requirement.exists", f"requirement already exists: {requirement_id}")
    now = utc_now()
    fields = {
        "requirement_id": requirement_id,
        "title": title,
        "status": "draft",
        "priority": "P1",
        "owner": actor,
        "created_at": now,
        "updated_at": now,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_compose_requirement_text(fields, body, cleaned_entries), encoding="utf-8", newline="\n")
    event = make_event(
        "requirement.created",
        None,
        actor,
        on_behalf_of,
        None,
        "draft",
        payload={"requirement_id": requirement_id, "title": title, "status": "draft"},
        entrypoint="agent",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Create requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": "draft", "entries": [item["id"] for item in cleaned_entries], "commit": commit}


def update_requirement(repository, requirement_id, body, entries, actor, on_behalf_of):
    repository = ensure_repository(repository)
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    fields, _old_body, _old_entries = requirement
    if fields.get("status") not in ("draft", "submitted"):
        raise PlatformError(
            "requirement.not_editable",
            f"requirement {requirement_id} is confirmed and can no longer be revised via API",
            details={"requirement_id": requirement_id, "status": fields.get("status")},
        )
    cleaned_entries = _validate_entries(entries)
    fields["updated_at"] = utc_now()
    target = requirement_file_path(repository, requirement_id)
    target.write_text(_compose_requirement_text(fields, body, cleaned_entries), encoding="utf-8", newline="\n")
    event = make_event(
        "requirement.updated",
        None,
        actor,
        on_behalf_of,
        fields.get("status"),
        fields.get("status"),
        payload={"requirement_id": requirement_id, "fields": ["body", "entries"]},
        entrypoint="agent",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Update requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": fields.get("status"), "commit": commit}


def submit_requirement(repository, requirement_id, actor, on_behalf_of):
    repository = ensure_repository(repository)
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    fields, body, entries = requirement
    if fields.get("status") != "draft":
        raise PlatformError(
            "requirement.invalid_state",
            f"only a draft requirement can be submitted: {requirement_id}",
            details={"requirement_id": requirement_id, "status": fields.get("status")},
        )
    fields["status"] = "submitted"
    fields["updated_at"] = utc_now()
    target = requirement_file_path(repository, requirement_id)
    target.write_text(_compose_requirement_text(fields, body, entries), encoding="utf-8", newline="\n")
    event = make_event(
        "requirement.submitted",
        None,
        actor,
        on_behalf_of,
        "draft",
        "submitted",
        payload={"requirement_id": requirement_id},
        entrypoint="agent",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Submit requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": "submitted", "commit": commit}


def confirm_requirement(repository, requirement_id, entries, actor):
    """人本人交互登录的确认动作：全量勾选清单与当前条目集合原子比对。

    清单一致 → status 翻 confirmed + 恰落一个 requirement.confirmed 事件
    （actor=on_behalf_of=登录人）；缺条/多条/假编号一律 entries_mismatch。
    """
    repository = ensure_repository(repository)
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    fields, body, current_entries = requirement
    if fields.get("status") != "submitted":
        raise PlatformError(
            "requirement.invalid_state",
            f"only a submitted requirement can be confirmed: {requirement_id}",
            details={"requirement_id": requirement_id, "status": fields.get("status")},
        )
    current_ids = [item["id"] for item in current_entries]
    submitted = sorted(str(item) for item in (entries or []))
    if submitted != sorted(current_ids):
        raise PlatformError(
            "requirement.entries_mismatch",
            "confirmation list does not match the requirement entry set exactly",
            details={
                "requirement_id": requirement_id,
                "expected": sorted(current_ids),
                "got": submitted,
            },
        )
    fields["status"] = "confirmed"
    fields["updated_at"] = utc_now()
    target = requirement_file_path(repository, requirement_id)
    target.write_text(
        _compose_requirement_text(fields, body, current_entries),
        encoding="utf-8", newline="\n",
    )
    event = make_event(
        "requirement.confirmed",
        None,
        actor,
        actor,
        "submitted",
        "confirmed",
        payload={"requirement_id": requirement_id, "entries": list(current_ids), "confirmed_by": actor},
        entrypoint="operator",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Confirm requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": "confirmed", "entries": list(current_ids), "commit": commit}


def record_pat_event(repository, event_type, actor, payload):
    """pat.issued / pat.revoked：事件落状态仓账本（git commit）；pats.json 本体在平台侧。"""
    repository = ensure_repository(repository)
    event = make_event(event_type, None, actor, actor, None, None, payload=payload, entrypoint="operator")
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, [event]))
    commit_paths(repository, [events_file.relative_to(repository)], event_type)
    return event
