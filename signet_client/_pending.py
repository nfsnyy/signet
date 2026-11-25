"""T21 core-modularization 过渡承载模块（S5-S6 临时）：尚未迁入最终域模块的函数暂存于此，
随 S6/S7 逐步搬空后删除。函数体与基线 signet_client.py 逐字节一致。
"""

import argparse
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

from .credentials import (
    inspect_handoff,
    resolve_cli_user,
    resolve_token,
    store_client_config,
    verify_sigt_local,
    workspace_handoff_token,
)
from .http import (
    HTTP_EXIT_CODES,
    ClientInputError,
    ServiceUnavailable,
    emit_error,
    emit_unreachable,
    perform,
    resolve_base_url,
)
from .legacy_ship import local_git_ship, run_local_ship
from .projection import (
    contract_assets,
    handoff_claim,
    land_projection,
    pull_package,
    read_upload,
    resolve_task_id,
)

def present(fields):
    return {key: value for key, value in fields.items() if value is not None}

def build_request(role, arguments, sigt_payload=None):
    """组请求；sigt_payload 非空表示工作区自动附带模式（任务取自令牌，免 workspace 字段）。"""

    def task_id():
        if sigt_payload:
            return sigt_payload["task"]
        return resolve_task_id(arguments.workspace)

    command = arguments.command

    def sigt_attribution():
        # AC-16 委托流归因：actor=令牌签发人（问责锁定），on_behalf_of=CLI 本人登录身份（执行者）。
        fields = {"actor": sigt_payload["actor"]}
        cli_user = resolve_cli_user()
        if cli_user:
            fields["on_behalf_of"] = cli_user
        return fields

    if command == "health":
        return "GET", "/api/health", None, None
    if command == "login":
        return "POST", "/api/login", {"user": arguments.user, "password": arguments.password}, None
    if command == "log":
        if sigt_payload:
            return "GET", f"/api/tasks/{task_id()}/events", None, None
        query = urllib.parse.urlencode({"repo": arguments.repo})
        return "GET", f"/api/tasks/{arguments.task_id}/events?{query}", None, None
    if command == "status":
        if sigt_payload:
            return "GET", f"/api/tasks/{task_id()}/status", None, None
        query = urllib.parse.urlencode({"workspace": arguments.workspace})
        return "GET", f"/api/tasks/{task_id()}/status?{query}", None, None
    if command == "claim":
        if sigt_payload:
            return "POST", f"/api/tasks/{task_id()}/claims", sigt_attribution(), None
        fields = present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of})
        return "POST", f"/api/tasks/{task_id()}/claims", fields, None
    if command == "submit":
        fields = dict(sigt_attribution()) if sigt_payload else present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of})
        files = [("report", *read_upload(arguments.report, "internal.error", missing_exit=4))]
        files.extend(("evidence", *read_upload(path, "submit.evidence_missing")) for path in arguments.evidence)
        return "POST", f"/api/tasks/{task_id()}/submission", fields, files
    if command == "block":
        fields = dict(sigt_attribution()) if sigt_payload else present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "reason": arguments.reason})
        if "reason" not in fields:
            fields["reason"] = arguments.reason
        files = [("evidence", *read_upload(path, "submit.evidence_missing")) for path in arguments.evidence]
        return "POST", f"/api/tasks/{task_id()}/blocks", fields, files
    if command == "enhancement":
        fields = dict(sigt_attribution()) if sigt_payload else present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "title": arguments.title, "details": arguments.details})
        for key in ("title", "details", "workspace"):
            if key not in fields and hasattr(arguments, key):
                value = getattr(arguments, key)
                if value is not None:
                    fields[key] = value
        return "POST", f"/api/tasks/{task_id()}/enhancements", fields, None
    if command == "collab-send":
        fields = dict(sigt_attribution()) if sigt_payload else present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "type": arguments.type, "to_task": arguments.to_task, "summary": arguments.summary, "details": arguments.details})
        for key in ("type", "to_task", "summary", "details", "workspace"):
            if key not in fields and hasattr(arguments, key):
                value = getattr(arguments, key)
                if value is not None:
                    fields[key] = value
        return "POST", f"/api/tasks/{task_id()}/collaborations", fields, None
    if command == "collab-answer":
        fields = dict(sigt_attribution()) if sigt_payload else present({"workspace": arguments.workspace, "actor": arguments.actor, "on_behalf_of": arguments.on_behalf_of, "action": arguments.action, "note": arguments.note})
        for key in ("action", "note", "workspace"):
            if key not in fields and hasattr(arguments, key):
                value = getattr(arguments, key)
                if value is not None:
                    fields[key] = value
        return "POST", f"/api/collaborations/{arguments.message_id}/answers", fields, None
    if command == "init-repository":
        fields = present({"repo": arguments.repo, "actor": arguments.actor, "name": arguments.name, "owner": arguments.owner})
        return "POST", "/api/repositories", fields, None
    if command == "init-task":
        # api-remote-ready（REQ-3/AC-09）：--workspace 纯客户端本地使用、不出网——
        # 服务端建任务后由 CLI 经任务包拉取自行落地投影。
        fields = present({"repo": arguments.repo, "title": arguments.title, "requirement": arguments.requirement, "owner": arguments.owner, "actor": arguments.actor})
        return "POST", "/api/tasks", fields, None
    if command in ("freeze", "refreeze"):
        manifest, asset_files = contract_assets(arguments.contract)
        card_name, card_b = read_upload(arguments.acceptance_card, "c1.invalid_card")
        contract_name, contract_b = read_upload(arguments.contract, "c2.invalid_contract")
        mapping_name, mapping_b = read_upload(arguments.mapping, "c1.invalid_mapping")
        fields = present({
            "repo": arguments.repo,
            "workspace": arguments.workspace,
            "h1_actor": arguments.h1_actor,
            "h2_actor": arguments.h2_actor,
            "reason": getattr(arguments, "reason", None),
            "push": str(arguments.push).lower(),
            # AC-05：三件套随件摘要（服务端校验不符即 c3.content_hash_mismatch）。
            "card_sha256": hashlib.sha256(card_b).hexdigest(),
            "contract_sha256": hashlib.sha256(contract_b).hexdigest(),
            "mapping_sha256": hashlib.sha256(mapping_b).hexdigest(),
            "asset_manifest": json.dumps(manifest, ensure_ascii=False),
        })
        files = [
            ("card", card_name, card_b),
            ("contract", contract_name, contract_b),
            ("mapping", mapping_name, mapping_b),
        ]
        files.extend(asset_files)
        if arguments.operation_source:
            files.append(("operation_source", *read_upload(arguments.operation_source, "c2.operation_source_missing")))
        endpoint = "freeze" if command == "freeze" else "refreeze"
        return "POST", f"/api/tasks/{arguments.task_id}/{endpoint}", fields, files
    if command == "sync":
        # AC-08：sync 改内容上传——读本地各工作区 outbox 组装 proposals，submit 类
        # 提案的证据字节随 multipart 上传（服务端按文件名匹配，不按 client 路径读）。
        proposals = []
        evidence_uploads = []
        workspace_paths = list(arguments.workspace or [])
        for workspace in workspace_paths:
            outbox = Path(workspace) / "outbox"
            if not outbox.is_dir():
                continue
            for path in sorted(outbox.glob("*.json")):
                try:
                    proposal = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(proposal, dict):
                    continue
                proposals.append(proposal)
                for item in (proposal.get("parameters") or {}).get("evidence") or []:
                    source = Path(str(item.get("path") or ""))
                    if source.is_file():
                        evidence_uploads.append(("evidence", source.name, source.read_bytes()))
        fields = present({"repo": arguments.repo, "actor": arguments.actor, "push": str(arguments.push).lower()})
        if evidence_uploads:
            fields["proposals"] = json.dumps(proposals, ensure_ascii=False)
            fields["workspace"] = json.dumps(workspace_paths, ensure_ascii=False)
            return "POST", "/api/sync", fields, evidence_uploads
        fields["proposals"] = proposals
        fields["workspace"] = workspace_paths
        return "POST", "/api/sync", fields, None
    if command == "verify":
        fields = present({
            "repo": arguments.repo,
            "verifier": arguments.verifier,
            "confirmed_by": arguments.confirmed_by,
            "execute": str(arguments.execute).lower(),
            "push": str(arguments.push).lower(),
            "reverify": str(getattr(arguments, "reverify", False)).lower(),
        })
        files = []
        if arguments.results:
            files.append(("results", *read_upload(arguments.results, "verify.invalid_results")))
        if arguments.manual_results:
            files.append(("manual_results", *read_upload(arguments.manual_results, "verify.invalid_manual_results")))
        if arguments.review_report:
            files.append(("review_report", *read_upload(arguments.review_report, "verify.ai_review_invalid_report")))
        return "POST", f"/api/tasks/{arguments.task_id}/verify", fields, files
    if command == "reject":
        fields = present({"repo": arguments.repo, "category": arguments.category, "reason": arguments.reason, "actor": arguments.actor, "workspace": arguments.workspace, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/reject", fields, None
    if command == "close":
        fields = present({
            "repo": arguments.repo,
            "decision": arguments.decision,
            "actor": arguments.actor,
            "reason": arguments.reason,
            "push": str(arguments.push).lower(),
            "ship": str(getattr(arguments, "ship", False)).lower(),
            "ship_commit": getattr(arguments, "ship_commit", None),
            "ship_fingerprint": getattr(arguments, "ship_fingerprint", None),
        })
        return "POST", f"/api/tasks/{arguments.task_id}/close", fields, None
    if command == "void":
        fields = present({"repo": arguments.repo, "reason": arguments.reason, "actor": arguments.actor, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/void", fields, None
    if command == "adjudicate":
        fields = present({"repo": arguments.repo, "category": arguments.category, "reason": arguments.reason, "actor": arguments.actor, "workspace": arguments.workspace, "push": str(arguments.push).lower()})
        return "POST", f"/api/tasks/{arguments.task_id}/adjudication", fields, None
    if command == "veto":
        manifest, asset_files = contract_assets(arguments.contract) if arguments.contract else ([], [])
        files = []
        digests = {}
        for field in ("card", "contract", "mapping"):
            path = getattr(arguments, {"card": "acceptance_card"}.get(field, field))
            if path:
                name, content = read_upload(path, "veto.file_missing")
                files.append((field, name, content))
                digests[f"{field}_sha256"] = hashlib.sha256(content).hexdigest()
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
            "asset_manifest": json.dumps(manifest, ensure_ascii=False),
        })
        fields.update(digests)
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
                                  help="handoff 令牌会话启动：本地预检验签后幂等接管并落地任务包")
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
            sigt_payload, code, message = verify_sigt_local(token)
            if code:
                return emit_error(code, message + "（工作区 .signet-token 验签失败）", {}, 2)
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
