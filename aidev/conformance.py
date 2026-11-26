"""verify conformance 阶段对账（T12/REQ-2）。

对账入口（钉名）：``check_contract(interfaces, surface, repository=None)``

- ``interfaces``：冻结契约的 interfaces 声明段；None/缺省时本步零输出跳过。
- ``surface``：活体面——in-proc ASGI app（可调用对象）或 base URL 字符串。
- ``repository``：状态仓路径；事件对账读取其 ``events/events.jsonl``，为 None 时
  事件项跳过。

逐类 diff（findings 去重键 = code + declaration_id + field）：

- 端点：按 auth 四凭据族（anonymous/user_token/sigt/session_cookie）逐族探测——
  无凭据 / ``Authorization: Bearer`` / ``Authorization: sigt`` /
  ``Cookie: signet_session=``。实际状态 ∉ 声明集合且允许/拒绝位不变
  → ``c8.conformance_status_mismatch``；允许/拒绝位反转
  → ``c8.conformance_auth_mismatch``；2xx 响应体对照
  ``responses.success`` 严格逐字段 diff（缺 ``c8.conformance_field_missing`` /
  多 ``c8.conformance_field_unexpected`` / 类型漂移
  ``c8.conformance_type_drift``），非 2xx 对照 ``responses.rejection`` 宽松
  diff（缺/漂移仍报，多字段不报——拒绝形态允许附加说明字段）。
- 表单：GET 声明页（优先带探测会话，页面会话只认 cookie），解析
  input/select/textarea 控件的 name/type/required 与声明逐字段比对，
  错误指向表单声明 id 与字段。
- 事件：repository 内该 event_type 全部事件 payload 逐项校验；零事件跳过。

探测凭据由 verify 环境在进程内经 ``configure_probes`` 注入；未注入时各凭据族
按上述请求形状发占位值（对账面按请求形态分流时仍可四族全覆盖）。

T23/REQ-5 写端点探测方法学（偿还设计稿债务 #38① 的本任务批次）：

- 路径模板除 ``{task_id}`` 外增补 ``{user}``（探测账号）与 ``{name}``（探测
  文档 hints.md）替换；
- POST 端点按钉名路径合成最小合法请求体（账号建/停/重置、SIGT 重发、文档
  幂等回写），四凭据族共用同形请求——匿名/sigt 在授权层被拒（401/403），
  user_token/session_cookie 走通正向 2xx；
- 副作用纪律：探测账号（conf-probe-* 随机别名）在 check_contract 收尾从
  users.json 移除（事后清理）；SIGT 重发与文档幂等回写（base 匹配 + 内容
  逐字节不变 → 服务端零写盘/零 commit/零审计）本身零账本副作用。
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIAL_FAMILIES = ("anonymous", "user_token", "sigt", "session_cookie")
SESSION_COOKIE_NAME = "signet_session"

CODE_FIELD_MISSING = "c8.conformance_field_missing"
CODE_FIELD_UNEXPECTED = "c8.conformance_field_unexpected"
CODE_TYPE_DRIFT = "c8.conformance_type_drift"
CODE_STATUS_MISMATCH = "c8.conformance_status_mismatch"
CODE_AUTH_MISMATCH = "c8.conformance_auth_mismatch"

_PROBE = {"user_token": None, "sigt": None, "session_cookie": None, "task_id": None}

# T23 写端点探测状态：本次 check_contract 创建的探测账号（收尾统一清理）与
# {user} 模板替换账号（直建编排账号，与 EP-02 探测副作用解耦——匿名/sigt 族
# 探测在授权层被拒，并不真正建号）。
_PROBE_STATE = {"accounts": [], "user": None}

PROBE_DOC_NAME = "hints.md"
PROBE_SIGT_ACTOR = "conformance-probe"


def configure_probes(user_token=None, sigt=None, session_cookie=None, task_id=None):
    """注入探测凭据（verify 环境进程内合成；全部置 None 复位）。

    user_token / session_cookie 为会话令牌（Bearer 头 / Cookie 头两种运载），
    sigt 为任务能力令牌（``sigt`` scheme 头）；task_id 用于端点路径
    ``{task_id}`` 模板替换，缺省时尝试从 sigt 令牌段解析。
    """
    _PROBE["user_token"] = user_token
    _PROBE["sigt"] = sigt
    _PROBE["session_cookie"] = session_cookie
    _PROBE["task_id"] = task_id


def check_contract(interfaces, surface, repository=None):
    """对冻结 interfaces 声明与活体面逐项 diff，返回 finding 字典列表（空 = 全合）。"""
    if not interfaces:
        return []
    task_id = _probe_task_id()
    findings = []
    try:
        for endpoint in interfaces.get("endpoints") or []:
            findings.extend(_check_endpoint(endpoint, surface, task_id, repository=repository))
    finally:
        _teardown_probe_accounts()
    for form in interfaces.get("forms") or []:
        findings.extend(_check_form(form, surface))
    if repository is not None:
        findings.extend(_check_events(interfaces.get("events") or [], repository))
    return _dedup(findings)


# ---------------------------------------------------------------------------
# 探测传输：in-proc ASGI 直调或 base URL
# ---------------------------------------------------------------------------

async def _asgi_request(app, method, path, headers, body=b""):
    captured = {"status": None, "headers": {}, "body": b""}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii", "replace"),
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1"))
                    for key, value in headers.items()],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)
    return captured["status"], captured["headers"], captured["body"]


def _request(surface, method, path, headers, body=None):
    if isinstance(surface, str):
        url = surface.rstrip("/") + path
        request = urllib.request.Request(url, method=method, headers=headers, data=body)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
    return asyncio.run(_asgi_request(surface, method, path, headers, body=body or b""))


def _family_headers(family):
    if family == "user_token":
        return {"Authorization": "Bearer " + (_PROBE["user_token"] or "conformance-probe")}
    if family == "sigt":
        return {"Authorization": "sigt " + (_PROBE["sigt"] or "conformance-probe")}
    if family == "session_cookie":
        cookie = SESSION_COOKIE_NAME + "=" + (_PROBE["session_cookie"] or "conformance-probe")
        return {"Cookie": cookie}
    return {}


def _form_headers():
    headers = {"Accept": "text/html,application/xhtml+xml"}
    # 页面会话只认 cookie：优先带探测会话，其次 Bearer，其次 sigt，最后匿名。
    for family in ("session_cookie", "user_token", "sigt"):
        candidate = _family_headers(family)
        if candidate:
            headers.update(candidate)
            return headers
    return headers


def _probe_task_id():
    if _PROBE["task_id"]:
        return _PROBE["task_id"]
    sigt = _PROBE["sigt"]
    if not sigt:
        return ""
    try:
        segment = sigt.split("-", 1)[1].rsplit("-", 1)[0]
        padded = segment + "=" * (-len(segment) % 4)
        document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, IndexError, UnicodeDecodeError):
        return ""
    task = document.get("task") if isinstance(document, dict) else None
    return task if isinstance(task, str) else ""


# ---------------------------------------------------------------------------
# T23 写端点探测：请求体合成、探测账号生命周期与事后清理
# ---------------------------------------------------------------------------

def _probe_account():
    """{user} 模板探测账号：直建编排账号（债务 #38①「编排账号 + 事后清理」），
    不依赖 EP-02 探测副作用（其匿名/sigt 族探测并不真正建号）；直写存储不经
    管理动词，审计零噪音。"""
    if _PROBE_STATE["user"]:
        return _PROBE_STATE["user"]
    from . import authn
    from .jsonio import utc_now
    alias = f"conf-probe-{secrets.token_hex(4)}"
    users = authn.load_users()
    salt = secrets.token_hex(16)
    users[alias] = {
        "salt": salt,
        "password_hash": authn.password_digest(secrets.token_hex(8), salt),
        "created_at": utc_now(),
        "disabled": False,
    }
    authn.save_users(users)
    _PROBE_STATE["accounts"].append(alias)
    _PROBE_STATE["user"] = alias
    return alias


def _probe_doc_body(repository):
    """EP-07 正向探测体：现行字节原样回写——base 匹配 + 内容逐字节不变时服务端
    幂等零副作用（零写盘/零 commit/零审计），探测不污染权威账本。"""
    if not repository:
        return None
    target = Path(repository) / PROBE_DOC_NAME
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    return {"base_sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8")}


def _probe_body(method, path_template, repository):
    """按钉名路径合成写端点最小合法探测体（四凭据族共用同形请求；无声明体的
    端点返回 None，保持 T12 读端点探测形态零变更）。"""
    if method != "POST":
        return None
    if path_template == "/api/users":
        alias = f"conf-probe-{secrets.token_hex(4)}"
        _PROBE_STATE["accounts"].append(alias)
        return {"user": alias, "password": secrets.token_hex(8)}
    if path_template == "/api/users/{user}/password":
        return {"password": secrets.token_hex(8)}
    if path_template == "/api/tasks/{task_id}/sigt":
        return {"actor": PROBE_SIGT_ACTOR}
    if path_template == "/api/docs/{name}":
        return _probe_doc_body(repository)
    return None


def _teardown_probe_accounts():
    """探测账号事后清理（债务 #38① 纪律）：从 users.json 移除本次探测创建的
    全部账号；best-effort，清理失败不影响对账结论。"""
    aliases = _PROBE_STATE["accounts"]
    _PROBE_STATE["accounts"] = []
    _PROBE_STATE["user"] = None
    if not aliases:
        return
    try:
        from . import authn
        users = authn.load_users()
        changed = False
        for alias in aliases:
            if alias in users:
                users.pop(alias)
                changed = True
        if changed:
            authn.save_users(users)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# JSON schema 子集 diff（端点响应体与事件 payload 共用）
# ---------------------------------------------------------------------------

def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_matches(expected, actual):
    if expected == actual:
        return True
    # JSON Schema 语义：integer 是 number 的子类型。
    return expected == "number" and actual == "integer"


def _join_path(path, key):
    return f"{path}.{key}" if path else str(key)


def _diff_json(value, schema, path, strict, declaration_id):
    findings = []
    if not isinstance(schema, dict):
        return findings
    expected_type = schema.get("type")
    if expected_type is not None:
        actual_type = _json_type(value)
        if not _type_matches(expected_type, actual_type):
            findings.append({
                "code": CODE_TYPE_DRIFT,
                "declaration_id": declaration_id,
                "field": path,
                "expected": expected_type,
                "actual": actual_type,
            })
            return findings
    properties = schema.get("properties")
    if isinstance(properties, dict) and isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                findings.append({
                    "code": CODE_FIELD_MISSING,
                    "declaration_id": declaration_id,
                    "field": _join_path(path, key),
                })
        for key, subschema in properties.items():
            if key in value:
                findings.extend(_diff_json(value[key], subschema, _join_path(path, key),
                                           strict, declaration_id))
        if strict:
            declared = set(properties)
            for key in value:
                if key not in declared:
                    findings.append({
                        "code": CODE_FIELD_UNEXPECTED,
                        "declaration_id": declaration_id,
                        "field": _join_path(path, key),
                    })
    items = schema.get("items")
    if isinstance(items, dict) and isinstance(value, list):
        for index, element in enumerate(value):
            findings.extend(_diff_json(element, items, f"{path}[{index}]", strict,
                                       declaration_id))
    return findings


def _json_document(body):
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None


# ---------------------------------------------------------------------------
# 端点：auth 矩阵探测 + 响应体 diff
# ---------------------------------------------------------------------------

def _check_endpoint(endpoint, surface, task_id, repository=None):
    findings = []
    method = endpoint.get("method") or "GET"
    path_template = endpoint.get("path") or ""
    path = path_template.replace("{task_id}", task_id or "")
    if "{user}" in path:
        path = path.replace("{user}", _probe_account())
    if "{name}" in path:
        path = path.replace("{name}", PROBE_DOC_NAME)
    declaration_id = endpoint.get("id")
    auth = endpoint.get("auth") or {}
    responses = endpoint.get("responses") or {}
    for family in CREDENTIAL_FAMILIES:
        if family not in auth:
            continue
        declared = auth[family]
        declared_list = declared if isinstance(declared, list) else [declared]
        headers = _family_headers(family)
        body_fields = _probe_body(method, path_template, repository)
        request_body = None
        if body_fields is not None:
            request_body = json.dumps(body_fields, ensure_ascii=False).encode("utf-8")
            headers = {**headers, "Content-Type": "application/json"}
        status, _headers, body = _request(surface, method, path, headers, body=request_body)
        actual = status if status is not None else 0
        actual_allow = 200 <= actual <= 299
        if actual not in declared_list:
            declared_allow = any(200 <= int(code) <= 299 for code in declared_list)
            # 允许/拒绝位反转 = 声明只拒（无非 2xx）而实际放行；其余不符（含声明
            # 含 2xx 而实际为其他状态码）按状态码不符处理（钉扎：403 ∉ [200]
            # → status_mismatch；200 ∉ [401] → auth_mismatch）。
            finding = {
                "declaration_id": declaration_id,
                "field": None,
                "credential_family": family,
                "method": method,
                "path": path,
                "expected": declared_list,
                "actual": actual,
            }
            finding["code"] = CODE_AUTH_MISMATCH if (actual_allow and not declared_allow) else CODE_STATUS_MISMATCH
            findings.append(finding)
        schema = responses.get("success") if actual_allow else responses.get("rejection")
        if isinstance(schema, dict):
            document = _json_document(body)
            if document is not None:
                findings.extend(_diff_json(document, schema, "", actual_allow, declaration_id))
    return findings


# ---------------------------------------------------------------------------
# 表单：页面控件解析与声明比对
# ---------------------------------------------------------------------------

_CONTROL_TAG = re.compile(r"<(input|select|textarea)\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")


def _parse_form_controls(html_text):
    fields = {}
    for match in _CONTROL_TAG.finditer(html_text):
        tag = match.group(1).lower()
        attrs = {}
        for name, raw in _ATTRIBUTE.findall(match.group(0)):
            attrs[name.lower()] = True if raw == "" else raw.strip("\"'").lower() or True
        control_name = attrs.get("name")
        if not control_name or not isinstance(control_name, str):
            continue
        if tag == "input":
            control_type = attrs.get("type") if isinstance(attrs.get("type"), str) else "text"
        elif tag == "select":
            control_type = "select"
        else:
            control_type = "textarea"
        fields[control_name] = {"type": control_type, "required": "required" in attrs}
    return fields


def _check_form(form, surface):
    page = form.get("page") or ""
    declaration_id = form.get("id")
    declared_fields = form.get("fields") or []
    status, headers, body = _request(surface, "GET", page, _form_headers())
    if status is None or not (200 <= status <= 299):
        return [{
            "code": CODE_FIELD_MISSING,
            "declaration_id": declaration_id,
            "field": field.get("name"),
            "page": page,
            "actual_status": status,
        } for field in declared_fields]
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = str(value).lower()
            break
    if "text/html" not in content_type:
        # 对账面未以 HTML 形态提供该页（如 JSON 端点）→ 表单项零输出跳过。
        return []
    live = _parse_form_controls(body.decode("utf-8", "replace"))
    findings = []
    declared_names = set()
    for field in declared_fields:
        name = field.get("name")
        declared_names.add(name)
        if name not in live:
            findings.append({
                "code": CODE_FIELD_MISSING,
                "declaration_id": declaration_id,
                "field": name,
            })
            continue
        expected_type = field.get("type")
        if live[name]["type"] != expected_type:
            findings.append({
                "code": CODE_TYPE_DRIFT,
                "declaration_id": declaration_id,
                "field": name,
                "expected": expected_type,
                "actual": live[name]["type"],
            })
        expected_required = bool(field.get("required"))
        if live[name]["required"] != expected_required:
            findings.append({
                "code": CODE_TYPE_DRIFT,
                "declaration_id": declaration_id,
                "field": name,
                "expected": expected_required,
                "actual": live[name]["required"],
            })
    for name in live:
        if name not in declared_names:
            findings.append({
                "code": CODE_FIELD_UNEXPECTED,
                "declaration_id": declaration_id,
                "field": name,
            })
    return findings


# ---------------------------------------------------------------------------
# 事件：repository 内 events.jsonl 全量 payload 校验
# ---------------------------------------------------------------------------

def _check_events(declared_events, repository):
    findings = []
    if not declared_events:
        return findings
    events_file = Path(repository) / "events" / "events.jsonl"
    if not events_file.is_file():
        return findings
    try:
        lines = events_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return findings
    by_type = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("event_type"), str):
            by_type.setdefault(event["event_type"], []).append(event)
    for declaration in declared_events:
        matched = by_type.get(declaration.get("event_type"), [])
        if not matched:
            continue
        for event in matched:
            payload = event.get("payload")
            if isinstance(payload, dict):
                findings.extend(_diff_json(payload, declaration.get("payload") or {},
                                           "", True, declaration.get("id")))
    return findings


def _dedup(findings):
    seen = set()
    unique = []
    for finding in findings:
        key = (finding.get("code"), finding.get("declaration_id"), finding.get("field"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique
