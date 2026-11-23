"""T17 multi-project-support：项目注册表、服务端克隆、同步与引导骨架（仅标准库）。

存储全部落在服务端数据目录（``~/.signet/``，与 users.json / pats.json 同族，
不进任何状态仓）：

- ``projects.json``        项目注册表 ``{project_id: record}``；record 恰八键
  ``{project_id, name, state_repo, code_repo, status, owner, created_at, builtin}``
  （与冻结 interfaces EP-01/EP-02 的 project 形态逐键一致）。
- ``git-credentials.json`` 服务端 Git 凭据 ``{project_id: {state|code: {method,
  secret, updated_at}}}``——秘密只写不回显，任何响应/列表/事件/任务包均不含秘密；
  远端认证经 GIT_ASKPASS 供给（origin 恒明文，秘密不进 URL/.git/config/进程
  参数/日志/错误响应），askpass/ 脚本为凭据存储的衍生运行时工件（随凭据刷新）。
- ``clones/<project_id>/`` 每注册项目一份独立服务端状态仓克隆；克隆根放
  ``.signet-project`` 标记文件（内容=project_id）——gitrepo.commit_paths 凭该
  标记对项目克隆写后自动 push，push 失败回滚本地半态（AC-07）。

repo 解析次序（泛化 T16，向后兼容）：① 省略 → 会话活动项目（无则内置项目）；
② 路径形态 → 400 ``repo.invalid_identifier``（T16 钉名）；③ 逻辑 id 命中内置
project_id → 服务端绑定存储（T16 语义不动）；④ 命中注册表 → 该项目服务端克隆
（读前 fetch 整合 / 写前 fetch+冲突裁决）；⑤ 未知逻辑 id → 404
``repo.not_found``（T16 钉名）。停用项目 → 409 ``project.disabled``。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import authn
from .domain import make_event
from .errors import NotFoundError, OperationalError, PlatformError
from .gitrepo import is_ancestor, run_git
from .jsonio import append_jsonl, atomic_write_bytes, new_id, read_json, utc_now

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

GIT_METHODS = ("file", "https_pat", "ssh_agent")
SSH_KEY_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN ENCRYPTED PRIVATE KEY",
)

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


# ---------------------------------------------------------------------------
# 内置项目（启动自检）
# ---------------------------------------------------------------------------

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


def builtin_record(bound_repository):
    """绑定仓登记为内置项目（project.json 的 project_id；缺文件返回 None）。"""
    document = _read_project_json(bound_repository)
    if not document or not isinstance(document.get("project_id"), str) or not document["project_id"].strip():
        return None
    origin = _origin_url(bound_repository)
    return {
        "project_id": document["project_id"],
        "name": document.get("name") or Path(bound_repository).resolve().name,
        "state_repo": origin or str(Path(bound_repository).resolve()),
        "code_repo": os.environ.get("SIGNET_PLATFORM_REPO") or "",
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
# repo URL 校验（注册/配置同口径；顺序钉名见模块 docstring）
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


def looks_like_ssh_private_key(secret):
    text = str(secret or "")
    return any(marker in text for marker in SSH_KEY_MARKERS)


def validate_credential(method, secret):
    """凭据形态校验（钉名）：method 限定 file|https_pat|ssh_agent；ssh_key 或
    私钥形态秘密一律 400 project.credential_method_unsupported 且响应不回显材料。"""
    if method not in GIT_METHODS:
        raise PlatformError(
            "project.credential_method_unsupported",
            f"凭据 method 仅支持 {list(GIT_METHODS)}，不支持上传 SSH 私钥",
            details={"method": method},
        )
    if looks_like_ssh_private_key(secret):
        raise PlatformError(
            "project.credential_method_unsupported",
            "秘密形态疑似 SSH 私钥材料，本平台不支持上传 SSH 私钥（仅复用既有 SSH agent）",
            details={"method": method},
        )


def credential_required_for(url):
    """非 file:// 仓必须供给凭据（AC-19，先于可达性）。"""
    return url_scheme(url) != "file"


# ---------------------------------------------------------------------------
# 服务端 Git 凭据（秘密只写不回显）
# ---------------------------------------------------------------------------

def get_git_credential(project_id, repo_kind):
    record = load_credentials().get(project_id) or {}
    value = record.get(repo_kind)
    return value if isinstance(value, dict) else None


