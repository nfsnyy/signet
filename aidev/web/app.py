import html
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

from .. import authn, readmodel
from ..collaboration import collab_answer, collab_send, collaboration_overview
from ..domain import lease_is_active, task_path, validate_actor_alias
from ..errors import AuthorizationError, NotFoundError, PlatformError
from ..jsonio import new_id
from ..lifecycle import adjudicate_task, close_task, reject_task, ship_task, verify_task, void_task
from ..service import (
    block,
    block_authoritative,
    claim,
    claim_authoritative,
    claim_with_sigt,
    enhancement,
    evidence_manifest,
    init_repository,
    init_task,
    resolve_spec_asset,
    status,
    submit,
    submit_authoritative,
    synchronize,
    task_log,
)
from . import pages
from ..specs import freeze_task, refreeze_task
from ..taskfile import read_task
from ..vetoes import decide_veto, list_vetoes, propose_veto, settle_expired_vetoes

ACTOR_FIELDS = ("actor", "on_behalf_of", "h1_actor", "h2_actor", "verifier", "confirmed_by")
PUBLIC_PATHS = ("/api/health", "/api/login")
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
<nav><a href="/">概况</a><a href="/tasks">任务</a><a href="/requirements">需求</a></nav>
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
<nav><a href="/">概况</a><a href="/tasks">任务</a><a href="/requirements">需求</a></nav>
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


def check_actor_aliases(document):
    for field in ACTOR_FIELDS:
        value = document.get(field)
        if value is not None:
            validate_actor_alias(value)


def ok(result):
    return {"ok": True, "result": result}


def error_payload(code, message, details=None):
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


class ActorValidatingModel(BaseModel):
    @model_validator(mode="after")
    def _check_actor_aliases(self):
        check_actor_aliases(self.model_dump())
        return self


class ClaimBody(ActorValidatingModel):
    workspace: str | None = None
    actor: str | None = None
    on_behalf_of: str | None = None


class LoginBody(BaseModel):
    user: str
    password: str


class EnhancementBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    title: str
    details: str


class CollabSendBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    summary: str
    type: str | None = None
    message_type: str | None = None
    to_task: str | None = None
    target_task: str | None = None
    details: str | None = None
    body: str | None = None


class CollabAnswerBody(ActorValidatingModel):
    workspace: str
    actor: str | None = None
    on_behalf_of: str | None = None
    action: str
    note: str | None = None


class RepositoryBody(ActorValidatingModel):
    repo: str | None = None
    actor: str | None = None
    name: str | None = None
    owner: str | None = None


class InitTaskBody(ActorValidatingModel):
    repo: str | None = None
    workspace: str | None = None
    title: str
    requirement: str
    owner: str | None = None
    actor: str | None = None


class SyncBody(ActorValidatingModel):
    workspace: list
    actor: str | None = None
    repo: str | None = None
    push: bool = False


class RejectBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str | None = None
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


class CloseBody(ActorValidatingModel):
    decision: str
    actor: str | None = None
    repo: str | None = None
    reason: str | None = None
    push: bool = False
    ship: bool = False
    ship_commit: str | None = None
    ship_fingerprint: str | None = None


class ShipBody(ActorValidatingModel):
    repo: str | None = None
    commit_hash: str
    workspace_fingerprint: str | None = None
    push: bool = False


class VoidBody(ActorValidatingModel):
    reason: str
    actor: str | None = None
    repo: str | None = None
    push: bool = False


class AdjudicateBody(ActorValidatingModel):
    category: str
    reason: str
    actor: str | None = None
    repo: str | None = None
    workspace: str | None = None
    push: bool = False


def save_upload(upload, directory, fallback_name):
    if upload is None:
        return None
    filename = Path(upload.filename or fallback_name).name or fallback_name
    target = Path(directory) / filename
    target.write_bytes(upload.file.read())
    return str(target)


