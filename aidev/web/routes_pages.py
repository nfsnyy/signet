import os

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import authn, readmodel
from .. import projects as project_store
from ..errors import NotFoundError, PlatformError
from ..service import (
    init_task,
    normalize_req_items,
    project_metadata,
    validate_requirement_derive,
)
from . import pages
from .authnz import LOGIN_PAGE, login_shell_page, perform_login
from .context import (
    _issue_pat,
    _page_project_context,
    _pat_project_for_issue,
    _platform_event,
    _project_recent_events,
    _require_mutable_project,
    _require_project_record,
    page_session,
    resolve_repo,
)


def register(app):
    app.get("/settings")(settings_page)
    app.post("/settings/pats")(settings_pats_create)
    app.get("/")(overview)
    app.get("/requirements")(requirements_page)
    app.get("/requirements/{requirement_id}")(requirement_detail_page)
    app.get("/tasks")(tasks_board_page)
    app.get("/tasks/new")(task_new_page)
    app.post("/tasks")(task_create_page)
    app.get("/tasks/{task_id}")(task_detail_page)
    app.get("/guide")(guide_page)
    app.get("/projects")(projects_page)
    app.post("/projects")(projects_register_page)
    app.get("/projects/{project_id}")(project_detail_page)
    app.post("/projects/{project_id}/configure")(project_configure_page)
    app.post("/projects/{project_id}/disable")(project_disable_page)
    app.post("/projects/{project_id}/enable")(project_enable_page)
    app.post("/projects/{project_id}/switch")(project_switch_page)
    app.get("/login")(login_page)
    app.post("/login")(login_submit)


def settings_page(request: Request):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(pages.render_settings(
        session["user"], authn.list_pats(session["user"]), project_store.list_projects(),
        project_ctx=_page_project_context(app_ctx, session)))


async def settings_pats_create(request: Request):
    app_ctx = request.app.state.app_ctx
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


def overview(request: Request):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    snapshot = readmodel.project_snapshot(resolve_repo(app_ctx, None, session=session))
    return HTMLResponse(pages.render_overview(
        session["user"], snapshot, project_ctx=_page_project_context(app_ctx, session)))


def requirements_page(request: Request):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    entries = readmodel.list_requirements(resolve_repo(app_ctx, None, session=session))
    return HTMLResponse(pages.render_requirements(
        session["user"], entries, project_ctx=_page_project_context(app_ctx, session)))


def requirement_detail_page(request: Request, requirement_id: str):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    detail = readmodel.requirement_detail(resolve_repo(app_ctx, None, session=session), requirement_id)
    if detail is None:
        raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    return HTMLResponse(pages.render_requirement_detail(
        session["user"], detail, project_ctx=_page_project_context(app_ctx, session)))


def tasks_board_page(request: Request):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(pages.render_board(
        session["user"], readmodel.task_board(resolve_repo(app_ctx, None, session=session)),
        project_ctx=_page_project_context(app_ctx, session)))


def task_new_page(request: Request, requirement: str | None = None):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(pages.render_task_form(
        session["user"], requirement, project_ctx=_page_project_context(app_ctx, session)))


async def task_create_page(request: Request):
    app_ctx = request.app.state.app_ctx
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


def task_detail_page(request: Request, task_id: str):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    detail = readmodel.task_detail(resolve_repo(app_ctx, None, session=session), task_id)
    if detail is None:
        raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
    return HTMLResponse(pages.render_task_detail(
        session["user"], detail, project_ctx=_page_project_context(app_ctx, session)))


def guide_page(request: Request):
    # T15/REQ-3/AC-03：接入指南——匿名可读（规范非机密、获取动作经 API）。
    return HTMLResponse(pages.render_guide())


def projects_page(request: Request):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(pages.render_projects(
        session["user"], project_store.list_projects(), error=None, project_ctx=_page_project_context(app_ctx, session)))


async def projects_register_page(request: Request):
    app_ctx = request.app.state.app_ctx
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
        builtin_pid = project_metadata(app_ctx.configured_repo) if app_ctx.configured_repo else None
        record = project_store.register_project(
            name, state_repo, code_repo, credentials or None,
            session["user"], platform_repo, app_ctx.configured_repo, builtin_pid)
    except PlatformError as error:
        return HTMLResponse(pages.render_projects(
            session["user"], project_store.list_projects(), error=error.message,
            project_ctx=_page_project_context(app_ctx, session)))
    return RedirectResponse(url="/projects/{}".format(record["project_id"]), status_code=303)


def project_detail_page(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
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


async def project_configure_page(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
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


def project_disable_page(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    record = _require_mutable_project(project_id)
    if record.get("status") != "disabled":
        project_store.update_project(project_id, status="disabled")
        _platform_event(app_ctx, "project.disabled", session["user"], {"project_id": project_id})
    return RedirectResponse(url="/projects", status_code=303)


def project_enable_page(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
    session = page_session(request)
    if session is None:
        return RedirectResponse(url="/login", status_code=302)
    record = _require_mutable_project(project_id)
    if record.get("status") == "disabled":
        project_store.update_project(project_id, status="active")
        _platform_event(app_ctx, "project.enabled", session["user"], {"project_id": project_id})
    return RedirectResponse(url="/projects", status_code=303)


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


def login_page(request: Request):
    accept = request.headers.get("Accept", "").lower()
    if "text/html" not in accept:
        # 非浏览器客户端（如 urllib 探针）不带 text/html：以 302 自指重定向，
        # 客户端不跟随重定向即可观察到“匿名必须去 /login”的语义；
        # 浏览器导航始终携带 Accept: text/html，正常渲染登录页。
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(LOGIN_PAGE)


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
