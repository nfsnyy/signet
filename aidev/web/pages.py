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
    ("projects", "/projects", "项目"),
    ("guide", "/guide", "接入指南"),
)

_BADGE_CLASSES = {
    "Draft": "draft", "Ready": "ready", "In Progress": "progress",
    "Implemented": "implemented", "Verified": "verified", "Done": "done",
    "Blocked": "blocked", "Void": "void",
}

_REQ_STATUS_CLASSES = {
    "draft": "draft", "submitted": "progress", "confirmed": "done", "accepted": "done",
    "closed": "closed",
}

_ENTRY_STATUS_CLASSES = {
    "草案": "draft", "已确认": "ready", "已派生": "progress", "已覆盖": "done",
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


def _project_switcher(project_ctx):
    """T17 顶栏项目切换器：当前项目标识 + 每项目切换按钮（button-only 表单零命名字段，
    details/summary 展开，无 JS）。project_ctx None（匿名页）渲染占位。"""
    if not project_ctx:
        return ""
    current = project_ctx.get("current") or {}
    projects = project_ctx.get("projects") or []
    current_name = str(current.get("name") or current.get("project_id") or "未设置")
    rows = "".join(
        '<form method="post" action="/projects/{pid}/switch" class="switch-form">'
        '<button class="btn" type="submit">切换</button> '
        "<code>{pid}</code> {name}{badge}</form>".format(
            pid=html.escape(str(item.get("project_id") or "")),
            name=html.escape(str(item.get("name") or "")),
            badge="（内置）" if item.get("builtin") else ("（停用）" if item.get("status") == "disabled" else ""),
        )
        for item in projects
    ) or '<p class="muted">（尚未注册任何项目）</p>'
    return (
        '<span class="project-switch">项目 <code>{current}</code>'
        '<details><summary>切换</summary><div class="switch-list">{rows}</div></details>'
        "</span>"
    ).format(current=html.escape(current_name), rows=rows)


def _page(title, user, active, body, project_ctx=None):
    """统一版式骨架：topbar（brand/主导航/项目切换器/用户位）+ .page 正文，全站引用同一 stylesheet。"""
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
        f"{_project_switcher(project_ctx)}"
        f"<span class=\"user\">{html.escape(user)}"
        f"　<a href=\"/settings\" title=\"用户设置\">⚙ 用户设置</a></span></div>"
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


def _req_status_badge(status):
    """需求状态徽标（T9 列表状态徽标）：未知状态退化为纯文本。"""
    text = str(status or "-")
    cls = _REQ_STATUS_CLASSES.get(text)
    if cls is None:
        return html.escape(text)
    return f"<span class=\"badge {cls}\">{html.escape(text)}</span>"


def _entry_status_badge(status):
    """条目状态推导徽标（AC-03 口径）：草案 / 已确认 / 已派生 / 已覆盖。"""
    text = str(status or "-")
    cls = _ENTRY_STATUS_CLASSES.get(text)
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


def render_guide():
    """接入指南（REQ-3/AC-03）：匿名可读——获取 skill / 放置到本机 skills 目录 /
    登录并开启 session / 凭任务令牌领取任务；T8 骨架（topbar/h1+muted/无 JS），
    获取动作一律经 API，无本地路径指引（AC-10 渠道红线）。"""
    body = (
        "<h1>接入指南</h1>"
        "<p class=\"muted\">首次接入 signet 平台：三步拿到 skill 并开工；"
        "获取动作一律经平台 API，无需任何本机先验文件。</p>"
        "<div class=\"card\"><h2>第一步：获取 skill</h2>"
        "<p>下载 <a href=\"/api/skill\" download=\"SKILL.md\">SKILL.md（GET /api/skill）</a>"
        "：平台唯一事实源的当前"
        "版本全文，首行为版本标识（版本随平台 commit 自动更新，漂移可检出）。</p></div>"
        "<div class=\"card\"><h2>放置到本机 coder agent 的 skills 目录</h2>"
        "<p>kimi-code：将下载的 SKILL.md 存至本机 <code>.kimi-code/skills/aidev-agent/SKILL.md</code>"
        "（Windows 下即 <code>C:\\Users\\&lt;你&gt;\\.kimi-code\\skills\\aidev-agent\\</code>，"
        "目录不存在就新建）；其它 coder agent 放入其各自的 skills 目录即可。</p></div>"
        "<div class=\"card\"><h2>第二步：登录并开启 session</h2>"
        "<p>账号由 operator 开通：浏览器经 <a href=\"/login\">/login</a> 登录进工作面；"
        "CLI 侧用 login 动词写入本机令牌。</p></div>"
        "<div class=\"card\"><h2>第三步：凭任务令牌领取任务</h2>"
        "<p>从 operator 领到任务 sigt 后执行 <code>claim --handoff</code>——任务包自带 skill"
        "（工作区落地 <code>.signet-skill.md</code>）与材料清单（skill / 需求正文 / 验收卡 / "
        "hints 随包，规范材料经 <code>/api/norms/&lt;name&gt;</code> 随取），零本机先验文件。</p></div>"
    )
    return _page("接入指南", "未登录", "guide", body)


def render_overview(user, snapshot, project_ctx=None):
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
    current = (project_ctx or {}).get("current") or {}
    project_card = (
        "<section class=\"card\"><h2>当前项目</h2>"
        "<p>名称：{}　标识：<code>{}</code>　状态：{}　内置：{}</p>"
        "<p class=\"muted\">顶栏切换器可切换活动项目；项目注册与凭据管理见 <a href=\"/projects\">项目页</a>。</p>"
        "</section>"
    ).format(
        html.escape(str(current.get("name") or "-")),
        html.escape(str(current.get("project_id") or "-")),
        html.escape(str(current.get("status") or "-")),
        "是" if current.get("builtin") else "否",
    )
    body = (
        "<h1>signet 平台概况</h1>"
        f"<p class=\"muted\">{header_note}</p>"
        f"{project_card}"
        f"<h2>任务计数</h2><div class=\"stat-row\">{counts_items}</div>"
        "<p><a href=\"/tasks/new\">＋ 新建任务</a></p>"
        f"<h2>待验收（Implemented）</h2>{pending_items}"
        f"<h2>最近事件</h2><ul class=\"feed\">{event_items}</ul>"
        "<p class=\"muted\">最近事件仅放人读摘要；完整故事（报告 / 判据 / commit / 理由）"
        "见任务详情页的事件时间线。</p>"
    )
    return _page("signet 概况", user, "overview", body, project_ctx=project_ctx)


def render_requirements(user, entries, project_ctx=None):
    cards = "".join(
        "<div class=\"task-card\"><div class=\"title\"><a href=\"/requirements/{}\">{}</a></div>"
        "<div class=\"meta\"><code>{}</code>　状态 {}　优先级 {}　owner {}　来源 {}　进度 {:.0%}　任务 {}</div></div>".format(
            html.escape(entry["requirement_id"]),
            html.escape(entry.get("title") or ""),
            html.escape(entry["requirement_id"]),
            _req_status_badge(entry.get("status")),
            html.escape(entry.get("priority") or "-"),
            html.escape(entry.get("owner") or "-"),
            html.escape(entry.get("source") or "-"),
            entry.get("progress") or 0,
            entry.get("task_count", 0),
        )
        for entry in entries
    ) or "<p class=\"muted\">（暂无需求）</p>"
    body = (
        "<h1>需求列表</h1>"
        f"<p class=\"muted\">全部需求一览；点击进入需求详情（正文与关联任务）。当前登录用户 {html.escape(user)}</p>"
        f"{cards}"
        "<section class=\"card\"><h2>需求工作流</h2>"
        "<p class=\"muted\">草稿 → 提交 → owner 逐条确认 → 派生任务 → 覆盖收口；"
        "规范经 /api/norms/ 引用获取。</p></section>"
    )
    return _page("signet 需求列表", user, "requirements", body, project_ctx=project_ctx)


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


def render_board(user, tasks, project_ctx=None):
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
        "<section class=\"card\"><h2>看板说明</h2>"
        "<p class=\"muted\">列序固定：Draft → Ready → In Progress → Implemented → Verified → "
        "Done / Blocked / Void；事件时间线见任务详情页。</p></section>"
    )
    return _page("signet 任务看板", user, "tasks", body, project_ctx=project_ctx)


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


