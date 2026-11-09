"""登录、账号与服务端凭据（仅标准库）。

服务端数据目录为 ``~/.signet/``（不进 git），包含：

- ``users.json``          账号表（PBKDF2 口令散列，禁用标记）；
- ``server-secret``       HMAC 密钥，0600，惰性生成；
- ``service-audit.jsonl`` 管理动作审计（追加写）。

会话（浏览器 cookie 与 CLI 令牌）保存在内存表，服务重启即全部失效。
连续口令失败计数同样放在内存表，成功登录即重置。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

from .domain import validate_actor_alias
from .errors import PlatformError
from .jsonio import append_jsonl, atomic_write_bytes, read_json, utc_now

DATA_DIR_NAME = ".signet"
USERS_FILE = "users.json"
SECRET_FILE = "server-secret"
AUDIT_FILE = "service-audit.jsonl"
SESSION_COOKIE = "signet_session"
TOKEN_PREFIX = "sigt"
SIGT_VERSION = 1
MAX_FAILED_LOGINS = 5
PBKDF2_ITERATIONS = 120000

_sessions = {}
_sessions_lock = threading.Lock()
_failed_logins = {}
_failed_lock = threading.Lock()


def data_dir():
    return Path.home() / DATA_DIR_NAME


def users_path():
    return data_dir() / USERS_FILE


def secret_path():
    return data_dir() / SECRET_FILE


def audit_path():
    return data_dir() / AUDIT_FILE


def ensure_data_dir():
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if not users_path().is_file():
        atomic_write_bytes(users_path(), b"{}\n")
    load_server_secret()
    return directory


def load_server_secret(create=True):
    path = secret_path()
    if path.is_file():
        return path.read_bytes()
    if not create:
        raise PlatformError("auth.secret_missing", f"server secret does not exist: {path}")
    secret = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def load_users():
    path = users_path()
    if not path.is_file():
        return {}
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def save_users(users):
    atomic_write_bytes(users_path(), (json.dumps(users, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()


def create_user(alias, password):
    ensure_data_dir()
    validate_actor_alias(alias)
    if not password or not password.strip():
        raise PlatformError("admin.password_required", "password must be a non-empty string")
    users = load_users()
    if alias in users:
        raise PlatformError("admin.user_exists", f"user already exists: {alias}")
    salt = secrets.token_hex(16)
    users[alias] = {
        "salt": salt,
        "password_hash": password_digest(password, salt),
        "created_at": utc_now(),
        "disabled": False,
    }
    save_users(users)
    append_audit("create-user", alias)
    return {"user": alias, "created_at": users[alias]["created_at"]}


def set_user_password(alias, password):
    ensure_data_dir()
    if not password or not password.strip():
        raise PlatformError("admin.password_required", "password must be a non-empty string")
    users = load_users()
    if alias not in users:
        raise PlatformError("admin.user_missing", f"user does not exist: {alias}")
    salt = secrets.token_hex(16)
    users[alias]["salt"] = salt
    users[alias]["password_hash"] = password_digest(password, salt)
    users[alias]["disabled"] = False
    save_users(users)
    append_audit("reset-password", alias)
    return {"user": alias}


def disable_user(alias):
    ensure_data_dir()
    users = load_users()
    if alias not in users:
        raise PlatformError("admin.user_missing", f"user does not exist: {alias}")
    users[alias]["disabled"] = True
    save_users(users)
    append_audit("disable-user", alias)
    return {"user": alias}


def list_users():
    ensure_data_dir()
    return {"users": sorted(load_users())}


def authenticate(alias, password):
    record = load_users().get(alias)
    if not record or record.get("disabled"):
        return False
    salt = record.get("salt")
    expected = record.get("password_hash")
    if not salt or not expected or not isinstance(password, str):
        return False
    return hmac.compare_digest(password_digest(password, salt), expected)


def append_audit(action, alias):
    entry = {
        "timestamp": utc_now(),
        "action": action,
        "user": alias,
        "actor": "local-admin",
        "source": "aidev-admin",
    }
    append_jsonl(audit_path(), [entry])
    return entry


def create_session(user):
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = {"user": user, "created_at": utc_now()}
    return token


def find_session(token):
    if not token:
        return None
    with _sessions_lock:
        return _sessions.get(token)


def login_throttled(alias):
    with _failed_lock:
        return _failed_logins.get(alias, 0) >= MAX_FAILED_LOGINS


def note_login_failure(alias):
    with _failed_lock:
        _failed_logins[alias] = _failed_logins.get(alias, 0) + 1


def note_login_success(alias):
    with _failed_lock:
        _failed_logins.pop(alias, None)


def _b64url_json(document):
    body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def mint_sigt(task, actor, repo):
    secret = load_server_secret()
    segment = _b64url_json({"v": SIGT_VERSION, "task": task, "actor": actor, "repo": repo})
    signature = hmac.new(secret, segment.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{TOKEN_PREFIX}-{segment}-{signature}"


def parse_sigt(token):
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX + "-"):
        return None
    rest = token[len(TOKEN_PREFIX) + 1:]
    if "-" not in rest:
        return None
    segment, signature = rest.rsplit("-", 1)
    if not segment or not signature:
        return None
    try:
        padded = segment + "=" * (-len(segment) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if not isinstance(document.get("v"), int) or not isinstance(document.get("task"), str) \
            or not isinstance(document.get("actor"), str) or not isinstance(document.get("repo"), str):
        return None
    return {"segment": segment, "signature": signature, "payload": document}


def verify_sigt(token):
    parsed = parse_sigt(token)
    if parsed is None:
        return None
    secret = load_server_secret(create=False)
    expected = hmac.new(secret, parsed["segment"].encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, parsed["signature"]):
        return None
    return parsed["payload"]
