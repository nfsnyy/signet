"""T21 core-modularization：客户端登录凭据与 SIGT 域（自 signet_client.py 整函数搬入）。

职责：client.json 令牌/用户解析、sigt 纯格式解析（parse_sigt）与「未验签」
解析展示（inspect_handoff）、workspace_handoff_token。
T22/REQ-2（AC-05/AC-07）：客户端不得持有服务端签名密钥、不做本地 HMAC 验签——
令牌真实性、有效期、任务/项目边界与权限一律由服务端裁决；本模块只做格式检查
与明确标注未验签的解析展示。仅标准库，不得 import aidev。
"""

import base64
import json
from pathlib import Path

from .http import client_config_path, emit_error

def resolve_token():
    config_path = client_config_path()
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    token = config.get("token")
    return token if isinstance(token, str) and token.strip() else None

def resolve_cli_user():
    """CLI 本人登录身份（client.json 的 user 字段），用于 sigt 委托流的 on_behalf_of 归因。"""
    config_path = client_config_path()
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    user = config.get("user")
    return user if isinstance(user, str) and user.strip() else None

def store_client_config(base_url, token, user=None):
    directory = client_config_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    config = {"base_url": base_url, "token": token}
    if user:
        config["user"] = user
    client_config_path().write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

def parse_sigt(token):
    """纯格式解析 handoff 能力令牌（sigt）——不验签、不联网。

    返回 (payload, error_code, message)：格式非法即 sigt.invalid。
    T22/REQ-2（AC-05）：客户端不得持有服务端签名密钥，令牌真实性、有效期、
    任务/项目边界与权限一律交服务端裁决；本地只解出负载供请求构造与展示。
    """
    if not isinstance(token, str) or not token.startswith("sigt-"):
        return None, "sigt.invalid", "handoff 令牌（sigt）格式不合法：不是 sigt- 前缀"
    rest = token[len("sigt-"):]
    if "-" not in rest:
        return None, "sigt.invalid", "handoff 令牌（sigt）格式不合法：缺少签名段"
    segment, signature = rest.rsplit("-", 1)
    if not segment or not signature:
        return None, "sigt.invalid", "handoff 令牌（sigt）格式不合法：签名段为空"
    try:
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return None, "sigt.invalid", "handoff 令牌（sigt）负载不是合法 JSON"
    if not isinstance(payload.get("task"), str) or not payload["task"]:
        return None, "sigt.invalid", "handoff 令牌（sigt）负载缺少 task"
    return payload, None, None

def inspect_handoff(token):
    """纯本地格式解析并展示 handoff 能力令牌（sigt）——不验签（verified 恒 false）。

    T22/REQ-2（AC-05）：只做格式检查与明确标注「未验签」的解析展示；
    令牌真实性由服务端裁决（claim --handoff 等链路一律服务端验签）。
    """
    payload, code, message = parse_sigt(token)
    if code:
        return emit_error(code, message, {}, 2)
    result = {
        "v": payload.get("v"),
        "task": payload.get("task"),
        "actor": payload.get("actor"),
        "repo": payload.get("repo"),
        "verified": False,
        "note": "客户端仅做格式解析展示（未验签）：令牌真实性、有效期与权限由服务端裁决",
    }
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=True, sort_keys=True))
    return 0

WORKSPACE_TOKEN_FILE = ".signet-token"

def workspace_handoff_token(workspace):
    """工作区 .signet-token 存在时返回令牌文本（strip），否则 None。"""
    if not workspace:
        return None
    path = Path(workspace) / WORKSPACE_TOKEN_FILE
    if not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if token.startswith("sigt-") else None
