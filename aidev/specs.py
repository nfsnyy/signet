import json
import os
import shutil
import stat
import time
from pathlib import Path

from .domain import REFREEZABLE_STATES, SPEC_FILES, event_path, make_event, resolve_workspace_directory, task_path, workspace_agents
from .errors import OperationalError, PlatformError
from .gitrepo import commit_paths, ensure_repository
from .jsonio import append_jsonl, sha256_file, utc_now, write_json
from .runner import execute_contract_cases
from .service import propagate_pin_invalidation, stamp_events
from .taskfile import read_task, write_task
from .vetoes import approved_veto_for, classify_refreeze, latest_vetoes, veto_log_path

VALID_STRENGTHS = {"must", "must-not", "safety-veto"}
VALID_CONTRACT_TYPES = {"baseline", "target"}


def repository_lock(repository, name):
    lock_path = Path(repository) / f".aidev-{name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise OperationalError(f"{name}.already_running", f"another {name} operation holds the repository lock") from error
    os.close(descriptor)
    return lock_path


def read_json_document(path, code):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as error:
        raise PlatformError(code, f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise PlatformError(code, f"JSON document must be an object: {path}")
    return value


def required_list(document, key, code):
    value = document.get(key)
    if not isinstance(value, list):
        raise PlatformError(code, f"{key} must be a list")
    return value


def required_string(document, key, code):
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlatformError(code, f"{key} must be a non-empty string")
    return value


def check_identifiers(items, key, code):
    identifiers = []
    for item in items:
        if not isinstance(item, dict):
            raise PlatformError(code, "each item must be an object")
        identifiers.append(required_string(item, key, code))
    if len(identifiers) != len(set(identifiers)):
        raise PlatformError(code, f"duplicate {key}")
    return set(identifiers)


def mapping_lookup(rows, criterion_id):
    for row in rows:
        if row.get("criterion_id") == criterion_id:
            return row
    return None


def validate_mapping(card, contract, mapping):
    criteria = required_list(card, "criteria", "c1.invalid_card")
    cases = required_list(contract, "cases", "c1.invalid_contract")
    rows = required_list(mapping, "rows", "c1.invalid_mapping")
    criterion_ids = check_identifiers(criteria, "criterion_id", "c1.invalid_card")
    case_ids = check_identifiers(cases, "case_id", "c1.invalid_contract")
    mapped_criteria = set()
    mapped_cases = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PlatformError("c1.invalid_mapping", "each mapping row must be an object")
        criterion_id = required_string(row, "criterion_id", "c1.invalid_mapping")
        manual = row.get("manual")
        row_cases = row.get("case_ids", [])
        if criterion_id not in criterion_ids:
            raise PlatformError("c1.unknown_criterion", f"mapping references unknown criterion: {criterion_id}")
        if criterion_id in mapped_criteria:
            raise PlatformError("c1.duplicate_criterion", f"criterion is mapped more than once: {criterion_id}")
        if not isinstance(manual, bool):
            raise PlatformError("c1.invalid_mapping", "manual must be a boolean")
        if not isinstance(row_cases, list) or any(not isinstance(case_id, str) for case_id in row_cases):
            raise PlatformError("c1.invalid_mapping", "case_ids must be a list of strings")
        if manual and row_cases:
            raise PlatformError("c1.manual_with_cases", f"manual criterion has cases: {criterion_id}")
        if not manual and not row_cases:
            raise PlatformError("c1.uncovered_criterion", f"criterion has no case and is not manual: {criterion_id}")
        for case_id in row_cases:
            if case_id not in case_ids:
                raise PlatformError("c1.unknown_case", f"mapping references unknown case: {case_id}")
            if case_id in mapped_cases:
                raise PlatformError("c1.duplicate_case", f"case is mapped more than once: {case_id}")
            mapped_cases.add(case_id)
        mapped_criteria.add(criterion_id)
    uncovered = criterion_ids - mapped_criteria
    if uncovered:
        raise PlatformError("c1.uncovered_criterion", f"criteria without mapping: {sorted(uncovered)}")
    orphan = case_ids - mapped_cases
    if orphan:
        raise PlatformError("c1.orphan_case", f"cases without criterion: {sorted(orphan)}")
    criterion_by_id = {item["criterion_id"]: item for item in criteria}
    for criterion_id in criterion_ids:
        criterion = criterion_by_id[criterion_id]
        if criterion.get("strength") not in VALID_STRENGTHS:
            raise PlatformError("c1.invalid_strength", f"invalid strength: {criterion_id}")
        if criterion["strength"] == "safety-veto" and mapping_lookup(rows, criterion_id)["manual"]:
            raise PlatformError("c1.safety_veto_manual", f"safety-veto criterion must be machine verified: {criterion_id}")


def operation_set(document):
    if "operation_set" not in document:
        raise PlatformError("c2.operation_set_missing", "contract must declare operation_set, or null when operations do not apply")
    value = document["operation_set"]
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PlatformError("c2.invalid_operation_set", "operation_set must be null or a list of non-empty strings")
    return set(value)


def validate_operations(contract, operation_source, source_path):
    declared = operation_set(contract)
    cases = required_list(contract, "cases", "c2.invalid_contract")
    actual = set()
    for case in cases:
        actual.add(case.get("operation"))
    if declared is None:
        if operation_source is not None:
            raise PlatformError("c2.unexpected_operation_source", "operation source was provided for a contract without operations")
        if actual != {None}:
            raise PlatformError("c2.unexpected_case_operation", "cases cannot declare operations when operation_set is null")
        return
    if operation_source is None:
        raise PlatformError("c2.operation_source_missing", "operation source is required when operation_set is not null")
    source_path = Path(operation_source).resolve()
    if not source_path.is_file():
        raise PlatformError("c2.operation_source_missing", f"operation source does not exist: {source_path}")
    required_operations = read_json_document(source_path, "c2.invalid_operation_source").get("operations")
    if not isinstance(required_operations, list) or any(not isinstance(item, str) for item in required_operations):
        raise PlatformError("c2.invalid_operation_source", "operation source must contain an operations string list")
    required = set(required_operations)
    if declared != actual:
        raise PlatformError("c2.operation_set_mismatch", "declared operation set differs from case coverage")
    if declared != required:
        raise PlatformError("c2.requirement_operation_mismatch", "contract operation set differs from requirement source", details={"source": str(source_path)})


def safe_relative_path(value, code):
    if not isinstance(value, str) or not value.strip():
        raise PlatformError(code, "asset path must be a non-empty relative string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PlatformError(code, f"asset path must stay inside the spec directory: {value}")
    return path.as_posix()


def contract_asset_paths(contract):
    environment = contract.get("environment", {})
    assets = list(environment.get("dependency_files", []))
    for case in contract.get("cases", []):
        assets.extend(case.get("fixtures", []))
    return list(dict.fromkeys(assets))


def validate_environment_and_cases(contract, contract_file):
    environment = contract.get("environment")
    if not isinstance(environment, dict):
        raise PlatformError("c3.invalid_environment", "contract.environment must be an object")
    required_string(environment, "runner", "c3.invalid_environment")
    dependency_files = environment.get("dependency_files", [])
    endpoints = environment.get("endpoints", {})
    allowed_env = environment.get("allowed_env", [])
    setup_command = environment.get("setup_command")
    if not isinstance(dependency_files, list):
        raise PlatformError("c3.invalid_environment", "dependency_files must be a list")
    if not isinstance(endpoints, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in endpoints.items()):
        raise PlatformError("c3.invalid_environment", "endpoints must map names to non-empty addresses")
    if not isinstance(allowed_env, list) or any(not isinstance(item, str) for item in allowed_env):
        raise PlatformError("c3.invalid_environment", "allowed_env must be a list of strings")
    if setup_command is not None and (not isinstance(setup_command, list) or not setup_command or any(not isinstance(part, str) or not part for part in setup_command)):
        raise PlatformError("c3.invalid_environment", "setup_command must be null or a non-empty string list")
    asset_hashes = contract.get("assets", {})
    if not isinstance(asset_hashes, dict) or any(not isinstance(key, str) or not isinstance(value, str) or len(value) != 64 for key, value in asset_hashes.items()):
        raise PlatformError("c3.invalid_assets", "assets must map relative paths to SHA-256 strings")
    contract_directory = Path(contract_file).resolve().parent
    for dependency in dependency_files:
        safe_relative_path(dependency, "c3.invalid_dependency")
        if not (contract_directory / dependency).is_file():
            raise PlatformError("c3.missing_dependency", f"dependency file does not exist: {dependency}")
    for case in required_list(contract, "cases", "c3.invalid_contract"):
        command = case.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise PlatformError("c3.invalid_case", f"case command must be a non-empty string list: {case.get('case_id')}")
        expectation = case.get("expect")
        if not isinstance(expectation, dict) or not isinstance(expectation.get("exit_code"), int):
            raise PlatformError("c3.invalid_expectation", f"case expect.exit_code must be an integer: {case.get('case_id')}")
        if "stdout_contains" in expectation and not isinstance(expectation["stdout_contains"], str):
            raise PlatformError("c3.invalid_expectation", f"stdout_contains must be a string: {case.get('case_id')}")
        for fixture in case.get("fixtures", []):
            safe_relative_path(fixture, "c3.invalid_fixture")
            # 编排式 freeze 不上传资产文件：缺失时跳过落盘校验（随上传提供的仍校验哈希）。
            if not (contract_directory / fixture).is_file():
                continue
    for asset in contract_asset_paths(contract):
        expected = asset_hashes.get(asset)
        if not expected:
            raise PlatformError("c3.asset_hash_missing", f"asset is missing a frozen SHA-256: {asset}")
        asset_path = contract_directory / asset
        if asset_path.is_file() and sha256_file(asset_path) != expected:
            raise PlatformError("c3.asset_hash_mismatch", f"asset hash does not match contract: {asset}")


def copy_contract_assets(contract, contract_file, spec_directory, copied, repository=None):
    source_directory = Path(contract_file).resolve().parent
    for asset in contract_asset_paths(contract):
        source = source_directory / asset
        if not source.is_file():
            # 编排式 freeze 未上传资产文件：先在状态仓已冻结规格里按声明哈希
            # 内容寻址找回（同哈希=同内容），找不到才跳过（不进入冻结规格）。
            recovered = recover_frozen_asset(repository, contract, asset) if repository else None
            if recovered is None:
                continue
            source = recovered
        target = (Path(spec_directory) / asset).resolve()
        if Path(spec_directory).resolve() not in target.parents and target != Path(spec_directory).resolve():
            raise PlatformError("c3.invalid_asset_path", f"asset escapes the spec directory: {asset}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(spec_directory.parents[2]))


def recover_frozen_asset(repository, contract, asset):
    """按 contract.assets 声明的 sha256，在状态仓 specs/ 已冻结文件中找回编排未上传的资产。"""
    expected = (contract.get("assets") or {}).get(asset)
    if not isinstance(expected, str) or len(expected) != 64 or repository is None:
        return None
    specs_root = Path(repository) / "specs"
    if not specs_root.is_dir():
        return None
    for candidate in sorted(specs_root.rglob(Path(asset).name)):
        if not candidate.is_file() or candidate.name in SPEC_FILES.values():
            continue
        try:
            if sha256_file(candidate) == expected:
                return candidate
        except OSError:
            continue
    return None


def validate_three_piece(card, contract, mapping, operation_source, contract_file):
    validate_mapping(card, contract, mapping)
    validate_operations(contract, operation_source, operation_source)
    validate_environment_and_cases(contract, contract_file)
    contract_type = contract.get("contract_type")
    if contract_type not in VALID_CONTRACT_TYPES:
        raise PlatformError("c4.invalid_contract_type", "contract_type must be baseline or target")


def write_baseline_evidence(spec_directory, case_results):
    evidence_directory = Path(spec_directory) / "baseline-evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    records = []
    paths = []
    for case_id, result in case_results.items():
        evidence = result["evidence"]
        target = evidence_directory / evidence["filename"]
        target.write_bytes(evidence["content"])
        relative = target.relative_to(spec_directory.parents[2]).as_posix()
        paths.append(relative)
        records.append({
            "case_id": case_id,
            "passed": result["passed"],
            "exit_code": result["exit_code"],
            "evidence": {"path": relative, "sha256": evidence["sha256"]},
        })
    return records, paths


def remove_uncommitted_spec(spec_directory):
    path = Path(spec_directory)
    if not path.exists():
        return

    def make_writable(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        function(target)

    last_error = None
    for _attempt in range(5):
        try:
            shutil.rmtree(path, onerror=make_writable)
            break
        except PermissionError as error:
            last_error = error
            time.sleep(0.1)
    else:
        raise OperationalError("freeze.cleanup_failed", "failed to clean uncommitted frozen spec", details={"path": str(path), "error": str(last_error)})
    parent = path.parent
    while parent.name != "specs" and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def execute_baseline_cases(repository, spec_directory, metadata, contract, pin):
    provisional_metadata = dict(metadata)
    provisional_metadata["spec_id"] = pin["spec_id"]
    provisional_metadata["spec_pin"] = pin
    try:
        case_results = execute_contract_cases(repository, provisional_metadata)
        records, evidence_paths = write_baseline_evidence(spec_directory, case_results)
    except BaseException:
        remove_uncommitted_spec(spec_directory)
        raise
    failed = [record["case_id"] for record in records if not record["passed"]]
    if failed:
        remove_uncommitted_spec(spec_directory)
        raise PlatformError("c5.case_failed", "baseline contract has failing cases", details={"cases": failed})
    return records, evidence_paths


def freeze_task(repository, task_id, workspace, card_file, contract_file, mapping_file, operation_source, h1_actor, h2_actor, push=False):
    repository = ensure_repository(repository)
    workspace = resolve_workspace_directory(workspace) if workspace else None
    lock = repository_lock(repository, "freeze")
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        if metadata.get("state") != "Draft":
            raise PlatformError("freeze.invalid_state", "only a Draft task can be frozen", details={"state": metadata.get("state")})
        spec_id = metadata["spec_id"]
        version = "1"
        spec_directory = repository / "specs" / spec_id / version
        if spec_directory.exists():
            raise PlatformError("freeze.spec_exists", "frozen spec version already exists; refreeze is not implemented")
        card = read_json_document(card_file, "c1.invalid_card")
        contract = read_json_document(contract_file, "c2.invalid_contract")
        mapping = read_json_document(mapping_file, "c1.invalid_mapping")
        validate_three_piece(card, contract, mapping, operation_source, contract_file)
        spec_directory.mkdir(parents=True)
        sources = {
            "acceptance_card": Path(card_file),
            "contract": Path(contract_file),
            "mapping": Path(mapping_file),
        }
        pin = {"spec_id": spec_id}
        copied = []
        for key, filename in SPEC_FILES.items():
            target = spec_directory / filename
            shutil.copyfile(sources[key], target)
            copied.append(target.relative_to(repository))
            pin[key] = {"version": version, "sha256": sha256_file(target)}
        copy_contract_assets(contract, contract_file, spec_directory, copied, repository)
        baseline_records = []
        baseline_paths = []
        if contract.get("contract_type") == "baseline":
            baseline_records, baseline_paths = execute_baseline_cases(repository, spec_directory, metadata, contract, pin)
            copied.extend(baseline_paths)
        now = utc_now()
        events = [
            make_event("specification.content_confirmed", task_id, h1_actor, h1_actor, "Draft", "Draft", payload={"spec_id": spec_id, "version": version, "sha256": pin["acceptance_card"]["sha256"]}),
            make_event("specification.consistency_confirmed", task_id, h2_actor, h2_actor, "Draft", "Draft", payload={"spec_id": spec_id, "version": version, "contract_sha256": pin["contract"]["sha256"], "mapping_sha256": pin["mapping"]["sha256"]}),
        ]
        if baseline_records:
            events.append(make_event("specification.baseline_verified", task_id, h2_actor, h2_actor, "Draft", "Draft", payload={"spec_id": spec_id, "version": version, "cases": baseline_records}))
        metadata["spec_pin"] = pin
        metadata["updated_at"] = now
        metadata["state"] = "Ready"
        events.extend([
            make_event("specification.frozen", task_id, h2_actor, h2_actor, "Draft", "Ready", payload={"spec_id": spec_id, "version": version, "spec_pin": pin}),
            make_event("task.ready", task_id, h2_actor, h2_actor, "Draft", "Ready", payload={"spec_id": spec_id, "version": version}),
        ])
        write_task(path, metadata, body)
        events_file = event_path(repository)
        append_jsonl(events_file, stamp_events(repository, events))
        changed = copied + [path.relative_to(repository), events_file.relative_to(repository)]
        commit = commit_paths(repository, changed, f"Freeze {task_id}", push=push)
        if workspace is not None:
            write_task(workspace / "task.md", metadata, body)
            (workspace / "AGENTS.md").write_text(workspace_agents(metadata), encoding="utf-8", newline="\n")
            (workspace / "contract").mkdir(parents=True, exist_ok=True)
            for key, filename in SPEC_FILES.items():
                shutil.copyfile(spec_directory / filename, workspace / "contract" / filename)
        return {"task_id": task_id, "spec_id": spec_id, "version": version, "spec_pin": pin, "state": "Ready", "commit": commit}
    finally:
        lock.unlink(missing_ok=True)


def refreeze_task(repository, task_id, workspace, card_file, contract_file, mapping_file, operation_source, h1_actor, h2_actor, reason, push=False):
    repository = ensure_repository(repository)
    workspace = resolve_workspace_directory(workspace) if workspace else None
    if not reason or not reason.strip():
        raise PlatformError("refreeze.reason_required", "refreeze reason is required")
    lock = repository_lock(repository, "freeze")
    try:
        path = task_path(repository, task_id)
        if not path.is_file():
            raise PlatformError("task.not_found", f"task does not exist: {task_id}")
        metadata, body = read_task(path)
        pin = metadata.get("spec_pin")
        before = metadata.get("state")
        if not pin:
            # refreeze v2 补卡：Draft 未冻结任务允许 refreeze——以虚拟 v1 基线
            # 直接冻结为 v2（supersedes="1"），事件与元数据口径与常规 refreeze 一致。
            if before != "Draft":
                raise PlatformError("refreeze.no_spec", "task has no frozen specification; use freeze instead", details={"state": before})
            old_version = "1"
            pin = {
                "spec_id": metadata["spec_id"],
                "acceptance_card": {"version": "1", "sha256": None},
                "contract": {"version": "1", "sha256": None},
                "mapping": {"version": "1", "sha256": None},
            }
            initial_freeze = True
        else:
            if before not in REFREEZABLE_STATES:
                raise PlatformError("refreeze.invalid_state", "task state cannot be refrozen", details={"state": before})
            old_version = pin["acceptance_card"]["version"]
            initial_freeze = False
        spec_id = metadata["spec_id"]
        version = str(int(old_version) + 1)
        spec_directory = repository / "specs" / spec_id / version
        if spec_directory.exists():
            raise PlatformError("refreeze.spec_exists", f"frozen spec version already exists: {spec_id}@{version}")
        card = read_json_document(card_file, "c1.invalid_card")
        contract = read_json_document(contract_file, "c2.invalid_contract")
        mapping = read_json_document(mapping_file, "c1.invalid_mapping")
        validate_three_piece(card, contract, mapping, operation_source, contract_file)
        analysis = {"downgrade_required": False} if initial_freeze else classify_refreeze(repository, spec_id, old_version, card, contract, mapping)
        veto = None
        if analysis["downgrade_required"]:
            file_hashes = {
                "acceptance_card": sha256_file(Path(card_file)),
                "contract": sha256_file(Path(contract_file)),
                "mapping": sha256_file(Path(mapping_file)),
            }
            veto = approved_veto_for(latest_vetoes(repository), spec_id, file_hashes)
            if veto is None:
                raise PlatformError("refreeze.veto_required", "refreeze touches safety-veto surfaces and requires an approved veto first", details=analysis)
        spec_directory.mkdir(parents=True)
        sources = {
            "acceptance_card": Path(card_file),
            "contract": Path(contract_file),
            "mapping": Path(mapping_file),
        }
        new_pin = {"spec_id": spec_id}
        copied = []
        for key, filename in SPEC_FILES.items():
            target = spec_directory / filename
            shutil.copyfile(sources[key], target)
            copied.append(target.relative_to(repository))
            new_pin[key] = {"version": version, "sha256": sha256_file(target)}
        copy_contract_assets(contract, contract_file, spec_directory, copied, repository)
        baseline_records = []
        baseline_paths = []
        if contract.get("contract_type") == "baseline":
            baseline_records, baseline_paths = execute_baseline_cases(repository, spec_directory, metadata, contract, new_pin)
            copied.extend(baseline_paths)
        now = utc_now()
        version_document = {
            "spec_id": spec_id,
            "version": version,
            "supersedes": old_version,
            "reason": reason,
            "refrozen_at": now,
            "h1_actor": h1_actor,
            "h2_actor": h2_actor,
            "previous_contract_sha256": pin["contract"]["sha256"],
        }
        version_path = spec_directory / "version.json"
        write_json(version_path, version_document)
        copied.append(version_path.relative_to(repository))
        events = [
            make_event("specification.content_confirmed", task_id, h1_actor, h1_actor, before, before, payload={"spec_id": spec_id, "version": version, "supersedes": old_version, "sha256": new_pin["acceptance_card"]["sha256"]}),
            make_event("specification.consistency_confirmed", task_id, h2_actor, h2_actor, before, before, payload={"spec_id": spec_id, "version": version, "supersedes": old_version, "contract_sha256": new_pin["contract"]["sha256"], "mapping_sha256": new_pin["mapping"]["sha256"]}),
        ]
        if baseline_records:
            events.append(make_event("specification.baseline_verified", task_id, h2_actor, h2_actor, before, before, payload={"spec_id": spec_id, "version": version, "cases": baseline_records}))
        released_lease = metadata.get("lease")
        metadata["spec_pin"] = new_pin
        metadata["lease"] = None
        metadata["void_reason"] = None
        metadata["updated_at"] = now
        metadata["state"] = "Ready"
        events.append(make_event("specification.refrozen", task_id, h2_actor, h2_actor, before, "Ready", payload={"spec_id": spec_id, "old_version": old_version, "version": version, "supersedes": old_version, "reason": reason, "released_lease": released_lease, "spec_pin": new_pin}))
        if before != "Ready":
            events.append(make_event("task.ready", task_id, h2_actor, h2_actor, before, "Ready", payload={"spec_id": spec_id, "version": version, "refrozen": True}))
        extra_paths = []
        if veto is not None:
            other_changed = {}
            propagate_pin_invalidation(repository, pin, h2_actor, "veto-downgrade", task_id, other_changed, events, extra_paths, supersedes=version, veto_id=veto["veto_id"])
            for other_id, (other_metadata, other_body) in other_changed.items():
                other_path = task_path(repository, other_id)
                write_task(other_path, other_metadata, other_body)
                copied.append(other_path.relative_to(repository))
            applied = dict(veto)
            applied["resolution"] = {"applied_at": now, "old_version": old_version, "new_version": version, "applied_by": h2_actor}
            veto_log = veto_log_path(repository)
            append_jsonl(veto_log, [applied])
            extra_paths.append(veto_log.relative_to(repository))
            events.append(make_event("veto.applied", task_id, h2_actor, h2_actor, before, "Ready", payload={"veto_id": veto["veto_id"], "old_version": old_version, "version": version}))
        write_task(path, metadata, body)
        events_file = event_path(repository)
        append_jsonl(events_file, stamp_events(repository, events))
        changed = copied + [path.relative_to(repository), events_file.relative_to(repository)] + extra_paths
        commit = commit_paths(repository, changed, f"Refreeze {task_id}", push=push)
        if workspace is not None:
            write_task(workspace / "task.md", metadata, body)
            (workspace / "AGENTS.md").write_text(workspace_agents(metadata), encoding="utf-8", newline="\n")
            (workspace / "contract").mkdir(parents=True, exist_ok=True)
            for key, filename in SPEC_FILES.items():
                shutil.copyfile(spec_directory / filename, workspace / "contract" / filename)
        return {"task_id": task_id, "spec_id": spec_id, "old_version": old_version, "version": version, "supersedes": old_version, "spec_pin": new_pin, "state": "Ready", "commit": commit}
    finally:
        lock.unlink(missing_ok=True)
