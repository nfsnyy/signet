from fastapi import Request
from fastapi.responses import RedirectResponse

from .. import authn, readmodel
from .. import projects as project_store
from .authnz import credentials, ok
from ..errors import AuthorizationError, PlatformError
from ..service import (
    closeout_requirement,
    confirm_requirement,
    create_requirement,
    merge_backlog_item,
    record_pat_event,
    submit_requirement,
    update_requirement,
)
from .context import (
    _issue_pat,
    _pat_project_for_issue,
    _pat_scope_required,
    _requirement_attribution,
    resolve_repo,
)
from .models import (
    ConfirmBody,
    MergeBody,
    PatIssueBody,
    PatRevokeBody,
    RequirementCreateBody,
    RequirementUpdateBody,
)


def register(app):
    app.get("/api/project")(project_endpoint)
    app.get("/api/requirements")(requirements_endpoint)
    app.get("/api/requirements/{requirement_id}")(requirement_detail_endpoint)
    app.post("/api/pats")(pats_issue_endpoint)
    app.get("/api/pats")(pats_list_endpoint)
    app.post("/api/pats/{pat_id}/revoke")(pats_revoke_endpoint)
    app.post("/api/requirements")(requirement_create_endpoint)
    app.post("/api/requirements/{requirement_id}")(requirement_update_endpoint)
    app.post("/api/requirements/{requirement_id}/submission")(requirement_submit_endpoint)
    app.post("/api/requirements/{requirement_id}/closeout")(requirement_closeout_endpoint)
    app.post("/api/requirements/{requirement_id}/backlog-merge")(requirement_backlog_merge_endpoint)
    app.post("/api/requirements/{requirement_id}/confirm")(requirement_confirm_endpoint)


def project_endpoint(request: Request):
    app_ctx = request.app.state.app_ctx
    return ok(readmodel.project_snapshot(resolve_repo(app_ctx, None)))


def requirements_endpoint(request: Request, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    return ok({"requirements": readmodel.list_requirements(resolve_repo(app_ctx, repo, session=credentials(request)))})


def requirement_detail_endpoint(request: Request, requirement_id: str, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    detail = readmodel.requirement_detail(resolve_repo(app_ctx, repo, session=credentials(request)), requirement_id)
    if detail is None:
        raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
    return ok(detail)


def pats_issue_endpoint(request: Request, body: PatIssueBody | None = None):
    # body 可选：conformance/空体探测下 sigt 须先得到 403（鉴权先于字段校验）。
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "PAT 只能由本人登录签发，凭证不可代签")
    if body is None:
        raise PlatformError("request.invalid", "label, scope, expires_in and project_id are required")
    project_record = _pat_project_for_issue(body.project_id)
    record, token = _issue_pat(project_record, context["actor"], body.label, body.scope, body.expires_in)
    return ok({
        "pat_id": record["pat_id"],
        "label": record["label"],
        "obo": record["obo"],
        "scope": record["scope"],
        "project_id": record["project_id"],
        "expires_at": record["expires_at"],
        "token": token,
    })


def pats_list_endpoint(request: Request):
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "仅本人可见本人 PAT")
    return ok({"pats": authn.list_pats(context["actor"])})


def pats_revoke_endpoint(request: Request, pat_id: str, body: PatRevokeBody | None = None):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "仅本人可吊销本人 PAT")
    record = authn.revoke_pat(pat_id, context["actor"])
    project_id = record.get("project_id")
    if project_id:
        project_record = project_store.get_project(project_id)
        if project_record is not None and not project_record.get("builtin"):
            repository = project_store.ensure_clone(project_record)
            project_store.fetch_integrate(repository, for_write=True)
            record_pat_event(repository, "pat.revoked", context["actor"], {
                "pat_id": pat_id,
                "project_id": project_id,
            })
            return ok({"pat_id": pat_id, "status": record["status"]})
    record_pat_event(resolve_repo(app_ctx, None), "pat.revoked", context["actor"], {"pat_id": pat_id, "project_id": project_id})
    return ok({"pat_id": pat_id, "status": record["status"]})


def requirement_create_endpoint(request: Request, body: RequirementCreateBody):
    app_ctx = request.app.state.app_ctx
    actor, on_behalf_of = _requirement_attribution(request)
    _pat_scope_required(request, "requirement:create")
    return ok(create_requirement(
        resolve_repo(app_ctx, body.repo, for_write=True, session=credentials(request)),
        body.requirement_id, body.title, body.body, body.entries, actor, on_behalf_of,
        source=body.source))


def requirement_update_endpoint(request: Request, requirement_id: str, body: RequirementUpdateBody):
    app_ctx = request.app.state.app_ctx
    actor, on_behalf_of = _requirement_attribution(request)
    _pat_scope_required(request, "requirement:update")
    return ok(update_requirement(
        resolve_repo(app_ctx, body.repo, for_write=True, session=credentials(request)),
        requirement_id, body.body, body.entries, actor, on_behalf_of,
        source=body.source))


def requirement_submit_endpoint(request: Request, requirement_id: str, body: ConfirmBody | None = None):
    app_ctx = request.app.state.app_ctx
    actor, on_behalf_of = _requirement_attribution(request)
    _pat_scope_required(request, "requirement:submit")
    repo = body.repo if body else None
    return ok(submit_requirement(
        resolve_repo(app_ctx, repo, for_write=True, session=credentials(request)),
        requirement_id, actor, on_behalf_of))


def requirement_closeout_endpoint(request: Request, requirement_id: str, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "需求收口只认本人交互登录，任何凭证不可代过")
    return ok(closeout_requirement(
        resolve_repo(app_ctx, repo, for_write=True, session=context), requirement_id, context["actor"]))


def requirement_backlog_merge_endpoint(request: Request, requirement_id: str, body: MergeBody):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "backlog 归并只认本人交互登录，任何凭证不可代过")
    return ok(merge_backlog_item(
        resolve_repo(app_ctx, None, for_write=True, session=context),
        requirement_id, body.backlog_task_id, body.entry_id, context["actor"]))


async def requirement_confirm_endpoint(request: Request, requirement_id: str, repo: str | None = None):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "confirm 只认本人交互登录，任何凭证不可代过")
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
        form = await request.form()
        entries = [str(value) for value in form.getlist("entries")]
        confirm_requirement(
            resolve_repo(app_ctx, repo, for_write=True, session=context), requirement_id, entries, context["actor"])
        return RedirectResponse(url="/requirements/{}".format(requirement_id), status_code=303)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise PlatformError("request.invalid", "confirm body must be a JSON object")
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        raise PlatformError("request.invalid", "confirm entries must be a list")
    return ok(confirm_requirement(
        resolve_repo(app_ctx, repo, for_write=True, session=context), requirement_id, entries, context["actor"]))
