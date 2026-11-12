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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import validate_actor_alias
from .errors import PlatformError
from .jsonio import append_jsonl, atomic_write_bytes, read_json, utc_now

DATA_DIR_NAME = ".signet"
USERS_FILE = "users.json"
SECRET_FILE = "server-secret"
AUDIT_FILE = "service-audit.jsonl"
PATS_FILE = "pats.json"
SESSION_COOKIE = "signet_session"
TOKEN_PREFIX = "sigt"
SIGT_VERSION = 1
PAT_PREFIX = "pat-"
PAT_VERSION = 1
PAT_SCOPES = ("requirement:create", "requirement:update", "requirement:submit")
PAT_EXPIRES_IN = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
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


def pats_path():
    return data_dir() / PATS_FILE


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


def _iso_utc(value):
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def mint_pat(user, scope, obo, exp):
    """签发 PAT（与 sigt 同构：pat- + b64url(payload) + HMAC-SHA256）。

    payload = {v, user（签发人）, scope, obo（执行者标签）, exp（过期时刻，ISO-8601Z）}。
    exp 接受 ISO 字符串或 datetime；完整令牌只由调用方在签发当次回显。
    """
    secret = load_server_secret()
    segment = _b64url_json({
        "v": PAT_VERSION,
        "user": user,
        "scope": list(scope or []),
        "obo": obo,
        "exp": _iso_utc(exp),
    })
    signature = hmac.new(secret, segment.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{PAT_PREFIX}{segment}-{signature}"


def parse_pat(token):
    if not isinstance(token, str) or not token.startswith(PAT_PREFIX):
        return None
    rest = token[len(PAT_PREFIX):]
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
    if not isinstance(document.get("v"), int) or not isinstance(document.get("user"), str) \
            or not isinstance(document.get("obo"), str) or not isinstance(document.get("exp"), str) \
            or not isinstance(document.get("scope"), list):
        return None
    return {"segment": segment, "signature": signature, "payload": document}


def verify_pat(token):
    parsed = parse_pat(token)
    if parsed is None:
        return None
    secret = load_server_secret(create=False)
    expected = hmac.new(secret, parsed["segment"].encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, parsed["signature"]):
        return None
    return parsed["payload"]


def pat_is_expired(payload, at=None):
    at = at or datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(payload["exp"].replace("Z", "+00:00")) <= at
    except (KeyError, ValueError):
        return True


def _token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_pats():
    path = pats_path()
    if not path.is_file():
        return {}
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def save_pats(pats):
    atomic_write_bytes(pats_path(), (json.dumps(pats, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def pat_expires_at(expires_in):
    """有效期档位 → 过期时刻（ISO-8601Z）；档位非法即拒绝（签发前置校验）。"""
    if expires_in not in PAT_EXPIRES_IN:
        raise PlatformError("pat.invalid_expiry", f"expires_in must be one of {sorted(PAT_EXPIRES_IN)}")
    return _iso_utc(datetime.now(timezone.utc) + PAT_EXPIRES_IN[expires_in])


def register_pat(user, label, scope, obo, expires_in, token):
    """平台侧只落散列 + 元数据；完整令牌仅在签发当次由调用方回显。"""
    expires_at = pat_expires_at(expires_in)
    cleaned_scope = []
    for item in scope or []:
        if item not in PAT_SCOPES:
            raise PlatformError("pat.invalid_scope", f"scope is not issuable: {item}")
        if item not in cleaned_scope:
            cleaned_scope.append(item)
    if not cleaned_scope:
        raise PlatformError("pat.invalid_scope", "at least one scope must be granted")
    validate_actor_alias(obo)
    expires_at = _iso_utc(datetime.now(timezone.utc) + PAT_EXPIRES_IN[expires_in])
    pat_id = PAT_PREFIX + secrets.token_hex(4)
    pats = load_pats()
    pats[pat_id] = {
        "pat_id": pat_id,
        "label": label,
        "user": user,
        "obo": obo,
        "scope": cleaned_scope,
        "created_at": utc_now(),
        "expires_at": expires_at,
        "last_used": None,
        "status": "active",
        "token_sha256": _token_digest(token),
    }
    save_pats(pats)
    return pats[pat_id]


def find_pat_by_token(token):
    digest = _token_digest(token)
    for record in load_pats().values():
        if record.get("token_sha256") == digest:
            return record
    return None


def find_pat(pat_id):
    record = load_pats().get(pat_id)
    return record if isinstance(record, dict) else None


def list_pats(user):
    records = [record for record in load_pats().values() if record.get("user") == user]
    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return records


def revoke_pat(pat_id, user):
    record = find_pat(pat_id)
    if record is None:
        raise PlatformError("pat.not_found", f"PAT does not exist: {pat_id}", exit_code=7)
    if record.get("user") != user:
        raise PlatformError("authz.pat_owner", f"only the issuer can revoke this PAT: {pat_id}", exit_code=4)
    if record.get("status") != "revoked":
        record["status"] = "revoked"
        record["revoked_at"] = utc_now()
        pats = load_pats()
        pats[pat_id] = record
        save_pats(pats)
    return record


def touch_pat(token):
    digest = _token_digest(token)
    pats = load_pats()
    changed = False
    for record in pats.values():
        if record.get("token_sha256") == digest:
            record["last_used"] = utc_now()
            changed = True
    if changed:
        save_pats(pats)
