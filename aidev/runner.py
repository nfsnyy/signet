import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .domain import spec_path
from .errors import PlatformError
from .jsonio import read_json, sha256_file

COMMAND_TIMEOUT_SECONDS = 60
CASE_TIMEOUT_ENV = "SIGNET_CASE_TIMEOUT"


def default_case_timeout():
    """REQ-2：每案缺省上限取 runner 所在进程环境的 SIGNET_CASE_TIMEOUT，缺省或非法值回落 60s。"""
    raw = os.environ.get(CASE_TIMEOUT_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return COMMAND_TIMEOUT_SECONDS


def case_timeout_seconds(case):
    """REQ-2：cases[].timeout_seconds（正整数）单案覆盖优先，未声明回落 env/缺省 60s。"""
    declared = case.get("timeout_seconds")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    return default_case_timeout()


def frozen_contract(repository, metadata):
    contract_file = spec_path(repository, metadata, "contract")
    mapping_file = spec_path(repository, metadata, "mapping")
    return contract_file.parent, read_json(contract_file), read_json(mapping_file)


def validate_frozen_assets(spec_directory, contract):
    from .specs import contract_asset_paths, safe_relative_path
    asset_hashes = contract.get("assets", {})
    for asset in contract_asset_paths(contract):
        relative = safe_relative_path(asset, "verify.invalid_asset")
        expected = asset_hashes.get(relative)
        path = Path(spec_directory) / relative
        if not expected or not path.is_file():
            raise PlatformError("verify.missing_frozen_asset", f"frozen asset is missing: {relative}")
        if sha256_file(path) != expected:
            raise PlatformError("verify.asset_hash_mismatch", f"frozen asset hash changed: {relative}")


def clean_environment(environment):
    return {name: os.environ[name] for name in environment.get("allowed_env", []) if name in os.environ}


def resolve_command(command, runner):
    resolved = list(command)
    executable = resolved[0]
    if executable == runner:
        located = shutil.which(executable)
        if not located:
            raise PlatformError("verify.runner_not_found", f"runner is not available: {executable}")
        resolved[0] = located
    elif not Path(executable).exists() and shutil.which(executable) is None:
        raise PlatformError("verify.command_not_found", f"command is not available before clean environment setup: {executable}")
    return resolved


def run_clean_command(command, cwd, environment, timeout_seconds=None):
    timeout = timeout_seconds if timeout_seconds is not None else default_case_timeout()
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def copy_frozen_assets(spec_directory, temporary_directory, contract):
    from .specs import contract_asset_paths, safe_relative_path
    for asset in contract_asset_paths(contract):
        relative = safe_relative_path(asset, "verify.invalid_asset")
        source = Path(spec_directory) / relative
        target = Path(temporary_directory) / relative
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


def execute_contract_cases(repository, metadata):
    spec_directory, contract, _ = frozen_contract(repository, metadata)
    validate_frozen_assets(spec_directory, contract)
    environment_spec = contract.get("environment", {})
    runner = environment_spec["runner"]
    environment = clean_environment(environment_spec)
    case_results = {}
    with tempfile.TemporaryDirectory(prefix="aidev-verify-") as temporary:
        copy_frozen_assets(spec_directory, temporary, contract)
        if environment_spec.get("setup_command"):
            setup = resolve_command(environment_spec["setup_command"], runner)
            returncode, stdout, stderr = run_clean_command(setup, temporary, environment)
            if returncode != 0:
                raise PlatformError("verify.setup_failed", "contract setup command failed", details={"command": setup, "exit_code": returncode, "stderr": stderr})
        for case in contract.get("cases", []):
            case_id = case["case_id"]
            command = resolve_command(case["command"], runner)
            timeout_seconds = case_timeout_seconds(case)
            try:
                returncode, stdout, stderr = run_clean_command(command, temporary, environment, timeout_seconds=timeout_seconds)
            except subprocess.TimeoutExpired:
                returncode, stdout, stderr = -1, "", f"command timed out after {timeout_seconds} seconds"
            expectation = case["expect"]
            passed = returncode == expectation["exit_code"]
            if "stdout_contains" in expectation:
                passed = passed and expectation["stdout_contains"] in stdout
            content = case_log(case_id, command, returncode, stdout, stderr, passed)
            case_results[case_id] = {
                "passed": passed,
                "evidence": {
                    "filename": f"{case_id}.log",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content": content,
                },
                "exit_code": returncode,
            }
    return case_results


def machine_criterion_results(case_results, mapping):
    results = []
    for row in mapping.get("rows", []):
        if row.get("manual") is True:
            continue
        evidence = []
        passed = True
        for case_id in row.get("case_ids", []):
            case_result = case_results[case_id]
            passed = passed and case_result["passed"]
            evidence.append(case_result["evidence"])
        results.append({"criterion_id": row["criterion_id"], "passed": passed, "evidence": evidence})
    return results
