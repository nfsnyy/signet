import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import authn
from .. import projects as project_store
from ..errors import PlatformError
from . import (
    routes_core,
    routes_lifecycle,
    routes_pages,
    routes_projects,
    routes_requirements,
    routes_tasks,
)
from .authnz import (
    PUBLIC_TASK_READ_SUFFIXES,
    _is_public_path,
    authenticate_header,
    error_payload,
    path_authorization,
    pat_path_allowed,
)
from .context import AppContext, _guard_public_read, _known_project_id, _pat_target_project


def create_app(default_repo=None):
    # 服务端状态仓绑定只认 SIGNET_STATE_REPO；SIGNET_PLATFORM_REPO 仅供 runner/CLI
    # 定位被测代码，服务端不再读取它作为状态仓。
    configured_repo = default_repo or os.environ.get("SIGNET_STATE_REPO")
    authn.ensure_data_dir()
    if configured_repo:
        configured_repo = str(Path(configured_repo).resolve())
        # T17 启动自检：绑定仓登记/核对为内置项目（注册表恰一个 builtin）。
        project_store.ensure_builtin(configured_repo)
    # 文档路由（/openapi.json、/docs、/docs/oauth2-redirect、/redoc）由
    # routes_core 以模块级处理器注册（FastAPI.setup 的默认实现是闭包，
    # 关闭默认项以保持目标形态；响应行为与基线默认实现逐字一致）。
    app = FastAPI(title="signet 平台服务", version="0.3.0",
                  openapi_url=None, docs_url=None, redoc_url=None)
    # 统一设计系统静态资源：原型 style.css 固定挂 /static/style.css（AC-01）。
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
              name="static")
    app.state.app_ctx = AppContext(configured_repo)

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
                    guarded = await _guard_public_read(app.state.app_ctx, request)
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
                target = await _pat_target_project(app.state.app_ctx, request, context)
                pat_project = context.get("project_id")
                if _known_project_id(app.state.app_ctx, target) and pat_project and target != pat_project:
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
    # 路由注册：业务端点全部按域生活在独立模块（register 装饰器路径与
    # 方法逐字保持基线钉名；create_app 只承担组合/中间件/异常处理器）。
    # ------------------------------------------------------------------
    routes_core.register(app)
    routes_requirements.register(app)
    routes_tasks.register(app)
    routes_projects.register(app)
    routes_lifecycle.register(app)
    routes_pages.register(app)

    return app
