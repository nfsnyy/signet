"""T21 core-modularization：CLI parser 与命令分发域（自 signet_client.py 整函数搬入）。

职责：parser_for 动词面、run 入口、dispatch_request 分发、land_after_success 投影落地。
T22/REQ-1（AC-03）：login 前先经 EP-02 核对 API 协议版本，不兼容退出 4 并提示
「不兼容」「更新」（不做客户端自动更新）；T22/REQ-2（AC-05）：工作区 sigt 自动附带
只做格式解析，验签一律服务端裁决。仅标准库，不得 import aidev。
"""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

from .credentials import (
    inspect_handoff,
    parse_sigt,
    resolve_token,
    store_client_config,
    workspace_handoff_token,
)
from .http import (
    API_VERSION,
    HTTP_EXIT_CODES,
    ClientInputError,
    ServiceUnavailable,
    emit_error,
    emit_unreachable,
    perform,
    resolve_base_url,
)
from .legacy_ship import local_git_ship, run_local_ship
from .projection import handoff_claim, land_projection, pull_package
from .requests import build_request

def parser_for(role):
    parser = argparse.ArgumentParser(prog=f"aidev-{role}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    if role == "operator":
        login = subparsers.add_parser("login")
        login.add_argument("--user", required=True)
        login.add_argument("--password", required=True)
        repository = subparsers.add_parser("init-repository")
        repository.add_argument("--repo")
        repository.add_argument("--actor", required=True)
        repository.add_argument("--name")
        repository.add_argument("--owner")
        task = subparsers.add_parser("init-task")
        task.add_argument("--repo")
        task.add_argument("--workspace", required=True)
        task.add_argument("--title", required=True)
        task.add_argument("--requirement", required=True)
        task.add_argument("--owner", required=True)
        task.add_argument("--actor", required=True)
        freeze = subparsers.add_parser("freeze")
        freeze.add_argument("--repo")
        freeze.add_argument("--task-id", required=True)
        freeze.add_argument("--workspace")
        freeze.add_argument("--acceptance-card", required=True)
        freeze.add_argument("--contract", required=True)
        freeze.add_argument("--mapping", required=True)
        freeze.add_argument("--operation-source")
        freeze.add_argument("--h1-actor", required=True)
        freeze.add_argument("--h2-actor", required=True)
        freeze.add_argument("--push", action="store_true")
        refreeze = subparsers.add_parser("refreeze")
        refreeze.add_argument("--repo")
        refreeze.add_argument("--task-id", required=True)
        refreeze.add_argument("--workspace")
        refreeze.add_argument("--acceptance-card", required=True)
        refreeze.add_argument("--contract", required=True)
        refreeze.add_argument("--mapping", required=True)
        refreeze.add_argument("--operation-source")
        refreeze.add_argument("--h1-actor", required=True)
        refreeze.add_argument("--h2-actor", required=True)
        refreeze.add_argument("--reason", required=True)
        refreeze.add_argument("--push", action="store_true")
        verify = subparsers.add_parser("verify")
        verify.add_argument("--repo")
        verify.add_argument("--task-id", required=True)
        verify.add_argument("--results")
        verify.add_argument("--execute", action="store_true")
        verify.add_argument("--manual-results")
        verify.add_argument("--review-report")
        verify.add_argument("--reverify", action="store_true",
                            help="已 Verified 项目任务在新 main 基点显式重跑全部验收门（源 commit 不变）")
        verify.add_argument("--verifier", required=True)
        verify.add_argument("--confirmed-by", required=True)
        verify.add_argument("--push", action="store_true")
        reject = subparsers.add_parser("reject")
        reject.add_argument("--repo")
        reject.add_argument("--task-id", required=True)
        reject.add_argument("--category", choices=["implementation", "input"], required=True)
        reject.add_argument("--reason", required=True)
        reject.add_argument("--actor", required=True)
        reject.add_argument("--workspace")
        reject.add_argument("--push", action="store_true")
        close = subparsers.add_parser("close")
        close.add_argument("--repo")
        close.add_argument("--task-id", required=True)
        close.add_argument("--decision", choices=["accept", "reject"], required=True)
        close.add_argument("--actor", required=True)
        close.add_argument("--reason")
        close.add_argument("--push", action="store_true")
        close.add_argument("--ship", action="store_true", help="close 后顺带执行本机 ship（git add/commit/push 由 CLI 在 SIGNET_PLATFORM_REPO 工作副本执行）")
        ship_parser = subparsers.add_parser("ship")
        ship_parser.add_argument("--repo")
        ship_parser.add_argument("--task-id", required=True)
        void = subparsers.add_parser("void")
        void.add_argument("--repo")
        void.add_argument("--task-id", required=True)
        void.add_argument("--reason", choices=["definition-defect", "cancel", "specification-invalidated"], required=True)
        void.add_argument("--actor", required=True)
        void.add_argument("--push", action="store_true")
        adjudicate = subparsers.add_parser("adjudicate")
        adjudicate.add_argument("--repo")
        adjudicate.add_argument("--task-id", required=True)
        adjudicate.add_argument("--category", choices=["implementation", "definition"], required=True)
        adjudicate.add_argument("--reason", required=True)
        adjudicate.add_argument("--actor", required=True)
        adjudicate.add_argument("--workspace")
        adjudicate.add_argument("--push", action="store_true")
        sync = subparsers.add_parser("sync")
        sync.add_argument("--repo")
        sync.add_argument("--workspace", action="append", required=True)
        sync.add_argument("--actor", required=True)
        sync.add_argument("--push", action="store_true")
        veto = subparsers.add_parser("veto")
        veto.add_argument("--repo")
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
        inbox.add_argument("--repo")
        inbox.add_argument("--task-id")
    else:
        agent_login = subparsers.add_parser("login")
        agent_login.add_argument("--user", required=True)
        agent_login.add_argument("--password", required=True)
        inspect_parser = subparsers.add_parser("handoff-inspect")
        inspect_parser.add_argument("token")
        claim_parser = subparsers.add_parser("claim")
        claim_parser.add_argument("--handoff", metavar="SIGT",
                                  help="handoff 令牌会话启动：格式解析后交服务端验签裁决，幂等接管并落地任务包")
        claim_parser.add_argument("--workspace", required=True)
        claim_parser.add_argument("--actor")
        claim_parser.add_argument("--on-behalf-of")
        submit_parser = subparsers.add_parser("submit")
        submit_parser.add_argument("--workspace", required=True)
        submit_parser.add_argument("--actor")
        submit_parser.add_argument("--on-behalf-of")
        submit_parser.add_argument("--report", required=True)
        submit_parser.add_argument("--evidence", action="append", required=True)
        block_parser = subparsers.add_parser("block")
        block_parser.add_argument("--workspace", required=True)
        block_parser.add_argument("--actor")
        block_parser.add_argument("--on-behalf-of")
        block_parser.add_argument("--reason", required=True)
        block_parser.add_argument("--evidence", action="append", required=True)
        enhancement_parser = subparsers.add_parser("enhancement")
        enhancement_parser.add_argument("--workspace", required=True)
        enhancement_parser.add_argument("--actor")
        enhancement_parser.add_argument("--on-behalf-of")
        enhancement_parser.add_argument("--title", required=True)
        enhancement_parser.add_argument("--details", required=True)
        status_parser = subparsers.add_parser("status")
        status_parser.add_argument("--workspace", required=True)
        collab_send_parser = subparsers.add_parser("collab-send")
        collab_send_parser.add_argument("--workspace", required=True)
        collab_send_parser.add_argument("--actor")
        collab_send_parser.add_argument("--on-behalf-of")
        collab_send_parser.add_argument("--type", choices=["in-spec-request", "definition-doubt", "new-scope"], required=True)
        collab_send_parser.add_argument("--to-task", required=True)
        collab_send_parser.add_argument("--summary", required=True)
        collab_send_parser.add_argument("--details", required=True)
        collab_answer_parser = subparsers.add_parser("collab-answer")
        collab_answer_parser.add_argument("--workspace", required=True)
        collab_answer_parser.add_argument("--actor")
        collab_answer_parser.add_argument("--on-behalf-of")
        collab_answer_parser.add_argument("--message-id", required=True)
        collab_answer_parser.add_argument("--action", choices=["accept", "reject", "counter", "escalate", "close"], required=True)
        collab_answer_parser.add_argument("--note")
    subparsers.add_parser("health")
    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--repo", required=True)
    log_parser.add_argument("--task-id", required=True)
    return parser

def land_after_success(role, arguments, payload, base_url, token):
    """REQ-3/AC-09：operator 侧动词成功后自行拉取任务包并落地投影——服务端已删除
    全部 client 工作区写面；--workspace 自此纯客户端本地使用。返回退出码或 None。"""
    command = arguments.command
    repo = getattr(arguments, "repo", None)

    def pull(task_id):
        return pull_package(base_url, task_id, token=token, repo=repo)

    try:
        if command == "init-task":
            workspace = getattr(arguments, "workspace", None)
            task_id = (payload.get("result") or {}).get("task_id")
            if not workspace or not task_id:
                return None
            target = Path(workspace)
            if target.exists() and any(target.iterdir()):
                raise ClientInputError("workspace.not_empty", "agent workspace must be empty")
            land_projection(target, pull(task_id), token=token, repo=repo)
            return None
        if command in ("freeze", "refreeze", "reject", "adjudicate"):
            workspace = getattr(arguments, "workspace", None)
            task_id = getattr(arguments, "task_id", None)
            if not workspace or not task_id:
                return None
            land_projection(Path(workspace), pull(task_id), token=token, repo=repo)
            return None
        if command == "sync":
            for workspace in list(getattr(arguments, "workspace", None) or []):
                tasks = []
                outbox = Path(workspace) / "outbox"
                if outbox.is_dir():
                    for path in sorted(outbox.glob("*.json")):
                        try:
                            proposal = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        task_id = proposal.get("task_id") if isinstance(proposal, dict) else None
                        if task_id and task_id not in tasks:
                            tasks.append(task_id)
                for task_id in tasks:
                    land_projection(Path(workspace), pull(task_id), token=token, repo=repo)
            return None
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)
    return None

