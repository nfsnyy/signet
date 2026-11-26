"""T21 core-modularization：agent/operator 请求构造域（自 signet_client.py 整函数搬入，函数体零变更）。

职责：build_request/present——全部 CLI 动词到 HTTP 请求（方法/路径/字段/文件）的映射。
仅标准库，不得 import aidev。
"""

import hashlib
import json
import urllib.parse
from pathlib import Path

from .credentials import resolve_cli_user
from .http import ClientInputError
from .projection import contract_assets, read_upload, resolve_task_id

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
    # T23/REQ-5（AC-07）：远程 operator 日常生命周期动词——授权远程通道
    # （EP-01~EP-07，仅本人登录）；doc-put 的 base 冲突（409）映射退出码 3。
    if command == "user-create":
        return "POST", "/api/users", {"user": arguments.user, "password": arguments.password}, None
    if command == "user-disable":
        return "POST", f"/api/users/{urllib.parse.quote(arguments.user, safe='')}/disable", None, None
    if command == "user-reset-password":
        return "POST", f"/api/users/{urllib.parse.quote(arguments.user, safe='')}/password", {"password": arguments.password}, None
    if command == "user-list":
        return "GET", "/api/users", None, None
    if command == "sigt-reissue":
        fields = present({"actor": arguments.actor, "repo": getattr(arguments, "repo", None)})
        return "POST", f"/api/tasks/{arguments.task_id}/sigt", fields, None
    if command == "doc-get":
        return "GET", f"/api/docs/{urllib.parse.quote(arguments.name, safe='/')}", None, None
    if command == "doc-put":
        _name, raw = read_upload(arguments.file, "doc.file_missing")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ClientInputError("doc.file_invalid", f"doc-put 文件必须是 UTF-8 文本: {arguments.file}")
        fields = {"base_sha256": getattr(arguments, "base_sha256", None), "content": content}
        return "POST", f"/api/docs/{urllib.parse.quote(arguments.name, safe='/')}", fields, None
    raise ClientInputError("operation.unsupported", f"unsupported command: {command}")
