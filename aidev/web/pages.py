"""平台页面渲染（仅标准库）。

v2 产品化版式（T8）：统一 topbar 导航 + 页面头（h1 + <p class="muted">）+
卡片化分区，样式来自固定静态资源 /static/style.css（原型 doc/prototype/style.css
为基准）。任务说明/需求正文按分区规范渲染（背景/目标主体展示、范围/不做与参考
默认折叠），不规范原稿优雅降级为整稿可见；无 JS（v1 仅原生 details/form/link）。

T7 人读能力不回退：事件流 caption/#短码/相对时间/悬停绝对时间、时间线
id="timeline" + details/summary 结构化负载，标记与句式保持不变。
"""

import html
import re
from datetime import datetime, timezone

from . import eventcaption

BOARD_COLUMN_ORDER = ("Draft", "Ready", "In Progress", "Implemented", "Verified", "Done", "Blocked", "Void")

STYLE_URL = "/static/style.css"

NAV_ITEMS = (
    ("overview", "/", "概况"),
    ("tasks", "/tasks", "任务"),
    ("requirements", "/requirements", "需求"),
)

_BADGE_CLASSES = {
    "Draft": "draft", "Ready": "ready", "In Progress": "progress",
    "Implemented": "implemented", "Verified": "verified", "Done": "done",
    "Blocked": "blocked", "Void": "void",
}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 分区规范：背景/目标主体展示；范围/不做/边界/参考/关联折叠（默认不收起）。
_FOLD_KEYWORDS = ("范围", "不做", "边界", "参考", "关联")
_MAIN_KEYWORDS = ("背景", "目标")


def _relative_time(iso):
    """事件流时间：N 分钟/小时/天前（REQ-3 相对化，悬停 title 由调用方给绝对时间）。"""
    if not iso:
        return "-"
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    minutes = int((datetime.now(timezone.utc) - stamp).total_seconds() // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def _compact_time(iso):
    """时间线时间：MM-DD HH:MM（REQ-3 分流：时间线紧凑绝对，悬停 title 完整绝对）。"""
    text = str(iso or "")
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[5:10]} {text[11:16]}"
    return text or "-"


def _linked_caption(event, title, link_tasks):
    """人读句式 → HTML：任务引用替换成 #短码+标题（概况页带链接，时间线用 span）。"""
    task_id = str(event.get("task_id") or "")
    caption = html.escape(eventcaption.describe_event(event, title))
    if not task_id:
        return caption
    short = eventcaption.task_shortcode(task_id)
    ref_text = html.escape(f"{short} {title}" if title else short)
    if title:
        ref_html = ("<a href=\"/tasks/{}\"><span class=\"task-ref\">{}</span>"
                    " <span class=\"task-title\">{}</span></a>").format(
            html.escape(task_id), html.escape(short), html.escape(title)) if link_tasks else (
            "<span class=\"task-ref\">{}</span> <span class=\"task-title\">{}</span>").format(
            html.escape(short), html.escape(title))
    else:
        ref_html = ("<a href=\"/tasks/{}\"><span class=\"task-ref\">{}</span></a>").format(
            html.escape(task_id), html.escape(short)) if link_tasks else (
            "<span class=\"task-ref\">{}</span>").format(html.escape(short))
    return caption.replace(ref_text, ref_html, 1)


def _state_chip(event):
    """状态迁移 chip；驳回/作废类事件用 warn 样式。"""
    transition = eventcaption.state_transition(event)
    if not transition:
        return ""
    warn = event.get("event_type") in ("task.rejected", "task.void")
    cls = "chip warn" if warn else "chip state"
    return f"<span class=\"{cls}\">{html.escape(transition)}</span>"


def _feed_item(event, titles, link_tasks, time_text):
    """单条事件条目：时间（悬停绝对）+ 人读句式（自带 actor）+ 状态 chip（+ 概况页详情入口）。"""
    title = titles.get(str(event.get("task_id") or ""))
    caption = _linked_caption(event, title, link_tasks)
    chip = _state_chip(event)
    task_id = str(event.get("task_id") or "")
    more = ""
    if link_tasks and task_id:
        more = "<a class=\"feed-more\" href=\"/tasks/{}#timeline\">详情 →</a>".format(html.escape(task_id))
    return (
        "<li class=\"feed-item\"><span class=\"feed-time\" title=\"{}\">{}</span>"
        " <span class=\"feed-body\">{} {} {}</span></li>"
    ).format(
        html.escape(str(event.get("timestamp") or "")),
        html.escape(time_text),
        caption, chip, more,
    )


