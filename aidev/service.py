import json
import os
import shutil
from pathlib import Path

from .authn import mint_sigt
from .collaboration import process_collaboration_sync, validate_report_collaboration
from .domain import (
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
    validate_report,
    workspace_agents,
)
from .errors import OperationalError, PlatformError
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
    workspace = Path(workspace).resolve()
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
    """
    repository = ensure_repository(repository)
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    if metadata.get("state") != "Ready":
        raise PlatformError("claim.not_ready", "only a Ready task can be claimed", details={"state": metadata.get("state")})
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
    accepted_event, outcome = apply_claim(proposal, metadata)
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
