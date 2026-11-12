"""project 与需求只读面：服务端读模型（仅标准库）。

以状态仓为唯一事实源直读：tasks/*/task.md 反查、requirements/*/requirement.md
展原稿（v1 不做 markdown 渲染）、events/events.jsonl 时间线、git HEAD 作
commit 标识。统计口径与 T3 验收 fixture（contract/t3_common.py）逐字节对齐：
任务目录存在即计数，frontmatter 缺失或损坏时状态记 "?"；progress 为
Done /（反查任务总数 - Void），分母为 0 时取 0。
"""

import json
import re
from pathlib import Path

from .domain import spec_path
from .errors import PlatformError
from .gitrepo import current_head
from .taskfile import read_task

PENDING_VERIFICATION_STATE = "Implemented"
RECENT_EVENTS_LIMIT = 20
RE_ENTRY_SPLIT = re.compile(r"[：:\s]")


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


def task_titles(tasks):
    """task_id → title 映射：事件流/时间线的人读任务引用（title 缺省为 None，渲染层回落短码）。"""
    return {record["task_id"]: record.get("title") for record in tasks}


def task_events(repository, task_id, limit=200):
    """单任务事件时间线（新在前）；先全量过滤再截断，避免全局截断吞掉旧任务事件。"""
    events = [event for event in recent_events(repository, limit=100000)
              if event.get("task_id") == task_id]
    return events[:limit]


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
        "task_titles": task_titles(tasks),
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


def _requirement_entry_items(body):
    """正文「判据条目」节的条目 [{id, text}]（- REQ-<n>：…；节外条目不计）。"""
    items = []
    in_section = False
    for line in str(body or "").splitlines():
        if line.startswith("## "):
            in_section = "判据条目" in line
            continue
        if in_section and line.startswith("- "):
            token = line[2:].strip()
            entry_id = RE_ENTRY_SPLIT.split(token, maxsplit=1)[0].strip()
            if entry_id:
                text = token[len(entry_id):].lstrip("：: ").strip()
                items.append({"id": entry_id, "text": text})
    return items


def requirement_carrier_index(repository, tasks=None):
    """条目反查索引（AC-02/AC-03）：{requirement_id: {entry_id: [carrier…]}}。

    carrier = {"task_id", "ac_ids", "_state", "_created_at"}；承载判定为两来源
    并集——任务 metadata.req_items 含该条目，或其钉扎验收卡的 reqs 锚定该
    条目（未钉扎任务 ac_ids 为 []，T9 前旧任务经卡锚点自动纳入反查）。
    排序键 (created_at, task_id)：创建时序为主、task_id 升序作并列回退——
    uuid4 随机 id 下纯 task_id 序不确定，创建时序给出稳定序。
    """
    if tasks is None:
        tasks = list_tasks(repository)
    index = {}
    for record in tasks:
        requirement_id = record.get("requirement")
        if not requirement_id:
            continue
        task_id = record["task_id"]
        per_requirement = index.setdefault(requirement_id, {})
        ac_ids_by_entry = {}
        card = pinned_acceptance_card(repository, record)
        if card:
            for criterion in card.get("criteria") or []:
                criterion_id = str(criterion.get("criterion_id") or "")
                for anchor in criterion.get("reqs") or []:
                    bucket = ac_ids_by_entry.setdefault(str(anchor), [])
                    if criterion_id:
                        bucket.append(criterion_id)
        entry_ids = {str(item) for item in (record.get("req_items") or [])}
        entry_ids.update(ac_ids_by_entry)
        for entry_id in entry_ids:
            per_requirement.setdefault(entry_id, []).append({
                "task_id": task_id,
                "ac_ids": list(ac_ids_by_entry.get(entry_id, [])),
                "_state": record.get("state"),
                "_created_at": record.get("created_at") or "",
            })
    for per_requirement in index.values():
        for carriers in per_requirement.values():
            carriers.sort(key=lambda carrier: (carrier["_created_at"], carrier["task_id"]))
    return index


