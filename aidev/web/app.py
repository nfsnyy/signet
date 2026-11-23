import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from starlette.concurrency import run_in_threadpool

from .. import authn, distribution, readmodel
from .. import projects as project_store
from ..collaboration import collab_answer, collab_send, collaboration_overview
from ..domain import lease_is_active, task_path, validate_actor_alias, make_event, event_path
from ..errors import AuthorizationError, NotFoundError, PlatformError
from ..gitrepo import commit_paths
from ..jsonio import append_jsonl
from ..lifecycle import adjudicate_task, close_task, reject_task, ship_task, verify_task, void_task
from ..service import (
    block,
    block_authoritative,
    build_task_package,
    claim,
    claim_authoritative,
    claim_with_sigt,
    closeout_requirement,
    confirm_requirement,
    create_requirement,
    enhancement,
    evidence_manifest,
    init_repository,
    init_task,
    merge_backlog_item,
    normalize_req_items,
    project_metadata,
    record_pat_event,
    resolve_spec_asset,
    resolve_task_evidence,
    sigt_repo_field,
    stamp_events,
    status,
    submit,
    submit_authoritative,
    submit_requirement,
    synchronize_uploaded,
    task_log,
    unique_evidence_name,
    update_requirement,
    validate_requirement_derive,
)
from . import pages
from ..specs import freeze_task, refreeze_task
from ..taskfile import read_task
from ..vetoes import decide_veto, list_vetoes, propose_veto, settle_expired_vetoes

ACTOR_FIELDS = ("actor", "on_behalf_of", "h1_actor", "h2_actor", "verifier", "confirmed_by")
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
    repo: str | None = None
    actor: str | None = None
    on_behalf_of: str | None = None


class ProjectRegisterBody(ActorValidatingModel):
    name: str | None = None
    state_repo: str | None = None
    code_repo: str | None = None
    credentials: dict | None = None
    actor: str | None = None


class ProjectConfigureBody(ActorValidatingModel):
    name: str | None = None
    credentials: dict | None = None
    state_repo: str | None = None
    code_repo: str | None = None
    actor: str | None = None


class CredentialRemoveBody(ActorValidatingModel):
    repo: str | None = None
    actor: str | None = None


class PatRevokeBody(ActorValidatingModel):
    """POST /api/pats/{pat_id}/revoke 的可选 JSON 体（空体合法）。"""

    actor: str | None = None


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
    req_items: list | None = None


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


class PatIssueBody(BaseModel):
    label: str
    scope: list
    expires_in: str
    project_id: str | None = None


class RequirementCreateBody(BaseModel):
    requirement_id: str
    title: str
    body: str | None = None
    entries: list = []
    source: str | None = None
    repo: str | None = None


class RequirementUpdateBody(BaseModel):
    body: str | None = None
    entries: list = []
    source: str | None = None
    repo: str | None = None


class MergeBody(BaseModel):
    backlog_task_id: str
    entry_id: str


class ConfirmBody(BaseModel):
    entries: list = []
    repo: str | None = None


def save_upload(upload, directory, fallback_name):
    if upload is None:
        return None
    filename = Path(upload.filename or fallback_name).name or fallback_name
    target = Path(directory) / filename
    target.write_bytes(upload.file.read())
    return str(target)


def parse_asset_manifest(manifest):
    """AC-05（api-remote-ready）：asset_manifest 目标形态——[{"path","sha256"}]
    对象列表；旧纯字符串列表或条目缺键一律 c3.invalid_fixture；路径越界
    c3.invalid_asset_path。返回 [(path, sha256), ...]。"""
    try:
        entries = json.loads(manifest or "[]")
    except json.JSONDecodeError:
        raise PlatformError("c3.invalid_fixture", "asset manifest is not valid JSON")
    if not isinstance(entries, list):
        raise PlatformError("c3.invalid_fixture", "asset manifest must be a list")
    parsed = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PlatformError("c3.invalid_fixture", "each asset manifest entry must be an object with path and sha256")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative.strip() or not isinstance(digest, str) or len(digest) != 64:
            raise PlatformError("c3.invalid_fixture", "each asset manifest entry must declare path and a 64-hex sha256")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PlatformError("c3.invalid_asset_path", f"asset escapes the spec directory: {relative}")
        parsed.append((relative_path.as_posix(), digest))
    return parsed


def _contract_asset_declarations(contract_path):
    """宽容解析上传的 contract.json 取 assets 声明；解析失败返回 None——
    契约合法性由 freeze_task 的 c2.invalid_contract 统一裁决。"""
    try:
        document = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    assets = document.get("assets")
    return assets if isinstance(assets, dict) else {}


