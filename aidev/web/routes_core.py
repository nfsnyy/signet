from fastapi import Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response

import hashlib
import re
from pathlib import Path

from .. import authn, distribution
from ..errors import NotFoundError, PlatformError
from ..gitrepo import commit_paths, run_git
from ..jsonio import atomic_write_bytes
from .authnz import ok, perform_login, require_user_context
from .context import resolve_repo
from .models import DocPutBody, LoginBody, UserCreateBody, UserPasswordBody

# FastAPI 默认文档路由的钉名路径（基线行为等价重实现为模块级处理器，
# 消除 FastAPI.setup 闭包形态；路径/响应与默认实现逐字一致）。
OPENAPI_URL = "/openapi.json"
DOCS_URL = "/docs"
DOCS_OAUTH2_REDIRECT_URL = "/docs/oauth2-redirect"
REDOC_URL = "/redoc"


def register(app):
    app.get("/api/health")(health)
    app.get("/api/skill")(skill_endpoint)
    app.get("/api/norms/{name:path}")(norm_endpoint)
    app.get("/api/client/download")(client_download_endpoint)
    app.get("/api/client/version")(client_version_endpoint)
    app.post("/api/login")(api_login)
    app.get("/api/users")(users_list_endpoint)
    app.post("/api/users")(users_create_endpoint)
    app.post("/api/users/{user}/disable")(users_disable_endpoint)
    app.post("/api/users/{user}/password")(users_password_endpoint)
    app.get("/api/docs/{name:path}")(doc_get_endpoint)
    app.post("/api/docs/{name:path}")(doc_put_endpoint)
    app.get(OPENAPI_URL, include_in_schema=False)(openapi_json_endpoint)
    app.get(DOCS_URL, include_in_schema=False)(swagger_docs_endpoint)
    app.get(DOCS_OAUTH2_REDIRECT_URL, include_in_schema=False)(swagger_oauth2_redirect_endpoint)
    app.get(REDOC_URL, include_in_schema=False)(redoc_endpoint)


def health():
    return {"status": "ok"}


def skill_endpoint(request: Request):
    document = distribution.read_skill()
    return Response(
        content=document["content"].encode("utf-8"),
        media_type="text/plain; charset=utf-8",
    )


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


def client_download_endpoint(request: Request):
    # T22/REQ-1（EP-01）：匿名可达的版本化 zip 薄客户端包；字节确定性构建，
    # 与 EP-02 版本面 sha256/size 恒等。
    package = distribution.build_client_package()
    return Response(
        content=package["content"],
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="signet-client-{package["client_version"]}.zip"'},
    )


def client_version_endpoint(request: Request):
    # T22/REQ-1（EP-02）：版本面恰四键——client_version/api_version/sha256/size，
    # 与包内 manifest.json 对应字段一致。
    package = distribution.build_client_package()
    return ok({
        "client_version": package["client_version"],
        "api_version": package["api_version"],
        "sha256": package["sha256"],
        "size": package["size"],
    })


