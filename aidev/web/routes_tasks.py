import tempfile
from pathlib import Path

from fastapi import File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response

from .. import readmodel
from ..collaboration import collab_answer, collab_send
from ..domain import task_path
from ..errors import AuthorizationError, NotFoundError, PlatformError
from ..service import (
    block,
    block_authoritative,
    build_task_package,
    claim,
    claim_authoritative,
    claim_with_sigt,
    enhancement,
    evidence_manifest,
    init_task,
    resolve_spec_asset,
    resolve_task_evidence,
    status,
    submit,
    submit_authoritative,
    task_log,
    validate_requirement_derive,
)
from ..taskfile import read_task
from ..vetoes import settle_expired_vetoes
from .authnz import credentials, load_authoritative_task, ok
from .context import (
    _sigt_task_guard,
    _verify_sigt_project,
    evidence_endpoint_auth,
    request_actor,
    require_workspace_lease,
    resolve_repo,
    stage_workspace_evidence,
    task_repository,
    workspace_task_id,
)
from .models import (
    ClaimBody,
    CollabAnswerBody,
    CollabSendBody,
    EnhancementBody,
    InitTaskBody,
    check_actor_aliases,
)
from .uploads import save_upload


def register(app):
    app.get("/api/tasks")(task_board_endpoint)
    app.get("/api/tasks/{task_id}")(task_detail_endpoint)
    app.get("/api/tasks/{task_id}/status")(status_endpoint)
    app.get("/api/tasks/{task_id}/package")(task_package_endpoint)
    app.get("/api/tasks/{task_id}/events")(events_endpoint)
    app.post("/api/tasks/{task_id}/claims")(claims_endpoint)
    app.get("/api/tasks/{task_id}/assets/{name:path}")(task_asset_endpoint)
    app.get("/api/tasks/{task_id}/evidence/{name:path}")(task_evidence_endpoint)
    app.post("/api/tasks/{task_id}/submission")(submission_endpoint)
    app.post("/api/tasks/{task_id}/blocks")(blocks_endpoint)
    app.post("/api/tasks/{task_id}/enhancements")(enhancements_endpoint)
    app.post("/api/tasks/{task_id}/collaborations")(collaborations_endpoint)
    app.post("/api/collaborations/{message_id}/answers")(collab_answers_endpoint)
    app.post("/api/tasks")(init_task_endpoint)


