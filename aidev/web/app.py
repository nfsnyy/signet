import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import authn, readmodel
from .. import projects as project_store
from ..errors import NotFoundError, PlatformError
from ..service import (
    init_task,
    normalize_req_items,
    project_metadata,
    validate_requirement_derive,
)
from . import pages, routes_core, routes_lifecycle, routes_projects, routes_requirements, routes_tasks
from .authnz import (
    LOGIN_PAGE,
    PUBLIC_TASK_READ_SUFFIXES,
    _is_public_path,
    authenticate_header,
    error_payload,
    login_shell_page,
    path_authorization,
    pat_path_allowed,
    perform_login,
)
from .context import (
    AppContext,
    _guard_public_read,
    _issue_pat,
    _known_project_id,
    _page_project_context,
    _pat_project_for_issue,
    _pat_target_project,
    _platform_event,
    _project_recent_events,
    _require_mutable_project,
    _require_project_record,
    page_session,
    resolve_repo,
)


def create_app(default_repo=None):
    # 服务端状态仓绑定只认 SIGNET_STATE_REPO；SIGNET_PLATFORM_REPO 仅供 runner/CLI
    # 定位被测代码，服务端不再读取它作为状态仓。
    configured_repo = default_repo or os.environ.get("SIGNET_STATE_REPO")
    authn.ensure_data_dir()
    if configured_repo:
        configured_repo = str(Path(configured_repo).resolve())
        # T17 启动自检：绑定仓登记/核对为内置项目（注册表恰一个 builtin）。
        project_store.ensure_builtin(configured_repo)
    app = FastAPI(title="signet 平台服务", version="0.3.0")
    # 统一设计系统静态资源：原型 style.css 固定挂 /static/style.css（AC-01）。
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
              name="static")
    app_ctx = AppContext(configured_repo)
    app.state.app_ctx = app_ctx

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
        if path.startswith("/api/") and not _is_public_path(path):
            if request.method == "GET" and path.startswith("/api/tasks/") and "workspace" not in request.query_params:
                rest = path[len("/api/tasks/"):]
                parts = tuple(part for part in rest.split("/") if part)
                public_read = bool(parts[1:] in PUBLIC_TASK_READ_SUFFIXES and parts[0])
                evidence_read = bool(len(parts) >= 2 and parts[1] == "evidence")
                if public_read or evidence_read:
                    # T6 读面匿名语义保持；但携带凭据的请求仍按凭据族裁决
                    # （AC-09：A-PAT 读 B 任务读面 → 403 authz.project_denied；
                    # A sigt 打 B 任务读面 → 端点守卫 403 task_mismatch）。
                    guarded = await _guard_public_read(app_ctx, request)
                    if guarded is not None:
                        return guarded
                    return await call_next(request)
            context, auth_error = authenticate_header(request.headers.get("Authorization"))
            if auth_error is not None:
                status_code, payload = auth_error
                return JSONResponse(status_code=status_code, content=payload)
            if context is None:
                # 页面会话 cookie 同样是有效凭据：读端点要支持浏览器直接访问
                # （AC-01：带有效会话 cookie 返回 200 JSON）。
                cookie_token = request.cookies.get(authn.SESSION_COOKIE)
                cookie_session = authn.find_session(cookie_token) if cookie_token else None
                if cookie_session:
                    context = {
                        "kind": "user",
                        "actor": cookie_session["user"],
                        "task": None,
                        "via": "cookie",
                        "active_project": cookie_session.get("active_project"),
                    }
            if context is None:
                return JSONResponse(
                    status_code=401,
                    content=error_payload("auth.required", "valid credentials are required for this endpoint"),
                )
            if context["kind"] == "pat":
                # T17 鉴权顺序钉名：先项目边界（跨项目 → 403 authz.project_denied），
                # 后操作权限（同项目无该操作权限 → 403 authz.pat_scope_denied）；
                # 停用项目凭据作用于本项目（读与写）→ 409 project.disabled。
                # 过期/吊销 401 已在上游拦截。
                target = await _pat_target_project(app_ctx, request, context)
                pat_project = context.get("project_id")
                if _known_project_id(app_ctx, target) and pat_project and target != pat_project:
                    return JSONResponse(
                        status_code=403,
                        content=error_payload("authz.project_denied", "PAT 仅限其绑定项目使用，跨项目一律拒绝"),
                    )
                if pat_project and target == pat_project:
                    record = project_store.get_project(pat_project)
                    if record is not None and record.get("status") == "disabled":
                        return JSONResponse(
                            status_code=409,
                            content=error_payload("project.disabled", "PAT 绑定项目已停用，读写一律拒绝"),
                        )
                if not pat_path_allowed(request.method, path):
                    return JSONResponse(
                        status_code=403,
                        content=error_payload("authz.pat_scope_denied", "PAT 仅可用于需求起草动词端点（create/update/submit），其余端点一律拒绝"),
                    )
                authn.touch_pat(context["token"])
            if context["kind"] == "sigt":
                if path.startswith("/api/projects"):
                    return JSONResponse(
                        status_code=403,
                        content=error_payload("authz.operator_only", "capability tokens (sigt) cannot use project-facing endpoints"),
                    )
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

    # ------------------------------------------------------------------
    # API 路由按域注册（处理器实现见 routes_core/requirements/tasks/
    # projects/lifecycle 模块；装饰器路径与方法逐字保持基线钉名）。
    # ------------------------------------------------------------------
    routes_core.register(app)
    routes_requirements.register(app)
    routes_tasks.register(app)
    routes_projects.register(app)
    routes_lifecycle.register(app)

    # ------------------------------------------------------------------
    # 页面路由（T17 页面面 + T15 skill/guide/settings）：S8 归 routes_pages。
    # ------------------------------------------------------------------

    @app.get("/settings")
    def settings_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_settings(
            session["user"], authn.list_pats(session["user"]), project_store.list_projects(),
            project_ctx=_page_project_context(app_ctx, session)))

    @app.post("/settings/pats")
    async def settings_pats_create(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        form = await request.form()
        project_record = _pat_project_for_issue(str(form.get("project") or ""))
        record, token = _issue_pat(
            project_record,
            session["user"],
            str(form.get("label") or ""),
            [str(value) for value in form.getlist("scope")],
            str(form.get("expires_in") or "24h"),
        )
        return HTMLResponse(pages.render_pat_issued(
            session["user"], record, token, project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/")
    def overview(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        snapshot = readmodel.project_snapshot(resolve_repo(app_ctx, None, session=session))
        return HTMLResponse(pages.render_overview(
            session["user"], snapshot, project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/requirements")
    def requirements_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        entries = readmodel.list_requirements(resolve_repo(app_ctx, None, session=session))
        return HTMLResponse(pages.render_requirements(
            session["user"], entries, project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/requirements/{requirement_id}")
    def requirement_detail_page(request: Request, requirement_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.requirement_detail(resolve_repo(app_ctx, None, session=session), requirement_id)
        if detail is None:
            raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
        return HTMLResponse(pages.render_requirement_detail(
            session["user"], detail, project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/tasks")
    def tasks_board_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_board(
            session["user"], readmodel.task_board(resolve_repo(app_ctx, None, session=session)),
            project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/tasks/new")
    def task_new_page(request: Request, requirement: str | None = None):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_task_form(
            session["user"], requirement, project_ctx=_page_project_context(app_ctx, session)))

    @app.post("/tasks")
    async def task_create_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        form = await request.form()
        title = str(form.get("title") or "").strip()
        requirement = str(form.get("requirement") or "").strip()
        req_items = normalize_req_items(str(form.get("req_items") or "").replace("，", ",").split(","))
        if not title or not requirement:
            raise PlatformError("request.invalid", "title and requirement are required and must be non-empty")
        # T9 派生闸门（AC-05）+ T10 条目引用校验（AC-01）：页面表单与 cookie API
        # 同一条校验路径——需求须存在且 confirmed，承载条目编号须命中「判据条目」节。
        validate_requirement_derive(resolve_repo(app_ctx, None, session=session), requirement)
        # 与 POST /api/tasks 同一条创建路径：owner 锁定为登录用户；workspace 由
        # 客户端在任务创建后自行落地（api-remote-ready，REQ-3）。
        result = init_task(resolve_repo(app_ctx, None, for_write=True, session=session), None, title, requirement,
                           session["user"], session["user"], req_items=req_items)
        return RedirectResponse(url="/tasks/{}".format(result["task_id"]), status_code=303)

    @app.get("/tasks/{task_id}")
    def task_detail_page(request: Request, task_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.task_detail(resolve_repo(app_ctx, None, session=session), task_id)
        if detail is None:
            raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
        return HTMLResponse(pages.render_task_detail(
            session["user"], detail, project_ctx=_page_project_context(app_ctx, session)))

    @app.get("/guide")
    def guide_page(request: Request):
        # T15/REQ-3/AC-03：接入指南——匿名可读（规范非机密、获取动作经 API）。
        return HTMLResponse(pages.render_guide())

    # ------------------------------------------------------------------
    # T17 页面面：/projects（注册+列表，FORM-01）、/projects/{id}（详情+
    # 配置+凭据+停用/启用）、全站顶栏项目切换器（POST .../switch，button-only
    # 零命名字段表单，303 回来源页；未登录一律跳登录页——首版无只读用户）。
    # ------------------------------------------------------------------

    @app.get("/projects")
    def projects_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_projects(
            session["user"], project_store.list_projects(), error=None, project_ctx=_page_project_context(app_ctx, session)))

    @app.post("/projects")
    async def projects_register_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        form = await request.form()
        name = str(form.get("name") or "")
        state_repo = str(form.get("state_repo") or "")
        code_repo = str(form.get("code_repo") or "")
        state_credential = str(form.get("state_credential") or "")
        code_credential = str(form.get("code_credential") or "")
        credentials = {}
        if state_credential:
            credentials["state"] = {"method": "https_pat", "secret": state_credential}
        if code_credential:
            credentials["code"] = {"method": "https_pat", "secret": code_credential}
        try:
            platform_repo = os.environ.get("SIGNET_PLATFORM_REPO")
            builtin_pid = project_metadata(configured_repo) if configured_repo else None
            record = project_store.register_project(
                name, state_repo, code_repo, credentials or None,
                session["user"], platform_repo, configured_repo, builtin_pid)
        except PlatformError as error:
            return HTMLResponse(pages.render_projects(
                session["user"], project_store.list_projects(), error=error.message,
                project_ctx=_page_project_context(app_ctx, session)))
        return RedirectResponse(url="/projects/{}".format(record["project_id"]), status_code=303)

    @app.get("/projects/{project_id}")
    def project_detail_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        record = _require_project_record(project_id)
        return HTMLResponse(pages.render_project_detail(
            session["user"], record,
            project_store.project_credentials_view(project_id),
            _project_recent_events(app_ctx, record),
            notice=None,
            project_ctx=_page_project_context(app_ctx, session),
        ))

    @app.post("/projects/{project_id}/configure")
    async def project_configure_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if name:
            try:
                project_store.update_project(project_id, name=name)
                _platform_event(app_ctx, "project.configured", session["user"], {
                    "project_id": project_id,
                    "fields": ["name"],
                })
            except PlatformError:
                pass
        return RedirectResponse(url="/projects/{}".format(project_id), status_code=303)

    @app.post("/projects/{project_id}/disable")
    def project_disable_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        record = _require_mutable_project(project_id)
        if record.get("status") != "disabled":
            project_store.update_project(project_id, status="disabled")
            _platform_event(app_ctx, "project.disabled", session["user"], {"project_id": project_id})
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/{project_id}/enable")
    def project_enable_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        record = _require_mutable_project(project_id)
        if record.get("status") == "disabled":
            project_store.update_project(project_id, status="active")
            _platform_event(app_ctx, "project.enabled", session["user"], {"project_id": project_id})
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/{project_id}/switch")
    def project_switch_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        _require_project_record(project_id)
        token = request.cookies.get(authn.SESSION_COOKIE)
        authn.set_session_active_project(token, project_id)
        target = request.headers.get("referer") or "/"
        if not target.startswith("/"):
            target = "/"
        return RedirectResponse(url=target, status_code=303)

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

    return app