def dispatch_request(role, arguments, sigt, sigt_payload):
    if role == "agent" and sigt is None:
        workspace = getattr(arguments, "workspace", None)
        token = workspace_handoff_token(workspace) if workspace else None
        if token:
            sigt = token
            # T22/REQ-2（AC-05）：工作区 sigt 自动附带只做格式解析——客户端不持有
            # 服务端密钥，令牌真实性由服务端裁决（伪造/越权一律服务端 401/403）。
            sigt_payload, code, message = parse_sigt(token)
            if code:
                return emit_error(code, message + "（工作区 .signet-token 解析失败）", {}, 2)
    try:
        method, path, fields, files = build_request(role, arguments, sigt_payload)
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)
    base_url = resolve_base_url()
    token = None if arguments.command == "login" or sigt else resolve_token()
    try:
        status, payload = perform(method, base_url + path, fields=fields, files=files, role=role, token=token, sigt=sigt)
    except ServiceUnavailable as error:
        return emit_unreachable(error)
    if arguments.command == "login" and status == 200 and isinstance(payload, dict) and payload.get("ok"):
        result = payload.get("result") or {}
        store_client_config(base_url, result.get("token"), result.get("user"))
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    if isinstance(payload, dict) and payload.get("ok") is True and "result" in payload:
        landing = land_after_success(role, arguments, payload, base_url, token)
        if landing is not None:
            return landing
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    if isinstance(payload, dict) and payload.get("ok") is False and isinstance(payload.get("error"), dict):
        error = payload["error"]
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return HTTP_EXIT_CODES.get(status, 4)
    if status == 200 and isinstance(payload, dict):
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=True, sort_keys=True))
        return 0
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return emit_error(error["code"], error.get("message") or "", error.get("details") or {}, HTTP_EXIT_CODES.get(status, 4))
    message = json.dumps(payload, ensure_ascii=True) if payload is not None else f"HTTP {status}"
    return emit_error("request.invalid", message[:500], {}, 2 if status in (400, 409, 422) else 4)

