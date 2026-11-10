"""project 与需求只读面的页面渲染（仅标准库）。

v1 展原稿：需求正文以 HTML 转义后的原始 markdown 放入 <pre>，不做渲染。
页面把关键数据（任务计数、需求标识、正文片段）放在响应前部，便于
人工审阅与机器抽查直接命中。
"""

import html

BOARD_COLUMN_ORDER = ("Draft", "Ready", "In Progress", "Implemented", "Verified", "Done", "Blocked", "Void")


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
        "<nav><a href=\"/tasks\">任务看板</a>　<a href=\"/tasks/new\">新建任务</a>　<a href=\"/requirements\">需求列表</a></nav>"
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
        "<nav><a href=\"/tasks\">任务看板</a>　<a href=\"/\">返回概况</a></nav>"
    )
    return _page("signet 需求列表", body)


def _lease_text(lease):
    """租约展示：claimed_by + expires_at；无租约显示「无租约」。"""
    if not lease:
        return "无租约"
    return "租约 {} @ 到期 {}".format(
        html.escape(str(lease.get("claimed_by") or "-")),
        html.escape(str(lease.get("expires_at") or "-")),
    )


def _pin_version_text(spec_pin):
    """钉扎版本展示：验收卡 v* / 契约 v* / 映射 v*；未钉扎显示「未钉扎」。"""
    if not isinstance(spec_pin, dict):
        return "未钉扎"
    parts = []
    for key, label in (("acceptance_card", "验收卡"), ("contract", "契约"), ("mapping", "映射")):
        version = (spec_pin.get(key) or {}).get("version")
        if version is not None:
            parts.append("{} v{}".format(label, html.escape(str(version))))
    return " / ".join(parts) or "未钉扎"


def render_board(user, tasks):
    """任务看板：按状态分列；卡片含租约到期与钉扎版本（人工核查点）。"""
    columns = {state: [] for state in BOARD_COLUMN_ORDER}
    extras = []
    for record in tasks:
        state = record.get("state") or "?"
        if state not in columns:
            extras.append(state)
            columns[state] = []
        item = (
            "<li><a href=\"/tasks/{tid}\">{title}</a>（<code>{tid}</code>）<br>"
            "owner：{owner}　状态：{state}　{lease}<br>"
            "钉扎版本：{pin}　更新：{updated}</li>"
        ).format(
            tid=html.escape(str(record.get("task_id") or "")),
            title=html.escape(str(record.get("title") or "")),
            owner=html.escape(str(record.get("owner") or "-")),
            state=html.escape(str(state)),
            lease=_lease_text(record.get("lease")),
            pin=_pin_version_text(record.get("spec_pin")),
            updated=html.escape(str(record.get("updated_at") or "-")),
        )
        columns[state].append(item)
    order = [state for state in BOARD_COLUMN_ORDER if columns[state] or state not in extras]
    for state in extras:
        order.append(state)
    sections = []
    for state in order:
        items = "".join(columns[state]) or "<li>（无）</li>"
        sections.append(
            "<section><h2>{}（{}）</h2><ul>{}</ul></section>".format(
                html.escape(state), len(columns[state]), items
            )
        )
    body = (
        "<h1>任务看板</h1>"
        + "".join(sections)
        + "<p>当前登录用户：<strong>{}</strong></p>".format(html.escape(user))
        + "<nav><a href=\"/tasks/new\">新建任务</a>　<a href=\"/\">返回概况</a>　<a href=\"/requirements\">需求列表</a></nav>"
    )
    return _page("signet 任务看板", body)


def _handbook_tldr():
    """交接卡关键信息速览：置于详情页响应前部（机器抽查 300 字窗口可命中）。"""
    return (
        "<p>交接卡（验收操作手册）速览：机器验收 <code>verify --execute</code>（仅 Implemented 渲染本指引）；"
        "关闭任务 <code>close</code>；环境条款 <code>SIGNET_PLATFORM_REPO</code> / "
        "<code>SIGNET_BASE_URL</code> / <code>SIGNET_STATE_REPO</code></p>"
    )


def _dispatch_block(task_id):
    """派活卡：Draft/Ready（非 In Progress）任务渲染的领取指引块（AC-12）。

    与 Implemented 任务的交接卡形成对称交接面：任务 id、工作区指引、
    aidev-agent skill 入口与「不 push、不动远端」边界声明，可直接复制给 agent。
    """
    task_id = html.escape(str(task_id))
    return (
        "<h2>派活卡（agent 领取指引）</h2>"
        "<p>本任务待领取。operator 在页面或 API 领取（<code>POST /api/tasks/"
        + task_id
        + "/claims</code>）即签发 handoff 令牌（sigt），把令牌与工作区目录交给 agent。</p>"
        "<pre>"
        "任务 id："
        + task_id
        + "\n工作区：operator 提供空目录（init-task --workspace &lt;目录&gt;），agent 经 claim --handoff 落地任务包\n"
        "agent 入口：装载 aidev-agent skill（skills/aidev-agent/SKILL.md），执行\n"
        "  python aidev-agent.py claim --handoff &lt;sigt&gt; --workspace &lt;工作区目录&gt;\n"
        "边界：agent 只改代码完成任务，不 push、不动 git 远端（入库由 operator 负责）"
        "</pre>"
    )