def _unarchived_body_hint(detail):
    """REQ-1 历史口径：正文仍是 init-task 占位（冻结时无 task-draft.md 资产吞并）时，
    在既有任务说明渲染分支提示无说明存档；已有正文（含 annex 回填）不误报。"""
    body = detail.get("body")
    title = detail.get("title")
    if not isinstance(body, str) or not isinstance(title, str):
        return ""
    placeholder = f"# {title}\n\n## 任务说明\n\n待补充。"
    if body.strip() != placeholder:
        return ""
    return "<p class=\"hint\">历史任务无说明存档：该任务冻结时未携带 task-draft.md，任务说明仍为占位。</p>"


def render_task_detail(user, detail, project_ctx=None):
    """任务详情页：metadata 卡 + 任务说明分区渲染 + 钉扎验收卡（折叠）+ 交接/派活卡 + 时间线。"""
    task_id = str(detail.get("task_id") or "")
    state = str(detail.get("state") or "-")
    requirement = str(detail.get("requirement") or "-")
    req_items = [str(item) for item in (detail.get("req_items") or [])]
    req_items_text = "、".join("<code>{}</code>".format(html.escape(item)) for item in req_items) or "无"
    meta = (
        "<section class=\"op-area\"><h3>任务信息</h3>"
        "<p>状态：{badge}　owner：{owner}　requirement："
        "<a href=\"/requirements/{requirement}\"><code>{requirement}</code></a></p>"
        "<p>承载条目：{req_items}</p>"
        "<p>{lease}</p>"
        "<p>钉扎版本：{pin}</p>"
        "<p>创建：{created}　更新：{updated}</p></section>"
    ).format(
        badge=_state_badge(state),
        owner=html.escape(str(detail.get("owner") or "-")),
        requirement=html.escape(requirement),
        req_items=req_items_text,
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
        + _unarchived_body_hint(detail)
        + _render_document(detail.get("body"))
        + "<h2>验收卡</h2>"
        + _card_block(detail)
        + dispatch
        + handbook
        + _timeline_block(detail)
    )
    return _page("signet 任务详情", user, "tasks", body, project_ctx=project_ctx)


