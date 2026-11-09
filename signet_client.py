"""signet 平台薄壳 CLI 的公共 HTTP 客户端（仅标准库）。

aidev-agent.py / aidev-operator.py 两个入口脚本共用本模块，只做三件事：
参数解析、HTTP 请求、JSON 透传。平台业务逻辑全部在服务端（aidev.web），
本模块不得 import aidev 的任何子模块。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT = 60
HTTP_EXIT_CODES = {400: 2, 409: 3}
ROLE_HEADER = "X-Signet-Role"


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


def resolve_base_url():
    env_url = os.environ.get("SIGNET_BASE_URL")
    if env_url and env_url.strip():
        return env_url.rstrip("/")
    config_path = user_home() / ".signet" / "client.json"
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


def perform(method, url, fields=None, files=None, role=None):
    headers = {}
    if role:
        headers[ROLE_HEADER] = role
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
        with _REQUEST_OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, decode_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, decode_json(error.read())
    except urllib.error.URLError as error:
        raise ServiceUnavailable(url, error.reason) from error


def resolve_task_id(workspace):
    path = Path(workspace) / "task.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ClientInputError("task.not_found", f"无法读取工作区 task.md: {workspace}") from error
    if not text.startswith("---\n"):
        raise ClientInputError("task.invalid_frontmatter", "task.md 必须以 JSON frontmatter 开头")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ClientInputError("task.invalid_frontmatter", "task.md frontmatter 未闭合")
    try:
        metadata = json.loads(text[4:end])
    except json.JSONDecodeError as error:
        raise ClientInputError("task.invalid_frontmatter", "task.md frontmatter 不是合法 JSON") from error
    task_id = metadata.get("task_id") if isinstance(metadata, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise ClientInputError("task.invalid_frontmatter", "task.md frontmatter 缺少 task_id")
    return task_id


def read_upload(path, missing_code, missing_exit=2):
    try:
        return Path(path).name, Path(path).read_bytes()
    except OSError as error:
        raise ClientInputError(missing_code, f"文件不存在或不可读: {path}", exit_code=missing_exit) from error


def present(fields):
    return {key: value for key, value in fields.items() if value is not None}


def contract_assets(contract_path):
    """Collect the asset files a contract references (dependency_files + case fixtures).

    Paths are resolved relative to the contract file's directory, mirroring the
    server-side contract validation layout.
    """
    try:
        contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClientInputError("c2.invalid_contract", f"invalid JSON: {contract_path}") from error
    if not isinstance(contract, dict):
        raise ClientInputError("c2.invalid_contract", f"JSON document must be an object: {contract_path}")
    referenced = []
    environment = contract.get("environment")
    if isinstance(environment, dict):
        dependencies = environment.get("dependency_files", [])
        if isinstance(dependencies, list):
            referenced.extend(item for item in dependencies if isinstance(item, str))
    cases = contract.get("cases", [])
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict):
                fixtures = case.get("fixtures", [])
                if isinstance(fixtures, list):
                    referenced.extend(item for item in fixtures if isinstance(item, str))
    relative_paths = list(dict.fromkeys(referenced))
    base = Path(contract_path).resolve().parent
    files = []
    for relative in relative_paths:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ClientInputError("c3.invalid_fixture", f"asset path must stay inside the spec directory: {relative}")
        full_path = base / relative_path
        if not full_path.is_file():
            raise ClientInputError("c3.missing_fixture", f"fixture does not exist: {relative}")
        files.append(("assets", relative, full_path.read_bytes()))
    return relative_paths, files


def build_request(role, arguments):
    command = arguments.command
    if command == "health":
        return "GET", "/api/health", None, None
    if command == "log":
        query = urllib.parse.urlencode({"repo": arguments.repo})
        return "GET", f"/api/tasks/{arguments.task_id}/events?{query}", None, None
    if command == "status":
        task_id = resolve_task_id(arguments.workspace)
        query = urllib.parse.urlencode({"workspace": arguments.workspace})
        return "GET", f"/api/tasks/{task_id}/status?{query}", None, None
    if command == "claim":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of})
        return "POST", f"/api/tasks/{resolve_task_id(arguments.workspace)}/claims", fields, None
    if command == "submit":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of})
        files = [("report", *read_upload(arguments.report, "internal.error", missing_exit=4))]
        files.extend(("evidence", *read_upload(path, "submit.evidence_missing")) for path in arguments.evidence)
        return "POST", f"/api/tasks/{resolve_task_id(arguments.workspace)}/submission", fields, files
    if command == "block":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "reason": arguments.reason})
        files = [("evidence", *read_upload(path, "submit.evidence_missing")) for path in arguments.evidence]
        return "POST", f"/api/tasks/{resolve_task_id(arguments.workspace)}/blocks", fields, files
    if command == "enhancement":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "title": arguments.title, "details": arguments.details})
        return "POST", f"/api/tasks/{resolve_task_id(arguments.workspace)}/enhancements", fields, None
    if command == "collab-send":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "type": arguments.type, "to_task": arguments.to_task, "summary": arguments.summary, "details": arguments.details})
        return "POST", f"/api/tasks/{resolve_task_id(arguments.workspace)}/collaborations", fields, None
    if command == "collab-answer":
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "action": arguments.action, "note": arguments.note})
        return "POST", f"/api/collaborations/{arguments.message_id}/answers", fields, None
    if command == "init-repository":
        fields = present({"repo": arguments.repo, "actor": arguments.actor, "name": arguments.name, "owner": arguments.owner})
        return "POST", "/api/repositories", fields, None
    if command == "init-task":
        fields = present({"repo": arguments.repo, "workspace": arguments.workspace, "title": arguments.title, "requirement": arguments.requirement, "owner": arguments.owner, "actor": arguments.actor})
        return "POST", "/api/tasks", fields, None
    if command in ("freeze", "refreeze"):
        relative_paths, asset_files = contract_assets(arguments.contract)
        fields = present({
            "repo": arguments.repo,
            "workspace": arguments.workspace,
            "h1_actor": arguments.h1_actor,
            "h2_actor": arguments.h2_actor,
            "reason": getattr(arguments, "reason", None),
            "push": str(arguments.push).lower(),
            "asset_manifest": json.dumps(relative_paths, ensure_ascii=False),
        })
        files = [
            ("card", *read_upload(arguments.acceptance_card, "c1.invalid_card")),
            ("contract", *read_upload(arguments.contract, "c2.invalid_contract")),
            ("mapping", *read_upload(arguments.mapping, "c1.invalid_mapping")),
        ]
        files.extend(asset_files)
        if arguments.operation_source:
            files.append(("operation_source", *read_upload(arguments.operation_source, "c2.operation_source_missing")))
        endpoint = "freeze" if command == "freeze" else "refreeze"
        return "POST", f"/api/tasks/{arguments.task_id}/{endpoint}", fields, files
    if command == "sync":
        fields = present({"repo": arguments.repo, "workspace": list(arguments.workspace), "actor": arguments.actor, "push": str(arguments.push).lower()})
        return "POST", "/api/sync", fields, None
    if command == "verify":
        fields = present({
            "repo": arguments.repo,
            "verifier": arguments.verifier,
            "confirmed_by": arguments.confirmed_by,
            "execute": str(arguments.execute).lower(),
            "push": str(arguments.push).lower(),
        })
        files = []
        if arguments.results:
            files.append(("results", *read_upload(arguments.results, "verify.invalid_results")))
        if arguments.manual_results:
            files.append(("manual_results", *read_upload(arguments.manual_results, "verify.invalid_manual_results")))
        return "POST", f"/api/tasks/{arguments.task_id}/verify", fields, files
    if command == "reject":
        fields = present({"repo": arguments.repo, "category": arguments.category, "reason": arguments.reason, "actor": arguments.actor, "workspace": arguments.workspace, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/reject", fields, None
    if command == "close":
        fields = present({"repo": arguments.repo, "decision": arguments.decision, "actor": arguments.actor, "reason": arguments.reason, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/close", fields, None
    if command == "void":
        fields = present({"repo": arguments.repo, "reason": arguments.reason, "actor": arguments.actor, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/void", fields, None
    if command == "adjudicate":
        fields = present({"repo": arguments.repo, "category": arguments.category, "reason": arguments.reason, "actor": arguments.actor, "workspace": arguments.workspace, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/adjudication", fields, None
    if command == "veto":
        relative_paths, asset_files = contract_assets(arguments.contract) if arguments.contract else ([], [])
        fields = present({
            "repo": arguments.repo,
            "actor": arguments.actor,
            "task_id": arguments.task_id,
            "veto_id": arguments.veto_id,
            "reason": arguments.reason,
            "propose": str(arguments.propose).lower(),
            "approve": str(arguments.approve).lower(),
            "reject": str(arguments.reject).lower(),
            "withdraw": str(arguments.withdraw).lower(),
            "list_flag": str(arguments.list).lower(),
            "push": str(arguments.push).lower(),
            "asset_manifest": json.dumps(relative_paths, ensure_ascii=False),
        })
        files = []
        for field in ("card", "contract", "mapping"):
            path = getattr(arguments, {"card": "acceptance_card"}.get(field, field))
            if path:
                files.append((field, *read_upload(path, "veto.file_missing")))
        files.extend(asset_files)
        return "POST", "/api/vetoes", fields, files
    if command == "inbox":
        query = urllib.parse.urlencode(present({"repo": arguments.repo, "task_id": arguments.task_id}))
        return "GET", f"/api/inbox?{query}", None, None
    raise ClientInputError("operation.unsupported", f"unsupported command: {command}")


def parser_for(role):
    parser = argparse.ArgumentParser(prog=f"aidev-{role}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    if role == "operator":
        repository = subparsers.add_parser("init-repository")
        repository.add_argument("--repo", required=True)
        repository.add_argument("--actor", required=True)
        repository.add_argument("--name")
        repository.add_argument("--owner")
        task = subparsers.add_parser("init-task")
        task.add_argument("--repo", required=True)
        task.add_argument("--workspace", required=True)
        task.add_argument("--title", required=True)
        task.add_argument("--requirement", required=True)
        task.add_argument("--owner", required=True)
        task.add_argument("--actor", required=True)
        freeze = subparsers.add_parser("freeze")
        freeze.add_argument("--repo", required=True)
        freeze.add_argument("--task-id", required=True)
        freeze.add_argument("--workspace", required=True)
        freeze.add_argument("--acceptance-card", required=True)
        freeze.add_argument("--contract", required=True)
        freeze.add_argument("--mapping", required=True)
        freeze.add_argument("--operation-source")
        freeze.add_argument("--h1-actor", required=True)
        freeze.add_argument("--h2-actor", required=True)
        freeze.add_argument("--push", action="store_true")
        refreeze = subparsers.add_parser("refreeze")
        refreeze.add_argument("--repo", required=True)
        refreeze.add_argument("--task-id", required=True)
        refreeze.add_argument("--workspace", required=True)
        refreeze.add_argument("--acceptance-card", required=True)
        refreeze.add_argument("--contract", required=True)
        refreeze.add_argument("--mapping", required=True)
        refreeze.add_argument("--operation-source")
        refreeze.add_argument("--h1-actor", required=True)
        refreeze.add_argument("--h2-actor", required=True)
        refreeze.add_argument("--reason", required=True)
        refreeze.add_argument("--push", action="store_true")
        verify = subparsers.add_parser("verify")
        verify.add_argument("--repo", required=True)
        verify.add_argument("--task-id", required=True)
        verify.add_argument("--results")
        verify.add_argument("--execute", action="store_true")
        verify.add_argument("--manual-results")
        verify.add_argument("--verifier", required=True)
        verify.add_argument("--confirmed-by", required=True)
        verify.add_argument("--push", action="store_true")
        reject = subparsers.add_parser("reject")
        reject.add_argument("--repo", required=True)
        reject.add_argument("--task-id", required=True)
        reject.add_argument("--category", choices=["implementation", "input"], required=True)
        reject.add_argument("--reason", required=True)
        reject.add_argument("--actor", required=True)
        reject.add_argument("--workspace")
        reject.add_argument("--push", action="store_true")
        close = subparsers.add_parser("close")
        close.add_argument("--repo", required=True)
        close.add_argument("--task-id", required=True)
        close.add_argument("--decision", choices=["accept", "reject"], required=True)
        close.add_argument("--actor", required=True)
        close.add_argument("--reason")
        close.add_argument("--push", action="store_true")
        void = subparsers.add_parser("void")
        void.add_argument("--repo", required=True)
        void.add_argument("--task-id", required=True)
        void.add_argument("--reason", choices=["definition-defect", "cancel", "specification-invalidated"], required=True)
        void.add_argument("--actor", required=True)
        void.add_argument("--push", action="store_true")
        adjudicate = subparsers.add_parser("adjudicate")
        adjudicate.add_argument("--repo", required=True)
        adjudicate.add_argument("--task-id", required=True)
        adjudicate.add_argument("--category", choices=["implementation", "definition"], required=True)
        adjudicate.add_argument("--reason", required=True)
        adjudicate.add_argument("--actor", required=True)
        adjudicate.add_argument("--workspace")
        adjudicate.add_argument("--push", action="store_true")
        sync = subparsers.add_parser("sync")
        sync.add_argument("--repo", required=True)
        sync.add_argument("--workspace", action="append", required=True)
        sync.add_argument("--actor", required=True)
        sync.add_argument("--push", action="store_true")
        veto = subparsers.add_parser("veto")
        veto.add_argument("--repo", required=True)
        veto.add_argument("--actor", required=True)
        veto.add_argument("--task-id")
        veto.add_argument("--veto-id")
        veto.add_argument("--acceptance-card")
        veto.add_argument("--contract")
        veto.add_argument("--mapping")
        veto.add_argument("--reason")
        veto.add_argument("--propose", action="store_true")
        veto.add_argument("--approve", action="store_true")
        veto.add_argument("--reject", action="store_true")
        veto.add_argument("--withdraw", action="store_true")
        veto.add_argument("--list", action="store_true")
        veto.add_argument("--push", action="store_true")
        inbox = subparsers.add_parser("inbox")
        inbox.add_argument("--repo", required=True)
        inbox.add_argument("--task-id")
    else:
        claim_parser = subparsers.add_parser("claim")
        claim_parser.add_argument("--workspace", required=True)
        claim_parser.add_argument("--actor", required=True)
        claim_parser.add_argument("--on-behalf-of", required=True)
        submit_parser = subparsers.add_parser("submit")
        submit_parser.add_argument("--workspace", required=True)
        submit_parser.add_argument("--actor", required=True)
        submit_parser.add_argument("--on-behalf-of", required=True)
        submit_parser.add_argument("--report", required=True)
        submit_parser.add_argument("--evidence", action="append", required=True)
        block_parser = subparsers.add_parser("block")
        block_parser.add_argument("--workspace", required=True)
        block_parser.add_argument("--actor", required=True)
        block_parser.add_argument("--on-behalf-of", required=True)
        block_parser.add_argument("--reason", required=True)
        block_parser.add_argument("--evidence", action="append", required=True)
        enhancement_parser = subparsers.add_parser("enhancement")
        enhancement_parser.add_argument("--workspace", required=True)
        enhancement_parser.add_argument("--actor", required=True)
        enhancement_parser.add_argument("--on-behalf-of", required=True)
        enhancement_parser.add_argument("--title", required=True)
        enhancement_parser.add_argument("--details", required=True)
        status_parser = subparsers.add_parser("status")
        status_parser.add_argument("--workspace", required=True)
        collab_send_parser = subparsers.add_parser("collab-send")
        collab_send_parser.add_argument("--workspace", required=True)
        collab_send_parser.add_argument("--actor", required=True)
        collab_send_parser.add_argument("--on-behalf-of", required=True)
        collab_send_parser.add_argument("--type", choices=["in-spec-request", "definition-doubt", "new-scope"], required=True)
        collab_send_parser.add_argument("--to-task", required=True)
        collab_send_parser.add_argument("--summary", required=True)
        collab_send_parser.add_argument("--details", required=True)
        collab_answer_parser = subparsers.add_parser("collab-answer")
        collab_answer_parser.add_argument("--workspace", required=True)
        collab_answer_parser.add_argument("--actor", required=True)
        collab_answer_parser.add_argument("--on-behalf-of", required=True)
        collab_answer_parser.add_argument("--message-id", required=True)
        collab_answer_parser.add_argument("--action", choices=["accept", "reject", "counter", "escalate", "close"], required=True)
        collab_answer_parser.add_argument("--note")
    subparsers.add_parser("health")
    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--repo", required=True)
    log_parser.add_argument("--task-id", required=True)
    return parser


def emit_error(code, message, details, exit_code):
    response = {"ok": False, "error": {"code": code, "message": message, "details": details}}
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return exit_code


def run(role, argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    parser = parser_for(role)
    arguments = parser.parse_args(argv)
    try:
        method, path, fields, files = build_request(role, arguments)
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)
    base_url = resolve_base_url()
    try:
        status, payload = perform(method, base_url + path, fields=fields, files=files, role=role)
    except ServiceUnavailable as error:
        print(f"错误：无法连接 signet 平台服务（{base_url}）：{error.reason}", file=sys.stderr)
        print("请先启动服务（例如：python -m aidev.web.server，默认监听 127.0.0.1:8000），", file=sys.stderr)
        print("或通过环境变量 SIGNET_BASE_URL 或 ~/.signet/client.json 指定已启动的服务地址。", file=sys.stderr)
        return 3
    if status == 200 and isinstance(payload, dict):
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=True, sort_keys=True))
        return 0
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return emit_error(error["code"], error.get("message") or "", error.get("details") or {}, HTTP_EXIT_CODES.get(status, 4))
    message = json.dumps(payload, ensure_ascii=True) if payload is not None else f"HTTP {status}"
    return emit_error("request.invalid", message[:500], {}, 2 if status in (400, 409, 422) else 4)
