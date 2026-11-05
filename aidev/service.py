import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import OperationalError, PlatformError
from .gitrepo import commit_paths, ensure_remote_syncable, ensure_repository, initialize_repository
from .jsonio import append_jsonl, new_id, read_json, read_jsonl, sha256_file, utc_now, write_json
from .taskfile import read_task, write_task

SPEC_FILES = {
    "acceptance_card": "acceptance-card.json",
    "contract": "contract.json",
    "mapping": "mapping.json",
}

REPORT_SECTIONS = ("implemented", "executed", "not_executed", "deviations", "collaboration_requests")


def event_path(repository):
    return Path(repository) / "events" / "events.jsonl"


def task_path(repository, task_id):
    return Path(repository) / "tasks" / task_id / "task.md"


def spec_path(repository, metadata, key):
    return (
        Path(repository)
        / "specs"
        / metadata["spec_id"]
        / metadata["spec_pin"][key]["version"]
        / SPEC_FILES[key]
    )


def make_event(event_type, task_id, actor, on_behalf_of, before, after, proposal_id=None, payload=None, entrypoint="operator"):
    return {
        "event_id": new_id("event"),
        "event_type": event_type,
        "task_id": task_id,
        "timestamp": utc_now(),
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "entrypoint": entrypoint,
        "before": before,
        "after": after,
        "proposal_id": proposal_id,
        "payload": payload or {},
    }


def init_repository(repository, actor):
    repository = initialize_repository(repository)
    events = event_path(repository)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch()
    append_jsonl(events, [make_event("repository.initialized", None, actor, actor, None, None)])
    commit = commit_paths(repository, ["events/events.jsonl"], "Initialize AI development platform")
    return {"repository": str(repository), "commit": commit}


def workspace_agents(metadata):
    pin = json.dumps(metadata["spec_pin"], ensure_ascii=False, sort_keys=True)
    return f"""# 任务工作区约束

- task_id: {metadata['task_id']}
- requirement: {metadata['requirement']}
- spec_pin: {pin}
- 冻结文件：contract/acceptance-card.json、contract/contract.json、contract/mapping.json。
- 不得修改冻结文件，不得自行变更任务状态。
- 写操作必须通过 agent CLI 形成 outbox 提案，并等待 operator sync 接受。
- 提交报告必须包含：实现了什么、运行了什么、未运行或跳过项及原因、与验收卡的偏差、协作请求清单。
"""


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
    append_jsonl(event_path(repository), [event])
    commit = commit_paths(
        repository,
        [authoritative_task.relative_to(repository), event_path(repository).relative_to(repository)],
        f"Initialize {task_id}",
    )
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in ["contract", "evidence", "inbox", "outbox"]:
        (workspace / directory).mkdir(exist_ok=True)
    write_task(workspace / "task.md", metadata, body)
    (workspace / "AGENTS.md").write_text(workspace_agents(metadata), encoding="utf-8", newline="\n")
    return {"task_id": task_id, "spec_id": spec_id, "state": "Draft", "commit": commit}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def lease_is_active(lease, at=None):
    if not lease:
        return False
    at = at or datetime.now(timezone.utc)
    return parse_time(lease["expires_at"]) > at


def validate_workspace_pin(workspace, metadata):
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict):
        raise PlatformError("claim.spec_not_pinned", "task has no spec_pin")
    for key, filename in SPEC_FILES.items():
        expected = pin.get(key, {}).get("sha256")
        path = Path(workspace) / "contract" / filename
        if not expected or not path.is_file() or sha256_file(path) != expected:
            raise PlatformError("claim.spec_hash_mismatch", f"frozen file hash mismatch: {filename}")


def proposal_base(metadata):
    return {
        "state": metadata["state"],
        "spec_pin": metadata["spec_pin"],
        "lease": metadata.get("lease"),
        "updated_at": metadata["updated_at"],
    }


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