def render_task_form(user, requirement=None, project_ctx=None):
    """页面建任务表单：owner 由会话锁定，workspace 由平台生成；可携带承载条目编号。"""
    requirement_value = html.escape(requirement or "")
    body = (
        "<h1>新建任务</h1>"
        "<p class=\"muted\">填写标题与需求标识；owner 自动取当前登录用户（身份锁定，不可指定），"
        "workspace 由平台生成标识。承载条目可选：编号须命中已确认需求的「判据条目」节。</p>"
        "<form method=\"post\" action=\"/tasks\" class=\"card\">"
        "<label>标题：<input name=\"title\" required></label>"
        "<label>需求：<input name=\"requirement\" value=\"{}\" required></label>".format(requirement_value)
        + "<label>承载条目（可空，逗号分隔）：<input name=\"req_items\" placeholder=\"REQ-1, REQ-3\"></label>"
        "<button class=\"btn\" type=\"submit\">创建任务</button>"
        "<p class=\"hint\">创建成功后自动跳转到任务详情页。</p>"
        "</form>"
    )
    return _page("signet 新建任务", user, "tasks", body, project_ctx=project_ctx)


def _entry_trace_cell(entry_id, carriers, merges):
    """合一表「内容」列：条目原文 + 条目反查（承载任务链接与 AC 编号）+ 归并徽标。"""
    text = html.escape(str(carriers.get("text") or ""))
    trace = ""
    tasks = carriers.get("tasks") or []
    if tasks:
        links = " ".join(
            "<a href=\"/tasks/{tid}\">{short}</a>（{acs}）".format(
                tid=html.escape(str(item.get("task_id") or "")),
                short=html.escape(eventcaption.task_shortcode(item.get("task_id"))),
                acs=html.escape(", ".join(str(ac) for ac in (item.get("ac_ids") or [])) or "无 AC 锚点"),
            )
            for item in tasks
        )
        trace = "<span class=\"entry-trace\">{}</span>".format(links)
    else:
        trace = "<span class=\"entry-trace muted\">无承载任务</span>"
    merge_badges = "".join(
        "<a class=\"badge merged\" href=\"/tasks/{tid}\">归并自 {short}</a>".format(
            tid=html.escape(str(backlog_task_id)),
            short=html.escape(eventcaption.task_shortcode(backlog_task_id)),
        )
        for backlog_task_id in (merges.get(entry_id) or [])
        if backlog_task_id
    )
    return text + trace + merge_badges