def task_board_endpoint(request: Request, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    return ok({"tasks": readmodel.task_board(resolve_repo(app_ctx, repo, session=credentials(request)))})


def task_detail_endpoint(request: Request, task_id: str, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    _sigt_task_guard(request, task_id)
    repository = task_repository(app_ctx, request, task_id, repo)
    detail = readmodel.task_detail(repository, task_id)
    if detail is None:
        raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
    return ok(detail)


def status_endpoint(request: Request, task_id: str, workspace: str | None = Query(default=None), repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    if workspace:
        workspace_task_id(workspace, task_id)
        return ok({**status(workspace), "task_id": task_id})
    _sigt_task_guard(request, task_id)
    repository = task_repository(app_ctx, request, task_id, repo)
    metadata = load_authoritative_task(repository, task_id)
    return ok({"task": metadata, "inbox": [], "outbox_collab": [], "authoritative": True, "task_id": task_id})


def task_package_endpoint(request: Request, task_id: str, repo: str | None = None):
    # T15/REQ-2/AC-02：只读幂等任务包读面（claims POST 不入 interfaces 声明，
    # 为 conformance 提供探针安全的 GET 通路；匿名 401，三凭据族 200）。
    # api-remote-ready：repo 仅接受逻辑 id 或省略（AC-01/02 同口径）——
    # CLI 投影拉取携带 --repo 时经查询参数透传，未绑定服务维持本地仓语义。
    app_ctx = request.app.state.app_ctx
    repository = task_repository(app_ctx, request, task_id, repo)
    metadata, body = read_task(task_path(repository, task_id))
    return ok(build_task_package(repository, metadata, body))


def events_endpoint(
    request: Request,
    task_id: str,
    repo: str | None = None,
    role: str = Header(default="operator", alias="X-Signet-Role"),
):
    app_ctx = request.app.state.app_ctx
    repository = task_repository(app_ctx, request, task_id, repo)
    if role == "operator":
        settle_expired_vetoes(repository)
    return ok({"events": task_log(repository, task_id)})


def claims_endpoint(request: Request, task_id: str, body: ClaimBody | None = None):
    app_ctx = request.app.state.app_ctx
    workspace = body.workspace if body else None
    if workspace:
        workspace_task_id(workspace, task_id)
        return ok(claim(workspace, request_actor(request), request_actor(request)))
    context = credentials(request)
    on_behalf_of = body.on_behalf_of if body else None
    repo_value = body.repo if body else None
    if context["kind"] == "sigt":
        # sigt 幂等接管：同 actor 续期（lease.renewed）/ 无租约重 claim，
        # 响应携带完整任务包（AC-01/AC-03）。AC-16：on_behalf_of 记录执行者（CLI 本人登录身份）。
        # T17：任务按注册表全局定位；sigt 项目绑定不符即拒（403 authz.project_denied）。
        repository = task_repository(app_ctx, request, task_id, repo_value, for_write=True)
        return ok(claim_with_sigt(
            repository, task_id, context["actor"], on_behalf_of, sigt_repo=context.get("repo")))
    repository = task_repository(app_ctx, request, task_id, repo_value, for_write=True)
    return ok(claim_authoritative(repository, task_id, request_actor(request)))


def task_asset_endpoint(request: Request, task_id: str, name: str, repo: str | None = None):
    # api-remote-ready：repo 查询参数可选（AC-01/02 同口径），CLI 投影落地
    # 携带 --repo 时透传；省略落服务端绑定存储/活动项目。
    app_ctx = request.app.state.app_ctx
    repository = task_repository(app_ctx, request, task_id, repo)
    metadata = load_authoritative_task(repository, task_id)
    asset_path = resolve_spec_asset(repository, metadata, name)
    return Response(content=asset_path.read_bytes(), media_type="application/octet-stream")


def task_evidence_endpoint(request: Request, task_id: str, name: str, repo: str | None = None):
    # REQ-2：登录用户 Bearer/cookie 可读任意任务证据（200）；本任务 sigt 200、他任务 sigt 403；
    # 匿名对有效名 401；清单外 404；穿越 4xx 且不读仓外文件。名与路径解析先于授权。
    # AC-09 真实走查缺陷修复：证据下载与其它任务端点同构支持可选 repo 查询参数
    # （省略落绑定存储/活动项目并按注册表全局定位，显式 repo 仅限该项目内），
    # 不再经 resolve_repo(None) 钉死绑定存储导致非内置项目任务 400 task.not_found。
    app_ctx = request.app.state.app_ctx
    repository = task_repository(app_ctx, request, task_id, repo)
    load_authoritative_task(repository, task_id)
    evidence_path = resolve_task_evidence(repository, task_id, name)
    auth_failure = evidence_endpoint_auth(request, task_id)
    if auth_failure is not None:
        return auth_failure
    return Response(content=evidence_path.read_bytes(), media_type="application/octet-stream")


def submission_endpoint(
    request: Request,
    task_id: str,
    workspace: str | None = Form(None),
    actor: str | None = Form(None),
    on_behalf_of: str | None = Form(None),
    report: UploadFile = File(...),
    evidence: list[UploadFile] = File(default=[]),
):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    check_actor_aliases({"actor": actor, "on_behalf_of": on_behalf_of})
    with tempfile.TemporaryDirectory(prefix="aidev-web-submit-") as temporary:
        report_path = save_upload(report, temporary, "report.json")
        evidence_paths = [save_upload(item, temporary, "evidence") for item in evidence]
        if workspace:
            require_workspace_lease(workspace, task_id, context["actor"])
            evidence_paths = stage_workspace_evidence(evidence_paths, workspace)
            return ok(submit(workspace, context["actor"], context["actor"], report_path, evidence_paths))
        # 无工作区的编排直通：凭据必须是绑定到本任务的 sigt（中间件已做任务匹配），
        # 与 claim_authoritative 对称直接改写权威账本。AC-16：on_behalf_of 记录执行者。
        if context["kind"] != "sigt":
            raise AuthorizationError(
                "authz.sigt_required",
                "submission without a workspace requires a task capability token (sigt)",
            )
        repository = task_repository(app_ctx, request, task_id, None, for_write=True)
        _verify_sigt_project(context, repository)
        if not evidence_paths:
            evidence_paths = [report_path]
        return ok(submit_authoritative(repository, task_id, context["actor"], report_path, evidence_paths, on_behalf_of))


async def blocks_endpoint(request: Request, task_id: str):
    app_ctx = request.app.state.app_ctx
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
            repository = task_repository(app_ctx, request, task_id, None, for_write=True)
            return ok(block_authoritative(repository, task_id, context["actor"], reason, evidence_docs))
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
    repository = task_repository(app_ctx, request, task_id, None, for_write=True)
    return ok(block_authoritative(repository, task_id, context["actor"], reason, evidence_docs))


def enhancements_endpoint(request: Request, task_id: str, body: EnhancementBody):
    require_workspace_lease(body.workspace, task_id, request_actor(request))
    return ok(enhancement(body.workspace, request_actor(request), request_actor(request), body.title, body.details))


def collaborations_endpoint(request: Request, task_id: str, body: CollabSendBody):
    workspace_task_id(body.workspace, task_id)
    message_type = body.type if body.type is not None else body.message_type
    to_task = body.to_task if body.to_task is not None else body.target_task
    details = body.details if body.details is not None else body.body
    return ok(collab_send(body.workspace, request_actor(request), request_actor(request), message_type, to_task, body.summary, details))


def collab_answers_endpoint(request: Request, message_id: str, body: CollabAnswerBody):
    return ok(collab_answer(body.workspace, request_actor(request), request_actor(request), message_id, body.action, body.note))


def init_task_endpoint(request: Request, body: InitTaskBody):
    app_ctx = request.app.state.app_ctx
    title = (body.title or "").strip()
    requirement = (body.requirement or "").strip()
    if not title or not requirement:
        raise PlatformError("request.invalid", "title and requirement are required and must be non-empty")
    # T9 派生闸门（AC-05）：交互会话（cookie，页面同源动线）派生须需求存在且 confirmed；
    # operator CLI（Bearer 用户令牌）保留自由引用，兼容 T1–T8 既有工作流与基线测试。
    # T10 条目引用校验（AC-01）同口径：仅会话路径校验 req_items 命中「判据条目」节。
    context = credentials(request)
    session_path = context["kind"] == "user" and context.get("via") == "cookie"
    if session_path:
        validate_requirement_derive(resolve_repo(app_ctx, None), requirement)
    # 身份锁定（T2 AC-09 语义）：owner 与事件归因均以凭据身份为准，请求体所填 owner 被会话覆盖。
    actor = request_actor(request)
    # api-remote-ready（REQ-3/AC-07）：workspace 参数语义移除——服务端不创建/写
    # client 工作区；客户端在 init-task 成功后经任务包拉取自行落地投影。
    return ok(init_task(
        resolve_repo(app_ctx, body.repo, for_write=True, session=context), None, title, requirement, actor, actor,
        req_items=body.req_items, validate_req_items=session_path))