def _handbook_block(task_id):
    """交接卡（验收操作手册）：仅 Implemented 任务渲染。

    含 verify / close 命令模板与环境条款（SIGNET_PLATFORM_REPO / SIGNET_BASE_URL 等）。
    """
    task_id = html.escape(str(task_id))
    return (
        "<h2>交接卡（验收操作手册）</h2>"
        "<p>本任务已提交（Implemented），待验收。验收操作指引：</p>"
        "<ol>"
        "<li>机器验收（验收人执行，需 8000 端口空闲）：<br>"
        "<code>python aidev-operator.py verify --repo &lt;状态仓&gt; --task-id "
        + task_id
        + " --verifier &lt;验收人&gt; --confirmed-by &lt;确认人&gt; --execute</code></li>"
        "<li>关闭任务：<br>"
        "<code>python aidev-operator.py close --repo &lt;状态仓&gt; --task-id "
        + task_id
        + " --decision accept --actor &lt;验收人&gt;</code></li>"
        "</ol>"
        "<h3>环境条款（验收前置）</h3>"
        "<ul>"
        "<li><code>SIGNET_STATE_REPO</code>：可弃置的状态仓克隆（被测服务绑定，编排会真实写入）</li>"
        "<li><code>SIGNET_PLATFORM_REPO</code>：平台仓工作副本（runner 定位被测代码）</li>"
        "<li><code>SIGNET_BASE_URL</code>：被测服务自引用（机器用例由服务端执行，取服务端进程环境）</li>"
        "<li>默认端口 8000 须空闲（兼容性用例断言不可达时报错）</li>"
        "</ul>"
    )


def render_task_detail(user, detail):
    """任务详情页：metadata、task.md 原稿、钉扎验收卡全文；Implemented 附交接卡。"""
    task_id = html.escape(str(detail.get("task_id") or ""))
    lease = detail.get("lease")
    meta = (
        "<p>状态：{state}　owner：{owner}　requirement：<code>{requirement}</code><br>"
        "{lease}<br>钉扎版本：{pin}<br>创建：{created}　更新：{updated}</p>"
    ).format(
        state=html.escape(str(detail.get("state") or "-")),
        owner=html.escape(str(detail.get("owner") or "-")),
        requirement=html.escape(str(detail.get("requirement") or "-")),
        lease=_lease_text(lease),
        pin=_pin_version_text(detail.get("spec_pin")),
        created=html.escape(str(detail.get("created_at") or "-")),
        updated=html.escape(str(detail.get("updated_at") or "-")),
    )
    card = detail.get("card") or {}
    criteria = card.get("criteria") or []
    if card:
        criterion_items = "".join(
            "<li><code>{cid}</code>（{strength}）{text}</li>".format(
                cid=html.escape(str(item.get("criterion_id") or "")),
                strength=html.escape(str(item.get("strength") or "")),
                text=html.escape(str(item.get("text") or "")),
            )
            for item in criteria
        ) or "<li>（无条目）</li>"
        card_block = (
            "<h2>验收卡（钉扎，{title}）</h2><ul>{items}</ul>".format(
                title=html.escape(str(card.get("title") or "")),
                items=criterion_items,
            )
        )
    else:
        card_block = "<h2>验收卡（钉扎）</h2><p>未钉扎（任务尚未 freeze）</p>"
    handbook = _handbook_block(detail.get("task_id")) if detail.get("state") == "Implemented" else ""
    handbook_tldr = _handbook_tldr() if detail.get("state") == "Implemented" else ""
    dispatch = _dispatch_block(detail.get("task_id")) if detail.get("state") in ("Draft", "Ready") else ""
    body = (
        handbook_tldr
        + "<h1>{}（<code>{}</code>）</h1>".format(
            html.escape(str(detail.get("title") or "")), task_id
        )
        + meta
        + "<h2>任务说明（task.md 原稿）</h2><pre>{}</pre>".format(html.escape(str(detail.get("body") or "")))
        + card_block
        + dispatch
        + handbook
        + "<p>当前登录用户：<strong>{}</strong></p>".format(html.escape(user))
        + "<nav><a href=\"/tasks\">返回任务看板</a>　<a href=\"/\">返回概况</a></nav>"
    )
    return _page("signet 任务详情", body)


def render_task_form(user, requirement=None):
    """页面建任务表单：owner 由会话锁定，workspace 由平台生成。"""
    requirement_value = html.escape(requirement or "")
    body = (
        "<h1>新建任务</h1>"
        "<form method=\"post\" action=\"/tasks\">"
        "<label>标题：<input name=\"title\" required></label><br>"
        "<label>需求：<input name=\"requirement\" value=\"{}\" required></label><br>"
        "<button type=\"submit\">创建任务</button>"
        "</form>"
        "<p>说明：owner 自动取当前登录用户（身份锁定，不可指定）；workspace 由平台生成标识。</p>"
        "<p>当前登录用户：<strong>{}</strong></p>".format(requirement_value, html.escape(user))
        + "<nav><a href=\"/tasks\">返回任务看板</a></nav>"
    )
    return _page("signet 新建任务", body)


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
    create_link = "<p><a href=\"/tasks/new?requirement={}\">＋ 从需求「{}」创建任务</a></p>".format(
        html.escape(detail["requirement_id"]),
        html.escape(detail["requirement_id"]),
    )
    body = (
        f"{heading}"
        f"{meta}"
        f"{create_link}"
        f"<h2>正文（原稿）</h2><pre>{html.escape(detail['body'])}</pre>"
        f"<h2>关联任务</h2><ul>{task_items}</ul>"
        f"<p>当前登录用户：<strong>{html.escape(user)}</strong></p>"
        "<nav><a href=\"/tasks\">任务看板</a>　<a href=\"/requirements\">返回需求列表</a>　<a href=\"/\">返回概况</a></nav>"
    )
    return _page("signet 需求详情", body)