def _entry_table_rows(detail, selectable):
    """合一表数据行：编号 / 内容（含反查与归并）/ 状态徽标 / 操作（可勾选或只读）。"""
    merges = detail.get("merges") or {}
    rows = []
    for item in (detail.get("entries_trace") or []):
        entry_id = str(item.get("id") or "")
        if selectable:
            operation = (
                "<input type=\"checkbox\" name=\"entries\" value=\"{}\" checked>".format(html.escape(entry_id))
            )
        else:
            operation = "—"
        rows.append(
            "<tr id=\"entry-{eid}\"><td><code>{eid}</code></td>"
            "<td>{content}</td><td>{status}</td><td>{operation}</td></tr>".format(
                eid=html.escape(entry_id),
                content=_entry_trace_cell(entry_id, item, merges),
                status=_entry_status_badge(item.get("status")),
                operation=operation,
            )
        )
    return "".join(rows) or "<tr><td colspan=\"4\">（无判据条目）</td></tr>"


def render_requirement_detail(user, detail, project_ctx=None):
    """需求详情页：元信息 + 正文 + 合一判据条目表（#7）+ 关联任务。

    条目区三列表合一——判据条目 / 条目状态推导 / 逐条确认并入单表
    （<table class="entry-table">，表头恰 编号/内容/状态/操作 四列）：
    行内状态列为推导徽标（AC-03 口径），submitted 态操作列渲染默认勾选的
    确认 checkbox（表单 action 指向 confirm 端点，全量清单一交提交），
    其余状态操作列只读；条目反查（AC-02）与归并留痕（AC-06）同行呈现。
    """
    requirement_id = str(detail.get("requirement_id") or "")
    status = str(detail.get("status") or "-")
    task_items = "".join(
        "<li><a href=\"/tasks/{}\"><code>{}</code></a></li>".format(
            html.escape(task_id), html.escape(task_id))
        for task_id in detail["tasks"]
    ) or "<li>无关联任务</li>"
    selectable = status == "submitted" and bool(detail.get("entries_trace"))
    table_rows = _entry_table_rows(detail, selectable)
    if selectable:
        entry_table = (
            "<form method=\"post\" action=\"/api/requirements/{}/confirm\">"
            "<table class=\"entry-table\"><tr><th>编号</th><th>内容</th><th>状态</th><th>操作</th></tr>"
            "{}</table>"
            "<p><button class=\"btn\" type=\"submit\">确认全部条目</button>"
            "<span class=\"muted\">　本人登录逐项核对后一次提交全量清单；清单与当前条目集合"
            "完全一致才会生效（缺条 / 多条 / 编号不存在一律拒绝）。</span></p></form>"
        ).format(html.escape(requirement_id), table_rows)
    else:
        entry_table = (
            "<table class=\"entry-table\"><tr><th>编号</th><th>内容</th><th>状态</th><th>操作</th></tr>"
            "{}</table>".format(table_rows)
        )
    body = (
        "<h1>{}（<code>{}</code>）</h1>".format(
            html.escape(detail.get("title") or ""), html.escape(requirement_id))
        + "<p class=\"muted\">状态：{}　优先级：{}　owner：{}　来源：{}　进度：{:.0%}　关联任务：{} 个</p>".format(
            _req_status_badge(detail.get("status")),
            html.escape(detail.get("priority") or "-"),
            html.escape(detail.get("owner") or "-"),
            html.escape(detail.get("source") or "-"),
            detail.get("progress") or 0,
            len(detail["tasks"]),
        )
        + "<p><a href=\"/tasks/new?requirement={}\">＋ 从需求「{}」创建任务</a></p>".format(
            html.escape(requirement_id), html.escape(requirement_id))
        + "<h2>正文</h2>"
        + _render_document(detail["body"])
        + "<h2>判据条目</h2>"
        + entry_table
        + "<h2>关联任务</h2><ul class=\"related-tasks\">{}</ul>".format(task_items)
    )
    return _page("signet 需求详情", user, "requirements", body, project_ctx=project_ctx)


