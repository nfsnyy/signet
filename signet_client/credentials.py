"""T21 core-modularization：客户端登录凭据与 SIGT 域（自 signet_client.py 整函数搬入，函数体零变更）。

职责：client.json 令牌/用户解析、server-secret 读取、verify_sigt_local/inspect_handoff
本地验签、workspace_handoff_token。仅标准库，不得 import aidev。
"""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from .http import CLIENT_FILE, SECRET_FILE, client_config_path, emit_error, user_home

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

def server_secret_candidates():
    """本机服务端密钥的候选路径：USERPROFILE 优先，回落 HOMEDRIVE/HOMEPATH。

    临时 HOME（USERPROFILE 覆写）下验签必须仍能取到机器级密钥——
    USERPROFILE 覆写用于隔离 client.json，不应把密钥也隔离掉。
    """
    candidates = [user_home()]
    home_drive = os.environ.get("HOMEDRIVE")
    home_path = os.environ.get("HOMEPATH")
    if home_drive and home_path:
        candidates.append(Path(home_drive + home_path))
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique

def read_server_secret():
    for candidate in server_secret_candidates():
        secret_path = candidate / ".signet" / SECRET_FILE
        try:
            return secret_path.read_bytes(), secret_path
        except OSError:
            continue
    return None, user_home() / ".signet" / SECRET_FILE

def verify_sigt_local(token):
    """纯本地解析并验签 handoff 能力令牌（sigt），不联网。

    读取本机 ~/.signet/server-secret 复算 HMAC-SHA256 签名（取前 32 个
    hex 字符）并与令牌签名段比对；返回 (payload, error_code, message)。
    """
    if not isinstance(token, str) or not token.startswith("sigt-"):
        return None, "sigt.invalid", "handoff 令牌（sigt）格式不合法：不是 sigt- 前缀"
    rest = token[len("sigt-"):]
    if "-" not in rest:
        return None, "sigt.invalid", "handoff 令牌（sigt）格式不合法：缺少签名段"
    segment, signature = rest.rsplit("-", 1)
    try:
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return None, "sigt.invalid", "handoff 令牌（sigt）负载不是合法 JSON"
    if not isinstance(payload.get("task"), str) or not payload["task"]:
        return None, "sigt.invalid", "handoff 令牌（sigt）负载缺少 task"
    secret, secret_path = read_server_secret()
    if secret is None:
        return None, "auth.secret_missing", f"无法本地验签：本机缺少服务端密钥 {secret_path}"
    expected = hmac.new(secret, segment.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        return None, "sigt.signature_invalid", "handoff 令牌（sigt）验签失败：令牌损坏或不属于本平台"
    return payload, None, None

def inspect_handoff(token):
    """纯本地解析并验签 handoff 能力令牌（sigt），输出 v/task/actor/repo 与 valid。"""
    if not isinstance(token, str) or not token.startswith("sigt-"):
        return emit_error("sigt.invalid", "token is not a well-formed sigt", {}, 2)
    rest = token[len("sigt-"):]
    if "-" not in rest:
        return emit_error("sigt.invalid", "token is not a well-formed sigt", {}, 2)
    segment, signature = rest.rsplit("-", 1)
    try:
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return emit_error("sigt.invalid", "token payload is not valid base64url JSON", {}, 2)
    secret, secret_path = read_server_secret()
    if secret is None:
        return emit_error("auth.secret_missing", f"server secret does not exist: {secret_path}", {}, 2)
    expected = hmac.new(secret, segment.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    valid = hmac.compare_digest(expected, signature)
    result = {
        "v": payload.get("v"),
        "task": payload.get("task"),
        "actor": payload.get("actor"),
        "repo": payload.get("repo"),
        "valid": valid,
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