def materialize_assets(uploads, manifest, temporary, contract_path=None):
    """AC-04/AC-05：资产内容上传——逐件校验 sha256：manifest 摘要 vs 上传字节、
    contract 声明 vs 上传字节，任一不符 → c3.content_hash_mismatch 整次拒绝；
    通过后写入请求级临时目录。"""
    entries = parse_asset_manifest(manifest)
    declared = _contract_asset_declarations(contract_path) if contract_path else {}
    uploads = uploads or []
    if len(uploads) != len(entries):
        raise PlatformError("c3.invalid_fixture", "uploaded assets do not match the contract asset manifest")
    for (relative, digest), upload in zip(entries, uploads):
        content = upload.file.read()
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            raise PlatformError("c3.content_hash_mismatch", f"asset content hash mismatch: {relative}")
        declared_digest = declared.get(relative)
        if declared_digest is not None and declared_digest != actual:
            raise PlatformError(
                "c3.content_hash_mismatch",
                f"declared asset hash does not match the uploaded content: {relative}",
            )
        target = Path(temporary) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256):
    """AC-05：三件套随件摘要校验——任一摘要值与上传字节不符 →
    c3.content_hash_mismatch 整次拒绝（零副作用：不写规格目录/不落事件）。"""
    expected = {
        "card_sha256": (card_path, card_sha256),
        "contract_sha256": (contract_path, contract_sha256),
        "mapping_sha256": (mapping_path, mapping_sha256),
    }
    for field, (path, digest) in expected.items():
        if not digest or not path:
            raise PlatformError("request.invalid", f"{field} is required with the uploaded specification files")
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != digest:
            raise PlatformError(
                "c3.content_hash_mismatch",
                f"{field} does not match the uploaded content",
                details={"field": field, "expected": digest, "actual": actual},
            )


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

    def resolve_repo(request_repo, for_write=False, session=None):
        """T17 泛化解析次序：① 省略 → 会话活动项目（无则内置项目=绑定存储）；
        ② 路径形态 → 400 repo.invalid_identifier（T16 钉名，零副作用）；
        ③ 逻辑 id=内置 project_id → 绑定存储（T16 语义不动）；
        ④ 命中注册表 → 该项目服务端克隆（读前 fetch 整合 / 写前 fetch+冲突裁决：
        behind 自动 ff 整合，diverged 写动词拒 sync.diverged_remote 零副作用）；
        ⑤ 未知逻辑 id → 404 repo.not_found（T16 钉名）。停用项目 → 409
        project.disabled（读写一律，零副作用）。未绑定服务维持本地仓直用语义。"""
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

    def task_repository(request, task_id, repo_value, for_write=False):
        """任务端点 repo 定位：显式 repo → 该项目内必须存在（跨项目读 → 404
        task.not_found）；省略 → 活动/绑定存储优先，未命中按注册表克隆检索
        （项目 A 的任务经省略 repo 的写动词/claims 可达，B 上下文显式读仍 404）。
        全局定位命中后同样执行项目状态/凭据边界（AC-03/AC-09）：定位项目停用
        → 409 project.disabled（读写一律，先于任何读写副作用）；PAT 定位到绑定
        项目之外 → 403 authz.project_denied（先边界后状态，与中间件同序）。"""
        context = request_context(request)
        if repo_value:
            repository = resolve_repo(repo_value, for_write=for_write, session=context)
            if not task_path(repository, task_id).is_file():
                raise NotFoundError("task.not_found", f"task does not exist in project context: {task_id}")
            return repository
        repository = resolve_repo(None, for_write=for_write, session=context)
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

    async def _pat_target_project(request, context):
        """PAT 项目边界裁决的目标项目解析：/api/projects/{pid} 路径段 → repo 查询
        参数 → JSON 体 project_id/repo（sync/PAT 通道体均为 JSON；credentials/remove
        体的 repo 为凭据种类 state|code，不作项目解析）→ 会话活动项目 → 内置项目。
        未知/路径形态返回原值由下游钉名错误处理（边界裁决只比对注册表已知项目）。"""
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

    async def _guard_public_read(request):
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
            target = await _pat_target_project(request, context)
            pat_project = context.get("project_id")
            if _known_project_id(target) and pat_project and target != pat_project:
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

    def _known_project_id(value):
        if not value:
            return False
        if configured_repo and value == project_metadata(configured_repo):
            return True
        record = project_store.get_project(value)
        return record is not None

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
                    guarded = await _guard_public_read(request)
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
                target = await _pat_target_project(request, context)
                pat_project = context.get("project_id")
                if _known_project_id(target) and pat_project and target != pat_project:
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

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # T15 skill 分发与平台可获取性（REQ-1/REQ-6）：EP-01 /api/skill、
    # EP-02 /api/norms/{name}——匿名可读的只读分发面，同源同字节。
    # ------------------------------------------------------------------

    @app.get("/api/skill")
    def skill_endpoint(request: Request):
        document = distribution.read_skill()
        return Response(
            content=document["content"].encode("utf-8"),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/api/norms/{name:path}")
    def norm_endpoint(request: Request, name: str):
        document = distribution.read_norm(None, name)
        body = document["content"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        # X-Signet-Norm 携「{name} v{version}@{commit}」（AC-07）。HTTP 头值经
        # latin-1 上线路由：非 ASCII 名（中文规范名）以 UTF-8 字节传输，客户端按
        # UTF-8 解码即原样标识。
        norm_label = f"{document['name']} v{document['version']}@{document['commit']}"
        norm_label = norm_label.encode("utf-8").decode("latin-1")
        return Response(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers={"X-Signet-Norm": norm_label},
        )

    @app.post("/api/login")
    def api_login(response: Response, body: LoginBody):
        token = perform_login(body.user, body.password)
        response.set_cookie(authn.SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
        return ok({"token": token, "user": body.user})

    @app.get("/api/project")
    def project_endpoint(request: Request):
        return ok(readmodel.project_snapshot(resolve_repo(None)))

    @app.get("/api/requirements")
    def requirements_endpoint(request: Request, repo: str | None = None):
        return ok({"requirements": readmodel.list_requirements(resolve_repo(repo, session=credentials(request)))})

    @app.get("/api/requirements/{requirement_id}")
    def requirement_detail_endpoint(request: Request, requirement_id: str, repo: str | None = None):
        detail = readmodel.requirement_detail(resolve_repo(repo, session=credentials(request)), requirement_id)
        if detail is None:
            raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
        return ok(detail)

    # ------------------------------------------------------------------
    # T9 需求起草流：settings 签发面 + PAT 生命周期 + 需求动词 + confirm。
    # ------------------------------------------------------------------

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

    @app.get("/settings")
    def settings_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_settings(
            session["user"], authn.list_pats(session["user"]), project_store.list_projects(),
            project_ctx=_page_project_context(session)))

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
            session["user"], record, token, project_ctx=_page_project_context(session)))

    @app.post("/api/pats")
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

    @app.get("/api/pats")
    def pats_list_endpoint(request: Request):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "仅本人可见本人 PAT")
        return ok({"pats": authn.list_pats(context["actor"])})

    @app.post("/api/pats/{pat_id}/revoke")
    def pats_revoke_endpoint(request: Request, pat_id: str, body: PatRevokeBody | None = None):
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
        record_pat_event(resolve_repo(None), "pat.revoked", context["actor"], {"pat_id": pat_id, "project_id": project_id})
        return ok({"pat_id": pat_id, "status": record["status"]})

    @app.post("/api/requirements")
    def requirement_create_endpoint(request: Request, body: RequirementCreateBody):
        actor, on_behalf_of = _requirement_attribution(request)
        _pat_scope_required(request, "requirement:create")
        return ok(create_requirement(
            resolve_repo(body.repo, for_write=True, session=credentials(request)),
            body.requirement_id, body.title, body.body, body.entries, actor, on_behalf_of,
            source=body.source))

    @app.post("/api/requirements/{requirement_id}")
    def requirement_update_endpoint(request: Request, requirement_id: str, body: RequirementUpdateBody):
        actor, on_behalf_of = _requirement_attribution(request)
        _pat_scope_required(request, "requirement:update")
        return ok(update_requirement(
            resolve_repo(body.repo, for_write=True, session=credentials(request)),
            requirement_id, body.body, body.entries, actor, on_behalf_of,
            source=body.source))

    @app.post("/api/requirements/{requirement_id}/submission")
    def requirement_submit_endpoint(request: Request, requirement_id: str, body: ConfirmBody | None = None):
        actor, on_behalf_of = _requirement_attribution(request)
        _pat_scope_required(request, "requirement:submit")
        repo = body.repo if body else None
        return ok(submit_requirement(
            resolve_repo(repo, for_write=True, session=credentials(request)),
            requirement_id, actor, on_behalf_of))

    @app.post("/api/requirements/{requirement_id}/closeout")
    def requirement_closeout_endpoint(request: Request, requirement_id: str, repo: str | None = None):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "需求收口只认本人交互登录，任何凭证不可代过")
        return ok(closeout_requirement(
            resolve_repo(repo, for_write=True, session=context), requirement_id, context["actor"]))

    @app.post("/api/requirements/{requirement_id}/backlog-merge")
    def requirement_backlog_merge_endpoint(request: Request, requirement_id: str, body: MergeBody):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "backlog 归并只认本人交互登录，任何凭证不可代过")
        return ok(merge_backlog_item(
            resolve_repo(None, for_write=True, session=context),
            requirement_id, body.backlog_task_id, body.entry_id, context["actor"]))

    @app.post("/api/requirements/{requirement_id}/confirm")
    async def requirement_confirm_endpoint(request: Request, requirement_id: str, repo: str | None = None):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "confirm 只认本人交互登录，任何凭证不可代过")
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
            form = await request.form()
            entries = [str(value) for value in form.getlist("entries")]
            confirm_requirement(
                resolve_repo(repo, for_write=True, session=context), requirement_id, entries, context["actor"])
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
            resolve_repo(repo, for_write=True, session=context), requirement_id, entries, context["actor"]))

    @app.get("/")
    def overview(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        snapshot = readmodel.project_snapshot(resolve_repo(None, session=session))
        return HTMLResponse(pages.render_overview(
            session["user"], snapshot, project_ctx=_page_project_context(session)))

    @app.get("/requirements")
    def requirements_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        entries = readmodel.list_requirements(resolve_repo(None, session=session))
        return HTMLResponse(pages.render_requirements(
            session["user"], entries, project_ctx=_page_project_context(session)))

    @app.get("/requirements/{requirement_id}")
    def requirement_detail_page(request: Request, requirement_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.requirement_detail(resolve_repo(None, session=session), requirement_id)
        if detail is None:
            raise PlatformError("requirement.not_found", f"requirement does not exist: {requirement_id}")
        return HTMLResponse(pages.render_requirement_detail(
            session["user"], detail, project_ctx=_page_project_context(session)))

    @app.get("/tasks")
    def tasks_board_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_board(
            session["user"], readmodel.task_board(resolve_repo(None, session=session)),
            project_ctx=_page_project_context(session)))

    @app.get("/tasks/new")
    def task_new_page(request: Request, requirement: str | None = None):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_task_form(
            session["user"], requirement, project_ctx=_page_project_context(session)))

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
        validate_requirement_derive(resolve_repo(None, session=session), requirement)
        # 与 POST /api/tasks 同一条创建路径：owner 锁定为登录用户；workspace 由
        # 客户端在任务创建后自行落地（api-remote-ready，REQ-3）。
        result = init_task(resolve_repo(None, for_write=True, session=session), None, title, requirement,
                           session["user"], session["user"], req_items=req_items)
        return RedirectResponse(url="/tasks/{}".format(result["task_id"]), status_code=303)

    @app.get("/tasks/{task_id}")
    def task_detail_page(request: Request, task_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        detail = readmodel.task_detail(resolve_repo(None, session=session), task_id)
        if detail is None:
            raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
        return HTMLResponse(pages.render_task_detail(
            session["user"], detail, project_ctx=_page_project_context(session)))

    @app.get("/guide")
    def guide_page(request: Request):
        # T15/REQ-3/AC-03：接入指南——匿名可读（规范非机密、获取动作经 API）。
        return HTMLResponse(pages.render_guide())

    # ------------------------------------------------------------------
    # T17 页面面：/projects（注册+列表，FORM-01）、/projects/{id}（详情+
    # 配置+凭据+停用/启用）、全站顶栏项目切换器（POST .../switch，button-only
    # 零命名字段表单，303 回来源页；未登录一律跳登录页——首版无只读用户）。
    # ------------------------------------------------------------------

    def _page_project_context(session):
        current = None
        current_id = session.get("active_project")
        if current_id:
            current = project_store.get_project(current_id)
        if current is None and configured_repo:
            builtin_pid = project_metadata(configured_repo)
            if builtin_pid:
                current = project_store.get_project(builtin_pid)
        return {"current": current, "projects": project_store.list_projects()}

    def _project_recent_events(record, limit=5):
        repository = configured_repo
        if not record.get("builtin"):
            try:
                repository = project_store.ensure_clone(record)
                project_store.fetch_integrate(repository, for_write=False)
            except PlatformError:
                repository = None
        if repository is None:
            return []
        return readmodel.recent_events(repository, limit=limit)

    @app.get("/projects")
    def projects_page(request: Request):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        return HTMLResponse(pages.render_projects(
            session["user"], project_store.list_projects(), error=None, project_ctx=_page_project_context(session)))

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
                project_ctx=_page_project_context(session)))
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
            _project_recent_events(record),
            notice=None,
            project_ctx=_page_project_context(session),
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
                _platform_event("project.configured", session["user"], {
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
            _platform_event("project.disabled", session["user"], {"project_id": project_id})
        return RedirectResponse(url="/projects", status_code=303)

    @app.post("/projects/{project_id}/enable")
    def project_enable_page(request: Request, project_id: str):
        session = page_session(request)
        if session is None:
            return RedirectResponse(url="/login", status_code=302)
        record = _require_mutable_project(project_id)
        if record.get("status") == "disabled":
            project_store.update_project(project_id, status="active")
            _platform_event("project.enabled", session["user"], {"project_id": project_id})
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

    @app.get("/api/tasks")
    def task_board_endpoint(request: Request, repo: str | None = None):
        return ok({"tasks": readmodel.task_board(resolve_repo(repo, session=credentials(request)))})

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

    @app.get("/api/tasks/{task_id}")
    def task_detail_endpoint(request: Request, task_id: str, repo: str | None = None):
        _sigt_task_guard(request, task_id)
        repository = task_repository(request, task_id, repo)
        detail = readmodel.task_detail(repository, task_id)
        if detail is None:
            raise NotFoundError("task.not_found", f"task does not exist: {task_id}")
        return ok(detail)

    @app.get("/api/tasks/{task_id}/status")
    def status_endpoint(request: Request, task_id: str, workspace: str | None = Query(default=None), repo: str | None = None):
        if workspace:
            workspace_task_id(workspace, task_id)
            return ok({**status(workspace), "task_id": task_id})
        _sigt_task_guard(request, task_id)
        repository = task_repository(request, task_id, repo)
        metadata = load_authoritative_task(repository, task_id)
        return ok({"task": metadata, "inbox": [], "outbox_collab": [], "authoritative": True, "task_id": task_id})

    @app.get("/api/tasks/{task_id}/package")
    def task_package_endpoint(request: Request, task_id: str, repo: str | None = None):
        # T15/REQ-2/AC-02：只读幂等任务包读面（claims POST 不入 interfaces 声明，
        # 为 conformance 提供探针安全的 GET 通路；匿名 401，三凭据族 200）。
        # api-remote-ready：repo 仅接受逻辑 id 或省略（AC-01/02 同口径）——
        # CLI 投影拉取携带 --repo 时经查询参数透传，未绑定服务维持本地仓语义。
        repository = task_repository(request, task_id, repo)
        metadata, body = read_task(task_path(repository, task_id))
        return ok(build_task_package(repository, metadata, body))

    @app.get("/api/tasks/{task_id}/events")
    def events_endpoint(
        request: Request,
        task_id: str,
        repo: str | None = None,
        role: str = Header(default="operator", alias="X-Signet-Role"),
    ):
        repository = task_repository(request, task_id, repo)
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
        repo_value = body.repo if body else None
        if context["kind"] == "sigt":
            # sigt 幂等接管：同 actor 续期（lease.renewed）/ 无租约重 claim，
            # 响应携带完整任务包（AC-01/AC-03）。AC-16：on_behalf_of 记录执行者（CLI 本人登录身份）。
            # T17：任务按注册表全局定位；sigt 项目绑定不符即拒（403 authz.project_denied）。
            repository = task_repository(request, task_id, repo_value, for_write=True)
            return ok(claim_with_sigt(
                repository, task_id, context["actor"], on_behalf_of, sigt_repo=context.get("repo")))
        repository = task_repository(request, task_id, repo_value, for_write=True)
        return ok(claim_authoritative(repository, task_id, request_actor(request)))

    @app.get("/api/tasks/{task_id}/assets/{name:path}")
    def task_asset_endpoint(request: Request, task_id: str, name: str, repo: str | None = None):
        # api-remote-ready：repo 查询参数可选（AC-01/02 同口径），CLI 投影落地
        # 携带 --repo 时透传；省略落服务端绑定存储/活动项目。
        repository = task_repository(request, task_id, repo)
        metadata = load_authoritative_task(repository, task_id)
        asset_path = resolve_spec_asset(repository, metadata, name)
        return Response(content=asset_path.read_bytes(), media_type="application/octet-stream")

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

    @app.get("/api/tasks/{task_id}/evidence/{name:path}")
    def task_evidence_endpoint(request: Request, task_id: str, name: str):
        # REQ-2：登录用户 Bearer/cookie 可读任意任务证据（200）；本任务 sigt 200、他任务 sigt 403；
        # 匿名对有效名 401；清单外 404；穿越 4xx 且不读仓外文件。名与路径解析先于授权。
        repository = resolve_repo(None)
        load_authoritative_task(repository, task_id)
        evidence_path = resolve_task_evidence(repository, task_id, name)
        auth_failure = evidence_endpoint_auth(request, task_id)
        if auth_failure is not None:
            return auth_failure
        return Response(content=evidence_path.read_bytes(), media_type="application/octet-stream")

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
                evidence_paths = stage_workspace_evidence(evidence_paths, workspace)
                return ok(submit(workspace, context["actor"], context["actor"], report_path, evidence_paths))
            # 无工作区的编排直通：凭据必须是绑定到本任务的 sigt（中间件已做任务匹配），
            # 与 claim_authoritative 对称直接改写权威账本。AC-16：on_behalf_of 记录执行者。
            if context["kind"] != "sigt":
                raise AuthorizationError(
                    "authz.sigt_required",
                    "submission without a workspace requires a task capability token (sigt)",
                )
            repository = task_repository(request, task_id, None, for_write=True)
            _verify_sigt_project(context, repository)
            if not evidence_paths:
                evidence_paths = [report_path]
            return ok(submit_authoritative(repository, task_id, context["actor"], report_path, evidence_paths, on_behalf_of))

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
                repository = task_repository(request, task_id, None, for_write=True)
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
        repository = task_repository(request, task_id, None, for_write=True)
        return ok(block_authoritative(repository, task_id, context["actor"], reason, evidence_docs))

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
        # T9 派生闸门（AC-05）：交互会话（cookie，页面同源动线）派生须需求存在且 confirmed；
        # operator CLI（Bearer 用户令牌）保留自由引用，兼容 T1–T8 既有工作流与基线测试。
        # T10 条目引用校验（AC-01）同口径：仅会话路径校验 req_items 命中「判据条目」节。
        context = credentials(request)
        session_path = context["kind"] == "user" and context.get("via") == "cookie"
        if session_path:
            validate_requirement_derive(resolve_repo(None), requirement)
        # 身份锁定（T2 AC-09 语义）：owner 与事件归因均以凭据身份为准，请求体所填 owner 被会话覆盖。
        actor = request_actor(request)
        # api-remote-ready（REQ-3/AC-07）：workspace 参数语义移除——服务端不创建/写
        # client 工作区；客户端在 init-task 成功后经任务包拉取自行落地投影。
        return ok(init_task(
            resolve_repo(body.repo, for_write=True, session=context), None, title, requirement, actor, actor,
            req_items=body.req_items, validate_req_items=session_path))

    # ------------------------------------------------------------------
    # T17 multi-project-support：项目注册表生命周期面（REQ-1）。
    # project.* 事件落平台绑定仓（注册表生命周期是平台级账本事件，且停用窗口
    # 项目远端 HEAD 不得前进——AC-03 钉名零副作用）；凭据族事件落各项目仓（AC-19）。
    # ------------------------------------------------------------------

    def _platform_event(event_type, actor, payload):
        """项目注册表生命周期事件：落平台绑定仓（单笔提交，T16 push 语义不变）。"""
        repository = resolve_repo(None)
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

    @app.get("/api/projects")
    def projects_list_endpoint(request: Request):
        return ok({"projects": project_store.list_projects()})

    @app.post("/api/projects")
    def projects_register_endpoint(request: Request, body: ProjectRegisterBody | None = None):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "项目注册只认本人登录（用户令牌或会话 cookie），任何凭证不可代注册")
        body = body or ProjectRegisterBody()
        platform_repo = os.environ.get("SIGNET_PLATFORM_REPO")
        builtin_pid = project_metadata(configured_repo) if configured_repo else None
        record = project_store.register_project(
            body.name, body.state_repo, body.code_repo, body.credentials,
            context["actor"], platform_repo, configured_repo, builtin_pid,
        )
        # project.registered 事件随骨架首笔提交落项目状态仓（见 projects.skeleton_tree）。
        return ok({"project": record})

    @app.get("/api/projects/{project_id}")
    def projects_detail_endpoint(request: Request, project_id: str):
        return ok({"project": _require_project_record(project_id)})

    @app.post("/api/projects/{project_id}/configure")
    def projects_configure_endpoint(request: Request, project_id: str, body: ProjectConfigureBody | None = None):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "项目配置只认本人登录，任何凭证不可代配置")
        record = _require_mutable_project(project_id)
        if record.get("status") == "disabled":
            raise PlatformError("project.disabled", f"project {project_id} is disabled; configure is rejected", exit_code=3)
        body = body or ProjectConfigureBody()
        if body.state_repo is not None or body.code_repo is not None:
            raise PlatformError(
                "project.repo_immutable",
                "首版不支持 state_repo/code_repo 改绑：仓库地址一经注册不可变更（零副作用）",
            )
        fields = []
        if body.name is not None and str(body.name).strip() and body.name != record.get("name"):
            record = project_store.update_project(project_id, name=str(body.name).strip())
            fields.append("name")
        supplied_credentials = body.credentials or {}
        if supplied_credentials:
            cleaned = project_store.validate_supplied_credentials(supplied_credentials)
            _apply_git_credential_events(project_id, cleaned, context["actor"])
            fields.extend(f"credentials.{kind}" for kind in ("state", "code") if kind in cleaned)
        if fields:
            _platform_event("project.configured", context["actor"], {
                "project_id": project_id,
                "fields": fields,
            })
        return ok({"project": _require_project_record(project_id)})

    @app.post("/api/projects/{project_id}/disable")
    def projects_disable_endpoint(request: Request, project_id: str):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "项目停用只认本人登录，任何凭证不可代操作")
        record = _require_mutable_project(project_id)
        if record.get("status") != "disabled":
            record = project_store.update_project(project_id, status="disabled")
            _platform_event("project.disabled", context["actor"], {"project_id": project_id})
        return ok({"project": _require_project_record(project_id)})

    @app.post("/api/projects/{project_id}/enable")
    def projects_enable_endpoint(request: Request, project_id: str):
        context = credentials(request)
        if context["kind"] != "user":
            raise AuthorizationError("authz.session_required", "项目启用只认本人登录，任何凭证不可代操作")
        record = _require_mutable_project(project_id)
        if record.get("status") == "disabled":
            record = project_store.update_project(project_id, status="active")
            _platform_event("project.enabled", context["actor"], {"project_id": project_id})
        return ok({"project": _require_project_record(project_id)})

    @app.get("/api/projects/{project_id}/credentials")
    def projects_credentials_endpoint(request: Request, project_id: str):
        _require_project_record(project_id)
        return ok({"credentials": project_store.project_credentials_view(project_id)})

    @app.post("/api/projects/{project_id}/credentials/remove")
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
        card_sha256: str = Form(...),
        contract_sha256: str = Form(...),
        mapping_sha256: str = Form(...),
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
            # AC-05：三件套随件摘要校验（不符 → c3.content_hash_mismatch 零副作用）。
            verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
            materialize_assets(assets, asset_manifest, temporary, contract_path)
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
        card_sha256: str = Form(...),
        contract_sha256: str = Form(...),
        mapping_sha256: str = Form(...),
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
            verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
            materialize_assets(assets, asset_manifest, temporary, contract_path)
            return ok(refreeze_task(repository, task_id, workspace, card_path, contract_path, mapping_path, operation_source_path, h1_actor, h2_actor, reason, push))

    @app.post("/api/sync")
    async def sync_endpoint(request: Request):
        # AC-08（api-remote-ready）：sync 改内容上传——请求体 {repo?, actor, push,
        # proposals}（proposals 必填，缺失 → 400 request.invalid）；submit 类提案的
        # 证据字节随 multipart 文件上传（按文件名匹配），服务端不按 client 路径读证据；
        # workspace 仅保留为 collab 经典通道（§8 范围外，维持现行服务端读语义），
        # 投影写回整体删除（客户端经任务包拉取自行落地）。
        content_type = request.headers.get("content-type", "")
        repo = None
        push = False
        proposals = None
        workspace_paths = []
        evidence_lookup = {}
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            repo = form.get("repo")
            push = str(form.get("push") or "").lower() == "true"
            raw_proposals = form.get("proposals")
            if raw_proposals is not None:
                try:
                    proposals = json.loads(raw_proposals)
                except (TypeError, ValueError):
                    raise PlatformError("request.invalid", "proposals is not valid JSON")
            raw_workspace = form.get("workspace")
            if raw_workspace:
                try:
                    workspace_value = json.loads(raw_workspace)
                except (TypeError, ValueError):
                    raise PlatformError("request.invalid", "workspace must be a JSON list of workspace paths")
                if not isinstance(workspace_value, list):
                    raise PlatformError("request.invalid", "workspace must be a JSON list of workspace paths")
                workspace_paths = [str(item) for item in workspace_value]
            for upload in form.getlist("evidence"):
                if upload is None or not getattr(upload, "filename", None):
                    continue
                name = Path(upload.filename).name
                evidence_lookup[name] = upload.file.read()
        else:
            try:
                payload = await request.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                raise PlatformError("request.invalid", "sync body must be a JSON object")
            repo = payload.get("repo")
            push = bool(payload.get("push"))
            proposals = payload.get("proposals")
            workspace_value = payload.get("workspace")
            if workspace_value is not None:
                if not isinstance(workspace_value, list):
                    raise PlatformError("request.invalid", "workspace must be a list of workspace paths")
                workspace_paths = [str(item) for item in workspace_value]
        # 异步接线（T16 返场修复，同 verify_endpoint）：synchronize_uploaded 为阻塞
        # 调用（git 锁/提交/远端对账），经线程池执行保持事件循环可响应。校验顺序
        # 保持 repo 解析（AC-01/02）→ proposals 校验 → 应用。
        def run_sync():
            repository = operator_repo(repo)
            if proposals is None:
                raise PlatformError("request.invalid", "proposals is required")
            if not isinstance(proposals, list):
                raise PlatformError("request.invalid", "proposals must be a list")
            return synchronize_uploaded(
                repository,
                proposals,
                evidence_lookup,
                request_actor(request),
                push,
                workspace_paths=workspace_paths,
            )

        result = await run_in_threadpool(run_sync)
        return ok(result)

    @app.post("/api/tasks/{task_id}/verify")
    async def verify_endpoint(request: Request, task_id: str):
        # 手工解析（替代 FastAPI Form 注入）：AC-01/02 要求 repo 路径形态/未知逻辑 id
        # 的拒绝先于字段校验——JSON 体（编排探针）与 multipart 表单（CLI）双形态。
        content_type = request.headers.get("content-type", "")
        temporary = tempfile.TemporaryDirectory(prefix="aidev-web-verify-")
        try:
            results_file = manual_results_file = review_report_file = None
            if content_type.startswith("multipart/form-data"):
                form = await request.form()
                repo = form.get("repo")
                verifier = form.get("verifier")
                confirmed_by = form.get("confirmed_by")
                execute = str(form.get("execute") or "").lower() == "true"
                push = str(form.get("push") or "").lower() == "true"
                for field, fallback in (
                    ("results", "results.json"),
                    ("manual_results", "manual-results.json"),
                    ("review_report", "ai-review-report.json"),
                ):
                    upload = form.get(field)
                    if upload is None or not getattr(upload, "filename", None):
                        continue
                    path = Path(temporary.name) / fallback
                    path.write_bytes(upload.file.read())
                    if field == "results":
                        results_file = str(path)
                    elif field == "manual_results":
                        manual_results_file = str(path)
                    else:
                        review_report_file = str(path)
            elif content_type.startswith("application/json"):
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise PlatformError("request.invalid", "verify body must be a JSON object")
                repo = payload.get("repo")
                verifier = payload.get("verifier")
                confirmed_by = payload.get("confirmed_by")
                execute = str(payload.get("execute") or "").lower() == "true"
                push = str(payload.get("push") or "").lower() == "true"
            else:
                repo = verifier = confirmed_by = None
                execute = push = False

            # 异步接线（T16 返场修复）：verify_task 为阻塞调用（git 提交 + 净室子
            # 进程用例会回调本服务），必须经线程池执行——留在事件循环会使净室用例
            # 的 HTTP 自回调全部超时。校验顺序保持 repo 解析 → 字段校验 → verify_task。
            def run_verify():
                repository = operator_repo(repo)
                if not verifier or not confirmed_by:
                    raise PlatformError("request.invalid", "verifier and confirmed_by are required")
                check_actor_aliases({"verifier": verifier, "confirmed_by": confirmed_by})
                return verify_task(repository, task_id, verifier, confirmed_by, results_file, execute, manual_results_file, push, review_report_file=review_report_file)

            result = await run_in_threadpool(run_verify)
            return ok(result)
        finally:
            temporary.cleanup()

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
        card_sha256: str | None = Form(None),
        contract_sha256: str | None = Form(None),
        mapping_sha256: str | None = Form(None),
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
            # AC-04/AC-05：veto-propose 同 freeze 走内容上传 + 随件摘要校验。
            if propose and card_path and contract_path and mapping_path:
                verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256)
                materialize_assets(assets, asset_manifest, temporary, contract_path)
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
