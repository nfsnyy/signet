"""T21 core-modularization：项目注册表与生命周期域（自 aidev/projects.py 整函数搬入，函数体零变更）。

职责：注册表/凭据存储读写、内置项目自检、repo 形态校验与解析定位（含克隆定位标记
与任务仓检索）。公开导入面为 aidev.projects（纯重导出）。
"""

import json
import re
import threading
from pathlib import Path

from .. import authn
from ..errors import NotFoundError, PlatformError
from ..gitrepo import run_git
from ..jsonio import atomic_write_bytes, read_json, utc_now

REGISTRY_FILE = "projects.json"

CREDENTIALS_FILE = "git-credentials.json"

CLONES_DIR_NAME = "clones"

PROJECT_MARKER = ".signet-project"

PROJECT_RECORD_KEYS = ("project_id", "name", "state_repo", "code_repo", "status", "owner", "created_at", "builtin")

URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$")

SCP_LIKE_RE = re.compile(r"^[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+:\S+$")

EMBEDDED_CREDENTIALS_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s@]+@[^/\s]+")

PATH_FORM_DRIVE_RE = re.compile(r"^[A-Za-z]:")

PATH_FORM_SEPARATOR_RE = re.compile(r"[/\\]")

GIT_IDENTITY = ("signet-platform", "signet-platform@local.invalid")

_registry_lock = threading.Lock()

def data_dir():
    return authn.data_dir()

def registry_path():
    return data_dir() / REGISTRY_FILE

def credentials_path():
    return data_dir() / CREDENTIALS_FILE

def clones_root():
    return data_dir() / CLONES_DIR_NAME

def load_registry():
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}

def save_registry(registry):
    atomic_write_bytes(registry_path(), (json.dumps(registry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))

def load_credentials():
    path = credentials_path()
    if not path.is_file():
        return {}
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}

def save_credentials(document):
    atomic_write_bytes(credentials_path(), (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))

def get_project(project_id):
    record = load_registry().get(project_id)
    return record if isinstance(record, dict) else None

def list_projects():
    return sorted(load_registry().values(), key=lambda item: (str(item.get("created_at") or ""), str(item.get("project_id") or "")))

def put_project(record):
    with _registry_lock:
        registry = load_registry()
        registry[record["project_id"]] = record
        save_registry(registry)

def update_project(project_id, **fields):
    with _registry_lock:
        registry = load_registry()
        record = registry.get(project_id)
        if record is None:
            raise NotFoundError("project.not_found", f"project does not exist: {project_id}")
        record.update(fields)
        save_registry(registry)
        return record

def _read_project_json(repository):
    path = Path(repository) / "project.json"
    if not path.is_file():
        return None
    try:
        document = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None

