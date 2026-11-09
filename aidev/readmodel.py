"""project 与需求只读面：服务端读模型（仅标准库）。

以状态仓为唯一事实源直读：tasks/*/task.md 反查、requirements/*/requirement.md
展原稿（v1 不做 markdown 渲染）、events/events.jsonl 时间线、git HEAD 作
commit 标识。统计口径与 T3 验收 fixture（contract/t3_common.py）逐字节对齐：
任务目录存在即计数，frontmatter 缺失或损坏时状态记 "?"；progress 为
Done /（反查任务总数 - Void），分母为 0 时取 0。
"""

import json
from pathlib import Path

from .gitrepo import current_head

PENDING_VERIFICATION_STATE = "Implemented"
RECENT_EVENTS_LIMIT = 20


def list_tasks(repository):
    """tasks/ 下每个含 task.md 的目录一条记录，按 task_id 排序。"""
    tasks = []
    tasks_dir = Path(repository) / "tasks"
    if not tasks_dir.is_dir():
        return tasks
    for entry in sorted(tasks_dir.iterdir()):
        if entry.is_dir() and (entry / "task.md").is_file():
            metadata = _task_metadata(entry)
            record = {"task_id": entry.name}
            record.update(metadata)
            record["state"] = record.get("state") or "?"
            tasks.append(record)
    return tasks


def _task_metadata(task_dir):
    """容错解析 task.md 的 JSON frontmatter；缺失/损坏时返回空 dict。"""
    try:
        text = (task_dir / "task.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        metadata = json.loads("\n".join(lines[1:end]))
    except (StopIteration, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def task_counts(tasks):
    """各状态任务数；各状态之和恒等于任务目录数。"""
    counts = {}
    for record in tasks:
        state = record.get("state") or "?"
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def pending_verification(tasks):
    """待验收置顶列表：状态为 Implemented（已提交、待 operator 验收）的任务。"""
    pending = [
        {
            "task_id": record["task_id"],
            "title": record.get("title"),
            "owner": record.get("owner"),
            "updated_at": record.get("updated_at"),
        }
        for record in tasks
        if record.get("state") == PENDING_VERIFICATION_STATE
    ]
    pending.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return pending


def recent_events(repository, limit=RECENT_EVENTS_LIMIT):
    """事件时间线，新的在前。events.jsonl 缺失或单行损坏时跳过该行。"""
    path = Path(repository) / "events" / "events.jsonl"
    events = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    events.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return events[:limit]


def project_snapshot(repository):
    """GET /api/project 的 result 负载。"""
    repository = Path(repository)
    tasks = list_tasks(repository)
    return {
        "repo": str(repository),
        "project": _project_metadata(repository),
        "task_counts": task_counts(tasks),
        "task_total": len(tasks),
        "pending_verification": pending_verification(tasks),
        "recent_events": recent_events(repository),
        "commit": current_head(repository),
    }


def _project_metadata(repository):
    path = Path(repository) / "project.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_requirement_text(text):
    """解析 requirement.md：frontmatter 字段 + 去块后原稿正文。

    正文口径与验收逐字节一致：自原始文本中首个 "\\n---\\n"（从索引 4 起找）
    之后的内容原样保留（含换行），不做渲染、不增删字符。
    """
    marker = "\n---\n"
    end_index = text.find(marker, 4)
    if not text.startswith("---\n") or end_index < 0:
        raise ValueError("requirement.md must start with a closed YAML frontmatter block")
    fields = {}
    for line in text[4:end_index].splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[end_index + len(marker):]


def requirement_task_ids(tasks, requirement_id):
    """反查 requirement 字段等于该 id 的任务集合（升序 task_id）。"""
    return sorted(
        record["task_id"]
        for record in tasks
        if record.get("requirement") == requirement_id
    )


def requirement_progress(tasks, requirement_id):
    """progress = Done 数 /（反查任务总数 - Void 数），分母为 0 时取 0。"""
    matched = [record for record in tasks if record.get("requirement") == requirement_id]
    done = sum(1 for record in matched if record.get("state") == "Done")
    denominator = sum(1 for record in matched if record.get("state") != "Void")
    return (done / denominator) if denominator else 0


def list_requirements(repository):
    """GET /api/requirements 的 result.requirements 负载（按 requirement_id 排序）。"""
    repository = Path(repository)
    tasks = list_tasks(repository)
    entries = []
    requirements_dir = repository / "requirements"
    if requirements_dir.is_dir():
        for entry in sorted(requirements_dir.iterdir()):
            file_path = entry / "requirement.md"
            if not entry.is_dir() or not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
                fields, _ = parse_requirement_text(text)
            except (OSError, ValueError):
                continue
            entries.append(_requirement_entry(entry.name, fields, tasks))
    return entries


def _requirement_entry(requirement_id, fields, tasks):
    return {
        "requirement_id": requirement_id,
        "title": fields.get("title"),
        "status": fields.get("status"),
        "priority": fields.get("priority"),
        "owner": fields.get("owner"),
        "progress": requirement_progress(tasks, requirement_id),
        "task_count": len(requirement_task_ids(tasks, requirement_id)),
    }


def requirement_detail(repository, requirement_id):
    """GET /api/requirements/{id} 的 result 负载；不存在时返回 None。"""
    repository = Path(repository)
    file_path = repository / "requirements" / requirement_id / "requirement.md"
    if not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        fields, body = parse_requirement_text(text)
    except ValueError:
        return None
    tasks = list_tasks(repository)
    return {
        "requirement_id": requirement_id,
        "title": fields.get("title"),
        "status": fields.get("status"),
        "priority": fields.get("priority"),
        "owner": fields.get("owner"),
        "progress": requirement_progress(tasks, requirement_id),
        "body": body,
        "tasks": requirement_task_ids(tasks, requirement_id),
    }