def validate_report(report):
    if not isinstance(report, dict):
        raise PlatformError("submit.invalid_report", "report must be a JSON object")
    if set(report) != set(REPORT_SECTIONS):
        raise PlatformError("submit.invalid_report", f"report must contain exactly these sections: {list(REPORT_SECTIONS)}")
    if not isinstance(report["implemented"], str) or not report["implemented"].strip():
        raise PlatformError("submit.invalid_report", "implemented must be a non-empty string")
    for executed in report["executed"]:
        if not isinstance(executed, dict):
            raise PlatformError("submit.invalid_report", "each executed item must be an object")
        command = executed.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise PlatformError("submit.invalid_report", "executed.command must be a non-empty string list")
        if not isinstance(executed.get("output"), str) or not executed["output"].strip():
            raise PlatformError("submit.invalid_report", "executed.output must be a non-empty string")
    for skipped in report["not_executed"]:
        if not isinstance(skipped, dict) or not isinstance(skipped.get("item"), str) or not skipped["item"].strip() or not isinstance(skipped.get("reason"), str) or not skipped["reason"].strip():
            raise PlatformError("submit.invalid_report", "each not_executed item must contain item and reason")
    for section in ("deviations", "collaboration_requests"):
        if any(not isinstance(item, dict) for item in report[section]):
            raise PlatformError("submit.invalid_report", f"{section} must contain objects")


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


def rejection(proposal, metadata, code, message):
    task_id = proposal["task_id"]
    event = make_event(
        "proposal.rejected",
        task_id,
        "operator",
        proposal["on_behalf_of"],
        metadata.get("state") if metadata else None,
        metadata.get("state") if metadata else None,
        proposal_id=proposal["proposal_id"],
        payload={"operation": proposal.get("operation"), "code": code, "reason": message, "actor": proposal.get("actor")},
    )
    result = {"proposal_id": proposal["proposal_id"], "accepted": False, "code": code, "reason": message}
    return event, result


def base_is_current(proposal, metadata):
    if proposal.get("base", {}).get("state") != metadata.get("state"):
        return "proposal.stale", "proposal base state is stale"
    if proposal.get("base", {}).get("spec_pin") != metadata.get("spec_pin"):
        return "proposal.spec_pin_mismatch", "proposal and authoritative spec_pin differ"
    if proposal.get("base", {}).get("updated_at") != metadata.get("updated_at"):
        return "proposal.stale", "proposal base task version is stale"
    return None, None