def effective_entry_carriers(carriers):
    """覆盖判定口径（AC-03）：Void 承载任务不作数，不计入有效集合。"""
    return [carrier for carrier in (carriers or []) if carrier.get("_state") != "Void"]


def entry_is_covered(carriers):
    """条目已覆盖 = 存在非 Void 承载任务且其全部 state=Done。"""
    effective = effective_entry_carriers(carriers)
    return bool(effective) and all(carrier.get("_state") == "Done" for carrier in effective)


def derive_entry_status(requirement_status, carriers):
    """条目状态推导徽标（AC-03）：草案 / 已确认 / 已派生 / 已覆盖。

    需求 confirmed（及终态 closed，展示口径视同已确认）时按承载任务推导；
    其余需求状态（draft/submitted）一律「草案」。
    """
    if requirement_status not in ("confirmed", "closed"):
        return "草案"
    effective = effective_entry_carriers(carriers)
    if not effective:
        return "已确认"
    if all(carrier.get("_state") == "Done" for carrier in effective):
        return "已覆盖"
    return "已派生"


def requirement_coverage(repository, requirement_id):
    """(已覆盖条目数, 条目总数)；需求文件缺失/损坏按 (0, 0) 计。"""
    repository = Path(repository)
    file_path = repository / "requirements" / requirement_id / "requirement.md"
    if not file_path.is_file():
        return (0, 0)
    try:
        _fields, body = parse_requirement_text(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (0, 0)
    entry_items = _requirement_entry_items(body)
    carriers_by_entry = requirement_carrier_index(repository).get(requirement_id, {})
    covered = sum(1 for item in entry_items if entry_is_covered(carriers_by_entry.get(item["id"], [])))
    return (covered, len(entry_items))


def requirement_merges(repository, requirement_id):
    """backlog 归并留痕（AC-06）：{entry_id: [backlog_task_id…]}，按事件时间线升序。"""
    merges = {}
    for event in reversed(recent_events(repository, limit=100000)):
        if event.get("event_type") != "requirement.merged":
            continue
        payload = event.get("payload") or {}
        if str(payload.get("requirement_id")) != str(requirement_id):
            continue
        entry_id = str(payload.get("entry_id") or "")
        if entry_id:
            merges.setdefault(entry_id, []).append(str(payload.get("backlog_task_id") or ""))
    return merges


def list_requirements(repository):
    """GET /api/requirements 的 result.requirements 负载（按 requirement_id 排序）。

    进度口径（AC-03）= 已覆盖条目数 / 条目总数（条目总数为 0 时 progress=0）；
    旧「Done 任务数 / 非 Void 任务总数」口径已退场。
    """
    repository = Path(repository)
    tasks = list_tasks(repository)
    index = requirement_carrier_index(repository, tasks)
    entries = []
    requirements_dir = repository / "requirements"
    if requirements_dir.is_dir():
        for entry in sorted(requirements_dir.iterdir()):
            file_path = entry / "requirement.md"
            if not entry.is_dir() or not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
                fields, body = parse_requirement_text(text)
            except (OSError, ValueError):
                continue
            entries.append(_requirement_entry(entry.name, fields, body, tasks, index))
    return entries


def _requirement_entry(requirement_id, fields, body, tasks, index):
    entry_items = _requirement_entry_items(body)
    carriers_by_entry = (index.get(requirement_id) or {})
    covered = sum(1 for item in entry_items if entry_is_covered(carriers_by_entry.get(item["id"], [])))
    return {
        "requirement_id": requirement_id,
        "title": fields.get("title"),
        "status": fields.get("status"),
        "priority": fields.get("priority"),
        "owner": fields.get("owner"),
        "source": fields.get("source"),
        "progress": (covered / len(entry_items)) if entry_items else 0,
        "task_count": len(requirement_task_ids(tasks, requirement_id)),
    }


def requirement_detail(repository, requirement_id):
    """GET /api/requirements/{id} 的 result 负载；不存在时返回 None。

    负载含 metadata（title/status/priority/owner/source/progress）、正文原稿
    （body）、判据条目（entries，编号列表）、条目反查（entries_trace：每
    条目的承载任务与 AC 编号对应 + 推导状态徽标口径）、backlog 归并留痕
    （merges）与反查任务（tasks）。
    """
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
    index = requirement_carrier_index(repository, tasks)
    entry_items = _requirement_entry_items(body)
    carriers_by_entry = index.get(requirement_id) or {}
    entries_trace = [
        {
            "id": item["id"],
            "text": item["text"],
            "status": derive_entry_status(fields.get("status"), carriers_by_entry.get(item["id"], [])),
            "tasks": [
                {"task_id": carrier["task_id"], "ac_ids": list(carrier["ac_ids"])}
                for carrier in carriers_by_entry.get(item["id"], [])
            ],
        }
        for item in entry_items
    ]
    covered = sum(1 for item in entry_items if entry_is_covered(carriers_by_entry.get(item["id"], [])))
    return {
        "requirement_id": requirement_id,
        "title": fields.get("title"),
        "status": fields.get("status"),
        "priority": fields.get("priority"),
        "owner": fields.get("owner"),
        "source": fields.get("source"),
        "progress": (covered / len(entry_items)) if entry_items else 0,
        "body": body,
        "entries": [item["id"] for item in entry_items],
        "entries_trace": entries_trace,
        "merges": requirement_merges(repository, requirement_id),
        "tasks": requirement_task_ids(tasks, requirement_id),
    }


def _requirement_entries(body):
    """正文「判据条目」节的条目编号列表（- REQ-<n>：…；节外不计）。"""
    entries = []
    in_section = False
    for line in str(body or "").splitlines():
        if line.startswith("## "):
            in_section = "判据条目" in line
            continue
        if in_section and line.startswith("- "):
            token = line[2:].strip()
            entry_id = RE_ENTRY_SPLIT.split(token, maxsplit=1)[0].strip()
            if entry_id:
                entries.append(entry_id)
    return entries


def task_board(repository):
    """GET /api/tasks 看板负载：tasks/ 下每条任务元数据（含 spec_pin 版本与租约）。

    字段口径与 AC-01 一致：task_id/title/state/requirement/spec_pin（含 version）/
    lease（claimed_by 与 expires_at，无租约为 null）/updated_at；列表数与
    tasks/ 目录数一致（与 list_tasks 同一遍历口径）。spec_pin 额外带顶层
    version（= 钉扎验收卡版本），便于看板消费方直接取版本号。
    """
    records = []
    for record in list_tasks(repository):
        pin = record.get("spec_pin")
        if isinstance(pin, dict) and "version" not in pin:
            version = (pin.get("acceptance_card") or {}).get("version")
            if version is None:
                for key in ("contract", "mapping"):
                    version = (pin.get(key) or {}).get("version")
                    if version is not None:
                        break
            if version is not None:
                record["spec_pin"] = {**pin, "version": version}
        records.append(record)
    return records


def pinned_acceptance_card(repository, metadata):
    """读钉扎验收卡全文（specs/{spec_id}/{version}/acceptance-card.json）。

    未钉扎或读取/解析失败时返回 None。
    """
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict) or not metadata.get("spec_id"):
        return None
    try:
        path = spec_path(repository, metadata, "acceptance_card")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def task_detail(repository, task_id):
    """GET /api/tasks/{id} 的 result 负载；任务不存在时返回 None。

    负载含 metadata（title/state/owner/requirement/spec_pin）、task.md 正文
    （body）、钉扎验收卡全文（card，未钉扎为 None）、事件时间线（events，
    本任务事件新在前）与 task_titles（task_id → title，时间线人读引用）。
    """
    repository = Path(repository)
    path = repository / "tasks" / task_id / "task.md"
    if not path.is_file():
        return None
    try:
        metadata, body = read_task(path)
    except PlatformError:
        return None
    detail = dict(metadata)
    detail["task_id"] = task_id
    detail["body"] = body
    detail["card"] = pinned_acceptance_card(repository, metadata)
    detail["events"] = task_events(repository, task_id)
    detail["task_titles"] = task_titles(list_tasks(repository))
    return detail
