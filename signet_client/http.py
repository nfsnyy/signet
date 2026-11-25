"""T21 core-modularization：客户端 HTTP 与配置域（自 signet_client.py 整函数搬入，函数体零变更）。

职责：常量与超时、ClientInputError/ServiceUnavailable、base_url 解析、multipart/perform、
错误输出原语（emit_error/emit_unreachable——供凭据/投影/分发各域共用，置于底层避免模块环）。
仅标准库，不得 import aidev。
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

REQUEST_TIMEOUT = os.environ.get("SIGNET_REQUEST_TIMEOUT", "60")

HTTP_EXIT_CODES = {400: 2, 409: 3}

ROLE_HEADER = "X-Signet-Role"

CLIENT_FILE = "client.json"

SECRET_FILE = "server-secret"

def timeout_seconds():
    try:
        return float(REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        return 60.0

class ClientInputError(Exception):
    def __init__(self, code, message, exit_code=2, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

class ServiceUnavailable(Exception):
    def __init__(self, base_url, reason):
        super().__init__(str(reason))
        self.base_url = base_url
        self.reason = reason

def user_home():
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile)
    return Path.home()

def client_config_path():
    return user_home() / ".signet" / CLIENT_FILE

def resolve_base_url():
    env_url = os.environ.get("SIGNET_BASE_URL")
    if env_url and env_url.strip():
        return env_url.rstrip("/")
    config_path = client_config_path()
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = None
        if isinstance(config, dict):
            base_url = config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                return base_url.rstrip("/")
    return DEFAULT_BASE_URL

def decode_json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None

def encode_multipart(fields, files):
    boundary = "----signetBoundary" + os.urandom(8).hex()
    body = bytearray()
    for name, value in fields.items():
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for name, filename, content in files:
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        )
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"

_REQUEST_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def perform(method, url, fields=None, files=None, role=None, token=None, sigt=None, raw=False):
    headers = {}
    if role:
        headers[ROLE_HEADER] = role
    if sigt:
        headers["Authorization"] = "sigt " + sigt
    elif token:
        headers["Authorization"] = "Bearer " + token
    if files is not None:
        body, content_type = encode_multipart(fields or {}, files)
        headers["Content-Type"] = content_type
    elif fields is not None:
        body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        body = None
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _REQUEST_OPENER.open(request, timeout=timeout_seconds()) as response:
            content = response.read()
            return (response.status, content) if raw else (response.status, decode_json(content))
    except urllib.error.HTTPError as error:
        content = error.read()
        return (error.code, content) if raw else (error.code, decode_json(content))
    except urllib.error.URLError as error:
        raise ServiceUnavailable(url, error.reason) from error

def emit_error(code, message, details, exit_code):
    response = {"ok": False, "error": {"code": code, "message": message, "details": details}}
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return exit_code

def emit_unreachable(error):
    print(f"错误：无法连接 signet 平台服务（{error.base_url}）：{error.reason}", file=sys.stderr)
    print("请先启动服务（例如：python -m aidev.web.server，默认监听 127.0.0.1:8000），", file=sys.stderr)
    print("或通过环境变量 SIGNET_BASE_URL 或 ~/.signet/client.json 指定已启动的服务地址。", file=sys.stderr)
    return 3