def render_settings(user, pats=None, projects=None, project_ctx=None):
    """用户设置页（T9 settings 签发面 + T17 项目维度）：PAT 生成表单（FORM-02：
    label/scope/expires_in/project 四命名字段，project 为必选 select、页面字段
    project 映射 API 字段 project_id）+ 我的 PAT 列表（含项目列）。"""
    scope_inputs = "".join(
        "<label><input type=\"checkbox\" name=\"scope\" value=\"{}\"> {}</label>".format(scope, scope)
        for scope in ("requirement:create", "requirement:update", "requirement:submit")
    )
    project_options = "".join(
        "<option value=\"{}\">{}（{}）</option>".format(
            html.escape(str(project.get("project_id") or "")),
            html.escape(str(project.get("name") or "")),
            html.escape(str(project.get("project_id") or "")),
        )
        for project in (projects or [])
    )
    pat_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(record.get("label") or "-")),
            html.escape(str(record.get("project_id") or "-")),
            html.escape(", ".join(record.get("scope") or []) or "-"),
            html.escape(str(record.get("created_at") or "-")),
            html.escape(str(record.get("expires_at") or "-")),
            html.escape(str(record.get("last_used") or "从未使用")),
            html.escape(str(record.get("status") or "-")),
        )
        for record in (pats or [])
    )
    pat_table = (
        "<table><tr><th>标签</th><th>项目</th><th>scope</th><th>创建</th><th>过期</th>"
        "<th>最近使用</th><th>状态</th></tr>{}</table>"
    ).format(pat_rows or "<tr><td colspan=\"7\">（尚未签发任何 PAT）</td></tr>")
    body = (
        "<h1>用户设置</h1>"
        "<p class=\"muted\">管理起草令牌（PAT）：签发后完整令牌仅显示一次，平台侧只存散列。"
        "当前登录用户 {}</p>".format(html.escape(user))
        + "<section class=\"card\"><h2>生成新 PAT</h2>"
        "<form method=\"post\" action=\"/settings/pats\">"
        "<label>执行者标签（必填，归属落账用）：<input name=\"label\" required></label>"
        "<fieldset><legend>scope（仅需求起草三动词可勾）</legend>"
        + scope_inputs
        + "<label><input type=\"checkbox\" value=\"message:send\" disabled> message:send（预留项，不可勾选提交）</label>"
        "</fieldset>"
        "<label>有效期：<select name=\"expires_in\">"
        "<option value=\"24h\">24 小时</option>"
        "<option value=\"7d\">7 天</option>"
        "<option value=\"30d\">30 天</option>"
        "</select></label>"
        "<label>所属项目（必选）：<select name=\"project\" required>"
        + project_options
        + "</select></label>"
        "<button class=\"btn\" type=\"submit\">签发 PAT</button>"
        "</form></section>"
        "<section class=\"card\"><h2>我的 PAT</h2>" + pat_table + "</section>"
        "<section class=\"card\"><h2>凭证模型不变量</h2>"
        "<p class=\"muted\">PAT 是签发人授权给执行者标签的项目级能力凭证（actor=签发人，"
        "on_behalf_of=执行者标签，project_id=绑定项目）；跨项目使用一律 403 "
        "authz.project_denied，同项目无该操作权限 403 authz.pat_scope_denied；"
        "confirm / 签署类闸门（条目确认、freeze 签署等）只认本人登录（交互会话），"
        "PAT、sigt 等任何凭证都不可代过；需求起草动词（create / update / submit）"
        "仅接受 Bearer PAT 调用；服务端 Git 凭据（state/code 两仓独立配置）走项目"
        "配置 API，秘密只写不回显、不进状态仓。</p></section>"
    )
    return _page("signet 用户设置", user, "settings", body, project_ctx=project_ctx)


def render_pat_issued(user, record, token, project_ctx=None):
    """签发结果页：完整令牌仅此页显示一次。"""
    body = (
        "<h1>PAT 签发成功</h1>"
        "<p class=\"muted\">完整令牌仅本次显示一次，请立即复制保存；平台侧只存散列，遗失只能重新签发。</p>"
        "<section class=\"card\"><p>标签：{}　项目：<code>{}</code>　scope：{}　执行者：{}　过期：{}</p>"
        "<pre>{}</pre>"
        "<p><a href=\"/settings\">返回用户设置</a></p></section>"
    ).format(
        html.escape(str(record.get("label") or "-")),
        html.escape(str(record.get("project_id") or "-")),
        html.escape(", ".join(record.get("scope") or [])),
        html.escape(str(record.get("obo") or "-")),
        html.escape(str(record.get("expires_at") or "-")),
        html.escape(token),
    )
    return _page("signet PAT 已签发", user, "settings", body, project_ctx=project_ctx)