def _page(title, user, active, body):
    """统一版式骨架：topbar（brand/主导航/用户位）+ .page 正文，全站引用同一 stylesheet。"""
    nav = "".join(
        '<a href="{}"{}>{}</a>'.format(
            html.escape(href),
            ' class="active"' if key == active else "",
            html.escape(label),
        )
        for key, href, label in NAV_ITEMS
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        f"<link rel=\"stylesheet\" href=\"{STYLE_URL}\"></head><body>"
        "<div class=\"topbar\"><span class=\"brand\">signet</span>"
        f"<nav>{nav}</nav>"
        f"<span class=\"user\">{html.escape(user)}</span></div>"
        f"<div class=\"page\">{body}</div>"
        "</body></html>"
    )


def _state_badge(state):
    """状态徽标：未知状态退化为纯文本。"""
    text = str(state or "?")
    cls = _BADGE_CLASSES.get(text)
    if cls is None:
        return html.escape(text)
    return f"<span class=\"badge {cls}\">{html.escape(text)}</span>"


def _markdown_block(lines):
    """极简 markdown → HTML：段落、- 列表、续行归并；全部转义，不嵌套解析。"""
    chunks = []
    paragraph = []
    items = []

    def flush_paragraph():
        if paragraph:
            text = " ".join(part.strip() for part in paragraph if part.strip())
            if text:
                chunks.append(f"<p>{html.escape(text)}</p>")
            paragraph.clear()

    def flush_list():
        if items:
            chunks.append("<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>")
            items.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            items.append(stripped[2:].strip())
        elif items and line[:1] in (" ", "\t"):
            items[-1] = items[-1] + " " + stripped
        else:
            flush_list()
            paragraph.append(stripped)
    flush_paragraph()
    flush_list()
    return "".join(chunks)


def _split_sections(markdown):
    """按 markdown 标题切片：返回 (preamble, [(level, title, content_lines)])。"""
    preamble = []
    sections = []
    current = None
    for line in str(markdown or "").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            current = [len(match.group(1)), match.group(2).strip(), []]
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current[2].append(line)
    return preamble, [(level, title, content) for level, title, content in sections]


def _section_kind(title):
    """分区归类：范围/不做/边界/参考/关联折叠；背景/目标及其余小节主体展示。"""
    if any(keyword in title for keyword in _FOLD_KEYWORDS):
        return "fold"
    return "main"


def _render_document(markdown):
    """分区渲染任务说明/需求正文（.task-brief 容器）。

    规范稿：背景/目标等主区以可见标题主体展示，范围/参考等折叠进 <details>
    （无 open，默认折叠）；无小节标题的原稿优雅降级为整稿 <pre> 可见，不抛异常。
    """
    text = str(markdown or "")
    preamble, sections = _split_sections(text)
    if not any(level >= 2 for level, _, _ in sections):
        return "<section class=\"task-brief\"><pre>{}</pre></section>".format(html.escape(text))
    chunks = []
    intro = " ".join(line.strip() for line in preamble if line.strip())
    if intro:
        chunks.append(f"<p>{html.escape(intro)}</p>")
    for index, (level, title, content) in enumerate(sections):
        if index == 0 and level == 1:
            continue  # 文档大标题（页面 h1 已展示）
        inner = _markdown_block(content)
        if _section_kind(title) == "fold":
            chunks.append(
                "<details><summary>{}</summary><div class=\"section-body\">{}</div></details>".format(
                    html.escape(title), inner)
            )
        else:
            chunks.append(f"<h3>{html.escape(title)}</h3>{inner}")
    return "<section class=\"task-brief\">{}</section>".format("".join(chunks))


