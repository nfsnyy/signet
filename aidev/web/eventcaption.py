"""事件人读渲染层（T7 / page-human-readable REQ-1、REQ-2）。

describe_event：把一条账本事件映射为一句中文人话——谁 + 做了什么 + 对哪个任务
+ payload 关键字段；输出不得包含裸 event_type 串与 36 位 UUID（任务一律以
「#短码 + 标题」引用，短码 = task_id 去 task- 前缀取前 8 位）。

覆盖集以 contract/events_fixture.py 的 FIXTURE_EVENTS 为准；覆盖集之外的事件
类型走兜底句式，不得退化为裸 event_type。本模块只产文本与结构化行，HTML 转义
与链接拼装由 pages 负责。
"""

import re

EVENT_TYPE_COVERAGE = 22  # 覆盖集规模（夹具 FIXTURE_EVENTS 条数）

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def _mask_uuids(text):
    """payload 里的 36 位 UUID（如 spec_id）一律压成前 8 位 + 省略号（REQ-2）。"""
    return _UUID_RE.sub(lambda match: match.group(0)[:8] + "…", text)


def task_shortcode(task_id):
    """task-5186ad73-… → #5186ad73；缺 task- 前缀时直接取前 8 位。"""
    text = str(task_id or "")
    body = text[len("task-"):] if text.startswith("task-") else text
    return "#" + body[:8]


def _date_only(iso):
    """2026-11-11T06:46:21Z → 11-11（租约到期等短日期）。"""
    text = str(iso or "")
    return text[5:10] if len(text) >= 10 and text[4] == "-" else text


def _actor_text(event):
    """caption 主语：actor ≠ on_behalf_of 时按 REQ-1 追加「代表 X」。"""
    actor = str(event.get("actor") or "-")
    obo = str(event.get("on_behalf_of") or "")
    if obo and obo != actor:
        return f"{actor}（代表 {obo}）"
    return actor


def _task_ref(event, title):
    task_id = str(event.get("task_id") or "")
    if not task_id:
        return ""
    short = task_shortcode(task_id)
    return f"{short} {title}" if title else short


def describe_event(event, title=None):
    """单条事件的人读句式（纯文本，不含 HTML）。"""
    actor = _actor_text(event)
    ref = _task_ref(event, title)
    target = f" {ref}" if ref else ""
    payload = event.get("payload") or {}
    etype = event.get("event_type")

    if etype == "task.initialized":
        return f"{actor} 创建了任务{target}".strip()
    if etype == "repository.initialized":
        project = payload.get("project") or "-"
        return f"{actor} 初始化项目「{project}」"
    if etype == "specification.frozen":
        return f"{actor} 冻结规格 v{payload.get('version')}，任务进入 Ready"
    if etype == "specification.content_confirmed":
        text = f"{actor} 完成规格 v{payload.get('version')} 内容确认"
        sha = payload.get("sha256")
        return f"{text}（sha256 {sha[:8]}）" if sha else text
    if etype == "specification.consistency_confirmed":
        return (f"{actor} 完成规格 v{payload.get('version')} 一致性确认"
                f"（契约 {str(payload.get('contract_sha256') or '-')[:8]}"
                f" / 映射 {str(payload.get('mapping_sha256') or '-')[:8]}）")
    if etype == "specification.refrozen":
        return (f"{actor} 重冻规格 v{payload.get('old_version')} → v{payload.get('version')}"
                f"（{payload.get('reason') or '无理由'}）")
    if etype == "task.ready":
        return f"{actor} 确认规格就绪，任务进入 Ready（规格 v{payload.get('version')}）"
    if etype == "task.claimed":
        lease = payload.get("lease") or {}
        text = f"{actor} 领取了任务{target}"
        if lease.get("expires_at"):
            text += f"，租约至 {_date_only(lease['expires_at'])}"
        return text
    if etype == "lease.renewed":
        return f"{actor} 续租至 {_date_only(payload.get('lease', {}).get('expires_at'))}"
    if etype == "lease.expired":
        lease = payload.get("lease") or {}
        return f"{actor} 的租约到期（认领人 {lease.get('claimed_by') or '-'}）"
    if etype == "task.submitted":
        report = payload.get("report") or {}
        excerpt = str(report.get("implemented") or "").strip()
        if len(excerpt) > 50:
            excerpt = excerpt[:50] + "…"
        evidence = payload.get("evidence") or []
        text = f"{actor} 提交验收"
        if excerpt:
            text += f"：{excerpt}"
        text += f"（证据 {len(evidence)} 件）"
        return text
    if etype == "verification.initial":
        results = (payload.get("results") or {}).get("results") or []
        passed = sum(1 for item in results if item.get("passed") is True)
        fingerprint = str(payload.get("workspace_fingerprint") or "")
        text = f"{actor} 执行机器验收，{passed} 项判据通过"
        if fingerprint:
            text += f"，指纹 {fingerprint[:8]}"
        return text
    if etype == "verification.confirm":
        return f"{actor} 确认机器验收结果，任务验收通过"
    if etype == "task.verified":
        return f"{actor} 确认任务验收通过"
    if etype == "task.closed":
        return f"{actor} 关闭任务（决定 {payload.get('decision') or '-'}）"
    if etype == "task.shipped":
        commit = str(payload.get("commit_hash") or "")
        return f"{actor} 关闭并入库，commit {commit[:7]}（{payload.get('via') or 'ship'}）"
    if etype == "task.rejected":
        return f"{actor} 驳回任务（{payload.get('reason') or '无理由'}）"
    if etype == "task.void":
        return f"{actor} 作废任务（{payload.get('reason') or '无理由'}）"
    if etype == "task.blocked":
        return f"{actor} 阻塞任务：{payload.get('reason') or '无理由'}"
    if etype == "proposal.rejected":
        return (f"{actor} 的提案 {payload.get('operation') or '-'} 被驳回"
                f"（{payload.get('code') or '-'}：{payload.get('reason') or '无理由'}）")
    if etype == "enhancement.recorded":
        return f"{actor} 登记改进点「{payload.get('title') or '-'}」"
    if etype == "discussion.opened":
        return f"{actor} 发起讨论「{payload.get('topic') or '-'}」：{payload.get('reason') or '-'}"

    # 兜底：覆盖集之外的新事件类型——人读句，绝不退化为裸 event_type。
    if ref:
        return f"{actor} 对 {ref} 触发了一次平台新操作（该事件类型暂无人读映射）"
    return f"{actor} 触发了一次平台新操作（该事件类型暂无人读映射）"