def upsert_git_credential(project_id, repo_kind, method, secret=None):
    document = load_credentials()
    per_project = document.setdefault(project_id, {})
    entry = {"method": method, "updated_at": utc_now()}
    if secret is not None:
        entry["secret"] = str(secret)
    else:
        previous = per_project.get(repo_kind) or {}
        if "secret" in previous:
            entry["secret"] = previous["secret"]
    per_project[repo_kind] = entry
    save_credentials(document)
    refresh_askpass_scripts(project_id)
    _invalidate_credstate(project_id)
    return entry


def remove_git_credential(project_id, repo_kind):
    document = load_credentials()
    per_project = document.get(project_id)
    removed = False
    if isinstance(per_project, dict) and repo_kind in per_project:
        del per_project[repo_kind]
        removed = True
    save_credentials(document)
    refresh_askpass_scripts(project_id)
    _invalidate_credstate(project_id)
    return removed


def git_credential_metadata(project_id):
    """EP-07 元数据列表片段（无秘密）。"""
    entries = []
    record = load_credentials().get(project_id) or {}
    for repo_kind in ("state", "code"):
        value = record.get(repo_kind)
        if isinstance(value, dict):
            entries.append({
                "project_id": project_id,
                "repo": repo_kind,
                "method": value.get("method"),
                "updated_at": value.get("updated_at"),
            })
    return entries


# ---------------------------------------------------------------------------
# 服务端 Git 凭据供给（AC-19 防泄漏：秘密不进 URL/.git/config/进程参数/日志/错误响应）
# ---------------------------------------------------------------------------
#
# 供给方式 = GIT_ASKPASS 环境变量：origin 恒为明文 URL，git 认证时回调 askpass
# 脚本取用户名/密码（https_pat 语义同嵌入 userinfo——秘密作 username、密码空），
# 克隆配置、进程参数、错误 details 均不接触秘密；每次远端操作另带
# ``-c credential.helper=`` 清空全局 helper（防 GCM/store 代为持久化或弹窗，
# 实测不清空会挂起等待 GUI 凭据框）+ GIT_TERMINAL_PROMPT=0 fail-closed。
# askpass 脚本是凭据存储的衍生运行时工件（同在服务端数据目录、随凭据增删刷新），
# 秘密仍只落 ``git-credentials.json`` 与该脚本本身（AC-19 只写落证）。

ASKPASS_DIR_NAME = "askpass"


def askpass_dir():
    return data_dir() / ASKPASS_DIR_NAME


def askpass_path(project_id, repo_kind):
    safe_pid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(project_id))
    return askpass_dir() / f"{safe_pid}-{repo_kind}.sh"


def _shquote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


ASKPASS_TEMPLATE = (
    "#!/bin/sh\n"
    "# signet 服务端 git askpass（衍生工件：镜像 git-credentials.json，随凭据刷新/删除）\n"
    "case \"$1\" in\n"
    "  *Username*) printf '%s\\n' {secret} ;;\n"
    "  *Password*) printf '%s\\n' '' ;;\n"
    "esac\n"
)


def write_askpass_script(project_id, repo_kind, secret):
    script = askpass_path(project_id, repo_kind)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(ASKPASS_TEMPLATE.format(secret=_shquote(secret)), encoding="utf-8", newline="\n")
    try:
        os.chmod(script, 0o600)
    except OSError:
        pass
    return script


def remove_askpass_script(project_id, repo_kind):
    askpass_path(project_id, repo_kind).unlink(missing_ok=True)


def refresh_askpass_scripts(project_id):
    """凭据存储 → askpass 脚本同步：https_pat 且有秘密者写脚本，其余删除。"""
    record = load_credentials().get(project_id) or {}
    for repo_kind in ("state", "code"):
        entry = record.get(repo_kind)
        secret = entry.get("secret") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and entry.get("method") == "https_pat" and secret:
            write_askpass_script(project_id, repo_kind, secret)
        else:
            remove_askpass_script(project_id, repo_kind)


def git_auth_env(project_id=None, repo_kind="state"):
    """远端 git 操作子进程环境：GIT_ASKPASS（脚本在场时）+ 禁终端/弹窗提示。"""
    env = dict(os.environ)
    if project_id:
        script = askpass_path(project_id, repo_kind)
        if script.is_file():
            env["GIT_ASKPASS"] = str(script)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GCM_MODAL_PROMPT"] = "false"
    return env