def render_overview(user, snapshot):
    counts_items = "".join(
        "<a class=\"stat-card\" href=\"/tasks\"><span class=\"num\">{}</span>"
        "<span class=\"label\">{}</span></a>".format(count, html.escape(state))
        for state, count in snapshot["task_counts"].items()
    )
    pending_items = "".join(
        "<div class=\"task-card\"><div class=\"title\"><a href=\"/tasks/{}\"><code>{}</code>　{}</a></div>"
        "<div class=\"meta\">owner：{}</div></div>".format(
            html.escape(item["task_id"]),
            html.escape(item["task_id"]),
            html.escape(item.get("title") or ""),
            html.escape(item.get("owner") or "-"),
        )
        for item in snapshot["pending_verification"]
    ) or "<p class=\"muted\">（暂无待验收任务）</p>"
    titles = snapshot.get("task_titles") or {}
    event_items = "".join(
        _feed_item(event, titles, link_tasks=True, time_text=_relative_time(event.get("timestamp")))
        for event in snapshot["recent_events"]
    ) or "<li>无</li>"
    project = snapshot.get("project") or {}
    header_note = (
        "项目 {}（owner：{}）· 状态仓 <code>{}</code> · 账本 <code>{}</code>"
        " · 当前登录用户 {}"
    ).format(
        html.escape(str(project.get("name") or "-")),
        html.escape(str(project.get("owner") or "-")),
        html.escape(snapshot["repo"]),
        html.escape(snapshot["commit"][:12]),
        html.escape(user),
    )
    body = (
        "<h1>signet 平台概况</h1>"
        f"<p class=\"muted\">{header_note}</p>"
        f"<h2>任务计数</h2><div class=\"stat-row\">{counts_items}</div>"
        "<p><a href=\"/tasks/new\">＋ 新建任务</a></p>"
        f"<h2>待验收（Implemented）</h2>{pending_items}"
        f"<h2>最近事件</h2><ul class=\"feed\">{event_items}</ul>"
        "<p class=\"muted\">最近事件仅放人读摘要；完整故事（报告 / 判据 / commit / 理由）"
        "见任务详情页的事件时间线。</p>"
    )
    return _page("signet 概况", user, "overview", body)


