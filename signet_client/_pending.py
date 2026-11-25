"""T21 core-modularization 过渡承载模块（S5-S6 临时）：尚未迁入最终域模块的函数暂存于此，
随 S6/S7 逐步搬空后删除。函数体与基线 signet_client.py 逐字节一致。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

from .credentials import (
    WORKSPACE_TOKEN_FILE,
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

def resolve_task_id(workspace):
    path = Path(workspace) / "task.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ClientInputError("task.not_found", f"无法读取工作区 task.md: {workspace}") from error
    # CRLF 行尾容忍（债务 #11c）：与服务端 read_task 同一归一化口径。
    text = text.replace("\r\n", "\n")
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

def contract_assets(contract_path):
    """Collect the asset files a contract references (dependency_files + case fixtures).

    Paths are resolved relative to the contract file's directory, mirroring the
    server-side contract validation layout. Returns (manifest, files)——manifest 为
    api-remote-ready 目标形态 [{"path","sha256"}] 对象清单（随 freeze/veto 请求
    经 asset_manifest 字段上传，服务端逐件校验 sha256）。
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
    manifest = []
    for relative in relative_paths:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ClientInputError("c3.invalid_fixture", f"asset path must stay inside the spec directory: {relative}")
        full_path = base / relative_path
        if not full_path.is_file():
            raise ClientInputError("c3.missing_fixture", f"fixture does not exist: {relative}")
        content = full_path.read_bytes()
        manifest.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
        files.append(("assets", relative, content))
    return manifest, files

WORKSPACE_DIRECTORIES = ("contract", "evidence", "inbox", "outbox", "outbox-collab")

