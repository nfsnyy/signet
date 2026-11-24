import re
from pathlib import Path

from ..domain import event_path, make_event, task_path, validate_actor_alias
from ..errors import NotFoundError, PlatformError
from ..gitrepo import commit_paths, ensure_repository
from ..jsonio import append_jsonl, utc_now
from ..readmodel import parse_requirement_text, requirement_coverage
from .packages import stamp_events


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
    for key in ("requirement_id", "title", "status", "priority", "owner", "source", "created_at", "updated_at"):
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


REQ_SOURCES = ("走查反馈", "backlog 归并", "讨论沉淀", "外部输入")
DEFAULT_REQ_SOURCE = "讨论沉淀"


def _validate_req_source(source):
    """来源枚举校验（AC-05）：非枚举值一律 requirement.invalid_source（零副作用）。"""
    value = (source or "").strip()
    if value not in REQ_SOURCES:
        raise PlatformError(
            "requirement.invalid_source",
            f"source must be one of {list(REQ_SOURCES)}",
            details={"source": source},
        )
    return value


def create_requirement(repository, requirement_id, title, body, entries, actor, on_behalf_of, source=None):
    repository = ensure_repository(repository)
    validate_actor_alias(requirement_id)
    title = (title or "").strip()
    if not title:
        raise PlatformError("requirement.title_required", "title is required and must be non-empty")
    cleaned_entries = _validate_entries(entries)
    cleaned_source = _validate_req_source(source) if source is not None else DEFAULT_REQ_SOURCE
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
        "source": cleaned_source,
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
        payload={"requirement_id": requirement_id, "title": title, "status": "draft", "source": cleaned_source},
        entrypoint="agent",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Create requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": "draft", "entries": [item["id"] for item in cleaned_entries], "commit": commit}


def update_requirement(repository, requirement_id, body, entries, actor, on_behalf_of, source=None):
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
    changed_fields = ["body", "entries"]
    if source is not None:
        fields["source"] = _validate_req_source(source)
        changed_fields.append("source")
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
        payload={"requirement_id": requirement_id, "fields": changed_fields},
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


def closeout_requirement(repository, requirement_id, actor):
    """需求收口（AC-04）：status=confirmed 且条目覆盖率 100% 时翻 closed。

    覆盖率不足 → requirement.not_closable（零副作用：不改状态/不落事件/不 commit）；
    非 confirmed（含已 closed）→ requirement.invalid_state。成功落恰一个
    requirement.closed 事件（payload 逐条清单 + 确认人，归因 actor=on_behalf_of=登录人）。
    """
    repository = ensure_repository(repository)
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    fields, body, entries = requirement
    if fields.get("status") != "confirmed":
        raise PlatformError(
            "requirement.invalid_state",
            f"only a confirmed requirement can be closed out: {requirement_id}",
            details={"requirement_id": requirement_id, "status": fields.get("status")},
        )
    covered, total = requirement_coverage(repository, requirement_id)
    if covered < total:
        raise PlatformError(
            "requirement.not_closable",
            f"requirement coverage {covered}/{total} is below 100%",
            details={"requirement_id": requirement_id, "covered": covered, "total": total},
        )
    entry_ids = [item["id"] for item in entries]
    fields["status"] = "closed"
    fields["updated_at"] = utc_now()
    target = requirement_file_path(repository, requirement_id)
    target.write_text(_compose_requirement_text(fields, body, entries), encoding="utf-8", newline="\n")
    event = make_event(
        "requirement.closed",
        None,
        actor,
        actor,
        "confirmed",
        "closed",
        payload={"requirement_id": requirement_id, "entries": entry_ids, "confirmed_by": actor},
        entrypoint="operator",
    )
    commit = _commit_requirement_change(repository, requirement_id, [event], f"Closeout requirement {requirement_id}")
    return {"requirement_id": requirement_id, "status": "closed", "entries": entry_ids, "commit": commit}


def merge_backlog_item(repository, requirement_id, backlog_task_id, entry_id, actor):
    """backlog 归并/立项留痕（AC-06）：落 requirement.merged 事件并 git commit。

    backlog_task_id 须为现存任务（404 task.not_found）；entry_id 须命中本需求
    「判据条目」节现存条目（400 requirement.entry_not_found）。事件挂在 backlog
    任务时间线上（task_id=backlog_task_id），payload 四字段留痕。
    """
    repository = ensure_repository(repository)
    requirement = read_requirement(repository, requirement_id)
    if requirement is None:
        raise NotFoundError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    _fields, _body, entries = requirement
    backlog_task_id = (backlog_task_id or "").strip()
    if not task_path(repository, backlog_task_id).is_file():
        raise NotFoundError("task.not_found", f"task does not exist: {backlog_task_id}")
    entry_id = (entry_id or "").strip()
    if entry_id not in {item["id"] for item in entries}:
        raise PlatformError(
            "requirement.entry_not_found",
            f"requirement entry does not exist: {entry_id}",
            details={"requirement_id": requirement_id, "entry_id": entry_id},
        )
    event = make_event(
        "requirement.merged",
        backlog_task_id,
        actor,
        actor,
        None,
        None,
        payload={
            "requirement_id": requirement_id,
            "entry_id": entry_id,
            "backlog_task_id": backlog_task_id,
            "actor": actor,
        },
        entrypoint="operator",
    )
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, [event]))
    commit = commit_paths(repository, [events_file.relative_to(repository)], f"Merge backlog into {requirement_id}")
    return {"requirement_id": requirement_id, "entry_id": entry_id, "backlog_task_id": backlog_task_id, "commit": commit}