def ensure_api_compatible():
    """REQ-1/AC-03：登录前与服务器版本接口（EP-02）核对 API 协议版本。

    服务器申报版本与客户端一致时返回 None 放行；不一致即拒绝继续：退出码
    钉 4，输出明确提示「不兼容」与「更新」。首版不做客户端自动更新
    （自更新属独立 enhancement）。
    """
    base_url = resolve_base_url()
    try:
        status, payload = perform("GET", base_url + "/api/client/version")
    except ServiceUnavailable as error:
        return emit_unreachable(error)
    result = payload.get("result") if isinstance(payload, dict) else None
    server_version = result.get("api_version") if isinstance(result, dict) else None
    if status == 200 and isinstance(server_version, int) and server_version == API_VERSION:
        return None
    if status == 200 and isinstance(server_version, int):
        code = "client.api_incompatible"
        message = (
            f"客户端与服务器 API 协议版本不兼容（客户端 {API_VERSION}，服务器 {server_version}）："
            "请更新客户端包后重试"
        )
    else:
        code = "client.version_probe_failed"
        message = f"无法核对服务器 API 协议版本（HTTP {status}）：请更新客户端包或联系平台运维"
    print(json.dumps(
        {"ok": False, "error": {"code": code, "message": message, "details": {}}},
        ensure_ascii=False,
    ))
    return 4

