import html

from .. import authn
from ..domain import task_path
from ..errors import PlatformError
from ..taskfile import read_task

PUBLIC_PATHS = ("/api/health", "/api/login", "/api/skill", "/api/norms", "/guide")
# T15（AC-09）：公开面仅增 /api/skill、/api/norms 前缀与页面 /guide，其余端点匿名行为不变。
PUBLIC_PATH_PREFIXES = ("/api/norms",)
# T6 CRLF 用例固化的读面语义：任务详情与状态端点匿名可读（本机信任模型内），
# 事件/写面仍需凭据；/api/tasks 列表与其余子路径保持守卫。
PUBLIC_TASK_READ_SUFFIXES = {(), ("status",)}
OPERATOR_EXACT_PATHS = {
    ("POST", "/api/repositories"),
    ("POST", "/api/tasks"),
    ("POST", "/api/sync"),
    ("POST", "/api/vetoes"),
    ("GET", "/api/inbox"),
}
OPERATOR_TASK_SUFFIXES = ("/freeze", "/refreeze", "/verify", "/reject", "/close", "/ship", "/void", "/adjudication")


def _is_public_path(path):
    """T15 公开面判定：PUBLIC_PATHS 精确命中，或 PUBLIC_PATH_PREFIXES 前缀命中（/api/norms/*）。"""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix + "/") for prefix in PUBLIC_PATH_PREFIXES)


def path_authorization(method, path):
    """返回 (operator_domain, task_id)：operator_domain 为 True 表示该端点只接受用户令牌。"""
    if path.startswith("/api/tasks/"):
        rest = path[len("/api/tasks/"):]
        task_id, separator, suffix = rest.partition("/")
        if not separator or not task_id:
            return False, None
        if method == "POST" and ("/" + suffix) in OPERATOR_TASK_SUFFIXES:
            return True, task_id
        return False, task_id
    return (method, path) in OPERATOR_EXACT_PATHS, None


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>signet 登录</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body>
<div class="topbar"><span class="brand">signet</span>
<nav><a href="/">概况</a><a href="/tasks">任务</a><a href="/requirements">需求</a><a href="/projects">项目</a><a href="/guide">接入指南</a></nav>
<span class="user">未登录</span></div>
<div class="page" style="display:flex;justify-content:center;padding-top:80px;">
  <form method="post" action="/login" class="card">
    <h1 style="text-align:center;">signet 平台登录</h1>
    <p class="muted">账号由管理员开通，无自助注册；登录后自动进入概况页。</p>
    <label>用户：<input name="user" required autofocus></label>
    <label>口令：<input name="password" type="password" required></label>
    <button class="btn" type="submit">登 录</button>
    <p class="hint">提示：登录凭据由 operator 开通；会话通过 HttpOnly Cookie 维持。</p>
  </form>
</div>
</body>
</html>
"""

_LOGIN_SHELL = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body>
<div class="topbar"><span class="brand">signet</span>
<nav><a href="/">概况</a><a href="/tasks">任务</a><a href="/requirements">需求</a><a href="/projects">项目</a><a href="/guide">接入指南</a></nav>
<span class="user">未登录</span></div>
<div class="page" style="display:flex;justify-content:center;padding-top:80px;">
  <div class="card" style="max-width:520px;border:1px solid #e5e7eb;border-radius:10px;padding:20px;">
    <h1 style="text-align:center;">{heading}</h1>
    <p class="muted">{message}</p>
    <p style="text-align:center;"><a href="/login">返回登录页</a></p>
  </div>
</div>
</body>
</html>
"""


def login_shell_page(title, heading, message):
    """登录周边页（失败提示等）：与登录页同一居中卡片版式，同样引用样式。"""
    return _LOGIN_SHELL.format(
        title=title, heading=heading,
        message=html.escape(str(message)),
    )


def ok(result):
    return {"ok": True, "result": result}


def error_payload(code, message, details=None):
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


def authenticate_header(authorization):
    """凭据解析：Bearer 会话 / Bearer PAT / sigt。

    返回 (context, error)：error 为 (status_code, payload)（PAT 过期/吊销的
    专属 401）或 None。Bearer 值带 pat- 前缀时按 PAT 校验（签名 → 过期 →
    吊销），与 sigt 同构的能力令牌面。context 附令牌 repo/项目绑定字段
    （T17 项目边界裁决）与会话 active_project。
    """
    if not authorization:
        return None, None
    scheme, _, value = authorization.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "bearer" and value:
        if value.startswith(authn.PAT_PREFIX):
            payload = authn.verify_pat(value)
            if payload is None:
                return None, (401, error_payload("auth.required", "PAT signature is invalid"))
            if authn.pat_is_expired(payload):
                return None, (401, error_payload("auth.pat_expired", "PAT has expired"))
            record = authn.find_pat_by_token(value)
            if record is not None and record.get("status") == "revoked":
                return None, (401, error_payload("auth.pat_revoked", "PAT has been revoked"))
            context = {
                "kind": "pat",
                "actor": payload["user"],
                "obo": payload.get("obo") or payload["user"],
                "scope": list(payload.get("scope") or []),
                "token": value,
                "project_id": payload.get("project_id"),
                "via": "bearer",
            }
            return context, None
        session = authn.find_session(value)
        if session:
            return {
                "kind": "user",
                "actor": session["user"],
                "task": None,
                "via": "bearer",
                "active_project": session.get("active_project"),
            }, None
        payload = authn.verify_sigt(value)
        if payload is not None:
            # Bearer 形态的能力令牌：sigt 经 Bearer 传输同样识别（与 sigt scheme 同构），
            # 供任务绑定类读端点按 sigt 任务范围做授权分流。
            return {"kind": "sigt", "actor": payload["actor"], "task": payload["task"], "repo": payload.get("repo"), "via": "sigt"}, None
        return None, None
    if scheme == "sigt" and value:
        payload = authn.verify_sigt(value)
        if payload is not None:
            return {"kind": "sigt", "actor": payload["actor"], "task": payload["task"], "repo": payload.get("repo"), "via": "sigt"}, None
        return None, None
    return None, None


def pat_path_allowed(method, path):
    """PAT 接口隔离（AC-02）：仅需求起草三动词端点可达。

    POST /api/requirements（create）、POST /api/requirements/{id}（update）、
    POST /api/requirements/{id}/submission（submit）；confirm 与其余一律 403。
    """
    if method != "POST":
        return False
    if path == "/api/requirements":
        return True
    if not path.startswith("/api/requirements/"):
        return False
    parts = [part for part in path[len("/api/requirements/"):].split("/") if part]
    return len(parts) == 1 or (len(parts) == 2 and parts[1] == "submission")


def credentials(request):
    return request.state.auth


def load_authoritative_task(repository, task_id):
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    return read_task(path)[0]


def perform_login(user, password):
    if authn.login_throttled(user):
        raise PlatformError(
            "auth.rate_limited",
            "too many consecutive failed logins; the account is temporarily throttled",
            exit_code=5,
            details={"user": user},
        )
    if not authn.authenticate(user, password):
        authn.note_login_failure(user)
        raise PlatformError("auth.invalid_credentials", "unknown user or wrong password", exit_code=6)
    authn.note_login_success(user)
    return authn.create_session(user)
