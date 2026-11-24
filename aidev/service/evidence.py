import json
import re
import shutil
import urllib.parse
from pathlib import Path

from ..domain import task_path
from ..errors import NotFoundError, PlatformError
from ..jsonio import read_json, sha256_file
from ..runner import frozen_contract


DEFAULT_EVIDENCE_SIZE_LIMIT = 1048576
DEFAULT_EVIDENCE_TOTAL_LIMIT = 10485760


def load_evidence_limits(environment):
    """REQ-4：上限值读 contract environment 声明（evidence_size_limit_bytes /
    evidence_total_limit_bytes），缺省或非正整数时回落平台默认值。"""
    env = environment or {}
    size_limit = env.get("evidence_size_limit_bytes")
    total_limit = env.get("evidence_total_limit_bytes")
    if not isinstance(size_limit, int) or isinstance(size_limit, bool) or size_limit <= 0:
        size_limit = DEFAULT_EVIDENCE_SIZE_LIMIT
    if not isinstance(total_limit, int) or isinstance(total_limit, bool) or total_limit <= 0:
        total_limit = DEFAULT_EVIDENCE_TOTAL_LIMIT
    return size_limit, total_limit


def workspace_evidence_limits(workspace):
    """提案通道：取工作区 contract/contract.json environment 声明（与 pin 同源）。"""
    contract_file = Path(workspace) / "contract" / "contract.json"
    environment = {}
    if contract_file.is_file():
        try:
            document = read_json(contract_file)
            if isinstance(document, dict):
                environment = document.get("environment") or {}
        except (OSError, json.JSONDecodeError):
            environment = {}
    return load_evidence_limits(environment)


def authoritative_evidence_limits(repository, metadata):
    """直回流通道：取冻结规格 environment 声明；未钉扎/损坏时回落默认值。"""
    try:
        _directory, contract, _mapping = frozen_contract(repository, metadata)
        environment = contract.get("environment") or {}
    except Exception:
        environment = {}
    return load_evidence_limits(environment)


def evidence_manifest(evidence_files, limits=None):
    manifest = []
    staged = []
    for value in evidence_files:
        path = Path(value).resolve()
        if not path.is_file():
            raise PlatformError("submit.evidence_missing", f"evidence file does not exist: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise PlatformError("submit.evidence_empty", f"evidence file is empty: {path}")
        staged.append((path, size))
    if limits is not None:
        size_limit, total_limit = limits
        total = sum(size for _path, size in staged)
        if total > total_limit:
            raise PlatformError(
                "submit.evidence_total_exceeded",
                f"本次提交证据总量 {total} 字节，超出单任务证据总量上限 {total_limit} 字节，请裁剪证据内容后重新提交",
            )
        for path, size in staged:
            if size > size_limit:
                raise PlatformError(
                    "submit.evidence_too_large",
                    f"证据文件 {path.name} 大小 {size} 字节，超出单证据上限 {size_limit} 字节，请裁剪证据内容后重新提交",
                )
    for path, size in staged:
        manifest.append({"path": str(path), "sha256": sha256_file(path), "size": size})
    return manifest


def unique_evidence_name(name, taken):
    """REQ-1 重名短后缀：stem-2.ext、stem-3.ext…（taken 为已占用名集合）。"""
    if name not in taken:
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 2
    while f"{stem}-{counter}{suffix}" in taken:
        counter += 1
    return f"{stem}-{counter}{suffix}"


def materialize_submit_evidence(repository, task_id, evidence_items):
    """REQ-1：证据字节落盘 tasks/{task_id}/evidence/（原文件名；同任务重名自动加短后缀，
    多份内容各自保留、引用互不覆盖）；返回 (仓内相对路径列表, 以仓内相对路径重写的清单)。
    事件 payload 与报告引用一律改为 tasks/{task_id}/evidence/<name> + sha256 + size。"""
    evidence_directory = task_path(repository, task_id).parent / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    taken = {entry.name for entry in evidence_directory.iterdir()}
    relative_paths = []
    rewritten = []
    for item in evidence_items:
        source = Path(str(item.get("path") or ""))
        name = unique_evidence_name(source.name or "evidence", taken)
        taken.add(name)
        target = evidence_directory / name
        shutil.copyfile(source, target)
        relative = target.relative_to(Path(repository)).as_posix()
        relative_paths.append(relative)
        rewritten.append({"path": relative, "sha256": sha256_file(target), "size": target.stat().st_size})
    return relative_paths, rewritten


def materialize_submit_evidence_bytes(repository, task_id, evidence_items, bytes_by_name):
    """AC-08（api-remote-ready）：sync 上传通道的证据落盘——字节随 multipart 上传
    （按文件名匹配提案引用），服务端不按 client 路径读证据；返回 (仓内相对路径
    列表, 以仓内相对路径重写的清单)，语义与 materialize_submit_evidence 一致。"""
    evidence_directory = task_path(repository, task_id).parent / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    taken = {entry.name for entry in evidence_directory.iterdir()}
    relative_paths = []
    rewritten = []
    for item in evidence_items:
        name = Path(str(item.get("path") or "evidence")).name or "evidence"
        content = bytes_by_name[name]
        name = unique_evidence_name(name, taken)
        taken.add(name)
        target = evidence_directory / name
        target.write_bytes(content)
        relative = target.relative_to(Path(repository)).as_posix()
        relative_paths.append(relative)
        rewritten.append({"path": relative, "sha256": sha256_file(target), "size": target.stat().st_size})
    return relative_paths, rewritten


def evidence_basenames(evidence_items):
    return [Path(str(item.get("path") or "evidence")).name or "evidence" for item in evidence_items]


def resolve_task_evidence(repository, task_id, name):
    """REQ-2：按名定位本任务 evidence 目录内文件；穿越防护与资产端点同构
    （绝对路径、..、目录逃逸一律拒绝；不存在 404）。只解析与读取，不创建目录。"""
    decoded = urllib.parse.unquote(str(name or ""))
    relative = Path(decoded)
    if not decoded.strip() or decoded.startswith(("\\", "/")) or relative.is_absolute() \
            or ".." in relative.parts or re.match(r"^[A-Za-z]:", decoded):
        raise PlatformError("task.evidence_invalid", f"invalid evidence name: {name}")
    evidence_directory = task_path(repository, task_id).parent / "evidence"
    root = evidence_directory.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise PlatformError("task.evidence_invalid", f"evidence escapes the task evidence directory: {name}")
    if not path.is_file():
        raise NotFoundError("task.evidence_not_found", f"evidence does not exist: {name}")
    return path
