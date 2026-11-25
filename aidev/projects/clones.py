"""T21 core-modularization：服务端克隆 fetch 整合同步域（自 aidev/projects.py 整函数搬入，函数体零变更）。

职责：ensure_clone、fetch_integrate 读写前整合、克隆模板目录。公开导入面为
aidev.projects（纯重导出）。
"""

import shutil
import subprocess
from pathlib import Path

from ..errors import OperationalError
from ..gitrepo import is_ancestor, run_git

from .credentials import (
    _credstate_current,
    apply_credential_to_clone,
    git_auth_env,
    remote_auth_args,
)
from .registry import (
    GIT_IDENTITY,
    clone_dir,
    data_dir,
    read_project_marker,
    write_project_marker,
)

# ---------------------------------------------------------------------------
# 服务端克隆与同步（AC-06 读前 fetch / AC-07 写后 push / AC-08 冲突裁决）
# ---------------------------------------------------------------------------

def _git_template_dir():
    """空模板目录：克隆不复制样板 hooks/description，缩减服务端数据目录体积
    （验收环境对数据目录做全量字节扫，文件数直接决定扫描耗时）。"""
    path = data_dir() / "git-empty-template"
    path.mkdir(parents=True, exist_ok=True)
    return path

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
