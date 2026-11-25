"""T21 core-modularization：workspace 投影与任务材料域（自 signet_client.py 整函数搬入，函数体零变更）。

职责：resolve_task_id、contract_assets、任务包拉取与落地（pull_package/_land_package_files/
land_projection/land_handoff_workspace）、handoff_claim。仅标准库，不得 import aidev。
"""

import hashlib
import json
import urllib.parse
from pathlib import Path

from .credentials import WORKSPACE_TOKEN_FILE, resolve_cli_user, verify_sigt_local
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