def _safe_workspace_file(workspace, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not str(name).strip():
        raise ClientInputError("package.invalid_file", f"任务包文件路径不合法: {name}")
    return Path(workspace) / relative

def pull_package(base_url, task_id, token=None, sigt=None, repo=None):
    """REQ-3/AC-09：投影客户端拉取——经任务包读面取回完整投影。repo 为 CLI
    --repo 透传（逻辑 id/本地路径），省略时落服务端绑定存储。"""
    path = f"/api/tasks/{task_id}/package"
    if repo:
        path += "?" + urllib.parse.urlencode({"repo": repo})
    try:
        status, payload = perform("GET", base_url + path, token=token, sigt=sigt)
    except ServiceUnavailable as error:
        raise ClientInputError("service.unreachable", f"拉取任务包时无法连接服务: {error.base_url}", exit_code=3) from error
    if status != 200 or not (isinstance(payload, dict) and payload.get("ok")):
        raise ClientInputError("package.pull_failed", f"任务包拉取失败（HTTP {status}）: {task_id}", exit_code=4)
    return payload["result"]

def _land_package_files(workspace, package, token=None, sigt=None, repo=None):
    """把任务包落盘工作区：task.md、工作区附带文件、contract/ 资产（逐项 sha256 校验）。"""
    workspace = Path(workspace)
    if workspace.exists() and not workspace.is_dir():
        raise ClientInputError("workspace.not_directory", f"工作区路径不是目录: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in WORKSPACE_DIRECTORIES:
        (workspace / directory).mkdir(exist_ok=True)
    metadata = package.get("metadata")
    body = package.get("task_md")
    if not isinstance(metadata, dict) or not isinstance(body, str):
        raise ClientInputError("package.invalid", "服务端响应缺少任务包 metadata/task_md，已中止落地")
    header = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    (workspace / "task.md").write_text(f"---\n{header}\n---\n{body}", encoding="utf-8", newline="\n")
    for name, content in (package.get("workspace_files") or {}).items():
        target = _safe_workspace_file(workspace, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    base_url = resolve_base_url()
    task_id = metadata["task_id"]
    for asset in package.get("assets") or []:
        name = asset.get("name")
        expected = asset.get("sha256")
        if not name or not expected:
            raise ClientInputError("package.invalid", "任务包资产清单条目缺少 name/sha256")
        asset_path = f"/api/tasks/{task_id}/assets/{urllib.parse.quote(name)}"
        if repo:
            asset_path += "?" + urllib.parse.urlencode({"repo": repo})
        try:
            status, content = perform(
                "GET",
                base_url + asset_path,
                token=token,
                sigt=sigt,
                raw=True,
            )
        except ServiceUnavailable as error:
            raise ClientInputError("service.unreachable", f"资产下载时无法连接服务: {error.base_url}", exit_code=3) from error
        if status != 200:
            raise ClientInputError("asset.download_failed", f"资产下载失败（HTTP {status}）: {name}", exit_code=4)
        if hashlib.sha256(content).hexdigest() != expected:
            raise ClientInputError("asset.hash_mismatch", f"资产 sha256 与清单不一致: {name}", exit_code=4)
        target = _safe_workspace_file(workspace / "contract", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return task_id

def land_projection(workspace, package, token=None, repo=None):
    """REQ-3/AC-09：operator 侧投影落地——freeze/refreeze/sync/init-task/reject/adjudicate
    成功后，CLI 自行拉取任务包并把 task.md/contract/ 三件套落盘本地 --workspace
    （该参数自此纯客户端本地使用，不出网）。"""
    return _land_package_files(workspace, package, token=token, repo=repo)

def land_handoff_workspace(workspace, token, package):
    """把 claim --handoff 任务包落地工作区（task.md、工作区附带文件、contract/ 资产、.signet-token）。"""
    task_id = _land_package_files(workspace, package, sigt=token)
    token_path = Path(workspace) / WORKSPACE_TOKEN_FILE
    token_path.write_text(token + "\n", encoding="utf-8", newline="\n")
    return task_id

def handoff_claim(arguments):
    """claim --handoff：本地预检验签（零网络）→ sigt 幂等接管 → 任务包落地工作区。"""
    payload, code, message = verify_sigt_local(arguments.handoff)
    if code:
        return emit_error(code, message, {}, 2)
    task_id = payload["task"]
    base_url = resolve_base_url()
    fields = {"actor": payload["actor"]}
    cli_user = resolve_cli_user()
    if cli_user:
        fields["on_behalf_of"] = cli_user
    try:
        status, response = perform("POST", f"{base_url}/api/tasks/{task_id}/claims", fields=fields, sigt=arguments.handoff)
    except ServiceUnavailable as error:
        return emit_unreachable(error)
    if not (isinstance(response, dict) and response.get("ok")):
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return emit_error(error["code"], error.get("message") or "", error.get("details") or {}, HTTP_EXIT_CODES.get(status, 4))
        return emit_error("request.invalid", json.dumps(response, ensure_ascii=True)[:500], {}, 2 if status in (400, 409) else 4)
    try:
        land_handoff_workspace(arguments.workspace, arguments.handoff, response.get("result", {}).get("package") or {})
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return 0

def _git_local(repository, arguments, timeout=60):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )

def local_git_ship(task_id):
    """ship 的 CLI 本机副作用（AC-05）：平台仓由 SIGNET_PLATFORM_REPO 定位——
    工作副本有未提交改动时 add/commit/push（提交信息含 task_id），干净时幂等 noop。
    纯本机 git 操作，不经服务端；task.shipped 事件由 API ship / close --ship 落账。
    """
    root = os.environ.get("SIGNET_PLATFORM_REPO") or ""
    if not root.strip():
        raise ClientInputError(
            "ship.platform_repo_required",
            "ship 需要环境变量 SIGNET_PLATFORM_REPO 定位平台仓工作副本",
        )
    root = Path(root)
    probe = _git_local(root, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise ClientInputError("ship.not_a_worktree", f"SIGNET_PLATFORM_REPO 不是 git 工作副本: {root}")
    committed = False
    status = _git_local(root, ["status", "--porcelain"])
    if status.returncode != 0:
        raise ClientInputError("ship.status_failed", f"无法读取工作副本状态: {(status.stderr or '')[-200:]}")
    if status.stdout.strip():
        add = _git_local(root, ["add", "-A"])
        if add.returncode != 0:
            raise ClientInputError("ship.add_failed", f"git add 失败: {(add.stderr or '')[-200:]}")
        commit = _git_local(root, ["commit", "-m", f"ship: task {task_id}"])
        if commit.returncode != 0:
            raise ClientInputError("ship.commit_failed", f"git commit 失败: {(commit.stderr or '')[-200:]}")
        committed = True
    pushed = False
    remotes = _git_local(root, ["remote"])
    if remotes.returncode == 0 and remotes.stdout.strip():
        remote = remotes.stdout.splitlines()[0].strip()
        push = _git_local(root, ["push", remote, "HEAD"], timeout=120)
        pushed = push.returncode == 0
        if not pushed:
            raise ClientInputError("ship.push_failed", f"git push 失败: {(push.stderr or '')[-200:]}")
    head = _git_local(root, ["rev-parse", "HEAD"])
    commit_hash = head.stdout.strip() if head.returncode == 0 else None
    return {
        "task_id": task_id,
        "platform_repo": str(root),
        "committed": committed,
        "noop": not committed,
        "pushed": pushed,
        "commit_hash": commit_hash,
    }

def run_local_ship(arguments):
    try:
        result = local_git_ship(arguments.task_id)
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=True, sort_keys=True))
    return 0

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
