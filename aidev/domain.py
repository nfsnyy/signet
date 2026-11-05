import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import PlatformError
from .jsonio import new_id, utc_now

SPEC_FILES = {
    "acceptance_card": "acceptance-card.json",
    "contract": "contract.json",
    "mapping": "mapping.json",
}

REPORT_SECTIONS = ("implemented", "executed", "not_executed", "deviations", "collaboration_requests")

REFREEZABLE_STATES = {"Ready", "In Progress", "Implemented", "Blocked"}

VOIDABLE_BY_SPEC_INVALIDATION = {"Ready", "In Progress", "Implemented", "Verified", "Blocked"}


def same_pin(left, right):
    return left and right and left == right


def invalid_marker_path(repository, spec_id, version):
    return Path(repository) / "specs" / spec_id / version / "INVALID.json"


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


ACTOR_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-*]{0,63}$")


def validate_actor_alias(actor):
    if not isinstance(actor, str) or not ACTOR_PATTERN.fullmatch(actor):
        raise PlatformError("actor.invalid_alias", "actor must be a lowercase alias such as nfsnyy, agent-a, or verifier-*", details={"actor": actor})


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def lease_is_active(lease, at=None):
    if not lease:
        return False
    at = at or datetime.now(timezone.utc)
    return parse_time(lease["expires_at"]) > at


def proposal_base(metadata):
    return {
        "state": metadata["state"],
        "spec_pin": metadata["spec_pin"],
        "lease": metadata.get("lease"),
        "updated_at": metadata["updated_at"],
    }


def base_is_current(proposal, metadata):
    if proposal.get("base", {}).get("state") != metadata.get("state"):
        return "proposal.stale", "proposal base state is stale"
    if proposal.get("base", {}).get("spec_pin") != metadata.get("spec_pin"):
        return "proposal.spec_pin_mismatch", "proposal and authoritative spec_pin differ"
    if proposal.get("base", {}).get("updated_at") != metadata.get("updated_at"):
        return "proposal.stale", "proposal base task version is stale"
    return None, None


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
    for item in report["collaboration_requests"]:
        if not isinstance(item.get("message_id"), str) or not item["message_id"].strip():
            raise PlatformError("submit.invalid_report", "each collaboration_requests item must contain a message_id")


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


def apply_submit(proposal, metadata, collaboration_validator=None):
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
        if collaboration_validator is not None:
            collaboration_validator(proposal["task_id"], report)
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