def apply_claim(proposal, metadata):
    if metadata.get("state") != "Ready":
        return None, rejection(proposal, metadata, "claim.not_ready", "authoritative task is not Ready")
    code, message = base_is_current(proposal, metadata)
    if code:
        return None, rejection(proposal, metadata, code, message)
    if lease_is_active(metadata.get("lease")):
        return None, rejection(proposal, metadata, "claim.lease_active", "authoritative task already has an active lease")
    now = datetime.now(timezone.utc)
    metadata["lease"] = {
        "claimed_by": proposal["actor"],
        "claimed_at": utc_now(),
        "expires_at": (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proposal_id": proposal["proposal_id"],
    }
    metadata["updated_at"] = utc_now()
    metadata["state"] = "In Progress"
    event = make_event(
        "task.claimed",
        proposal["task_id"],
        proposal["actor"],
        proposal["on_behalf_of"],
        "Ready",
        "In Progress",
        proposal_id=proposal["proposal_id"],
        payload={"lease": metadata["lease"]},
        entrypoint="agent",
    )
    result = {"proposal_id": proposal["proposal_id"], "accepted": True, "state": "In Progress"}
    return event, (None, result)


def apply_submit(proposal, metadata):
    if metadata.get("state") != "In Progress":
        return None, rejection(proposal, metadata, "submit.invalid_state", "authoritative task is not In Progress")
    code, message = base_is_current(proposal, metadata)
    if code:
        return None, rejection(proposal, metadata, code, message)
    lease = metadata.get("lease") or {}
    if not lease_is_active(lease):
        return None, rejection(proposal, metadata, "submit.lease_expired", "authoritative lease is not active")
    if lease.get("claimed_by") != proposal.get("actor"):
        return None, rejection(proposal, metadata, "submit.lease_owner_mismatch", "proposal actor does not hold the lease")
    report = proposal.get("parameters", {}).get("report")
    evidence = proposal.get("parameters", {}).get("evidence")
    try:
        validate_report(report)
        if not isinstance(evidence, list) or not evidence:
            raise PlatformError("submit.evidence_empty", "at least one evidence item is required")
        for item in evidence:
            if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
                raise PlatformError("submit.invalid_evidence", "evidence must contain path and sha256")
    except PlatformError as error:
        return None, rejection(proposal, metadata, error.code, error.message)
    metadata["lease"] = None
    metadata["updated_at"] = utc_now()
    metadata["state"] = "Implemented"
    event = make_event(
        "task.submitted",
        proposal["task_id"],
        proposal["actor"],
        proposal["on_behalf_of"],
        "In Progress",
        "Implemented",
        proposal_id=proposal["proposal_id"],
        payload={"report": report, "evidence": evidence},
        entrypoint="agent",
    )
    result = {"proposal_id": proposal["proposal_id"], "accepted": True, "state": "Implemented"}
    return event, (None, result)


def apply_block(proposal, metadata):
    if metadata.get("state") != "In Progress":
        return None, rejection(proposal, metadata, "block.invalid_state", "authoritative task is not In Progress")
    code, message = base_is_current(proposal, metadata)
    if code:
        return None, rejection(proposal, metadata, code, message)
    lease = metadata.get("lease") or {}
    if not lease_is_active(lease):
        return None, rejection(proposal, metadata, "block.lease_expired", "authoritative lease is not active")
    if lease.get("claimed_by") != proposal.get("actor"):
        return None, rejection(proposal, metadata, "block.lease_owner_mismatch", "proposal actor does not hold the lease")
    reason = proposal.get("parameters", {}).get("reason")
    evidence = proposal.get("parameters", {}).get("evidence")
    if not reason or not evidence:
        return None, rejection(proposal, metadata, "block.invalid_payload", "block reason and evidence are required")
    metadata["lease"] = None
    metadata["updated_at"] = utc_now()
    metadata["state"] = "Blocked"
    blocked_event = make_event(
        "task.blocked",
        proposal["task_id"],
        proposal["actor"],
        proposal["on_behalf_of"],
        "In Progress",
        "Blocked",
        proposal_id=proposal["proposal_id"],
        payload={"reason": reason, "evidence": evidence},
        entrypoint="agent",
    )
    discussion_event = make_event(
        "discussion.opened",
        proposal["task_id"],
        "operator",
        proposal["on_behalf_of"],
        "Blocked",
        "Blocked",
        proposal_id=proposal["proposal_id"],
        payload={"topic": "block", "reason": reason},
    )
    result = {"proposal_id": proposal["proposal_id"], "accepted": True, "state": "Blocked"}
    return [blocked_event, discussion_event], (None, result)


def apply_enhancement(proposal, metadata):
    if metadata.get("state") != "In Progress":
        return None, rejection(proposal, metadata, "enhancement.invalid_state", "authoritative task is not In Progress")
    code, message = base_is_current(proposal, metadata)
    if code:
        return None, rejection(proposal, metadata, code, message)
    title = proposal.get("parameters", {}).get("title")
    details = proposal.get("parameters", {}).get("details")
    if not title or not details:
        return None, rejection(proposal, metadata, "enhancement.invalid_payload", "title and details are required")
    event = make_event(
        "enhancement.recorded",
        proposal["task_id"],
        proposal["actor"],
        proposal["on_behalf_of"],
        "In Progress",
        "In Progress",
        proposal_id=proposal["proposal_id"],
        payload={"title": title, "details": details},
        entrypoint="agent",
    )
    result = {"proposal_id": proposal["proposal_id"], "accepted": True, "state": "In Progress"}
    return event, (None, result)


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
                accepted_event, outcome = apply_submit(proposal, metadata)
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
        paths = []
        for task_id, (metadata, body) in changed_tasks.items():
            path = task_path(repository, task_id)
            write_task(path, metadata, body)
            paths.append(path.relative_to(repository))
        if new_events:
            append_jsonl(events_file, new_events)
            paths.append(events_file.relative_to(repository))
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
    return {
        "task": metadata,
        "inbox": sorted(path.name for path in inbox.glob("*.json")) if inbox.is_dir() else [],
        "authoritative": False,
    }


def task_log(repository, task_id):
    repository = ensure_repository(repository)
    return [event for event in read_jsonl(event_path(repository)) if event.get("task_id") == task_id]