def render_projects(user, projects, error=None, project_ctx=None):
    """T17 /projects 页：注册卡（FORM-01 五控件：name/state_repo/code_repo 三必选
    text + state_credential/code_credential 两可选只写 password，页面字段映射 API
    credentials.state/code 秘密）+ 项目表格（空态「尚未注册任何项目」在场）。"""
    error_block = (
        "<p class=\"error\">注册失败：{}</p>".format(html.escape(str(error)))
    ) if error else ""
    rows = "".join(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td>"
        "<td><code>{}</code></td><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("name") or "")),
            html.escape(str(item.get("project_id") or "")),
            _req_status_badge(item.get("status")),
            html.escape(str(item.get("state_repo") or "")),
            html.escape(str(item.get("code_repo") or "")),
            html.escape(str(item.get("owner") or "-")),
            html.escape(str(item.get("created_at") or "-")),
            _project_ops_cell(item),
        )
        for item in (projects or [])
    )
    table = (
        "<table><tr><th>name</th><th>project_id</th><th>状态</th><th>状态仓</th><th>代码仓</th>"
        "<th>owner</th><th>创建时间</th><th>操作</th></tr>{}</table>"
    ).format(rows or "<tr><td colspan=\"8\">（尚未注册任何项目）</td></tr>")
    body = (
        "<h1>项目</h1>"
        "<p class=\"muted\">注册真实软件项目（显式绑定一个状态仓 + 一个代码仓）；平台为每个项目维护"
        "独立服务端状态仓克隆，project_id 成为需求/任务/规格/事件/凭据的路由与隔离边界。"
        "当前登录用户 {}</p>".format(html.escape(user))
        + error_block
        + "<section class=\"card\"><h2>注册新项目</h2>"
        "<form method=\"post\" action=\"/projects\">"
        "<label>项目名称：<input name=\"name\" required></label>"
        "<label>状态仓远端地址：<input name=\"state_repo\" required></label>"
        "<label>代码仓远端地址：<input name=\"code_repo\" required></label>"
        "<label>状态仓凭据（可选，密码只写不回显）：<input type=\"password\" name=\"state_credential\"></label>"
        "<label>代码仓凭据（可选，密码只写不回显）：<input type=\"password\" name=\"code_credential\"></label>"
        "<button class=\"btn\" type=\"submit\">注册项目</button>"
        "<p class=\"hint\">远端仓 URL 形态（^[a-z][a-z0-9+.-]*:// 或 scp-like git@host:path）由人事先创建；"
        "平台不代建远端仓，代码仓全程零写。</p>"
        "</form></section>"
        "<section class=\"card\"><h2>项目列表</h2>" + table + "</section>"
    )
    return _page("signet 项目", user, "projects", body, project_ctx=project_ctx)


def _project_ops_cell(item):
    """项目表格操作位：切换（button-only 表单）+ 配置链接 + 停用/启用（button-only）。"""
    pid = html.escape(str(item.get("project_id") or ""))
    ops = [
        '<form method="post" action="/projects/{}/switch" class="switch-form">'
        '<button class="btn" type="submit">切换</button></form>'.format(pid),
        '<a href="/projects/{}">配置</a>'.format(pid),
    ]
    if item.get("builtin"):
        ops.append('<span class="muted">内置</span>')
    elif item.get("status") == "disabled":
        ops.append(
            '<form method="post" action="/projects/{}/enable" class="switch-form">'
            '<button class="btn" type="submit">启用</button></form>'.format(pid)
        )
    else:
        ops.append(
            '<form method="post" action="/projects/{}/disable" class="switch-form">'
            '<button class="btn" type="submit">停用</button></form>'.format(pid)
        )
    return " ".join(ops)


