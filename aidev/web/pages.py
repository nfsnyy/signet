"""project 与需求只读面的页面渲染（仅标准库）。

v1 展原稿：需求正文以 HTML 转义后的原始 markdown 放入 <pre>，不做渲染。
页面把关键数据（任务计数、需求标识、正文片段）放在响应前部，便于
人工审阅与机器抽查直接命中。
"""

import html


def _page(title, body):
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head><body>"
        f"{body}</body></html>"
    )


def render_overview(user, snapshot):
    counts_items = "".join(
        f"<li>{html.escape(state)}：{count}</li>"
        for state, count in snapshot["task_counts"].items()
    )
    pending_items = "".join(
        "<li><code>{}</code>　{}（owner：{}）</li>".format(
            html.escape(item["task_id"]),
            html.escape(item.get("title") or ""),
            html.escape(item.get("owner") or "-"),
        )
        for item in snapshot["pending_verification"]
    ) or "<li>无</li>"
    event_items = "".join(
        "<li><code>{}</code>　{}　task={}　actor={}</li>".format(
            html.escape(str(event.get("timestamp") or "")),
            html.escape(str(event.get("event_type") or "")),
            html.escape(str(event.get("task_id") or "-")),
            html.escape(str(event.get("actor") or "")),
        )
        for event in snapshot["recent_events"]
    ) or "<li>无</li>"
    project = snapshot.get("project") or {}
    meta = (
        "<p>当前登录用户：<strong>{}</strong>　项目：{}（owner：{}）"
        "　状态仓：<code>{}</code>　commit：<code>{}</code></p>"
    ).format(
        html.escape(user),
        html.escape(str(project.get("name") or "-")),
        html.escape(str(project.get("owner") or "-")),
        html.escape(snapshot["repo"]),
        html.escape(snapshot["commit"][:12]),
    )
    body = (
        "<h1>signet 平台概况</h1>"
        f"<h2>任务计数</h2><ul>{counts_items}</ul>"
        f"{meta}"
        f"<h2>待验收（Implemented）</h2><ul>{pending_items}</ul>"
        f"<h2>最近事件</h2><ul>{event_items}</ul>"
        "<nav><a href=\"/requirements\">需求列表</a></nav>"
    )
    return _page("signet 概况", body)


def render_requirements(user, entries):
    items = "".join(
        "<li><a href=\"/requirements/{}\">{}</a>"
        "（<code>{}</code>，状态 {}，优先级 {}，owner {}，进度 {:.0%}，任务 {}）</li>".format(
            html.escape(entry["requirement_id"]),
            html.escape(entry.get("title") or ""),
            html.escape(entry["requirement_id"]),
            html.escape(entry.get("status") or "-"),
            html.escape(entry.get("priority") or "-"),
            html.escape(entry.get("owner") or "-"),
            entry.get("progress") or 0,
            entry.get("task_count", 0),
        )
        for entry in entries
    ) or "<li>无需求</li>"
    body = (
        "<h1>需求列表</h1>"
        f"<ul>{items}</ul>"
        f"<p>当前登录用户：<strong>{html.escape(user)}</strong></p>"
        "<nav><a href=\"/\">返回概况</a></nav>"
    )
    return _page("signet 需求列表", body)


def render_requirement_detail(user, detail):
    task_items = "".join(
        f"<li><code>{html.escape(task_id)}</code></li>"
        for task_id in detail["tasks"]
    ) or "<li>无关联任务</li>"
    heading = "<h1>{}（<code>{}</code>）</h1>".format(
        html.escape(detail.get("title") or ""),
        html.escape(detail["requirement_id"]),
    )
    meta = "<p>状态：{}　优先级：{}　owner：{}　进度：{:.0%}　任务数：{}</p>".format(
        html.escape(detail.get("status") or "-"),
        html.escape(detail.get("priority") or "-"),
        html.escape(detail.get("owner") or "-"),
        detail.get("progress") or 0,
        len(detail["tasks"]),
    )
    body = (
        f"{heading}"
        f"{meta}"
        f"<h2>正文（原稿）</h2><pre>{html.escape(detail['body'])}</pre>"
        f"<h2>关联任务</h2><ul>{task_items}</ul>"
        f"<p>当前登录用户：<strong>{html.escape(user)}</strong></p>"
        "<nav><a href=\"/requirements\">返回需求列表</a>　<a href=\"/\">返回概况</a></nav>"
    )
    return _page("signet 需求详情", body)