def render_requirements(user, entries):
    cards = "".join(
        "<div class=\"task-card\"><div class=\"title\"><a href=\"/requirements/{}\">{}</a></div>"
        "<div class=\"meta\"><code>{}</code>　状态 {}　优先级 {}　owner {}　进度 {:.0%}　任务 {}</div></div>".format(
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
    ) or "<p class=\"muted\">（暂无需求）</p>"
    body = (
        "<h1>需求列表</h1>"
        f"<p class=\"muted\">全部需求一览；点击进入需求详情（正文与关联任务）。当前登录用户 {html.escape(user)}</p>"
        f"{cards}"
    )
    return _page("signet 需求列表", user, "requirements", body)


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
    """任务看板：按状态分列卡片；卡片含租约到期与钉扎版本（人工核查点）。"""
    columns = {state: [] for state in BOARD_COLUMN_ORDER}
    extras = []
    for record in tasks:
        state = record.get("state") or "?"
        if state not in columns:
            extras.append(state)
            columns[state] = []
        item = (
            "<div class=\"task-card\"><div class=\"title\"><a href=\"/tasks/{tid}\">{title}</a></div>"
            "<div class=\"meta\"><code>{tid}</code><br>owner：{owner}　状态：{badge}　{lease}<br>"
            "钉扎版本：{pin}　更新：{updated}</div></div>"
        ).format(
            tid=html.escape(str(record.get("task_id") or "")),
            title=html.escape(str(record.get("title") or "")),
            owner=html.escape(str(record.get("owner") or "-")),
            badge=_state_badge(state),
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
        items = "".join(columns[state]) or "<p class=\"muted\">（无）</p>"
        sections.append(
            "<section class=\"board-col\"><h3>{}（{}）</h3>{}</section>".format(
                html.escape(state), len(columns[state]), items
            )
        )
    body = (
        "<h1>任务看板</h1>"
        "<p class=\"muted\">按状态分列全部任务；点击任务卡进入详情页的事件时间线。</p>"
        "<p><a href=\"/tasks/new\">＋ 新建任务</a></p>"
        f"<div class=\"board\">{''.join(sections)}</div>"
    )
    return _page("signet 任务看板", user, "tasks", body)


def _handbook_tldr():
    """交接卡关键信息速览：置于交接卡响应前部（机器抽查 300 字窗口可命中）。"""
    return (
        "<p>交接卡（验收操作手册）速览：机器验收 <code>verify --execute</code>（仅 Implemented 渲染本指引）；"
        "关闭任务 <code>close</code>；环境条款 <code>SIGNET_PLATFORM_REPO</code> / "
        "<code>SIGNET_BASE_URL</code> / <code>SIGNET_STATE_REPO</code></p>"
    )


def _dispatch_block(task_id):
    """派活卡：Draft/Ready（非 In Progress）任务渲染的领取指引块（dispatch-card）。"""
    task_id = html.escape(str(task_id))
    return (
        "<div class=\"dispatch-card\"><h3>派活卡（agent 领取指引）</h3>"
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
        "</pre></div>"
    )


def _handbook_block(task_id):
    """交接卡（验收操作手册）：仅 Implemented 任务渲染（handoff-card）。"""
    task_id = html.escape(str(task_id))
    return (
        "<div class=\"handoff-card\"><h3>交接卡（验收操作手册）</h3>"
        + _handbook_tldr()
        + "<p>本任务已提交（Implemented），待验收。验收操作指引：</p>"
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
        "</ul></div>"
    )


def _timeline_block(detail):
    """事件时间线（AC-05）：一行摘要 + details/summary 展开结构化负载。

    时间线内任务引用用 span（本页即详情，直链由概况页负责），展开区为字段化
    表格，裸 JSON 不上页面；悬停 title 一律完整绝对时间（REQ-3）。
    """
    events = detail.get("events") or []
    titles = detail.get("task_titles") or {}
    heading = "<h2 id=\"timeline\">事件时间线</h2>"
    if not events:
        return heading + "<p class=\"muted\">（暂无事件）</p>"
    items = "".join(
        _timeline_item(event, titles)
        for event in events
    )
    return heading + "<ul class=\"feed\">" + items + "</ul>"


def _timeline_item(event, titles):
    title = titles.get(str(event.get("task_id") or ""))
    summary = "{} {}".format(
        _linked_caption(event, title, link_tasks=False),
        _state_chip(event),
    )
    rows = eventcaption.event_detail_rows(event)
    if rows:
        table = "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(html.escape(str(label)), html.escape(str(value)))
            for label, value in rows
        )
        body = (
            "<details><summary>{}</summary><div class=\"feed-detail\">"
            "<table><tr><th>字段</th><th>内容</th></tr>{}</table></div></details>"
        ).format(summary, table)
    else:
        body = summary
    return (
        "<li class=\"feed-item\"><span class=\"feed-time\" title=\"{}\">{}</span>"
        " <span class=\"feed-body\">{}</span></li>"
    ).format(
        html.escape(str(event.get("timestamp") or "")),
        html.escape(_compact_time(event.get("timestamp"))),
        body,
    )


def _card_block(detail):
    """钉扎验收卡：合同文本精确优先、默认折叠（<details> 无 open）。"""
    card = detail.get("card") or {}
    criteria = card.get("criteria") or []
    if not card:
        return "<p class=\"muted\">验收卡（钉扎）：未钉扎（任务尚未 freeze）</p>"
    criterion_items = "".join(
        "<li><code>{cid}</code>（{strength}）{text}</li>".format(
            cid=html.escape(str(item.get("criterion_id") or "")),
            strength=html.escape(str(item.get("strength") or "")),
            text=html.escape(str(item.get("text") or "")),
        )
        for item in criteria
    ) or "<li>（无条目）</li>"
    return (
        "<details><summary>验收卡（钉扎）：{title}</summary>"
        "<ul class=\"card-criteria\">{items}</ul></details>"
    ).format(
        title=html.escape(str(card.get("title") or "")),
        items=criterion_items,
    )


def render_task_detail(user, detail):
    """任务详情页：metadata 卡 + 任务说明分区渲染 + 钉扎验收卡（折叠）+ 交接/派活卡 + 时间线。"""
    task_id = str(detail.get("task_id") or "")
    state = str(detail.get("state") or "-")
    requirement = str(detail.get("requirement") or "-")
    meta = (
        "<section class=\"op-area\"><h3>任务信息</h3>"
        "<p>状态：{badge}　owner：{owner}　requirement："
        "<a href=\"/requirements/{requirement}\"><code>{requirement}</code></a></p>"
        "<p>{lease}</p>"
        "<p>钉扎版本：{pin}</p>"
        "<p>创建：{created}　更新：{updated}</p></section>"
    ).format(
        badge=_state_badge(state),
        owner=html.escape(str(detail.get("owner") or "-")),
        requirement=html.escape(requirement),
        lease=_lease_text(detail.get("lease")),
        pin=_pin_version_text(detail.get("spec_pin")),
        created=html.escape(str(detail.get("created_at") or "-")),
        updated=html.escape(str(detail.get("updated_at") or "-")),
    )
    handbook = _handbook_block(task_id) if state == "Implemented" else ""
    dispatch = _dispatch_block(task_id) if state in ("Draft", "Ready") else ""
    body = (
        "<h1>{}（<code>{}</code>）</h1>".format(
            html.escape(str(detail.get("title") or "")), html.escape(task_id)
        )
        + "<p class=\"muted\">任务 <code>{}</code> · requirement <a href=\"/requirements/{}\"><code>{}</code></a>"
          " · 当前状态 {}</p>".format(
              html.escape(task_id), html.escape(requirement),
              html.escape(requirement), _state_badge(state))
        + meta
        + "<h2>任务说明</h2>"
        + _render_document(detail.get("body"))
        + "<h2>验收卡</h2>"
        + _card_block(detail)
        + dispatch
        + handbook
        + _timeline_block(detail)
    )
    return _page("signet 任务详情", user, "tasks", body)


def render_task_form(user, requirement=None):
    """页面建任务表单：owner 由会话锁定，workspace 由平台生成。"""
    requirement_value = html.escape(requirement or "")
    body = (
        "<h1>新建任务</h1>"
        "<p class=\"muted\">填写标题与需求标识；owner 自动取当前登录用户（身份锁定，不可指定），"
        "workspace 由平台生成标识。</p>"
        "<form method=\"post\" action=\"/tasks\" class=\"card\">"
        "<label>标题：<input name=\"title\" required></label>"
        "<label>需求：<input name=\"requirement\" value=\"{}\" required></label>".format(requirement_value)
        + "<button class=\"btn\" type=\"submit\">创建任务</button>"
        "<p class=\"hint\">创建成功后自动跳转到任务详情页。</p>"
        "</form>"
    )
    return _page("signet 新建任务", user, "tasks", body)


def render_requirement_detail(user, detail):
    requirement_id = str(detail.get("requirement_id") or "")
    task_items = "".join(
        "<li><a href=\"/tasks/{}\"><code>{}</code></a></li>".format(
            html.escape(task_id), html.escape(task_id))
        for task_id in detail["tasks"]
    ) or "<li>无关联任务</li>"
    body = (
        "<h1>{}（<code>{}</code>）</h1>".format(
            html.escape(detail.get("title") or ""), html.escape(requirement_id))
        + "<p class=\"muted\">状态：{}　优先级：{}　owner：{}　进度：{:.0%}　关联任务：{} 个</p>".format(
            html.escape(detail.get("status") or "-"),
            html.escape(detail.get("priority") or "-"),
            html.escape(detail.get("owner") or "-"),
            detail.get("progress") or 0,
            len(detail["tasks"]),
        )
        + "<p><a href=\"/tasks/new?requirement={}\">＋ 从需求「{}」创建任务</a></p>".format(
            html.escape(requirement_id), html.escape(requirement_id))
        + "<h2>正文</h2>"
        + _render_document(detail["body"])
        + "<h2>关联任务</h2><ul class=\"related-tasks\">{}</ul>".format(task_items)
    )
    return _page("signet 需求详情", user, "requirements", body)