def remote_auth_args():
    """清空 credential.helper 的 -c 前缀（防全局 helper 持久化秘密或 GUI 弹窗）。"""
    return ["-c", "credential.helper="]


def _git_template_dir():
    """空模板目录：克隆不复制样板 hooks/description，缩减服务端数据目录体积
    （验收环境对数据目录做全量字节扫，文件数直接决定扫描耗时）。"""
    path = data_dir() / "git-empty-template"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _credstate_path(clone):
    """凭据状态标记：存于 .git/ 内部（不进工作树、不被 add -A 提交、不进项目仓）。"""
    return Path(clone) / ".git" / "signet-credstate"


def _credstate_expected(record):
    """凭据维护期望指纹（不含秘密：secret 轮换必然刷新 updated_at）。"""
    creds = load_credentials().get(record["project_id"]) or {}
    fingerprint = {
        "state_repo": record.get("state_repo"),
        "state": {k: v for k, v in (creds.get("state") or {}).items() if k != "secret"},
        "code": {k: v for k, v in (creds.get("code") or {}).items() if k != "secret"},
    }
    return hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _credstate_current(clone, record):
    path = _credstate_path(clone)
    try:
        return path.read_text(encoding="utf-8").strip() == _credstate_expected(record)
    except OSError:
        return False


def _write_credstate(clone, record):
    _credstate_path(clone).write_text(_credstate_expected(record) + "\n", encoding="utf-8", newline="\n")


def _invalidate_credstate(project_id):
    _credstate_path(clone_dir(project_id)).unlink(missing_ok=True)


