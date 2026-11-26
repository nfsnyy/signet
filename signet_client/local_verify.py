"""T24/REQ-6（AC-01）：operator 本机执行验收——CLI verify --execute 新语义。

流程：任务包取冻结用例（contract 资产按 spec_pin 逐件 sha256 核验）→ 项目远程仓
fetch code_submission 钉扎的精确 commit 建本机 detached 验收 worktree
（HEAD==被测 commit、status 干净）→ 按冻结契约 environment.allowed_env 白名单
净室逐案执行（SIGNET_CODE_WORKTREE 经白名单注入指向被测 worktree）→ 装配
results 文档（verifier/spec_pin/results + code attest 段）连同证据字节经
multipart 上传 POST /api/tasks/{task_id}/verify；服务端校验落账
Implemented→Verified。

平台不提供远程 runner；普通项目验收不获取平台源码；本模块不依赖预置
SIGNET_PLATFORM_REPO 定位被测对象（被测代码来自项目远程仓精确 commit）。
conformance 阶段随用例迁至本机：冻结契约 interfaces 非空时，对被测 worktree
（无 worktree 时回落 SIGNET_PLATFORM_REPO 工作副本）起的实例逐项 diff，
findings 非空即本地失败、不上传。仅标准库，不得 import aidev。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from .credentials import resolve_token
from .http import (
    HTTP_EXIT_CODES,
    ClientInputError,
    ServiceUnavailable,
    emit_error,
    emit_unreachable,
    perform,
    resolve_base_url,
)
from .projection import pull_package, read_upload

GIT_OP_TIMEOUT_SECONDS = 120
DEFAULT_CASE_TIMEOUT_SECONDS = 60
CASE_TIMEOUT_ENV = "SIGNET_CASE_TIMEOUT"
CONFORMANCE_TIMEOUT_SECONDS = 300

SPEC_PIN_FILES = {
    "acceptance_card": "acceptance-card.json",
    "contract": "contract.json",
    "mapping": "mapping.json",
}


# ---------------------------------------------------------------------------
# HTTP 读面（任务包 / 资产 / 项目记录）
# ---------------------------------------------------------------------------

def _get_json(base_url, path, token, error_code):
    try:
        status, payload = perform("GET", base_url + path, token=token)
    except ServiceUnavailable as error:
        raise ClientInputError("service.unreachable", f"无法连接服务: {error.base_url}", exit_code=3) from error
    if status != 200 or not (isinstance(payload, dict) and payload.get("ok")):
        raise ClientInputError(error_code, f"GET {path} 失败（HTTP {status}）", exit_code=4)
    return payload.get("result") or {}


def _get_bytes(base_url, path, token, error_code):
    try:
        status, content = perform("GET", base_url + path, token=token, raw=True)
    except ServiceUnavailable as error:
        raise ClientInputError("service.unreachable", f"无法连接服务: {error.base_url}", exit_code=3) from error
    if status != 200:
        raise ClientInputError(error_code, f"GET {path} 失败（HTTP {status}）", exit_code=4)
    return content


def fetch_frozen_assets(base_url, token, task_id, package, temporary, repo=None):
    """任务包 contract 资产落地临时目录并逐件核验（AC-01）。

    核验链：下载字节 sha256 对任务包清单（传输完整）→ 三件套对 spec_pin（权威
    钉扎）→ 用例/夹具文件对 contract.assets（冻结资产完整）。任一不符即中止，
    不进入执行。返回 (spec_directory, contract, mapping, card)。"""
    metadata = package.get("metadata") or {}
    spec_pin = metadata.get("spec_pin")
    if not isinstance(spec_pin, dict):
        raise ClientInputError("verify.spec_not_pinned", "任务未钉扎规格（无 spec_pin），无法本机执行验收")
    manifest = package.get("assets") or []
    if not manifest:
        raise ClientInputError("verify.package_invalid", "任务包无 contract 资产清单（任务未冻结？）")
    spec_directory = Path(temporary) / "frozen"
    contents = {}
    for entry in manifest:
        name = entry.get("name")
        expected = entry.get("sha256")
        if not name or not expected:
            raise ClientInputError("verify.package_invalid", "任务包资产清单条目缺少 name/sha256")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ClientInputError("verify.package_invalid", f"资产名越出规格目录: {name}")
        asset_path = f"/api/tasks/{task_id}/assets/{urllib.parse.quote(name)}"
        if repo:
            asset_path += "?" + urllib.parse.urlencode({"repo": repo})
        content = _get_bytes(base_url, asset_path, token, "verify.asset_download_failed")
        if hashlib.sha256(content).hexdigest() != expected:
            raise ClientInputError(
                "verify.asset_hash_mismatch",
                f"资产下载字节 sha256 与任务包清单不符: {name}",
            )
        target = spec_directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        contents[name] = content
    for key, filename in SPEC_PIN_FILES.items():
        expected = (spec_pin.get(key) or {}).get("sha256")
        actual = hashlib.sha256(contents.get(filename, b"")).hexdigest() if filename in contents else None
        if not expected or actual != expected:
            raise ClientInputError(
                "verify.asset_hash_mismatch",
                f"冻结三件套 sha256 与 spec_pin 不符: {filename}（规格可能被篡改）",
            )
    try:
        contract = json.loads(contents["contract.json"].decode("utf-8"))
        mapping = json.loads(contents["mapping.json"].decode("utf-8"))
        card = json.loads(contents["acceptance-card.json"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ClientInputError("verify.package_invalid", f"冻结三件套不是合法 JSON: {error}") from error
    asset_hashes = contract.get("assets") or {}
    for relative, expected in asset_hashes.items():
        path = spec_directory / relative
        if not path.is_file():
            raise ClientInputError("verify.missing_frozen_asset", f"冻结资产缺失: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ClientInputError("verify.asset_hash_mismatch", f"冻结资产 sha256 与 contract.assets 不符: {relative}")
    return spec_directory, contract, mapping, card


# ---------------------------------------------------------------------------
# 本机 detached 验收 worktree（被测对象 = 项目远程仓精确 commit）
# ---------------------------------------------------------------------------

def _git(cwd, arguments, env=None, timeout=GIT_OP_TIMEOUT_SECONDS):
    return subprocess.run(
        ["git", *arguments],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def build_code_worktree(base_url, token, code, temporary):
    """从项目远程仓 fetch 钉扎分支并建 detached 验收 worktree（HEAD==commit、status 干净）。"""
    project_id = code.get("code_repo_id")
    branch = code.get("branch")
    commit = code.get("commit_hash")
    record = _get_json(base_url, f"/api/projects/{urllib.parse.quote(str(project_id), safe='')}",
                       token, "verify.code_repo_unavailable").get("project") or {}
    url = record.get("code_repo")
    if not isinstance(url, str) or not url.strip():
        raise ClientInputError(
            "verify.code_repo_unavailable",
            f"项目 {project_id} 记录无 code_repo 远端地址，无法本机取被测 commit",
        )
    worktree = Path(temporary) / "code-worktree"
    env = _git_env()
    init = _git(None, ["init", "--initial-branch=main", str(worktree)], env=env)
    if init.returncode != 0:
        raise ClientInputError("verify.worktree_failed", f"git init 失败: {(init.stderr or '')[-200:]}")
    _git(worktree, ["remote", "add", "origin", url], env=env)
    fetch = _git(worktree, ["fetch", "origin", f"refs/heads/{branch}"], env=env)
    if fetch.returncode != 0:
        raise ClientInputError(
            "verify.code_fetch_failed",
            f"项目代码仓 fetch 失败（分支 {branch}）: {(fetch.stderr or '')[-200:]}",
        )
    checkout = _git(worktree, ["checkout", "--detach", commit], env=env)
    if checkout.returncode != 0:
        raise ClientInputError(
            "verify.commit_not_found",
            f"被测 commit 不在所报分支获取面内（{commit}）: {(checkout.stderr or '')[-200:]}",
        )
    head = _git(worktree, ["rev-parse", "HEAD"], env=env).stdout.strip()
    status = _git(worktree, ["status", "--porcelain"], env=env).stdout
    if head != commit or status != "":
        raise ClientInputError(
            "verify.worktree_dirty",
            f"验收 worktree 检查未通过（HEAD={head}，应={commit}；status={'干净' if status == '' else '脏'}）",
        )
    return worktree


# ---------------------------------------------------------------------------
# 净室逐案执行（镜像服务端 runner 语义，执行位置在 operator 本机）
# ---------------------------------------------------------------------------

def default_case_timeout():
    raw = os.environ.get(CASE_TIMEOUT_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_CASE_TIMEOUT_SECONDS


def case_timeout_seconds(case):
    declared = case.get("timeout_seconds")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    return default_case_timeout()


def clean_environment(environment):
    return {name: os.environ[name] for name in environment.get("allowed_env", []) if name in os.environ}


def resolve_command(command, runner):
    resolved = list(command)
    executable = resolved[0]
    if executable == runner:
        located = shutil.which(executable)
        if not located:
            raise ClientInputError("verify.runner_not_found", f"runner is not available: {executable}")
        resolved[0] = located
    elif not Path(executable).exists() and shutil.which(executable) is None:
        raise ClientInputError("verify.command_not_found", f"command is not available before clean environment setup: {executable}")
    return resolved


def contract_asset_paths(contract):
    """contract 引用的全部资产相对路径（dependency_files + 各案 fixtures，去重保序）。"""
    referenced = []
    environment = contract.get("environment") or {}
    dependencies = environment.get("dependency_files")
    if isinstance(dependencies, list):
        referenced.extend(item for item in dependencies if isinstance(item, str))
    for case in contract.get("cases") or []:
        if isinstance(case, dict):
            fixtures = case.get("fixtures")
            if isinstance(fixtures, list):
                referenced.extend(item for item in fixtures if isinstance(item, str))
    return list(dict.fromkeys(referenced))


def copy_frozen_assets(spec_directory, target_directory, contract):
    for relative in contract_asset_paths(contract):
        source = Path(spec_directory) / relative
        target = Path(target_directory) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def case_log(case_id, command, returncode, stdout, stderr, passed):
    return (
        f"case_id: {case_id}\n"
        f"command: {command}\n"
        f"exit_code: {returncode}\n"
        f"passed: {passed}\n\n"
        f"--- stdout ---\n{stdout}\n\n"
        f"--- stderr ---\n{stderr}\n"
    ).encode("utf-8")


def execute_cases_locally(spec_directory, contract, worktree):
    """按冻结契约白名单净室逐案执行；返回 {case_id: {passed, evidence, exit_code}}。"""
    environment_spec = contract.get("environment") or {}
    runner = environment_spec.get("runner") or "python"
    environment = clean_environment(environment_spec)
    if worktree is not None:
        # AC-01：SIGNET_CODE_WORKTREE 经白名单注入指向被测 worktree。
        environment["SIGNET_CODE_WORKTREE"] = str(worktree)
    case_results = {}
    with tempfile.TemporaryDirectory(prefix="signet-local-verify-") as exec_dir:
        copy_frozen_assets(spec_directory, exec_dir, contract)
        setup_command = environment_spec.get("setup_command")
        if setup_command:
            setup = resolve_command(setup_command, runner)
            result = subprocess.run(setup, cwd=exec_dir, env=environment, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=default_case_timeout(), check=False)
            if result.returncode != 0:
                raise ClientInputError("verify.setup_failed", "contract setup command failed",
                                       details={"command": setup, "exit_code": result.returncode,
                                                "stderr": (result.stderr or "")[-300:]})
        for case in contract.get("cases") or []:
            case_id = case["case_id"]
            command = resolve_command(case["command"], runner)
            timeout_seconds = case_timeout_seconds(case)
            try:
                completed = subprocess.run(
                    command, cwd=exec_dir, env=environment, capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=timeout_seconds, check=False)
                returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            except subprocess.TimeoutExpired:
                returncode, stdout, stderr = -1, "", f"command timed out after {timeout_seconds} seconds"
            expectation = case.get("expect") or {}
            passed = returncode == expectation.get("exit_code")
            if "stdout_contains" in expectation:
                passed = passed and expectation["stdout_contains"] in stdout
            content = case_log(case_id, command, returncode, stdout, stderr, passed)
            case_results[case_id] = {
                "passed": passed,
                "evidence": {"filename": f"{case_id}.log", "sha256": hashlib.sha256(content).hexdigest()},
                "exit_code": returncode,
                "_content": content,
            }
    return case_results


# ---------------------------------------------------------------------------
# results 文档装配（机测行 + 人工行 + safety-veto 复跑 + code attest 段）
# ---------------------------------------------------------------------------

def load_manual_results(manual_results_file, verifier):
    try:
        document = json.loads(Path(manual_results_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ClientInputError("verify.invalid_manual_results", f"manual results 不可读或非法 JSON: {error}") from error
    if not isinstance(document, dict):
        raise ClientInputError("verify.invalid_manual_results", "manual results 必须是 JSON 对象")
    if document.get("verifier") != verifier:
        raise ClientInputError("verify.actor_mismatch", "manual results verifier must match the verify actor")
    results = {}
    for result in document.get("results", []):
        if isinstance(result, dict) and result.get("criterion_id"):
            results[result["criterion_id"]] = result
    return results, document


def assemble_results_document(metadata, card, mapping, case_results, verifier, manual_results_file):
    safety_ids = {item.get("criterion_id") for item in card.get("criteria", [])
                  if isinstance(item, dict) and item.get("strength") == "safety-veto"}
    manual_rows = [row for row in mapping.get("rows", []) if isinstance(row, dict) and row.get("manual") is True]
    manual_results = {}
    manual_document = None
    if safety_ids or manual_rows:
        if not manual_results_file:
            raise ClientInputError("verify.manual_results_required",
                                   "manual criteria or safety-veto rerun results are required（--manual-results）")
        manual_results, manual_document = load_manual_results(manual_results_file, verifier)
    results = []
    for row in mapping.get("rows", []):
        if not isinstance(row, dict) or row.get("manual") is True:
            continue
        evidence = []
        passed = True
        for case_id in row.get("case_ids", []):
            case_result = case_results.get(case_id)
            if case_result is None:
                raise ClientInputError("verify.invalid_spec", f"mapping 行引用未知用例: {case_id}")
            passed = passed and case_result["passed"]
            evidence.append(dict(case_result["evidence"]))
        item = {"criterion_id": row["criterion_id"], "passed": passed, "evidence": evidence}
        if row["criterion_id"] in safety_ids:
            manual_result = manual_results.get(row["criterion_id"])
            if not manual_result or not isinstance(manual_result.get("manual_rerun"), dict):
                raise ClientInputError("verify.safety_rerun_required",
                                       f"safety-veto manual rerun is required: {row['criterion_id']}")
            item["manual_rerun"] = manual_result["manual_rerun"]
        results.append(item)
    for row in manual_rows:
        criterion_id = row["criterion_id"]
        if criterion_id not in manual_results:
            raise ClientInputError("verify.manual_results_required",
                                   f"manual criterion result is required: {criterion_id}")
        results.append(manual_results[criterion_id])
    document = {"verifier": verifier, "spec_pin": metadata["spec_pin"], "results": results}
    code = metadata.get("code_submission")
    if isinstance(code, dict):
        # AC-04：code attest 段恰三键，声明本机实测 commit（与 code_submission 钉扎一致）。
        document["code"] = {field: code.get(field) for field in ("code_repo_id", "branch", "commit_hash")}
    return document, manual_document


# ---------------------------------------------------------------------------
# conformance 本机阶段（对被测代码起的实例逐项 diff；探测凭据进程内合成）
# ---------------------------------------------------------------------------

CONFORMANCE_BOOTSTRAP = r"""
import json
import sys
from pathlib import Path