def state_transition(event):
    """状态迁移 chip 文本（before → after；无迁移返回空串）。"""
    before = event.get("before")
    after = event.get("after")
    if not before and not after:
        return ""
    if before and after and before == after:
        return ""
    arrow = f"{before} → {after}" if before else f"→ {after}"
    return arrow


def _scalar(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        return _mask_uuids(str(value))
    return None


def _leaf_rows(prefix, value):
    """把 payload 递归展开为 (标签, 文本) 标量行；嵌套结构用点号路径命名。"""
    scalar = _scalar(value)
    if scalar is not None:
        return [(prefix, scalar)] if prefix else []
    if isinstance(value, dict):
        rows = []
        for key in sorted(value):
            rows.extend(_leaf_rows(f"{prefix}.{key}" if prefix else str(key), value[key]))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value, start=1):
            rows.extend(_leaf_rows(f"{prefix} #{index}" if prefix else f"#{index}", item))
        return rows
    return []


def event_detail_rows(event):
    """时间线展开区的结构化负载：字段化 (标签, 文本) 行列表。

    特殊类型给出人读标签（判据结果表 / 报告摘要 / 证据清单 / 理由），
    其余类型回退到标量叶子行；整条路径不得出现裸 JSON。
    """
    payload = event.get("payload") or {}
    etype = event.get("event_type")
    rows = []

    if etype == "verification.initial":
        document = payload.get("results") or {}
        results = document.get("results") or []
        for item in results:
            verdict = "通过" if item.get("passed") is True else "未通过"
            note = "；".join(str(e) for e in (item.get("evidence") or []))
            rows.append((f"判据 {item.get('criterion_id') or '-'}", f"{verdict}　{note}".strip()))
        fingerprint = str(payload.get("workspace_fingerprint") or "")
        if fingerprint:
            rows.append(("验收指纹", fingerprint))
        rows.append(("执行机器用例", _scalar(payload.get("executed")) or "-"))
        return rows

    if etype == "task.submitted":
        report = payload.get("report") or {}
        implemented = str(report.get("implemented") or "").strip()
        if implemented:
            rows.append(("报告摘要", implemented))
        executed = report.get("executed") or []
        if executed:
            rows.append(("执行用例", f"{len(executed)} 项（报告 executed 段）"))
        if report.get("not_executed"):
            rows.append(("未执行项", f"{len(report['not_executed'])} 项（见报告）"))
        if report.get("deviations"):
            rows.append(("偏差", f"{len(report['deviations'])} 项（见报告）"))
        evidence = payload.get("evidence") or []
        for index, item in enumerate(evidence, start=1):
            if isinstance(item, dict):
                path = str(item.get("path") or "-")
                digest = str(item.get("sha256") or "")
                rows.append((f"证据 {index}", f"{path}（sha256 {digest[:12]}…）" if digest else path))
            else:
                rows.append((f"证据 {index}", str(item)))
        return rows

    if etype == "task.shipped":
        commit = str(payload.get("commit_hash") or "")
        if commit:
            rows.append(("入库 commit", commit))
        if payload.get("via"):
            rows.append(("入库方式", str(payload["via"])))
        fingerprint = str(payload.get("workspace_fingerprint") or "")
        if fingerprint:
            rows.append(("验收指纹", fingerprint))
        return rows

    if etype in ("task.rejected", "task.void"):
        if payload.get("category"):
            rows.append(("类别", str(payload["category"])))
        if payload.get("reason"):
            rows.append(("理由", str(payload["reason"])))
        return rows

    if etype == "task.blocked":
        if payload.get("reason"):
            rows.append(("阻塞理由", str(payload["reason"])))
        for index, item in enumerate(payload.get("evidence") or [], start=1):
            rows.append((f"证据 {index}", str(item)))
        return rows

    if etype in ("task.claimed", "lease.renewed", "lease.expired"):
        lease = payload.get("lease") or {}
        if lease.get("claimed_by"):
            rows.append(("认领人", str(lease["claimed_by"])))
        if lease.get("expires_at"):
            rows.append(("租约到期", str(lease["expires_at"])))
        if etype == "lease.renewed" and payload.get("previous_expires_at"):
            rows.append(("原到期时间", str(payload["previous_expires_at"])))
        return rows

    return _leaf_rows("", payload)