def render_project_detail(user, record, credentials, events, notice=None, project_ctx=None):
    """T17 /projects/{id} 页：元信息卡/配置表单（name 字段）/凭据区（API PAT 样例
    含 project_id 与 scope 列；服务端 Git 凭据区仅 method 元数据、无秘密）/
    停用|启用按钮/最近事件。"""
    pid = html.escape(str(record.get("project_id") or ""))
    notice_block = "<p class=\"notice\">{}</p>".format(html.escape(str(notice))) if notice else ""
    credential_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("kind") or ("git_credential" if item.get("repo") else "-"))),
            html.escape(str(item.get("pat_id") or "-")),
            html.escape(str(item.get("repo") or "-")),
            html.escape(str(item.get("method") or ", ".join(item.get("scope") or []) or "-")),
            html.escape(str(item.get("status") or "-")),
            html.escape(str(item.get("updated_at") or item.get("expires_at") or "-")),
        )
        for item in (credentials or [])
    )
    credential_table = (
        "<table><tr><th>类型</th><th>pat_id</th><th>repo</th><th>method/scope</th><th>状态</th>"
        "<th>过期/更新</th></tr>{}</table>"
    ).format(credential_rows or "<tr><td colspan=\"6\">（本项目暂无凭据记录）</td></tr>")
    event_items = "".join(
        "<li><code>{}</code>　{}（actor：{}）</li>".format(
            html.escape(str(event.get("event_type") or "-")),
            html.escape(str(event.get("timestamp") or "-")),
            html.escape(str(event.get("actor") or "-")),
        )
        for event in (events or [])
    ) or "<li>无</li>"
    if record.get("builtin"):
        lifecycle = '<p class="muted">内置项目（平台自身）：不可停用、不可改绑（409 project.builtin_protected）。</p>'
    elif record.get("status") == "disabled":
        lifecycle = (
            '<form method="post" action="/projects/{}/enable" class="switch-form">'
            '<button class="btn" type="submit">启用</button></form>'.format(pid)
        )
    else:
        lifecycle = (
            '<form method="post" action="/projects/{}/disable" class="switch-form">'
            '<button class="btn" type="submit">停用</button></form>'.format(pid)
        )
    body = (
        "<h1>项目详情 <code>{}</code></h1>".format(pid)
        + notice_block
        + "<section class=\"card\"><h2>项目信息</h2>"
        "<p>名称：{}　状态：{}　内置：{}　owner：{}　创建：{}</p>"
        "<p>状态仓：<code>{}</code></p><p>代码仓：<code>{}</code></p></section>".format(
            html.escape(str(record.get("name") or "")),
            _req_status_badge(record.get("status")),
            "是" if record.get("builtin") else "否",
            html.escape(str(record.get("owner") or "-")),
            html.escape(str(record.get("created_at") or "-")),
            html.escape(str(record.get("state_repo") or "")),
            html.escape(str(record.get("code_repo") or "")),
        )
        + "<section class=\"card\"><h2>配置</h2>"
        + ('<p class="muted">内置项目不可配置。</p>' if record.get("builtin") else
           "<form method=\"post\" action=\"/projects/{}/configure\">".format(pid)
           + "<label>项目名称：<input name=\"name\" value=\"{}\"></label>".format(
               html.escape(str(record.get("name") or "")))
           + "<button class=\"btn\" type=\"submit\">保存配置</button>"
           + "<p class=\"hint\">首版仅支持改名；state_repo/code_repo 改绑一律拒绝"
             "（400 project.repo_immutable）。服务端 Git 凭据经 API 配置（POST "
             "/api/projects/{0}/configure 携 credentials、凭据删除 POST "
             "/api/projects/{0}/credentials/remove），秘密只写不回显。</p></form>".format(pid))
        + "</section>"
        "<section class=\"card\"><h2>项目凭据</h2>" + credential_table
        + "<p class=\"muted\">凭据区仅列元数据：API PAT 不含令牌明文（平台侧只存散列）；"
          "服务端 Git 凭据只列 method，秘密存服务端数据目录、不进状态仓。</p></section>"
        "<section class=\"card\"><h2>生命周期</h2>" + lifecycle + "</section>"
        "<section class=\"card\"><h2>最近事件</h2><ul class=\"feed\">" + event_items + "</ul></section>"
    )
    return _page("signet 项目详情", user, "projects", body, project_ctx=project_ctx)