def api_login(response: Response, body: LoginBody):
    token = perform_login(body.user, body.password)
    response.set_cookie(authn.SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
    return ok({"token": token, "user": body.user})


# ---------------------------------------------------------------------------
# T23/REQ-5：账号日常生命周期远程通道（EP-01~EP-04）——仅本人登录；语义镜像
# aidev-admin（建号实时生效、停用即不可登录、重置口令解除停用），错误面沿用
# authn 现行钉名码；动作落 service-audit.jsonl（actor=操作 operator，source="api"）。
# ---------------------------------------------------------------------------


def users_list_endpoint(request: Request):
    require_user_context(request)
    return ok(authn.list_users())


def users_create_endpoint(request: Request, body: UserCreateBody):
    context = require_user_context(request)
    return ok(authn.create_user(body.user, body.password, audit_actor=context["actor"], audit_source="api"))


def users_disable_endpoint(request: Request, user: str):
    context = require_user_context(request)
    return ok(authn.disable_user(user, audit_actor=context["actor"], audit_source="api"))


def users_password_endpoint(request: Request, user: str, body: UserPasswordBody):
    context = require_user_context(request)
    return ok(authn.set_user_password(user, body.password, audit_actor=context["actor"], audit_source="api"))


# ---------------------------------------------------------------------------
# T23/REQ-5：hints/设计稿白名单文档带版本更新（EP-06/EP-07）——仅本人登录；
# 白名单恰为 hints.md 与 design/<单段名>.md，不开放任意状态仓写；乐观并发
# （base_sha256 匹配才落盘），LF 写入 + 状态仓 commit 留痕，不新增账本事件类型。
# ---------------------------------------------------------------------------

_DOC_ABSOLUTE_NAME = re.compile(r"^([A-Za-z]:)?[/\\]")


def _validate_doc_name(name):
    """白名单裁决：空名/绝对路径/父引用穿越 → 400 doc.invalid_name；形态合法但
    不在白名单（hints.md 与 design/<单段名>.md）→ 404 doc.not_found。"""
    label = str(name or "")
    if not label.strip():
        raise PlatformError("doc.invalid_name", "doc 名不得为空", details={"name": name})
    if _DOC_ABSOLUTE_NAME.match(label) or any(part == ".." for part in re.split(r"[/\\]", label)):
        raise PlatformError(
            "doc.invalid_name",
            "doc 名不得为绝对路径或含父引用穿越",
            details={"name": name},
        )
    if label == "hints.md":
        return label
    if label.startswith("design/"):
        rest = label[len("design/"):]
        if rest and "/" not in rest and "\\" not in rest and rest.endswith(".md"):
            return label
    raise NotFoundError("doc.not_found", f"doc 不在白名单（hints.md 与 design/<单段名>.md）: {label}")


def _state_repo_head(repository):
    head = run_git(repository, ["rev-parse", "HEAD"], check=False)
    value = (head.stdout or "").strip()
    return value if head.returncode == 0 and value else None


def doc_get_endpoint(request: Request, name: str):
    context = require_user_context(request)
    label = _validate_doc_name(name)
    repository = Path(resolve_repo(request.app.state.app_ctx, None, session=context))
    target = repository / label
    if not target.is_file():
        raise NotFoundError("doc.not_found", f"doc 不存在: {label}")
    raw = target.read_bytes()
    result = {
        "name": label,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": raw.decode("utf-8"),
    }
    head = _state_repo_head(repository)
    if head:
        result["commit"] = head
    return ok(result)


def doc_put_endpoint(request: Request, name: str, body: DocPutBody):
    context = require_user_context(request)
    label = _validate_doc_name(name)
    repository = resolve_repo(request.app.state.app_ctx, None, for_write=True, session=context)
    target = Path(repository) / label
    current = target.read_bytes() if target.is_file() else None
    if body.base_sha256 is None:
        if current is not None:
            raise PlatformError(
                "doc.version_conflict",
                "doc 已存在：base_sha256 缺省（null）仅允许创建新文档",
                exit_code=3,
                details={"name": label},
            )
    elif current is None or hashlib.sha256(current).hexdigest() != body.base_sha256:
        raise PlatformError(
            "doc.version_conflict",
            "base_sha256 与现行字节 sha256 不符（并发覆盖防护，零副作用）",
            exit_code=3,
            details={"name": label},
        )
    new_bytes = body.content.encode("utf-8")
    if current == new_bytes:
        # 幂等回写：字节不变即零副作用（无写盘/无 commit/无审计），commit 取现行 HEAD。
        return ok({"name": label, "sha256": hashlib.sha256(new_bytes).hexdigest(),
                   "commit": _state_repo_head(Path(repository))})
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, new_bytes)
    commit = commit_paths(repository, [target.relative_to(Path(repository))], f"Update doc {label}")
    authn.append_audit("update-doc", label, actor=context["actor"], source="api")
    return ok({"name": label, "sha256": hashlib.sha256(new_bytes).hexdigest(), "commit": commit})


async def openapi_json_endpoint(request: Request) -> JSONResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    schema = request.app.openapi()
    if root_path and request.app.root_path_in_servers:
        server_urls = {s.get("url") for s in schema.get("servers", [])}
        if root_path not in server_urls:
            schema = dict(schema)
            schema["servers"] = [{"url": root_path}] + schema.get("servers", [])
    return JSONResponse(schema)


async def swagger_docs_endpoint(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    openapi_url = root_path + OPENAPI_URL
    oauth2_redirect_url = DOCS_OAUTH2_REDIRECT_URL
    if oauth2_redirect_url:
        oauth2_redirect_url = root_path + oauth2_redirect_url
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title=f"{request.app.title} - Swagger UI",
        oauth2_redirect_url=oauth2_redirect_url,
        init_oauth=request.app.swagger_ui_init_oauth,
        swagger_ui_parameters=request.app.swagger_ui_parameters,
    )


async def swagger_oauth2_redirect_endpoint(request: Request) -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


async def redoc_endpoint(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    openapi_url = root_path + OPENAPI_URL
    return get_redoc_html(openapi_url=openapi_url, title=f"{request.app.title} - ReDoc")