def _origin_url(repository):
    result = run_git(repository, ["remote", "get-url", "origin"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None

# ---------------------------------------------------------------------------
# 内置项目（启动自检）
# ---------------------------------------------------------------------------

def builtin_record(bound_repository):
    """绑定仓登记为内置项目（project.json 的 project_id；缺文件返回 None）。

    T24/REQ-7（AC-05）：code_repo 取状态仓 project.json 的 code_repo 字段（URL
    形态远端，服务端 ship 推送目标；带外运维写入，缺省为空=无代码闭环）；
    SIGNET_PLATFORM_REPO 本机路径不再充当推送目标（仅服务 distribution/runner
    定位被测代码）。"""
    document = _read_project_json(bound_repository)
    if not document or not isinstance(document.get("project_id"), str) or not document["project_id"].strip():
        return None
    origin = _origin_url(bound_repository)
    code_repo = document.get("code_repo")
    return {
        "project_id": document["project_id"],
        "name": document.get("name") or Path(bound_repository).resolve().name,
        "state_repo": origin or str(Path(bound_repository).resolve()),
        "code_repo": code_repo.strip() if isinstance(code_repo, str) else "",
        "status": "active",
        "owner": document.get("owner") or "",
        "created_at": document.get("created_at") or utc_now(),
        "builtin": True,
    }

def ensure_builtin(bound_repository):
    """启动自检：注册表生成/核对内置项目记录（幂等；恰一个 builtin）。"""
    record = builtin_record(bound_repository)
    if record is None:
        return None
    with _registry_lock:
        registry = load_registry()
        existing = registry.get(record["project_id"])
        if existing is not None:
            merged = dict(existing)
            merged.update({key: record[key] for key in ("state_repo", "code_repo", "status", "builtin")})
            merged["name"] = record["name"]
            registry[record["project_id"]] = merged
        else:
            registry[record["project_id"]] = record
        for project_id, other in registry.items():
            if project_id != record["project_id"] and other.get("builtin") is True:
                other["builtin"] = False
        save_registry(registry)
        return registry[record["project_id"]]

# ---------------------------------------------------------------------------
# repo URL 校验（注册/配置同口径；顺序钉名见兼容面 docstring）
# ---------------------------------------------------------------------------

def normalize_path_value(value):
    """AC-02 规范化：斜杠统一为反斜杠、去尾斜杠、小写（路径派生比对用）。"""
    return str(value).replace("/", "\\").rstrip("\\").lower()

def is_path_form_repo(value):
    """T16 同构的本地文件系统路径形态判定。"""
    if not value:
        return False
    if PATH_FORM_DRIVE_RE.match(value):
        return True
    if value.startswith(("/", "\\")) or value in (".", ".."):
        return True
    return bool(PATH_FORM_SEPARATOR_RE.search(value))

def is_url_form(value):
    return bool(URL_SCHEME_RE.match(value)) or bool(SCP_LIKE_RE.match(value))

def url_scheme(value):
    match = re.match(r"^([a-z][a-z0-9+.-]*)://", value)
    return match.group(1) if match else None

def is_platform_derived(value, platform_repo, bound_repository, builtin_project_id):
    """AC-02 回退禁四形态：PLATFORM env 值/路径派生（含子路径与尾随斜杠变体）、
    平台自身 project_id、绑定仓文件系统路径（规范化后相等）。"""
    candidate = normalize_path_value(value)
    if platform_repo:
        platform_norm = normalize_path_value(platform_repo)
        if candidate == platform_norm or (candidate.startswith(platform_norm + "\\")):
            return True
    if builtin_project_id and value.strip() == builtin_project_id:
        return True
    if bound_repository:
        bound_norm = normalize_path_value(Path(bound_repository).resolve())
        if candidate == bound_norm:
            return True
    return False

def validate_repo_url(value, kind, platform_repo, bound_repository, builtin_project_id):
    """单仓 URL 静态校验（钉名错误码）：回退禁 → 形态（URL/scp-like 形态先行，
    无方案值再按路径形态/非法形态分流）→ 嵌凭据。"""
    label = "state_repo" if kind == "state" else "code_repo"
    if is_platform_derived(value, platform_repo, bound_repository, builtin_project_id):
        raise PlatformError(
            "project.platform_repo_forbidden",
            f"{label} 不得绑定平台自身仓、平台自身项目或绑定存储（回退禁止）",
            details={label: value},
        )
    if not is_url_form(value):
        if is_path_form_repo(value):
            raise PlatformError(
                "project.repo_path_forbidden",
                f"{label} 仅接受远端仓 URL 形态（^[a-z][a-z0-9+.-]*:// 或 scp-like git@host:path），本地文件系统路径一律拒绝",
                details={label: value},
            )
        raise PlatformError(
            f"project.{kind}_repo_invalid",
            f"{label} 形态非法：非 URL 非 scp-like",
            details={label: value},
        )
    if EMBEDDED_CREDENTIALS_RE.match(value):
        raise PlatformError(
            "project.repo_url_with_credentials",
            f"{label} 不得嵌入 user:pass@ 形态凭据，请经 credentials 单独供给",
            details={label: value},
        )
    return value

def clone_dir(project_id):
    return clones_root() / project_id

def read_project_marker(repository):
    """仓库根 ``.signet-project`` 标记 → project_id（项目克隆识别，非项目仓 None）。"""
    try:
        text = (Path(repository) / PROJECT_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None

def write_project_marker(repository, project_id):
    (Path(repository) / PROJECT_MARKER).write_text(project_id + "\n", encoding="utf-8", newline="\n")

def search_task_repo(task_id, skip_repository=None):
    """省略 repo 的任务端点全局定位：活动/绑定存储未命中时按注册表克隆检索。"""
    from ..domain import task_path

    for record in list_projects():
        if record.get("builtin"):
            continue
        clone = clone_dir(record["project_id"])
        if not (clone / ".git").exists():
            continue
        if skip_repository and Path(clone).resolve() == Path(skip_repository).resolve():
            continue
        if task_path(clone, task_id).is_file():
            return clone
    return None

def project_id_for_repository(repository):
    """repo 字段（sigt）落点：项目克隆标记 → project_id；否则读仓内 project.json；
    两者皆无（未初始化临时仓）回落 None（调用方退化为仓路径字符串）。"""
    marker = read_project_marker(repository)
    if marker:
        return marker
    document = _read_project_json(repository)
    if document and isinstance(document.get("project_id"), str) and document["project_id"].strip():
        return document["project_id"]
    return None
