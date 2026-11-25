from fastapi import Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import authn, distribution
from .authnz import ok, perform_login
from .models import LoginBody

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