def apply_credential_to_clone(clone, project_record, repo_kind="state"):
    """把凭据应用到服务端克隆（AC-19 防泄漏形态）：origin 恒设明文 URL（同时迁移
    旧形态嵌秘密配置）；ssh_agent 复用本机 agent（core.sshCommand 无秘密）；
    askpass 脚本随凭据存储刷新；成功后落凭据状态标记（ensure_clone 存量路径
    凭标记免 git 直返——冻结用例时延敏感，本机每次 git spawn 约 0.2~1.2s）。
    凭据删除后 fail-closed（下一次远端认证失败）。"""
    url = project_record[repo_kind + "_repo"]
    run_git(clone, ["remote", "set-url", "origin", url], check=False)
    credential = get_git_credential(project_record["project_id"], repo_kind)
    if credential and credential.get("method") == "ssh_agent":
        run_git(clone, ["config", "core.sshCommand", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"], check=False)
    else:
        run_git(clone, ["config", "--unset", "core.sshCommand"], check=False)
    refresh_askpass_scripts(project_record["project_id"])
    _write_credstate(clone, project_record)


# ---------------------------------------------------------------------------
# 服务端克隆与同步（AC-06 读前 fetch / AC-07 写后 push / AC-08 冲突裁决）
# ---------------------------------------------------------------------------

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


def ensure_clone(project_record):
    """项目服务端克隆在场（缺则建）：origin=明文状态仓远端，认证走 GIT_ASKPASS。
    已存在克隆先比对凭据状态标记——一致则免 git 直返（时延敏感路径），不一致才
    全量维护（origin 去秘密化迁移 + ssh 配置 + askpass 刷新 + 重落标记）。"""
    target = clone_dir(project_record["project_id"])
    if (target / ".git").exists():
        if not _credstate_current(target, project_record):
            apply_credential_to_clone(target, project_record)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = project_record["state_repo"]
    result = subprocess.run(
        ["git", "clone", "--template", str(_git_template_dir()), "--no-checkout", url, str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env=git_auth_env(project_record["project_id"], "state"),
    )
    if result.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise OperationalError(
            "project.state_repo_unreachable",
            f"项目状态仓不可克隆：{project_record['state_repo']}",
            details={"stderr": (result.stderr or result.stdout)[-300:]},
        )
    run_git(target, ["remote", "set-url", "origin", project_record["state_repo"]])
    run_git(target, ["config", "user.name", GIT_IDENTITY[0]])
    run_git(target, ["config", "user.email", GIT_IDENTITY[1]])
    write_project_marker(target, project_record["project_id"])
    apply_credential_to_clone(target, project_record)
    checkout = run_git(target, ["checkout", "main"], check=False)
    if checkout.returncode != 0:
        run_git(target, ["checkout", "-b", "main"], check=False)
    return target


def _local_and_upstream(clone):
    """一次 for-each-ref 取齐本地 main 与远端 main（省一次 git spawn；
    服务端克隆恒以 main 为工作分支）。"""
    result = run_git(
        clone,
        ["for-each-ref", "--format=%(refname)%09%(objectname)",
         "refs/heads/main", "refs/remotes/origin/main"],
    )
    refs = {}
    for line in result.stdout.splitlines():
        name, _, sha = line.partition("\t")
        if name and sha:
            refs[name] = sha
    local = refs.get("refs/heads/main")
    if local is None:
        raise OperationalError(
            "git.command_failed",
            "git rev-parse HEAD failed",
            {"stderr": "unborn branch: refs/heads/main"},
        )
    return local, refs.get("refs/remotes/origin/main")


def fetch_integrate(clone, for_write=False):
    """读前 fetch 整合 / 写前 fetch+冲突裁决。

    behind（远端领先非分叉）→ ff 整合（外部提交不丢）；diverged → 写动词拒绝
    ``sync.diverged_remote``（T16 钉名）且零副作用，读动词服务本地现状。
    读路径 fetch 失败（远端暂不可达）容忍并服务本地。"""
    env = git_auth_env(read_project_marker(clone), "state")
    fetch = run_git(clone, [*remote_auth_args(), "fetch", "origin"], check=False, env=env)
    if fetch.returncode != 0:
        if for_write:
            raise OperationalError(
                "git.fetch_failed",
                "项目远端暂不可达，写动词整体失败（无半态）",
                details={"stderr": fetch.stderr.strip()[-300:]},
            )
        return
    local, upstream = _local_and_upstream(clone)
    if upstream is None or local == upstream:
        return
    if is_ancestor(clone, local, upstream):
        run_git(clone, ["merge", "--ff-only", "origin/main"])
        return
    if is_ancestor(clone, upstream, local):
        return
    if for_write:
        raise OperationalError(
            "sync.diverged_remote",
            "项目状态仓远端已分叉；写动词拒绝（零副作用），请先整合远端历史",
            details={"local": local, "remote": upstream},
        )


def search_task_repo(task_id, skip_repository=None):
    """省略 repo 的任务端点全局定位：活动/绑定存储未命中时按注册表克隆检索。"""
    from .domain import task_path

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


# ---------------------------------------------------------------------------
# 引导骨架（AC-12/AC-13）
# ---------------------------------------------------------------------------

SKELETON_DIRS = ("requirements", "events", "specs", "tasks")
SKELETON_FILES = ("project.json", "hints.md", "requirements/getting-started.md", "events/events.jsonl",
                  "specs/.gitkeep", "tasks/.gitkeep")

GETTING_STARTED_BODY = """# 入门引导

欢迎使用 signet 平台管理本项目。第一步：确认需求——owner 本人登录后打开本需求详情页，
逐项核对「判据条目」并确认（清单与条目集合完全一致才会生效）；第二步：建首个任务——
从已确认需求派生任务（页面「从需求创建任务」入口或 operator CLI），随后走
领取 → 实现 → 提交 → 验收闭环。平台规范（skill/三件套等）一律经 /api/norms/ 与
/api/skill 引用获取，本仓不复制规范全文。
"""

HINTS_TEMPLATE = """# {name} 项目建议区（hints）

## 如何反哺（分通道，勿混）

- **经验教训**（后辈该怎么干活）→ 写你工作区里的 `hints-proposal.md`（随手记，一条一行即可），
  submit 后由 operator 验收时评审合入本文件。
- **改进点**（平台/项目该怎么改）→ 用 `enhancement` 动词登记，进 backlog 容器，operator 分拣。

## 平台规范（引用，不内嵌）

- 起草与验收规范经平台 API 获取：`/api/norms/`（规范名册）与 `/api/skill`（agent skill），
  本文件不复制规范全文；项目骨架初始仅含本文件与引导需求。
"""


def skeleton_tree(record, actor="signet-platform"):
    """骨架落盘：目录骨架 + project.json + hints.md（反哺节两锚点 + /api/norms 引用）
    + getting-started 引导需求 + events.jsonl（首笔 project.registered 事件——
    骨架提交即含事件，远端基线非空）。注册/配置/停用等注册表生命周期事件同落本仓。"""
    clone = clone_dir(record["project_id"])
    (clone / "requirements").mkdir(parents=True, exist_ok=True)
    (clone / "events").mkdir(parents=True, exist_ok=True)
    (clone / "specs").mkdir(parents=True, exist_ok=True)
    (clone / "tasks").mkdir(parents=True, exist_ok=True)
    files = {
        "project.json": (json.dumps({
            "project_id": record["project_id"],
            "name": record["name"],
            "owner": record["owner"],
            "created_at": record["created_at"],
        }, ensure_ascii=False, sort_keys=True) + "\n"),
        "hints.md": HINTS_TEMPLATE.format(name=record["name"]),
        "requirements/getting-started.md": (
            "---\n"
            "requirement_id: getting-started\n"
            "title: 入门引导\n"
            "status: confirmed\n"
            "priority: P1\n"
            f"owner: {record['owner']}\n"
            f"created_at: {record['created_at']}\n"
            f"updated_at: {record['created_at']}\n"
            "---\n\n"
            + GETTING_STARTED_BODY
        ),
        "events/events.jsonl": "",
        "specs/.gitkeep": "",
        "tasks/.gitkeep": "",
    }
    for relative, content in files.items():
        target = clone / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    registered = make_event(
        "project.registered", None, actor, actor, None, None,
        payload={"project_id": record["project_id"], "name": record["name"]},
        entrypoint="operator",
    )
    document = _read_project_json(clone)
    if document and isinstance(document.get("project_id"), str):
        registered.setdefault("project_id", document["project_id"])
    append_jsonl(clone / "events" / "events.jsonl", [registered])
    return clone


# ---------------------------------------------------------------------------
# 注册（AC-01/AC-02/AC-14/AC-19）
# ---------------------------------------------------------------------------

def _check_required(name, state_repo, code_repo):
    if not str(name or "").strip():
        raise PlatformError("project.name_required", "name 必填且非空")
    if not str(state_repo or "").strip():
        raise PlatformError("project.state_repo_required", "必须提供状态仓远端地址（state_repo）")
    if not str(code_repo or "").strip():
        raise PlatformError("project.code_repo_required", "必须提供代码仓远端地址（code_repo）")


def validate_supplied_credentials(credentials):
    """注册/配置携 credentials 的形态预检（方法/私钥钉名码；零副作用）。"""
    cleaned = {}
    if credentials is None:
        return cleaned
    if not isinstance(credentials, dict):
        raise PlatformError("request.invalid", "credentials 必须为 {state|code: {method, secret?}} 对象")
    for repo_kind in ("state", "code"):
        value = credentials.get(repo_kind)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise PlatformError("request.invalid", f"credentials.{repo_kind} 必须为对象")
        method = value.get("method")
        secret = value.get("secret")
        validate_credential(method, secret)
        entry = {"method": method}
        if secret is not None:
            entry["secret"] = str(secret)
        cleaned[repo_kind] = entry
    return cleaned


def _check_credential_supply(state_repo, code_repo, cleaned):
    """凭据供给序（AC-19）：非 file:// 仓未供给凭据 → 400 project.credential_required，
    先于可达性（不得先注册成功再克隆失败）。"""
    for repo_kind, url in (("state", state_repo), ("code", code_repo)):
        if credential_required_for(url) and repo_kind not in cleaned:
            raise PlatformError(
                "project.credential_required",
                f"{repo_kind}_repo 为非 file:// 远端仓，必须经 credentials.{repo_kind} 供给服务端 Git 凭据",
                details={"repo": repo_kind},
            )


def _probe_clone(state_repo, credential):
    """可达性探针：明文 URL 克隆到临时目录验证可克隆（不存在/非 git →
    state_repo_unreachable）。凭据经临时 askpass 供给（秘密不进 URL/进程参数），
    成功返回临时目录路径（调用方接手），失败清理零残留。"""
    temporary = Path(tempfile.mkdtemp(prefix="aidev-t17-probe-"))
    target = temporary / "clone"
    env = git_auth_env()
    askpass = temporary / "askpass.sh"
    if credential and credential.get("method") == "https_pat" and credential.get("secret"):
        askpass.write_text(ASKPASS_TEMPLATE.format(secret=_shquote(credential["secret"])), encoding="utf-8", newline="\n")
        env["GIT_ASKPASS"] = str(askpass)
    if credential and credential.get("method") == "ssh_agent":
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
    result = subprocess.run(
        ["git", *remote_auth_args(), "clone", "--template", str(_git_template_dir()),
         "--no-checkout", state_repo, str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env,
    )
    if result.returncode != 0 or not (target / ".git").exists():
        shutil.rmtree(temporary, ignore_errors=True)
        raise PlatformError(
            "project.state_repo_unreachable",
            f"state_repo 指向不存在/非 git/不可克隆的远端：{state_repo}",
            details={"stderr": (result.stderr or result.stdout)[-300:]},
        )
    return temporary, target


def register_project(name, state_repo, code_repo, credentials, actor, platform_repo, bound_repository, builtin_project_id):
    """注册全流程（校验序：形态 → 回退禁 → 凭据供给 → 可达性；全程零副作用至落库）。

    成功：注册表记录 + 服务端克隆 + 引导骨架单笔 commit 推送（远端立即可用）。
    任一步失败：无注册残留、无半成品克隆目录、远端零写（代码仓全程零写）。
    """
    state_repo = str(state_repo or "").strip()
    code_repo = str(code_repo or "").strip()
    name = str(name or "").strip()
    _check_required(name, state_repo, code_repo)
    validate_repo_url(state_repo, "state", platform_repo, bound_repository, builtin_project_id)
    validate_repo_url(code_repo, "code", platform_repo, bound_repository, builtin_project_id)
    cleaned_credentials = validate_supplied_credentials(credentials)
    _check_credential_supply(state_repo, code_repo, cleaned_credentials)

    state_credential = cleaned_credentials.get("state")
    temporary, probed = _probe_clone(state_repo, state_credential)
    project_id = new_id("project")
    record = {
        "project_id": project_id,
        "name": name,
        "state_repo": state_repo,
        "code_repo": code_repo,
        "status": "active",
        "owner": actor,
        "created_at": utc_now(),
        "builtin": False,
    }
    target = clone_dir(project_id)
    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(probed), str(target))
        shutil.rmtree(temporary, ignore_errors=True)
        run_git(target, ["remote", "set-url", "origin", state_repo])
        run_git(target, ["config", "user.name", GIT_IDENTITY[0]])
        run_git(target, ["config", "user.email", GIT_IDENTITY[1]])
        write_project_marker(target, project_id)
        for repo_kind, entry in cleaned_credentials.items():
            upsert_git_credential(project_id, repo_kind, entry["method"], entry.get("secret"))
        apply_credential_to_clone(target, record)
        # 骨架：先落 worktree，再让 git 自然 add（含 .gitkeep 占位）
        # 骨架：先落 worktree（含首笔 registered 事件），再入库——.signet-project
        # 服务端标记文件不得进项目仓（git reset 取消其暂存）。
        skeleton_tree(record, actor=actor)
        checkout = run_git(target, ["checkout", "-b", "main"], check=False)
        if checkout.returncode != 0:
            run_git(target, ["checkout", "main"], check=False)
        run_git(target, ["add", "-A"])
        run_git(target, ["reset", "-q", "--", PROJECT_MARKER], check=False)
        if not run_git(target, ["diff", "--cached", "--name-only"]).stdout.strip():
            raise OperationalError("project.bootstrap_failed", "骨架无可提交内容")
        run_git(target, ["commit", "-m", f"Bootstrap project skeleton for {project_id}"])
        push = run_git(
            target,
            [*remote_auth_args(), "push", "origin", "main"],
            check=False,
            env=git_auth_env(project_id, "state"),
        )
        if push.returncode != 0:
            raise OperationalError(
                "project.bootstrap_failed",
                "骨架首笔 push 失败",
                details={"stderr": (push.stderr or push.stdout)[-300:]},
            )
        put_project(record)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return record


# ---------------------------------------------------------------------------
# 项目凭据视图（EP-07 = API PAT 记录 + 服务端 Git 凭据元数据；无秘密无令牌明文）
# ---------------------------------------------------------------------------

def project_credentials_view(project_id):
    view = []
    pats = authn.load_pats()
    for pat_id, pat in sorted(pats.items()):
        if pat.get("project_id") != project_id:
            continue
        view.append({
            "pat_id": pat.get("pat_id"),
            "label": pat.get("label"),
            "obo": pat.get("obo"),
            "scope": list(pat.get("scope") or []),
            "project_id": project_id,
            "status": pat.get("status"),
            "created_at": pat.get("created_at"),
            "expires_at": pat.get("expires_at"),
            "last_used": pat.get("last_used"),
            "kind": "api_pat",
        })
    view.extend(git_credential_metadata(project_id))
    return view
