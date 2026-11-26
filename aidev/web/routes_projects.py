import os

from fastapi import Request

from .. import projects as project_store
from .authnz import credentials, ok
from ..errors import AuthorizationError, PlatformError
from ..service import project_metadata, record_pat_event
from .context import (
    _apply_git_credential_events,
    _platform_event,
    _require_mutable_project,
    _require_project_record,
)
from .models import CredentialRemoveBody, ProjectConfigureBody, ProjectRegisterBody


def register(app):
    app.get("/api/projects")(projects_list_endpoint)
    app.post("/api/projects")(projects_register_endpoint)
    app.get("/api/projects/{project_id}")(projects_detail_endpoint)
    app.post("/api/projects/{project_id}/configure")(projects_configure_endpoint)
    app.post("/api/projects/{project_id}/disable")(projects_disable_endpoint)
    app.post("/api/projects/{project_id}/enable")(projects_enable_endpoint)
    app.get("/api/projects/{project_id}/credentials")(projects_credentials_endpoint)
    app.post("/api/projects/{project_id}/credentials/remove")(projects_credentials_remove_endpoint)


def projects_list_endpoint(request: Request):
    return ok({"projects": project_store.list_projects()})


def projects_register_endpoint(request: Request, body: ProjectRegisterBody | None = None):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "项目注册只认本人登录（用户令牌或会话 cookie），任何凭证不可代注册")
    body = body or ProjectRegisterBody()
    platform_repo = os.environ.get("SIGNET_PLATFORM_REPO")
    builtin_pid = project_metadata(app_ctx.configured_repo) if app_ctx.configured_repo else None
    record = project_store.register_project(
        body.name, body.state_repo, body.code_repo, body.credentials,
        context["actor"], platform_repo, app_ctx.configured_repo, builtin_pid,
    )
    # project.registered 事件随骨架首笔提交落项目状态仓（见 projects.skeleton_tree）。
    return ok({"project": record})


def projects_detail_endpoint(request: Request, project_id: str):
    return ok({"project": _require_project_record(project_id)})


def projects_configure_endpoint(request: Request, project_id: str, body: ProjectConfigureBody | None = None):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "项目配置只认本人登录，任何凭证不可代配置")
    body = body or ProjectConfigureBody()
    record = _require_project_record(project_id)
    if record.get("builtin"):
        # T24/REQ-7（AC-08）：内置项目放行 credentials-only（凭据供给属日常运维，
        # 服务端 ship 的 code 凭据来源）；name/state_repo/code_repo 改绑与停用维持
        # project.builtin_protected（凭据放行不扩大内置保护面）。
        name_change = body.name is not None and str(body.name).strip() and body.name != record.get("name")
        if name_change or body.state_repo is not None or body.code_repo is not None:
            raise PlatformError(
                "project.builtin_protected",
                "内置项目（平台自身）不可改绑 name/state_repo/code_repo；仅放行 credentials-only 配置",
                exit_code=3,
                details={"project_id": project_id},
            )
    else:
        record = _require_mutable_project(project_id)
        if body.state_repo is not None or body.code_repo is not None:
            raise PlatformError(
                "project.repo_immutable",
                "首版不支持 state_repo/code_repo 改绑：仓库地址一经注册不可变更（零副作用）",
            )
    if record.get("status") == "disabled":
        raise PlatformError("project.disabled", f"project {project_id} is disabled; configure is rejected", exit_code=3)
    fields = []
    if not record.get("builtin") and body.name is not None and str(body.name).strip() and body.name != record.get("name"):
        record = project_store.update_project(project_id, name=str(body.name).strip())
        fields.append("name")
    supplied_credentials = body.credentials or {}
    if supplied_credentials:
        cleaned = project_store.validate_supplied_credentials(supplied_credentials)
        _apply_git_credential_events(app_ctx, project_id, cleaned, context["actor"])
        fields.extend(f"credentials.{kind}" for kind in ("state", "code") if kind in cleaned)
    if fields:
        _platform_event(app_ctx, "project.configured", context["actor"], {
            "project_id": project_id,
            "fields": fields,
        })
    return ok({"project": _require_project_record(project_id)})


def projects_disable_endpoint(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "项目停用只认本人登录，任何凭证不可代操作")
    record = _require_mutable_project(project_id)
    if record.get("status") != "disabled":
        record = project_store.update_project(project_id, status="disabled")
        _platform_event(app_ctx, "project.disabled", context["actor"], {"project_id": project_id})
    return ok({"project": _require_project_record(project_id)})


def projects_enable_endpoint(request: Request, project_id: str):
    app_ctx = request.app.state.app_ctx
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "项目启用只认本人登录，任何凭证不可代操作")
    record = _require_mutable_project(project_id)
    if record.get("status") == "disabled":
        record = project_store.update_project(project_id, status="active")
        _platform_event(app_ctx, "project.enabled", context["actor"], {"project_id": project_id})
    return ok({"project": _require_project_record(project_id)})


def projects_credentials_endpoint(request: Request, project_id: str):
    _require_project_record(project_id)
    return ok({"credentials": project_store.project_credentials_view(project_id)})


def projects_credentials_remove_endpoint(request: Request, project_id: str, body: CredentialRemoveBody | None = None):
    context = credentials(request)
    if context["kind"] != "user":
        raise AuthorizationError("authz.session_required", "凭据记录删除只认本人登录，任何凭证不可代操作")
    record = _require_mutable_project(project_id)
    repo_kind = (body.repo if body else None) or ""
    if repo_kind not in ("state", "code"):
        raise PlatformError("request.invalid", "repo 必须为 state|code")
    if record.get("status") == "disabled":
        raise PlatformError("project.disabled", f"project {project_id} is disabled; credential removal is rejected", exit_code=3)
    removed = project_store.remove_git_credential(project_id, repo_kind)
    if removed:
        repository = project_store.ensure_clone(record)
        project_store.fetch_integrate(repository, for_write=True)
        record_pat_event(repository, "git_credential.removed", context["actor"], {
            "project_id": project_id,
            "repo": repo_kind,
        })
        project_store.apply_credential_to_clone(repository, record)
    return ok({"project_id": project_id, "repo": repo_kind})
