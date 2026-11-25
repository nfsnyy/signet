"""T21 core-modularization：内置项目旧 local ship 兼容面（自 signet_client.py 整函数搬入，函数体零变更）。

职责：_git_local/local_git_ship/run_local_ship——T18 冻结案 case-regression-builtin 与
close --ship 本机兼容路径直接消费的钉名。仅标准库，不得 import aidev。
"""

import json
import os
import subprocess
from pathlib import Path

from .http import ClientInputError, emit_error

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
