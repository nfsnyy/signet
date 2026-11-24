import json
import re
import shutil
from pathlib import Path

from fastapi.responses import JSONResponse

from .. import authn, readmodel
from .. import projects as project_store
from ..domain import lease_is_active, make_event, event_path, task_path, validate_actor_alias
from ..errors import AuthorizationError, NotFoundError, PlatformError
from ..gitrepo import commit_paths
from ..jsonio import append_jsonl
from ..service import (
    project_metadata,
    record_pat_event,
    sigt_repo_field,
    stamp_events,
    unique_evidence_name,
)
from ..taskfile import read_task
from ..vetoes import settle_expired_vetoes
from .authnz import authenticate_header, credentials, error_payload


class AppContext:
    """create_app 组合期绑定的服务端状态：状态仓绑定（SIGNET_STATE_REPO 或
    显式 default_repo）。端点经 request.app.state.app_ctx 取本对象，
    保持同一进程多应用实例互不干扰。"""

    def __init__(self, configured_repo):
        self.configured_repo = configured_repo


PATH_FORM_REPO = re.compile(r"^[A-Za-z]:")
SEPARATOR_REPO = re.compile(r"[/\\]")


def is_path_form_repo(value):
    """AC-01：repo 路径形态判定——盘符（C:\\...）、POSIX 绝对（/...）、
    相对（a/b、..\\..）、点/父引用四类；纯逻辑 id（如 project-xxxx）不算。"""
    if not value:
        return False
    if PATH_FORM_REPO.match(value):
        return True
    if value.startswith(("/", "\\")) or value in (".", ".."):
        return True
    return bool(SEPARATOR_REPO.search(value))


def resolve_repo(app_ctx, request_repo, for_write=False, session=None):
    """T17 泛化解析次序：① 省略 → 会话活动项目（无则内置项目=绑定存储）；
    ② 路径形态 → 400 repo.invalid_identifier（T16 钉名，零副作用）；
    ③ 逻辑 id=内置 project_id → 绑定存储（T16 语义不动）；
    ④ 命中注册表 → 该项目服务端克隆（读前 fetch 整合 / 写前 fetch+冲突裁决：
    behind 自动 ff 整合，diverged 写动词拒 sync.diverged_remote 零副作用）；
    ⑤ 未知逻辑 id → 404 repo.not_found（T16 钉名）。停用项目 → 409
    project.disabled（读写一律，零副作用）。未绑定服务维持本地仓直用语义。"""
    configured_repo = app_ctx.configured_repo
    request_repo = (request_repo or "").strip() or None
    if configured_repo:
        if not request_repo:
            active = (session or {}).get("active_project")
            if active:
                request_repo = active
            else:
                return configured_repo
        if is_path_form_repo(request_repo):
            raise PlatformError(
                "repo.invalid_identifier",
                "repo 仅接受逻辑 project_id 或省略（落会话活动项目/服务端绑定存储），文件系统路径形态一律拒绝",
                details={"repo": request_repo},
            )
        builtin_pid = project_metadata(configured_repo)
        if builtin_pid and request_repo == builtin_pid:
            return configured_repo
        record = project_store.get_project(request_repo)
        if record is None:
            raise NotFoundError(
                "repo.not_found",
                f"unknown repository project_id: {request_repo}",
                details={"repo": request_repo, "bound_project_id": builtin_pid},
            )
        if record.get("status") == "disabled":
            raise PlatformError(
                "project.disabled",
                f"project {request_repo} is disabled; all reads and writes are rejected",
                exit_code=3,
                details={"project_id": request_repo},
            )
        clone = project_store.ensure_clone(record)
        project_store.fetch_integrate(clone, for_write=for_write)
        return str(clone)
    if not request_repo:
        raise PlatformError(
            "git.not_repository",
            "未指定状态仓库：请在请求中提供 repo，或服务端以 SIGNET_STATE_REPO 启动",
        )
    return request_repo


def request_context(request):
    """公开读端点（详情/状态匿名可读）可能未经中间件鉴权——auth 缺失容忍。"""
    return getattr(request.state, "auth", None)


