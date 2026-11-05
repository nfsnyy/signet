import json
from pathlib import Path

from .errors import PlatformError
from .jsonio import new_id, read_json, read_jsonl, utc_now, write_json
from .domain import event_path, make_event, task_path
from .taskfile import read_task

VALID_TYPES = {"in-spec-request", "definition-doubt", "new-scope"}
ANSWER_ACTIONS = {"accept", "reject", "counter", "escalate", "close"}
TERMINAL_STATUSES = {"accepted", "rejected", "escalated", "closed"}
ACTION_STATUS = {"accept": "accepted", "reject": "rejected", "escalate": "escalated", "close": "closed"}
ALLOWED_ACTIONS = {
    "in-spec-request": {"accept", "reject", "counter", "escalate", "close"},
    "definition-doubt": {"escalate", "close"},
    "new-scope": {"close"},
}


def discussions_directory(repository):
    return Path(repository) / "discussions"


def message_archive_path(repository, message_id):
    return discussions_directory(repository) / f"{message_id}.json"


def load_message(path, code):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError(code, f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise PlatformError(code, f"collaboration message must be an object: {path}")
    return value


def _required_string(document, key, code):
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlatformError(code, f"{key} must be a non-empty string")
    return value


def validate_new_message(document, code="collab.invalid_message"):
    message_id = _required_string(document, "message_id", code)
    message_type = _required_string(document, "type", code)
    if message_type not in VALID_TYPES:
        raise PlatformError(code, f"unknown collaboration message type: {message_type}")
    _required_string(document, "from_task", code)
    _required_string(document, "to_task", code)
    _required_string(document, "created_by", code)
    _required_string(document, "summary", code)
    _required_string(document, "details", code)
    if document.get("status") != "open":
        raise PlatformError(code, "a new collaboration message must have status open")
    history = document.get("history")
    if not isinstance(history, list) or len(history) != 1:
        raise PlatformError(code, "a new collaboration message must have exactly one history entry")
    _required_string(document, "awaiting", code)
    if not isinstance(document.get("round"), int) or document["round"] != 0:
        raise PlatformError(code, "a new collaboration message must start at round 0")
    return message_id


def validate_message_update(incoming, archived, code="collab.invalid_update"):
    incoming_history = incoming.get("history")
    archived_history = archived.get("history") or []
    if not isinstance(incoming_history, list) or len(incoming_history) <= len(archived_history):
        raise PlatformError(code, "message update does not extend the history")
    if incoming_history[: len(archived_history)] != archived_history:
        raise PlatformError(code, "message history must extend the archived history")
    latest = incoming_history[-1]
    if not isinstance(latest, dict) or latest.get("action") not in ANSWER_ACTIONS:
        raise PlatformError(code, "latest history entry must be a valid answer action")
    status = incoming.get("status")
    if latest["action"] == "counter":
        if status != "open":
            raise PlatformError(code, "a countered message must remain open")
    elif ACTION_STATUS.get(latest["action"]) != status:
        raise PlatformError(code, "message status does not match the latest answer action")
    return latest


def collab_send(workspace, actor, on_behalf_of, message_type, to_task, summary, details):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    from_task = metadata["task_id"]
    if message_type not in VALID_TYPES:
        raise PlatformError("collab.invalid_type", f"type must be one of {sorted(VALID_TYPES)}")
    if not to_task or to_task == from_task:
        raise PlatformError("collab.invalid_recipient", "to-task must name another task")
    if metadata.get("state") in {"Void", "Done"}:
        raise PlatformError("collab.invalid_state", "a final task cannot send collaboration messages", details={"state": metadata.get("state")})
    if message_type == "definition-doubt" and metadata.get("state") != "Blocked":
        raise PlatformError("collab.not_blocked", "definition-doubt requires the sender task to be Blocked first")
    if not summary or not summary.strip() or not details or not details.strip():
        raise PlatformError("collab.details_required", "summary and details are required")
    now = utc_now()
    message = {
        "message_id": new_id("msg"),
        "type": message_type,
        "from_task": from_task,
        "to_task": to_task,
        "created_at": now,
        "created_by": actor,
        "on_behalf_of": on_behalf_of,
        "summary": summary,
        "details": details,
        "status": "open",
        "round": 0,
        "awaiting": to_task,
        "history": [{"at": now, "by": actor, "action": "sent", "note": summary}],
    }
    write_json(workspace / "outbox-collab" / f"{message['message_id']}.json", message)
    return {"message_id": message["message_id"], "type": message_type, "status": "open", "authoritative": False}


def collab_answer(workspace, actor, on_behalf_of, message_id, action, note):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    own_task = metadata["task_id"]
    inbox_path = workspace / "inbox" / f"{message_id}.json"
    if not inbox_path.is_file():
        raise PlatformError("collab.not_found", f"collaboration message does not exist in inbox: {message_id}")
    message = load_message(inbox_path, "collab.invalid_message")
    if own_task not in {message.get("from_task"), message.get("to_task")}:
        raise PlatformError("collab.not_addressed", "message does not involve this task")
    if message.get("status") != "open":
        raise PlatformError("collab.invalid_state", "message is already closed", details={"status": message.get("status")})
    if message.get("awaiting") != own_task:
        raise PlatformError("collab.not_your_turn", "another task is expected to answer this message", details={"awaiting": message.get("awaiting")})
    message_type = message.get("type")
    if action not in ALLOWED_ACTIONS.get(message_type, set()):
        raise PlatformError("collab.invalid_action", f"{message_type} messages cannot be answered with {action}", details={"allowed": sorted(ALLOWED_ACTIONS.get(message_type, set()))})
    if action == "counter" and message.get("round", 0) >= 2:
        raise PlatformError("collab.round_limit", "negotiation exceeded two rounds; escalate instead")
    if action != "close" and (not note or not note.strip()):
        raise PlatformError("collab.note_required", f"{action} requires a note")
    now = utc_now()
    message["round"] = message.get("round", 0) + 1
    message["awaiting"] = message["from_task"] if own_task == message["to_task"] else message["to_task"]
    if action in ACTION_STATUS:
        message["status"] = ACTION_STATUS[action]
    message["history"].append({"at": now, "by": actor, "action": action, "note": note or ""})
    write_json(inbox_path, message)
    write_json(workspace / "outbox-collab" / f"{message_id}.json", message)
    return {"message_id": message_id, "type": message_type, "status": message["status"], "round": message["round"], "authoritative": False}


def _load_archive(repository):
    directory = discussions_directory(repository)
    archive = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                archive[path.stem] = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
    return archive


def process_collaboration_sync(repository, workspaces):
    repository = Path(repository)
    new_events = []
    results = []
    changed_paths = []
    archive = _load_archive(repository)
    for workspace in workspaces:
        outbox = Path(workspace).resolve() / "outbox-collab"
        if not outbox.is_dir():
            continue
        for path in sorted(outbox.glob("*.json")):
            try:
                document = load_message(path, "collab.invalid_message")
            except PlatformError as error:
                results.append({"message_id": path.stem, "accepted": False, "code": error.code})
                continue
            message_id = document.get("message_id") or path.stem
            current = archive.get(message_id)
            if current is None:
                try:
                    validate_new_message(document)
                except PlatformError as error:
                    new_events.append(make_event("discussion.rejected", document.get("from_task") if isinstance(document.get("from_task"), str) else None, "operator", document.get("created_by"), None, None, payload={"message_id": message_id, "code": error.code, "reason": error.message}))
                    results.append({"message_id": message_id, "accepted": False, "code": error.code})
                    continue
                from_exists = task_path(repository, document["from_task"]).is_file()
                to_exists = task_path(repository, document["to_task"]).is_file()
                if not from_exists or not to_exists:
                    new_events.append(make_event("discussion.rejected", document["from_task"], "operator", document.get("created_by"), None, None, payload={"message_id": message_id, "code": "task.not_found", "reason": "message endpoint task does not exist in this repository"}))
                    results.append({"message_id": message_id, "accepted": False, "code": "task.not_found"})
                    continue
                archive[message_id] = document
                target = message_archive_path(repository, message_id)
                write_json(target, document)
                changed_paths.append(target.relative_to(repository))
                payload = {"message": document}
                new_events.append(make_event("discussion.opened", document["from_task"], document["created_by"], document.get("on_behalf_of"), None, None, payload=payload, entrypoint="agent"))
                new_events.append(make_event("discussion.received", document["to_task"], document["created_by"], document.get("on_behalf_of"), None, None, payload=payload, entrypoint="agent"))
                results.append({"message_id": message_id, "accepted": True, "status": "open"})
            else:
                try:
                    latest = validate_message_update(document, current)
                except PlatformError as error:
                    results.append({"message_id": message_id, "accepted": False, "code": error.code, "duplicate": True})
                    continue
                archive[message_id] = document
                target = message_archive_path(repository, message_id)
                write_json(target, document)
                changed_paths.append(target.relative_to(repository))
                payload = {"message_id": message_id, "action": latest["action"], "status": document["status"], "round": document["round"], "note": latest.get("note", "")}
                new_events.append(make_event("discussion.answered", document["to_task"], latest.get("by"), document.get("on_behalf_of"), None, None, payload=payload, entrypoint="agent"))
                new_events.append(make_event("discussion.updated", document["from_task"], latest.get("by"), document.get("on_behalf_of"), None, None, payload=payload, entrypoint="agent"))
                results.append({"message_id": message_id, "accepted": True, "status": document["status"], "round": document["round"]})
    return new_events, changed_paths, results


def collaboration_overview(repository, task_id=None):
    archive = _load_archive(repository)
    messages = sorted(archive.values(), key=lambda item: item.get("created_at", ""))
    if task_id:
        messages = [message for message in messages if task_id in {message.get("from_task"), message.get("to_task")}]
    return {"messages": messages}


def validate_report_collaboration(repository, task_id, report):
    requests = report.get("collaboration_requests") or []
    for item in requests:
        if not isinstance(item, dict) or not isinstance(item.get("message_id"), str) or not item["message_id"].strip():
            raise PlatformError("submit.invalid_collaboration", "each collaboration_requests item must contain a message_id")
        archive_path = message_archive_path(repository, item["message_id"])
        if not archive_path.is_file():
            raise PlatformError("submit.collaboration_unknown", f"unknown collaboration message: {item['message_id']}")
        message = read_json(archive_path)
        if task_id not in {message.get("from_task"), message.get("to_task")}:
            raise PlatformError("submit.collaboration_unrelated", f"collaboration message does not involve this task: {item['message_id']}")
