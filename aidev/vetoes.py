import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import OperationalError, PlatformError
from .gitrepo import commit_paths, ensure_repository
from .jsonio import append_jsonl, new_id, read_jsonl, sha256_file, utc_now
from .service import REFREEZABLE_STATES, event_path, make_event, parse_time, stamp_events, task_path
from .taskfile import read_task

SAFETY_STRENGTH = "safety-veto"
VETO_DELAY_HOURS = 24
VETO_LOG = Path("vetoes") / "vetoes.jsonl"


def veto_log_path(repository):
    return Path(repository) / VETO_LOG


def load_vetoes(repository):
    return read_jsonl(veto_log_path(repository))


def latest_vetoes(repository):
    latest = {}
    for veto in load_vetoes(repository):
        latest[veto.get("veto_id")] = veto
    return latest


def veto_lock(repository):
    lock_path = Path(repository) / ".aidev-veto.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise OperationalError("veto.already_running", "another veto operation holds the repository lock") from error
    os.close(descriptor)
    return lock_path


def load_document(path, code):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError(code, f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise PlatformError(code, f"JSON document must be an object: {path}")
    return value


def _criteria_by_id(card):
    return {item.get("criterion_id"): item for item in card.get("criteria", []) if isinstance(item, dict) and item.get("criterion_id")}


def _cases_by_id(contract):
    return {item.get("case_id"): item for item in contract.get("cases", []) if isinstance(item, dict) and item.get("case_id")}


def _mapping_rows(mapping):
    return {item.get("criterion_id"): item for item in mapping.get("rows", []) if isinstance(item, dict) and item.get("criterion_id")}


def _case_signature(case):
    if not isinstance(case, dict):
        return None
    return {
        "command": case.get("command"),
        "expect": case.get("expect"),
        "fixtures": sorted(case.get("fixtures") or []),
    }


def analyze_safety_surfaces(old_card, old_contract, old_mapping, new_card, new_contract, new_mapping):
    old_criteria = _criteria_by_id(old_card)
    new_criteria = _criteria_by_id(new_card)
    old_cases = _cases_by_id(old_contract)
    new_cases = _cases_by_id(new_contract)
    old_rows = _mapping_rows(old_mapping)
    new_rows = _mapping_rows(new_mapping)
    safety_ids = [criterion_id for criterion_id, criterion in old_criteria.items() if criterion.get("strength") == SAFETY_STRENGTH]
    affected_criteria = []
    for criterion_id in safety_ids:
        new_criterion = new_criteria.get(criterion_id)
        if new_criterion is None:
            affected_criteria.append(criterion_id)
            continue
        if new_criterion.get("strength") != SAFETY_STRENGTH or new_criterion.get("text") != old_criteria[criterion_id].get("text"):
            affected_criteria.append(criterion_id)
    safety_cases = set()
    for criterion_id in safety_ids:
        row = old_rows.get(criterion_id) or {}
        for case_id in row.get("case_ids") or []:
            if case_id in old_cases:
                safety_cases.add(case_id)
    affected_cases = []
    for case_id in sorted(safety_cases):
        if _case_signature(old_cases.get(case_id)) != _case_signature(new_cases.get(case_id)):
            affected_cases.append(case_id)
    for criterion_id in safety_ids:
        old_case_ids = sorted((old_rows.get(criterion_id) or {}).get("case_ids") or [])
        new_case_ids = sorted((new_rows.get(criterion_id) or {}).get("case_ids") or [])
        if old_case_ids != new_case_ids:
            for case_id in set(old_case_ids) ^ set(new_case_ids):
                if case_id not in affected_cases:
                    affected_cases.append(case_id)
    affected_environment = []
    old_dependencies = sorted(old_contract.get("environment", {}).get("dependency_files") or [])
    new_dependencies = sorted(new_contract.get("environment", {}).get("dependency_files") or [])
    if old_dependencies != new_dependencies:
        affected_environment.append("environment.dependency_files")
    old_assets = old_contract.get("assets") or {}
    new_assets = new_contract.get("assets") or {}
    for case_id in safety_cases:
        for fixture in (old_cases.get(case_id) or {}).get("fixtures") or []:
            if old_assets.get(fixture) != new_assets.get(fixture):
                affected_environment.append(fixture)
    return {
        "downgrade_required": bool(affected_criteria or affected_cases or affected_environment),
        "safety_criteria": affected_criteria,
        "cases": affected_cases,
        "environment": sorted(set(affected_environment)),
    }


def classify_refreeze(repository, spec_id, old_version, new_card, new_contract, new_mapping):
    version_directory = Path(repository) / "specs" / spec_id / old_version
    old_card = load_document(version_directory / "acceptance-card.json", "veto.invalid_spec")
    old_contract = load_document(version_directory / "contract.json", "veto.invalid_spec")
    old_mapping = load_document(version_directory / "mapping.json", "veto.invalid_spec")
    return analyze_safety_surfaces(old_card, old_contract, old_mapping, new_card, new_contract, new_mapping)


def approved_veto_for(vetoes, spec_id, file_hashes):
    for veto in vetoes.values():
        if veto.get("spec_id") != spec_id:
            continue
        if veto.get("status") not in {"approved", "expired_effective"}:
            continue
        if veto.get("resolution"):
            continue
        files = veto.get("files") or {}
        if all(files.get(key, {}).get("sha256") == digest for key, digest in file_hashes.items()):
            return veto
    return None


def propose_veto(repository, task_id, card_file, contract_file, mapping_file, reason, actor, push=False):
    repository = ensure_repository(repository)
    if not reason or not reason.strip():
        raise PlatformError("veto.reason_required", "veto reason is required")
    if not actor or not actor.strip():
        raise PlatformError("veto.actor_required", "veto actor is required")
    lock = veto_lock(repository)
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, _ = read_task(path)
        pin = metadata.get("spec_pin")
        if not pin:
            raise PlatformError("veto.no_spec", "task has no frozen specification", details={"state": metadata.get("state")})
        if metadata.get("state") not in REFREEZABLE_STATES:
            raise PlatformError("veto.invalid_state", "task state cannot propose a veto", details={"state": metadata.get("state")})
        try:
            hashes = {
                "acceptance_card": sha256_file(card_file),
                "contract": sha256_file(contract_file),
                "mapping": sha256_file(mapping_file),
            }
        except FileNotFoundError as error:
            raise PlatformError("veto.file_missing", f"specification file does not exist: {error.filename}") from error
        new_card = load_document(card_file, "veto.invalid_card")
        new_contract = load_document(contract_file, "veto.invalid_contract")
        new_mapping = load_document(mapping_file, "veto.invalid_mapping")
        spec_id = metadata["spec_id"]
        old_version = pin["acceptance_card"]["version"]
        analysis = classify_refreeze(repository, spec_id, old_version, new_card, new_contract, new_mapping)
        if not analysis["downgrade_required"]:
            raise PlatformError("veto.not_required", "refreeze does not touch safety-veto surfaces; use refreeze directly", details=analysis)
        proposed_at = utc_now()
        veto = {
            "veto_id": new_id("veto"),
            "spec_id": spec_id,
            "task_id": task_id,
            "reason": reason,
            "proposed_by": actor,
            "proposed_at": proposed_at,
            "delay_until": (datetime.now(timezone.utc) + timedelta(hours=VETO_DELAY_HOURS)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "status": "proposed",
            "files": {key: {"sha256": digest} for key, digest in hashes.items()},
            "analysis": analysis,
            "decided_by": None,
            "decided_at": None,
            "effective_at": None,
            "resolution": None,
        }
        log_path = veto_log_path(repository)
        append_jsonl(log_path, [veto])
        event = make_event("veto.proposed", task_id, actor, actor, metadata["state"], metadata["state"], payload={"veto_id": veto["veto_id"], "spec_id": spec_id, "analysis": analysis, "delay_until": veto["delay_until"]})
        events_file = event_path(repository)
        append_jsonl(events_file, stamp_events(repository, [event]))
        commit = commit_paths(repository, [log_path.relative_to(repository), events_file.relative_to(repository)], f"Propose veto for {task_id}", push=push)
        veto["commit"] = commit
        return veto
    finally:
        lock.unlink(missing_ok=True)


def decide_veto(repository, veto_id, action, actor, reason=None, push=False):
    repository = ensure_repository(repository)
    if action not in {"approve", "reject", "withdraw"}:
        raise PlatformError("veto.invalid_action", "action must be approve, reject, or withdraw")
    if not actor or not actor.strip():
        raise PlatformError("veto.actor_required", "veto actor is required")
    lock = veto_lock(repository)
    try:
        vetoes = latest_vetoes(repository)
        veto = vetoes.get(veto_id)
        if not veto:
            raise PlatformError("veto.not_found", f"veto does not exist: {veto_id}")
        if veto.get("status") != "proposed":
            raise PlatformError("veto.invalid_state", "only a proposed veto can be decided", details={"status": veto.get("status")})
        if parse_time(veto["delay_until"]) <= datetime.now(timezone.utc):
            raise PlatformError("veto.invalid_state", "veto delay window has expired; wait for settlement on the next command", details={"delay_until": veto["delay_until"]})
        decided_at = utc_now()
        update = dict(veto)
        update["decided_by"] = actor
        update["decided_at"] = decided_at
        if action == "approve":
            if actor == veto["proposed_by"]:
                raise PlatformError("veto.self_approve", "a veto cannot be approved by its proposer")
            update["status"] = "approved"
            update["effective_at"] = decided_at
        elif action == "reject":
            if actor == veto["proposed_by"]:
                raise PlatformError("veto.self_reject", "a veto cannot be rejected by its proposer; use withdraw")
            if not reason or not reason.strip():
                raise PlatformError("veto.reason_required", "rejection reason is required")
            update["status"] = "rejected"
            update["reject_reason"] = reason
        else:
            if actor != veto["proposed_by"]:
                raise PlatformError("veto.not_proposer", "only the proposer can withdraw a veto")
            update["status"] = "withdrawn"
        log_path = veto_log_path(repository)
        append_jsonl(log_path, [update])
        event = make_event(f"veto.{action}d", veto.get("task_id"), actor, veto.get("proposed_by"), "proposed", update["status"], payload={"veto_id": veto_id, "reason": reason or veto.get("reason")})
        events_file = event_path(repository)
        append_jsonl(events_file, stamp_events(repository, [event]))
        commit = commit_paths(repository, [log_path.relative_to(repository), events_file.relative_to(repository)], f"{action.capitalize()} veto {veto_id}", push=push)
        update["commit"] = commit
        return update
    finally:
        lock.unlink(missing_ok=True)


def settle_expired_vetoes(repository, push=False):
    repository = Path(repository)
    if not (repository / ".git").exists():
        return {"settled": 0}
    lock_path = repository / ".aidev-veto-settle.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return {"settled": 0, "skipped": True}
    os.close(descriptor)
    try:
        now = datetime.now(timezone.utc)
        vetoes = latest_vetoes(repository)
        expired = [veto for veto in vetoes.values() if veto.get("status") == "proposed" and parse_time(veto["delay_until"]) <= now]
        if not expired:
            return {"settled": 0}
        updates = []
        events = []
        for veto in expired:
            update = dict(veto)
            update["status"] = "expired_effective"
            update["effective_at"] = veto["delay_until"]
            update["settled_at"] = utc_now()
            updates.append(update)
            events.append(make_event("veto.expired_effective", veto.get("task_id"), "operator", veto.get("proposed_by"), "proposed", "expired_effective", payload={"veto_id": veto["veto_id"], "effective_at": veto["delay_until"]}))
        log_path = veto_log_path(repository)
        append_jsonl(log_path, updates)
        events_file = event_path(repository)
        append_jsonl(events_file, stamp_events(repository, events))
        commit = commit_paths(repository, [log_path.relative_to(repository), events_file.relative_to(repository)], f"Settle {len(updates)} expired veto(s)", push=push)
        return {"settled": len(updates), "commit": commit}
    finally:
        lock_path.unlink(missing_ok=True)


def list_vetoes(repository):
    ensure_repository(repository)
    return sorted(latest_vetoes(repository).values(), key=lambda item: item.get("proposed_at", ""))