def task_repository(app_ctx, request, task_id, repo_value, for_write=False):
    """任务端点 repo 定位：显式 repo → 该项目内必须存在（跨项目读 → 404
    task.not_found）；省略 → 活动/绑定存储优先，未命中按注册表克隆检索
    （项目 A 的任务经省略 repo 的写动词/claims 可达，B 上下文显式读仍 404）。
    全局定位命中后同样执行项目状态/凭据边界（AC-03/AC-09）：定位项目停用
    → 409 project.disabled（读写一律，先于任何读写副作用）；PAT 定位到绑定
    项目之外 → 403 authz.project_denied（先边界后状态，与中间件同序）。"""
    context = request_context(request)
    if repo_value:
        repository = resolve_repo(app_ctx, repo_value, for_write=for_write, session=context)
        if not task_path(repository, task_id).is_file():
            raise NotFoundError("task.not_found", f"task does not exist in project context: {task_id}")
        return repository
    repository = resolve_repo(app_ctx, None, for_write=for_write, session=context)
    if task_path(repository, task_id).is_file():
        return repository
    found = project_store.search_task_repo(task_id, skip_repository=repository)
    if found is not None:
        found_pid = project_store.project_id_for_repository(found)
        if found_pid:
            if (context or {}).get("kind") == "pat":
                pat_project = ((context or {}).get("project_id") or "").strip()
                if pat_project and found_pid != pat_project:
                    raise AuthorizationError(
                        "authz.project_denied",
                        "PAT 仅限其绑定项目使用，跨项目一律拒绝（全局定位不豁免项目边界）",
                    )
            record = project_store.get_project(found_pid)
            if record is not None and record.get("status") == "disabled":
                raise PlatformError(
                    "project.disabled",
                    f"project {found_pid} is disabled; all reads and writes are rejected",
                    exit_code=3,
                    details={"project_id": found_pid},
                )
        project_store.fetch_integrate(found, for_write=for_write)
        return str(found)
    raise NotFoundError("task.not_found", f"task does not exist: {task_id}")


def operator_repo(app_ctx, request_repo):
    repository = resolve_repo(app_ctx, request_repo)
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


async def _pat_target_project(app_ctx, request, context):
    """PAT 项目边界裁决的目标项目解析：/api/projects/{pid} 路径段 → repo 查询
    参数 → JSON 体 project_id/repo（sync/PAT 通道体均为 JSON；credentials/remove
    体的 repo 为凭据种类 state|code，不作项目解析）→ 会话活动项目 → 内置项目。
    未知/路径形态返回原值由下游钉名错误处理（边界裁决只比对注册表已知项目）。"""
    configured_repo = app_ctx.configured_repo
    path = request.url.path
    if path.startswith("/api/projects/"):
        segment = path[len("/api/projects/"):].split("/")[0]
        if segment:
            return segment
    repo = (request.query_params.get("repo") or "").strip()
    if not repo and request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                body = await request.body()
                document = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                document = {}
            if isinstance(document, dict):
                repo = str(document.get("project_id") or document.get("repo") or "").strip()
    if not repo:
        repo = (context.get("active_project") or "").strip()
    if not repo and configured_repo:
        builtin_pid = project_metadata(configured_repo)
        if builtin_pid:
            repo = builtin_pid
    return repo or None


async def _guard_public_read(app_ctx, request):
    """公开读端点（T6 匿名可读）携带凭据时的裁决：PAT 先项目边界后操作权限
    （任务读面 PAT 一律 403 authz.pat_scope_denied；跨项目 → project_denied）；
    sigt 注入 state.auth 由端点守卫裁决任务/项目绑定；匿名（无 Authorization
    头）/未识别凭据零干预。"""
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    context, auth_error = authenticate_header(authorization)
    if auth_error is not None:
        status_code, payload = auth_error
        return JSONResponse(status_code=status_code, content=payload)
    if context is None:
        return None
    if context["kind"] == "pat":
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
        return JSONResponse(
            status_code=403,
            content=error_payload("authz.pat_scope_denied", "PAT 不可用于任务读端点"),
        )
    if context["kind"] == "sigt":
        request.state.auth = context
    return None


def _known_project_id(app_ctx, value):
    if not value:
        return False
    configured_repo = app_ctx.configured_repo
    if configured_repo and value == project_metadata(configured_repo):
        return True
    record = project_store.get_project(value)
    return record is not None