interfaces_file, task_id, repository = sys.argv[1], sys.argv[2], sys.argv[3]
interfaces = json.loads(Path(interfaces_file).read_text(encoding="utf-8"))

from aidev import authn
from aidev.conformance import check_contract, configure_probes
from aidev.web.app import create_app

probe_session = authn.create_session("conformance-probe")
sigt = authn.mint_sigt(task_id, "conformance-probe", repository)
app = create_app(repository)
configure_probes(user_token=probe_session, sigt=sigt, session_cookie=probe_session, task_id=task_id)
try:
    findings = check_contract(interfaces, app, repository)
finally:
    configure_probes()
print(json.dumps({"findings": findings}, ensure_ascii=False))
"""


def run_conformance_locally(contract, task_id, worktree, temporary):
    """冻结契约 interfaces 非空时对本机被测实例逐项 diff；findings 非空即失败。"""
    interfaces = contract.get("interfaces")
    if not interfaces:
        return
    code_root = Path(worktree) if worktree is not None else None
    if code_root is None:
        platform_repo = os.environ.get("SIGNET_PLATFORM_REPO")
        if platform_repo and (Path(platform_repo) / "aidev").is_dir():
            code_root = Path(platform_repo)
    if code_root is None:
        raise ClientInputError(
            "verify.conformance_target_missing",
            "冻结契约声明 interfaces 但无本机被测代码（无 code worktree 且未设 SIGNET_PLATFORM_REPO），"
            "conformance 无法本机探测",
        )
    repository = Path(temporary) / "conformance-repo"
    init = _git(None, ["init", "--initial-branch=main", str(repository)])
    if init.returncode != 0:
        raise ClientInputError("verify.conformance_failed", f"conformance 编排仓初始化失败: {(init.stderr or '')[-200:]}")
    interfaces_file = Path(temporary) / "conformance-interfaces.json"
    interfaces_file.write_text(json.dumps(interfaces, ensure_ascii=False), encoding="utf-8")
    home = Path(temporary) / "conformance-home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["HOMEDRIVE"] = str(home)[:2] if str(home)[1:2] == ":" else str(home)
    env["HOMEPATH"] = str(home)
    env["PYTHONPATH"] = str(code_root) + os.pathsep + (os.environ.get("PYTHONPATH") or "")
    env["PYTHONUTF8"] = "1"
    env.pop("SIGNET_STATE_REPO", None)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", CONFORMANCE_BOOTSTRAP, str(interfaces_file), task_id, str(repository)],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CONFORMANCE_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as error:
        raise ClientInputError("verify.conformance_failed", "conformance 本机探测超时") from error
    if completed.returncode != 0:
        raise ClientInputError(
            "verify.conformance_failed",
            f"conformance 本机探测进程失败: {(completed.stderr or completed.stdout or '')[-300:]}",
        )
    try:
        findings = (json.loads(completed.stdout.strip().splitlines()[-1]) or {}).get("findings") or []
    except (ValueError, IndexError) as error:
        raise ClientInputError("verify.conformance_failed", "conformance 本机探测输出不可解析") from error
    if findings:
        raise ClientInputError(
            findings[0].get("code") or "verify.conformance_failed",
            "conformance 本机探测未过（对被测代码活体面逐项 diff 有 findings）",
            details={"findings": findings},
        )


# ---------------------------------------------------------------------------
# 上传与入口
# ---------------------------------------------------------------------------

def _emit_response(status, payload):
    if isinstance(payload, dict) and payload.get("ok") is True and "result" in payload:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    if isinstance(payload, dict) and payload.get("ok") is False and isinstance(payload.get("error"), dict):
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return HTTP_EXIT_CODES.get(status, 4)
    message = json.dumps(payload, ensure_ascii=True) if payload is not None else f"HTTP {status}"
    return emit_error("request.invalid", message[:500], {}, 2 if status in (400, 409, 422) else 4)


def run_local_verify(arguments):
    """CLI verify --execute：本机执行冻结用例并上传结果/证据（服务端零执行）。"""
    try:
        return _run_local_verify(arguments)
    except ClientInputError as error:
        return emit_error(error.code, error.message, error.details, error.exit_code)


def _run_local_verify(arguments):
    task_id = arguments.task_id
    verifier = arguments.verifier
    confirmed_by = arguments.confirmed_by
    if not verifier or not confirmed_by:
        raise ClientInputError("request.invalid", "verifier and confirmed_by are required")
    base_url = resolve_base_url()
    token = resolve_token()
    if not token:
        raise ClientInputError("auth.required", "请先 login（verify --execute 需经任务包/资产/项目读面取数）", exit_code=3)
    repo = getattr(arguments, "repo", None)
    package = pull_package(base_url, task_id, token=token, repo=repo)
    metadata = package.get("metadata") or {}
    state = metadata.get("state")
    reverify = bool(getattr(arguments, "reverify", False))
    if state != "Implemented" and not (state == "Verified" and reverify):
        raise ClientInputError("verify.invalid_state", f"only an Implemented task can be verified（当前 {state}）", exit_code=3)
    with tempfile.TemporaryDirectory(prefix="signet-local-verify-") as temporary:
        spec_directory, contract, mapping, card = fetch_frozen_assets(base_url, token, task_id, package, temporary, repo=repo)
        worktree = None
        code = metadata.get("code_submission")
        if isinstance(code, dict):
            worktree = build_code_worktree(base_url, token, code, temporary)
        case_results = execute_cases_locally(spec_directory, contract, worktree)
        document, manual_document = assemble_results_document(
            metadata, card, mapping, case_results, verifier, getattr(arguments, "manual_results", None))
        run_conformance_locally(contract, task_id, worktree, temporary)
        uploads = []
        for case_id, case_result in case_results.items():
            uploads.append((case_result["evidence"]["filename"], case_result.pop("_content")))
        _collect_manual_evidence_uploads(manual_document, getattr(arguments, "manual_results", None), uploads)
        fields = {
            "verifier": verifier,
            "confirmed_by": confirmed_by,
            "execute": "false",
            "push": str(bool(getattr(arguments, "push", False))).lower(),
            "reverify": str(reverify).lower(),
        }
        if repo:
            fields["repo"] = repo
        files = [("results", "results.json", json.dumps(document, ensure_ascii=False).encode("utf-8"))]
        files.extend(("evidence", name, blob) for name, blob in uploads)
        review_report = getattr(arguments, "review_report", None)
        if review_report:
            files.append(("review_report", *read_upload(review_report, "verify.ai_review_invalid_report")))
        try:
            status, payload = perform("POST", f"{base_url}/api/tasks/{task_id}/verify",
                                      fields=fields, files=files, token=token)
        except ServiceUnavailable as error:
            return emit_unreachable(error)
    failed = [item.get("criterion_id") for item in document["results"] if item.get("passed") is not True]
    if failed:
        print(f"本机执行存在未通过判据（{', '.join(map(str, failed))}），结果已上传由服务端裁决落账", file=sys.stderr)
    return _emit_response(status, payload)


def _collect_manual_evidence_uploads(manual_document, manual_results_file, uploads):
    """人工/复跑段 filename 形态证据：从 --manual-results 同目录解析字节随件上传
    （找不到匹配字节时不上传，由服务端按 verify.evidence_missing 裁决）。"""
    if not isinstance(manual_document, dict) or not manual_results_file:
        return
    base_dir = Path(manual_results_file).resolve().parent
    known = {name for name, _blob in uploads}
    items = []
    for result in manual_document.get("results", []):
        if not isinstance(result, dict):
            continue
        for item in result.get("evidence", []):
            if isinstance(item, dict) and item.get("filename"):
                items.append(item)
        rerun = result.get("manual_rerun")
        if isinstance(rerun, dict):
            for item in rerun.get("evidence", []):
                if isinstance(item, dict) and item.get("filename"):
                    items.append(item)
    for item in items:
        name = Path(str(item["filename"])).name
        if name in known:
            continue
        candidate = base_dir / name
        if candidate.is_file():
            blob = candidate.read_bytes()
            if hashlib.sha256(blob).hexdigest() == item.get("sha256"):
                uploads.append((name, blob))
                known.add(name)
