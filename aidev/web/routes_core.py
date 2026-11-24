from fastapi import Request
from fastapi.responses import Response

from .. import authn, distribution
from .authnz import ok, perform_login
from .models import LoginBody


def register(app):
    app.get("/api/health")(health)
    app.get("/api/skill")(skill_endpoint)
    app.get("/api/norms/{name:path}")(norm_endpoint)
    app.post("/api/login")(api_login)


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


def api_login(response: Response, body: LoginBody):
    token = perform_login(body.user, body.password)
    response.set_cookie(authn.SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
    return ok({"token": token, "user": body.user})