def _page_project_context(app_ctx, session):
    current = None
    current_id = session.get("active_project")
    if current_id:
        current = project_store.get_project(current_id)
    if current is None and app_ctx.configured_repo:
        builtin_pid = project_metadata(app_ctx.configured_repo)
        if builtin_pid:
            current = project_store.get_project(builtin_pid)
    return {"current": current, "projects": project_store.list_projects()}


def _project_recent_events(app_ctx, record, limit=5):
    repository = app_ctx.configured_repo
    if not record.get("builtin"):
        try:
            repository = project_store.ensure_clone(record)
            project_store.fetch_integrate(repository, for_write=False)
        except PlatformError:
            repository = None
    if repository is None:
        return []
    return readmodel.recent_events(repository, limit=limit)


def _platform_event(app_ctx, event_type, actor, payload):
    """项目注册表生命周期事件：落平台绑定仓（单笔提交，T16 push 语义不变）。"""
    repository = resolve_repo(app_ctx, None)
    event = make_event(event_type, None, actor, actor, None, None, payload=payload, entrypoint="operator")
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, [event]))
    commit_paths(repository, [events_file.relative_to(repository)], event_type)


def _require_project_record(project_id):
    record = project_store.get_project(project_id)
    if record is None:
        raise NotFoundError("project.not_found", f"project does not exist: {project_id}")
    return record


def _require_mutable_project(project_id):
    record = _require_project_record(project_id)
    if record.get("builtin"):
        raise PlatformError(
            "project.builtin_protected",
            "内置项目（平台自身）不可停用、不可改绑、不可删凭据",
            exit_code=3,
            details={"project_id": project_id},
        )
    return record


def _apply_git_credential_events(project_id, credentials, actor):
    """configure 携 credentials upsert：写凭据存储 + git_credential.configured
    三键事件落该项目状态仓（秘密只写不回显）。"""
    record = _require_project_record(project_id)
    repository = project_store.ensure_clone(record)
    project_store.fetch_integrate(repository, for_write=True)
    for repo_kind in ("state", "code"):
        entry = credentials.get(repo_kind)
        if entry is None:
            continue
        project_store.upsert_git_credential(project_id, repo_kind, entry["method"], entry.get("secret"))
        record_pat_event(repository, "git_credential.configured", actor, {
            "project_id": project_id,
            "repo": repo_kind,
            "method": entry["method"],
        })
    project_store.apply_credential_to_clone(repository, record)


def _sigt_task_guard(request, task_id):
    """公开读端点（详情/状态匿名可读，T6 语义）的端点级 sigt 绑定校验：
    中间件对 GET 详情/状态放行，sigt 跨任务在这里拒（403 authz.task_mismatch）。"""
    context = getattr(request.state, "auth", None)
    if isinstance(context, dict) and context.get("kind") == "sigt" and context.get("task") != task_id:
        raise AuthorizationError(
            "authz.task_mismatch",
            f"token is bound to task {context.get('task')}, not {task_id}",
        )


def _verify_sigt_project(context, repository):
    """T17 钉名：sigt 的 repo 字段（项目 project_id 或兼容仓路径）必须与任务
    所属项目一致，不符即拒 403 authz.project_denied（零副作用）。"""
    if not isinstance(context, dict) or context.get("kind") != "sigt":
        return
    sigt_repo = context.get("repo")
    if sigt_repo is None:
        return
    expected = {sigt_repo_field(repository), str(Path(repository).resolve())}
    if sigt_repo not in expected:
        raise AuthorizationError(
            "authz.project_denied",
            f"sigt 绑定项目 {sigt_repo} 与任务所属项目不符",
            details={"sigt_project": sigt_repo, "task_project": sigt_repo_field(repository)},
        )


