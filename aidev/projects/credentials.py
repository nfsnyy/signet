"""T21 core-modularization：服务端 Git 凭据与 askpass 域（自 aidev/projects.py 整函数搬入，函数体零变更）。

职责：凭据 CRUD/校验、askpass 脚本、credstate 凭据状态、git_auth_env/remote_auth_args、
apply_credential_to_clone。公开导入面为 aidev.projects（纯重导出）。
"""

import hashlib
import json
import os
import re
from pathlib import Path

from ..errors import PlatformError
from ..gitrepo import run_git
from ..jsonio import utc_now

from .registry import (
    clone_dir,
    data_dir,
    load_credentials,
    save_credentials,
    url_scheme,
)

GIT_METHODS = ("file", "https_pat", "ssh_agent")

SSH_KEY_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN ENCRYPTED PRIVATE KEY",
)

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

ASKPASS_TEMPLATE = (
    "#!/bin/sh\n"
    "# signet 服务端 git askpass（衍生工件：镜像 git-credentials.json，随凭据刷新/删除）\n"
    "case \"$1\" in\n"
    "  *Username*) printf '%s\\n' {secret} ;;\n"
    "  *Password*) printf '%s\\n' '' ;;\n"
    "esac\n"
)

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

def askpass_dir():
    return data_dir() / ASKPASS_DIR_NAME

def askpass_path(project_id, repo_kind):
    safe_pid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(project_id))
    return askpass_dir() / f"{safe_pid}-{repo_kind}.sh"

def _shquote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"

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