def run(role, argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    parser = parser_for(role)
    arguments = parser.parse_args(argv)
    if arguments.command == "handoff-inspect":
        return inspect_handoff(arguments.token)
    if arguments.command == "login":
        incompatible = ensure_api_compatible()
        if incompatible is not None:
            return incompatible
    if role == "agent" and arguments.command == "claim" and getattr(arguments, "handoff", None):
        return handoff_claim(arguments)
    if role == "operator" and arguments.command == "ship":
        return run_local_ship(arguments)
    if role == "operator" and arguments.command == "close" and getattr(arguments, "ship", False):
        # close --ship 双通道（T18 REQ-7）：显式 --repo 命中非内置项目时由服务端推送
        # 已验收 merge_commit（本机零 git 副作用，不调用 local_git_ship）；省略 repo
        # 或内置项目维持本机兼容路径（AC-07：先跑本机 ship，commit_hash 随 close 落账）。
        repo = getattr(arguments, "repo", None)
        if repo:
            try:
                project_status, project_payload = perform(
                    "GET",
                    resolve_base_url() + f"/api/projects/{urllib.parse.quote(str(repo), safe='')}",
                    token=resolve_token(),
                )
            except ServiceUnavailable as error:
                return emit_unreachable(error)
            project = (
                (project_payload.get("result") or {}).get("project")
                if isinstance(project_payload, dict)
                else None
            )
            if project_status == 200 and isinstance(project, dict):
                if not project.get("builtin"):
                    return dispatch_request(role, arguments, sigt=None, sigt_payload=None)
            else:
                # 未知 repo id / 路径形态：本机零副作用，交由服务端按钉名码拒绝。
                return dispatch_request(role, arguments, sigt=None, sigt_payload=None)
        try:
            ship_result = local_git_ship(arguments.task_id)
        except ClientInputError as error:
            return emit_error(error.code, error.message, error.details, error.exit_code)
        arguments.ship_commit = ship_result.get("commit_hash")
        arguments.ship_fingerprint = None
        return dispatch_request(role, arguments, sigt=None, sigt_payload=None)
    return dispatch_request(role, arguments, sigt=None, sigt_payload=None)