def evidence_endpoint_auth(request, task_id):
    """证据端点内授权：镜像中间件凭据解析（Bearer 会话/PAT/sigt + cookie 会话），
     anonymous 401 / PAT 403 / 他任务 sigt 403；正常返回 None。"""
    context, auth_error = authenticate_header(request.headers.get("Authorization"))
    if auth_error is not None:
        status_code, payload = auth_error
        return JSONResponse(status_code=status_code, content=payload)
    if context is None:
        cookie_token = request.cookies.get(authn.SESSION_COOKIE)
        cookie_session = authn.find_session(cookie_token) if cookie_token else None
        if cookie_session:
            context = {"kind": "user", "actor": cookie_session["user"], "task": None, "via": "cookie"}
    if context is None:
        return JSONResponse(
            status_code=401,
            content=error_payload("auth.required", "valid credentials are required for this endpoint"),
        )
    if context["kind"] == "pat":
        return JSONResponse(
            status_code=403,
            content=error_payload("authz.pat_scope_denied", "PAT 不可用于证据下载端点"),
        )
    if context["kind"] == "sigt" and context["task"] != task_id:
        return JSONResponse(
            status_code=403,
            content=error_payload("authz.task_mismatch", f"token is bound to task {context['task']}, not {task_id}"),
        )
    return None


def stage_workspace_evidence(uploaded_paths, workspace):
    """REQ-1：上传证据落工作区 evidence/ 持久化。上传本体在请求级临时目录、
    随请求销毁；提案 sync 阶段需按持久路径取字节，不经 staging 会在 sync 时
    报 submit.evidence_missing。重名加短后缀，不覆盖工作区既有文件。"""
    target_dir = Path(workspace) / "evidence"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    taken = {entry.name for entry in target_dir.iterdir()}
    for path in uploaded_paths:
        source = Path(path)
        name = unique_evidence_name(source.name or "evidence", taken)
        taken.add(name)
        target = target_dir / name
        shutil.copyfile(source, target)
        staged.append(str(target))
    return staged


def _issue_pat(project_record, user, label, scope, expires_in):
    """签发项目级 PAT：平台侧只存散列+元数据（含 project_id），完整令牌仅
    当次响应回显；pat.issued 六键事件落该凭据所属项目状态仓（单笔提交并 push）。"""
    label = (label or "").strip()
    if not label:
        raise PlatformError("pat.label_required", "label（执行者标签）必填，归属落账用")
    validate_actor_alias(label)
    expires_at = authn.pat_expires_at(expires_in)  # 先校验档位（无效请求零痕迹）
    cleaned = []
    for item in scope or []:
        if item not in authn.PAT_SCOPES:
            raise PlatformError("pat.invalid_scope", f"scope is not issuable: {item}")
        if item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        raise PlatformError("pat.invalid_scope", "at least one issuable scope must be granted")
    token = authn.mint_pat(user, cleaned, label, expires_at, project_id=project_record["project_id"])
    record = authn.register_pat(user, label, cleaned, label, expires_in, token, project_id=project_record["project_id"])
    repository = project_store.ensure_clone(project_record)
    project_store.fetch_integrate(repository, for_write=True)
    record_pat_event(repository, "pat.issued", user, {
        "pat_id": record["pat_id"],
        "label": record["label"],
        "obo": label,
        "scope": cleaned,
        "expires_at": record["expires_at"],
        "project_id": project_record["project_id"],
    })
    return record, token


def _pat_project_for_issue(project_id):
    """POST /api/pats 项目解析与前置校验（钉名序：project.required →
    project.not_found → project.disabled）。"""
    if not str(project_id or "").strip():
        raise PlatformError("project.required", "project_id 必填：PAT 为项目级凭据")
    record = project_store.get_project(str(project_id).strip())
    if record is None:
        raise NotFoundError("project.not_found", f"project does not exist: {project_id}")
    if record.get("status") == "disabled":
        raise PlatformError("project.disabled", f"project {project_id} is disabled; credentials cannot be issued", exit_code=3)
    return record


def _requirement_attribution(request):
    """需求动词归因：PAT → actor=签发人 / on_behalf_of=执行者标签；本人会话 → 双字段=登录人。"""
    context = credentials(request)
    if context["kind"] == "pat":
        return context["actor"], context["obo"]
    if context["kind"] == "user":
        return context["actor"], context["actor"]
    raise AuthorizationError("authz.sigt_scope_denied", "sigt 令牌不可用于需求起草动词端点")


def _pat_scope_required(request, scope):
    context = credentials(request)
    if context["kind"] == "pat" and scope not in (context.get("scope") or []):
        raise AuthorizationError("authz.pat_scope_denied", f"PAT 未授予 {scope}")
