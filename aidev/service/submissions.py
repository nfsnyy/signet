import json
from pathlib import Path

from ..domain import lease_is_active, proposal_base, validate_report
from ..errors import PlatformError
from ..jsonio import new_id, utc_now, write_json
from ..taskfile import read_task
from .evidence import evidence_manifest, workspace_evidence_limits
from .packages import validate_workspace_pin


def read_report(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError("submit.invalid_report", f"report is not valid JSON: {path}") from error


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
    evidence = evidence_manifest(evidence_files, limits=workspace_evidence_limits(workspace))
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