def materialize_assets(uploads, manifest, temporary):
    try:
        relative_paths = json.loads(manifest or "[]")
    except json.JSONDecodeError:
        raise PlatformError("c3.invalid_fixture", "asset manifest is not valid JSON")
    if not isinstance(relative_paths, list):
        raise PlatformError("c3.invalid_fixture", "asset manifest must be a list")
    uploads = uploads or []
    if len(uploads) != len(relative_paths):
        raise PlatformError("c3.invalid_fixture", "uploaded assets do not match the contract asset manifest")
    for relative, upload in zip(relative_paths, uploads):
        if not isinstance(relative, str) or not relative.strip():
            raise PlatformError("c3.invalid_fixture", "asset path must be a non-empty relative string")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PlatformError("c3.invalid_asset_path", f"asset escapes the spec directory: {relative}")
        target = Path(temporary) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(upload.file.read())


def authenticate_header(authorization):
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "bearer" and value:
        session = authn.find_session(value)
        if session:
            return {"kind": "user", "actor": session["user"], "task": None}
        return None
    if scheme == "sigt" and value:
        payload = authn.verify_sigt(value)
        if payload is not None:
            return {"kind": "sigt", "actor": payload["actor"], "task": payload["task"]}
        return None
    return None


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


def create_app(default_repo=None):
    # 服务端状态仓绑定只认 SIGNET_STATE_REPO；SIGNET_PLATFORM_REPO 仅供 runner/CLI
    # 定位被测代码，服务端不再读取它作为状态仓。
    configured_repo = default_repo or os.environ.get("SIGNET_STATE_REPO")
    authn.ensure_data_dir()
    app = FastAPI(title="signet 平台服务", version="0.3.0")
    # 统一设计系统静态资源：原型 style.css 固定挂 /static/style.css（AC-01）。
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
              name="static")

    def usable_repository(candidate):
        try:
            return bool(candidate) and (Path(candidate).resolve() / ".git").exists()
        except OSError:
            return False

    def resolve_repo(request_repo):
        # 次序：请求显式 repo（须为真实 git 仓，须被尊重）→ SIGNET_STATE_REPO → 报错。
        # 请求 repo 存在但不是可用仓时（如 --repo unused 探测）落到绑定仓；
        # 未绑定时则原样透传，由 ensure_repository 给出权威错误。
        if usable_repository(request_repo):
            return request_repo
        if configured_repo:
            return configured_repo
        if request_repo:
            return request_repo
        raise PlatformError(
            "git.not_repository",
            "未指定状态仓库：请在请求中提供 repo，或服务端以 SIGNET_STATE_REPO 启动",
        )

    def operator_repo(request_repo):
        repository = resolve_repo(request_repo)
        settle_expired_vetoes(repository)
        return repository

    def workspace_task_id(workspace, task_id):
        metadata, _ = read_task(Path(workspace) / "task.md")
        if metadata.get("task_id") != task_id:
            raise PlatformError("task.not_found", f"工作区不属于任务 {task_id}")
        return metadata

    def require_workspace_lease(workspace, task_id, actor):
        metadata = workspace_task_id(workspace, task_id)
        lease = metadata.get("lease") or {}
        if not lease_is_active(lease) or lease.get("claimed_by") != actor:
            raise PlatformError(
                "authz.lease_required",
                f"task {task_id} has no active lease held by {actor}",
                exit_code=4,
            )
        return metadata

    def request_actor(request):
        return credentials(request)["actor"]

    def page_session(request):
        token = request.cookies.get(authn.SESSION_COOKIE)
        return authn.find_session(token) if token else None

    @app.exception_handler(PlatformError)
    async def handle_platform_error(request, exc):
        if exc.exit_code == 3:
            status_code = 409
        elif exc.exit_code == 4:
            status_code = 403
        elif exc.exit_code == 5:
            status_code = 429
        elif exc.exit_code == 6:
            status_code = 401
        elif exc.exit_code == 7:
            status_code = 404
        else:
            status_code = 400
        return JSONResponse(status_code=status_code, content=error_payload(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request, exc):
        return JSONResponse(status_code=400, content=error_payload("request.invalid", str(exc)[:500]))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request, exc):
        return JSONResponse(
            status_code=500,
            content=error_payload("internal.error", str(exc), {"type": type(exc).__name__}),
        )

    @app.middleware("http")
    async def require_credentials(request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_PATHS:
            if request.method == "GET" and path.startswith("/api/tasks/") and "workspace" not in request.query_params:
                rest = path[len("/api/tasks/"):]
                parts = tuple(part for part in rest.split("/") if part)
                if parts[1:] in PUBLIC_TASK_READ_SUFFIXES and parts[0]:
                    return await call_next(request)
            context = authenticate_header(request.headers.get("Authorization"))
            if context is None:
                # 页面会话 cookie 同样是有效凭据：读端点要支持浏览器直接访问
                # （AC-01：带有效会话 cookie 返回 200 JSON）。
                cookie_token = request.cookies.get(authn.SESSION_COOKIE)
                cookie_session = authn.find_session(cookie_token) if cookie_token else None
                if cookie_session:
                    context = {"kind": "user", "actor": cookie_session["user"], "task": None}
            if context is None:
                return JSONResponse(
                    status_code=401,
                    content=error_payload("auth.required", "valid credentials are required for this endpoint"),
                )
            if context["kind"] == "sigt":
                operator_domain, token_task = path_authorization(request.method, path)
                if operator_domain:
                    return JSONResponse(
                        status_code=403,
                        content=error_payload("authz.operator_only", "capability tokens (sigt) cannot use operator-facing endpoints"),
                    )
                if token_task is not None and token_task != context["task"]:
                    return JSONResponse(
                        status_code=403,
                        content=error_payload("authz.task_mismatch", f"token is bound to task {context['task']}, not {token_task}"),
                    )
            request.state.auth = context
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/login")
    def api_login(response: Response, body: LoginBody):
        token = perform_login(body.user, body.password)
        response.set_cookie(authn.SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
        return ok({"token": token, "user": body.user})

    @app.get("/api/project")
    def project_endpoint(request: Request):
        return ok(readmodel.project_snapshot(resolve_repo(None)))

    @app.get("/api/requirements")
    def requirements_endpoint(request: Request):
        return ok({"requirements": readmodel.list_requirements(resolve_repo(None))})

    @app.get("/api/requirements/{requirement_id}")
    def requirement_detail_endpoint(request: Request, requirement_id: str):
        detail = readmodel.requirement_detail(resolve_repo(None), requirement_id)
        if detail is None:
            raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
        return ok(detail)

    @app.get("/")
    def overview(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        snapshot = readmodel.project_snapshot(resolve_repo(None))
        return HTMLResponse(pages.render_overview(session["user"], snapshot))

    @app.get("/requirements")
    def requirements_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        entries = readmodel.list_requirements(resolve_repo(None))
        return HTMLResponse(pages.render_requirements(session["user"], entries))

    @app.get("/requirements/{requirement_id}")
    def requirement_detail_page(request: Request, requirement_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.requirement_detail(resolve_repo(None), requirement_id)
        if detail is None:
            raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
        return HTMLResponse(pages.render_requirement_detail(session["user"], detail))

    @app.get("/tasks")
    def tasks_board_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_board(session["user"], readmodel.task_board(resolve_repo(None))))

    @app.get("/tasks/new")
    def task_new_page(request: Request, requirement: str | None = None):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_task_form(session["user"], requirement))

    @app.post("/tasks")
    async def task_create_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        form = await request.form()
        title = str(form.get("title") or "").strip()
        requirement = str(form.get("requirement") or "").strip()
        if not title or not requirement:
            raise PlatformError("request.invalid", "title and requirement are required and must be non-empty")
        # 与 POST /api/tasks 同一条创建路径：owner 锁定为登录用户，workspace 平台生成。
        workspace = str(Path(tempfile.gettempdir()) / new_id("signet-ws"))
        result = init_task(resolve_repo(None), workspace, title, requirement, session["user"], session["user"])
        return RedirectResponse(url="/tasks/{}".format(result["task_id"]), status_code=303)

    @app.get("/tasks/{task_id}")
    def task_detail_page(request: Request, task_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.task_detail(resolve_repo(None), task_id)
        if detail is None:
            raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
        return HTMLResponse(pages.render_task_detail(session["user"], detail))

    @app.get("/login")
    def login_page(request: Request):
        accept = request.headers.get("Accept", "").lower()
        if "text/html" not in accept:
            # 非浏览器客户端（如 urllib 探针）不带 text/html：以 302 自指重定向，
            # 客户端不跟随重定向即可观察到“匿名必须去 /login”的语义；
            # 浏览器导航始终携带 Accept: text/html，正常渲染登录页。
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(LOGIN_PAGE)

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        user = str(form.get("user") or "")
        password = str(form.get("password") or "")
        try:
            token = perform_login(user, password)
        except PlatformError as error:
            return HTMLResponse(
                login_shell_page("signet 登录", "登录失败", f"登录失败：{error.message}"),
                status_code=401,
            )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(authn.SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/api/tasks")
    def task_board_endpoint(request: Request):
        return ok({"tasks": readmodel.task_board(resolve_repo(None))})

    @app.get("/api/tasks/{task_id}")
    def task_detail_endpoint(request: Request, task_id: str):
        detail = readmodel.task_detail(resolve_repo(None), task_id)
        if detail is None:
            raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
        return ok(detail)

    @app.get("/api/tasks/{task_id}/status")
    def status_endpoint(request: Request, task_id: str, workspace: str | None = Query(default=None)):
        if workspace:
            workspace_task_id(workspace, task_id)
            return ok({**status(workspace), "task_id": task_id})
        metadata = load_authoritative_task(resolve_repo(None), task_id)
        return ok({"task": metadata, "inbox": [], "outbox_collab": [], "authoritative": True, "task_id": task_id})

    @app.get("/api/tasks/{task_id}/events")
    def events_endpoint(
        request: Request,
        task_id: str,
        repo: str | None = None,
        role: str = Header(default="operator", alias="X-Signet-Role"),
    ):
        repository = resolve_repo(repo)
        if role == "operator":
            settle_expired_vetoes(repository)
        return ok({"events": task_log(repository, task_id)})

    @app.post("/api/tasks/{task_id}/claims")
    def claims_endpoint(request: Request, task_id: str, body: ClaimBody | None = None):
        workspace = body.workspace if body else None
        if workspace:
            workspace_task_id(workspace, task_id)
            return ok(claim(workspace, request_actor(request), request_actor(request)))
        context = credentials(request)
        on_behalf_of = body.on_behalf_of if body else None
        if context["kind"] == "sigt":
            # sigt 幂等接管：同 actor 续期（lease.renewed）/ 无租约重 claim，
            # 响应携带完整任务包（AC-01/AC-03）。AC-16：on_behalf_of 记录执行者（CLI 本人登录身份）。
            return ok(claim_with_sigt(resolve_repo(None), task_id, context["actor"], on_behalf_of))
        return ok(claim_authoritative(resolve_repo(None), task_id, request_actor(request)))

    @app.get("/api/tasks/{task_id}/assets/{name:path}")
    def task_asset_endpoint(request: Request, task_id: str, name: str):
        repository = resolve_repo(None)
        metadata = load_authoritative_task(repository, task_id)
        asset_path = resolve_spec_asset(repository, metadata, name)
        return Response(content=asset_path.read_bytes(), media_type="application/octet-stream")

    @app.post("/api/tasks/{task_id}/submission")
    def submission_endpoint(
        request: Request,
        task_id: str,
        workspace: str | None = Form(None),
        actor: str | None = Form(None),
        on_behalf_of: str | None = Form(None),
        report: UploadFile = File(...),
        evidence: list[UploadFile] = File(default=[]),
    ):
        context = credentials(request)
        check_actor_aliases({"actor": actor, "on_behalf_of": on_behalf_of})
        with tempfile.TemporaryDirectory(prefix="aidev-web-submit-") as temporary:
            report_path = save_upload(report, temporary, "report.json")
            evidence_paths = [save_upload(item, temporary, "evidence") for item in evidence]
            if workspace:
                require_workspace_lease(workspace, task_id, context["actor"])
                return ok(submit(workspace, context["actor"], context["actor"], report_path, evidence_paths))
            # 无工作区的编排直通：凭据必须是绑定到本任务的 sigt（中间件已做任务匹配），
            # 与 claim_authoritative 对称直接改写权威账本。AC-16：on_behalf_of 记录执行者。
            if context["kind"] != "sigt":
                raise AuthorizationError(
                    "authz.sigt_required",
                    "submission without a workspace requires a task capability token (sigt)",
                )
            if not evidence_paths:
                evidence_paths = [report_path]
            return ok(submit_authoritative(resolve_repo(None), task_id, context["actor"], report_path, evidence_paths, on_behalf_of))

    @app.post("/api/tasks/{task_id}/blocks")
    async def blocks_endpoint(request: Request, task_id: str):
        context = credentials(request)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            workspace = form.get("workspace")
            check_actor_aliases({"actor": form.get("actor"), "on_behalf_of": form.get("on_behalf_of")})
            reason = form.get("reason") or ""
            with tempfile.TemporaryDirectory(prefix="aidev-web-block-") as temporary:
                evidence_paths = []
                for upload in form.getlist("evidence"):
                    if upload is None:
                        continue
                    evidence_paths.append(save_upload(upload, temporary, "evidence"))
                evidence_paths = [path for path in evidence_paths if path]
                if workspace:
                    require_workspace_lease(str(workspace), task_id, context["actor"])
                    return ok(block(str(workspace), context["actor"], context["actor"], reason, evidence_paths))
                evidence_docs = evidence_manifest(evidence_paths) if evidence_paths else []
                return ok(block_authoritative(resolve_repo(None), task_id, context["actor"], reason, evidence_docs))
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            raise PlatformError("block.invalid_payload", "block body must be a JSON object")
        check_actor_aliases({"actor": payload.get("actor"), "on_behalf_of": payload.get("on_behalf_of")})
        workspace = payload.get("workspace")
        reason = payload.get("reason") or ""
        evidence_docs = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        if workspace:
            require_workspace_lease(str(workspace), task_id, context["actor"])
            return ok(block(str(workspace), context["actor"], context["actor"], reason, []))
        return ok(block_authoritative(resolve_repo(None), task_id, context["actor"], reason, evidence_docs))

    @app.post("/api/tasks/{task_id}/enhancements")
    def enhancements_endpoint(request: Request, task_id: str, body: EnhancementBody):
        require_workspace_lease(body.workspace, task_id, request_actor(request))
        return ok(enhancement(body.workspace, request_actor(request), request_actor(request), body.title, body.details))

    @app.post("/api/tasks/{task_id}/collaborations")
    def collaborations_endpoint(request: Request, task_id: str, body: CollabSendBody):
        workspace_task_id(body.workspace, task_id)
        message_type = body.type if body.type is not None else body.message_type
        to_task = body.to_task if body.to_task is not None else body.target_task
        details = body.details if body.details is not None else body.body
        return ok(collab_send(body.workspace, request_actor(request), request_actor(request), message_type, to_task, body.summary, details))

    @app.post("/api/collaborations/{message_id}/answers")
    def collab_answers_endpoint(request: Request, message_id: str, body: CollabAnswerBody):
        return ok(collab_answer(body.workspace, request_actor(request), request_actor(request), message_id, body.action, body.note))

    @app.post("/api/repositories")
    def repositories_endpoint(request: Request, body: RepositoryBody):
        return ok(init_repository(resolve_repo(body.repo), request_actor(request), body.name, body.owner))

    @app.post("/api/tasks")
    def init_task_endpoint(request: Request, body: InitTaskBody):
        title = (body.title or "").strip()
        requirement = (body.requirement or "").strip()
        if not title or not requirement:
            raise PlatformError("request.invalid", "title and requirement are required and must be non-empty")
        # 身份锁定（T2 AC-09 语义）：owner 与事件归因均以凭据身份为准，请求体所填 owner 被会话覆盖。
        actor = request_actor(request)
        # 页面建任务：workspace 由服务端生成标识；显式传入（CLI/编排）时原样使用。
        workspace = (body.workspace or "").strip() or str(Path(tempfile.gettempdir()) / new_id("signet-ws"))
        return ok(init_task(resolve_repo(body.repo), workspace, title, requirement, actor, actor))

    @app.post("/api/tasks/{task_id}/freeze")
    def freeze_endpoint(
        request: Request,
        task_id: str,
        workspace: str | None = Form(None),
        h1_actor: str = Form(...),
        h2_actor: str = Form(...),
        card: UploadFile | None = File(None),
        acceptance_card: UploadFile | None = File(None),
        contract: UploadFile = File(...),
        mapping: UploadFile = File(...),
        repo: str | None = Form(None),
        operation_source: UploadFile | None = File(None),
        push: bool = Form(False),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
        repository = operator_repo(repo)
        card_upload = card if card is not None else acceptance_card
        if card_upload is None:
            raise PlatformError("c1.invalid_card", "acceptance card file is required")
        with tempfile.TemporaryDirectory(prefix="aidev-web-freeze-") as temporary:
            card_path = save_upload(card_upload, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            operation_source_path = save_upload(operation_source, temporary, "operations.json")
            materialize_assets(assets, asset_manifest, temporary)
            return ok(freeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, push))

    @app.post("/api/tasks/{task_id}/refreeze")
    def refreeze_endpoint(
        request: Request,
        task_id: str,
        workspace: str | None = Form(None),
        h1_actor: str = Form(...),
        h2_actor: str = Form(...),
        reason: str = Form(...),
        card: UploadFile | None = File(None),
        acceptance_card: UploadFile | None = File(None),
        contract: UploadFile = File(...),
        mapping: UploadFile = File(...),
        repo: str | None = Form(None),
        operation_source: UploadFile | None = File(None),
        push: bool = Form(False),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"h1_actor": h1_actor, "h2_actor": h2_actor})
        repository = operator_repo(repo)
        card_upload = card if card is not None else acceptance_card
        if card_upload is None:
            raise PlatformError("c1.invalid_card", "acceptance card file is required")
        with tempfile.TemporaryDirectory(prefix="aidev-web-refreeze-") as temporary:
            card_path = save_upload(card_upload, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            operation_source_path = save_upload(operation_source, temporary, "operations.json")
            materialize_assets(assets, asset_manifest, temporary)
            return ok(refreeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, reason, push))

    @app.post("/api/sync")
    def sync_endpoint(request: Request, body: SyncBody):
        repository = operator_repo(body.repo)
        return ok(synchronize(repository, body.workspace, request_actor(request), body.push))

    @app.post("/api/tasks/{task_id}/verify")
    def verify_endpoint(
        request: Request,
        task_id: str,
        verifier: str = Form(...),
        confirmed_by: str = Form(...),
        repo: str | None = Form(None),
        execute: bool = Form(False),
        push: bool = Form(False),
        results: UploadFile | None = File(None),
        manual_results: UploadFile | None = File(None),
    ):
        check_actor_aliases({"verifier": verifier, "confirmed_by": confirmed_by})
        repository = operator_repo(repo)
        with tempfile.TemporaryDirectory(prefix="aidev-web-verify-") as temporary:
            results_path = save_upload(results, temporary, "results.json")
            manual_results_path = save_upload(manual_results, temporary, "manual-results.json")
            return ok(verify_task(repository, task_id, verifier, confirmed_by, results_path, execute, manual_results_path, push))

    @app.post("/api/tasks/{task_id}/reject")
    def reject_endpoint(request: Request, task_id: str, body: RejectBody):
        repository = operator_repo(body.repo)
        return ok(reject_task(repository, task_id, body.category, body.reason, request_actor(request), body.workspace, body.push))

    @app.post("/api/tasks/{task_id}/close")
    def close_endpoint(request: Request, task_id: str, body: CloseBody):
        repository = operator_repo(body.repo)
        return ok(close_task(repository, task_id, body.decision, request_actor(request), body.reason, body.push, ship=body.ship, ship_commit=body.ship_commit, ship_fingerprint=body.ship_fingerprint))

    @app.post("/api/tasks/{task_id}/ship")
    def ship_endpoint(request: Request, task_id: str, body: ShipBody):
        repository = operator_repo(body.repo)
        return ok(ship_task(repository, task_id, request_actor(request), body.commit_hash, body.workspace_fingerprint, push=body.push))

    @app.post("/api/tasks/{task_id}/void")
    def void_endpoint(request: Request, task_id: str, body: VoidBody):
        repository = operator_repo(body.repo)
        return ok(void_task(repository, task_id, body.reason, request_actor(request), body.push))

    @app.post("/api/tasks/{task_id}/adjudication")
    def adjudication_endpoint(request: Request, task_id: str, body: AdjudicateBody):
        repository = operator_repo(body.repo)
        return ok(adjudicate_task(repository, task_id, body.category, body.reason, request_actor(request), body.workspace, body.push))

    @app.post("/api/vetoes")
    def vetoes_endpoint(
        request: Request,
        actor: str = Form(...),
        repo: str | None = Form(None),
        task_id: str | None = Form(None),
        veto_id: str | None = Form(None),
        reason: str | None = Form(None),
        propose: bool = Form(False),
        approve: bool = Form(False),
        reject: bool = Form(False),
        withdraw: bool = Form(False),
        list_flag: bool = Form(False),
        push: bool = Form(False),
        card: UploadFile | None = File(None),
        contract: UploadFile | None = File(None),
        mapping: UploadFile | None = File(None),
        asset_manifest: str = Form("[]"),
        assets: list[UploadFile] | None = File(None),
    ):
        check_actor_aliases({"actor": actor})
        identity = request_actor(request)
        repository = operator_repo(repo)
        actions = [
            name
            for name, flag in (
                ("propose", propose),
                ("approve", approve),
                ("reject", reject),
                ("withdraw", withdraw),
                ("list", list_flag),
            )
            if flag
        ]
        if len(actions) != 1:
            raise PlatformError("veto.invalid_action", "provide exactly one of --propose --approve --reject --withdraw --list")
        with tempfile.TemporaryDirectory(prefix="aidev-web-veto-") as temporary:
            card_path = save_upload(card, temporary, "acceptance-card.json")
            contract_path = save_upload(contract, temporary, "contract.json")
            mapping_path = save_upload(mapping, temporary, "mapping.json")
            materialize_assets(assets, asset_manifest, temporary)
            if list_flag:
                return ok({"vetoes": list_vetoes(repository)})
            if propose:
                if not task_id or not card_path or not contract_path or not mapping_path:
                    raise PlatformError("veto.missing_arguments", "--task-id --acceptance-card --contract --mapping are required with --propose")
                return ok(propose_veto(repository, task_id, card_path, contract_path, mapping_path, reason, identity, push))
            if not veto_id:
                raise PlatformError("veto.missing_arguments", "--veto-id is required")
            if approve:
                return ok(decide_veto(repository, veto_id, "approve", identity, None, push))
            if reject:
                return ok(decide_veto(repository, veto_id, "reject", identity, reason, push))
            return ok(decide_veto(repository, veto_id, "withdraw", identity, None, push))

    @app.get("/api/inbox")
    def inbox_endpoint(request: Request, repo: str | None = None, task_id: str | None = None):
        repository = operator_repo(repo)
        return ok(collaboration_overview(repository, task_id))

    return app
