import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .errors import OperationalError

PROJECT_MARKER = ".signet-project"


def run_git(repository, arguments, check=True, env=None):
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        raise OperationalError(
            "git.command_failed",
            f"git {arguments[0]} failed",
            {"stderr": result.stderr.strip(), "exit_code": result.returncode},
        )
    return result


def ensure_repository(repository):
    repository = Path(repository).resolve()
    result = run_git(repository, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise OperationalError("git.not_repository", f"not a Git repository: {repository}")
    root = Path(result.stdout.strip()).resolve()
    if root != repository:
        raise OperationalError("git.not_repository_root", f"repository root must be {root}")
    return repository


def initialize_repository(repository):
    repository = Path(repository).resolve()
    repository.mkdir(parents=True, exist_ok=True)
    if (repository / ".git").exists():
        return repository
    if any(repository.iterdir()):
        raise OperationalError("git.repository_not_empty", f"directory is not empty: {repository}")
    run_git(repository, ["init", "-b", "main"])
    return repository


GIT_COMMIT_LOCK = ".aidev-git.lock"
GIT_LOCK_WAIT_SECONDS = 15.0
GIT_LOCK_POLL_SECONDS = 0.05


def git_commit_lock(repository):
    """commit_paths 全仓串行锁（T16 异步接线配套）：async 端点经线程池后，多个
    请求可并发进入各自域锁的提交段；git add/commit 临界区必须全仓串行，否则
    add/commit 交错会留下旁路 staged 文件，触发 git.staged_changes_present
    雪崩。自旋等待（不永久阻塞），超时如实报错。"""
    lock_path = Path(repository) / GIT_COMMIT_LOCK
    deadline = time.monotonic() + GIT_LOCK_WAIT_SECONDS
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            return lock_path
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OperationalError("git.commit_lock_timeout", "timed out waiting for the repository commit lock")
            time.sleep(GIT_LOCK_POLL_SECONDS)


def _rollback_half_state(repository, pre_head, paths):
    """AC-07 push 失败回滚：本地无半态提交/半态文件（远端恢复后无孤儿痕迹）。"""
    if pre_head:
        run_git(repository, ["reset", "--hard", pre_head], check=False)
    for path in paths:
        checked = run_git(repository, ["ls-files", "--", path], check=False)
        if checked.returncode == 0 and not checked.stdout.strip():
            target = Path(repository) / path
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)


def _push_origin_main(repository, paths, pre_head):
    arguments = ["push", "origin", "main"]
    env = None
    if (Path(repository) / PROJECT_MARKER).is_file():
        # 项目克隆（AC-19）：认证走 GIT_ASKPASS 环境（秘密不进 URL/参数/配置），
        # 并清空 credential.helper，防全局 helper（GCM/store）代为持久化或弹窗。
        from . import projects as project_store  # 延迟导入避免模块环

        project_id = project_store.read_project_marker(repository)
        env = project_store.git_auth_env(project_id, "state")
        arguments = [*project_store.remote_auth_args(), *arguments]
    push = run_git(repository, arguments, check=False, env=env)
    if push.returncode != 0:
        _rollback_half_state(repository, pre_head, paths)
        raise OperationalError(
            "git.push_failed",
            "push to origin/main failed; local half-state rolled back",
            {"stderr": (push.stderr or push.stdout).strip()[-300:], "exit_code": push.returncode},
        )


def commit_paths(repository, paths, message, push=False):
    repository = ensure_repository(repository)
    paths = sorted({Path(path).as_posix() for path in paths})
    auto_push = (Path(repository) / PROJECT_MARKER).is_file()
    lock_path = git_commit_lock(repository)

    def list_paths(arguments):
        # T23：非 ASCII 路径（如 design/中文名.md）在 git 默认 core.quotepath=true
        # 下以八进制转义加引号输出，与传入 paths 比对必误判越集；列路径命令统一关闭。
        return run_git(repository, ["-c", "core.quotepath=false", *arguments])

    try:
        staged = list_paths(["diff", "--cached", "--name-only"]).stdout
        if staged.strip():
            raise OperationalError(
                "git.staged_changes_present",
                "refusing to commit while unrelated staged changes exist",
                {"paths": staged.splitlines()},
            )
        pre_head_result = run_git(repository, ["rev-parse", "HEAD"], check=False)
        pre_head = pre_head_result.stdout.strip() if pre_head_result.returncode == 0 else None
        run_git(repository, ["add", "--", *paths])
        changed = list_paths(["diff", "--cached", "--name-only", "--", *paths]).stdout.splitlines()
        if not changed:
            if push or auto_push:
                _push_origin_main(repository, paths, pre_head)
            return None
        if any(path not in paths for path in changed):
            raise OperationalError("git.invalid_commit_scope", "refusing to commit files outside the allowed set")
        run_git(repository, ["commit", "--only", "-m", message, "--", *paths])
        committed = list_paths(["show", "--pretty=", "--name-only", "HEAD"]).stdout.splitlines()
        if any(path not in paths for path in committed if path.strip()):
            raise OperationalError("git.invalid_commit_scope", "commit contains files outside the allowed set")
        if push or auto_push:
            _push_origin_main(repository, paths, pre_head)
        return run_git(repository, ["rev-parse", "HEAD"]).stdout.strip()
    finally:
        lock_path.unlink(missing_ok=True)


def has_origin(repository):
    return run_git(repository, ["remote", "get-url", "origin"], check=False).returncode == 0


def fetch_origin(repository):
    run_git(repository, ["fetch", "origin"])


def ref_exists(repository, reference):
    return run_git(repository, ["rev-parse", "--verify", reference], check=False).returncode == 0


def current_head(repository):
    return run_git(repository, ["rev-parse", "HEAD"]).stdout.strip()


def is_ancestor(repository, ancestor, descendant):
    return run_git(repository, ["merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def ensure_remote_syncable(repository):
    if not has_origin(repository):
        return
    fetch_origin(repository)
    local_head = current_head(repository)
    upstream_head_ref = "refs/remotes/origin/main"
    if not ref_exists(repository, upstream_head_ref):
        return
    upstream_head = run_git(repository, ["rev-parse", upstream_head_ref]).stdout.strip()
    if local_head == upstream_head:
        return
    if is_ancestor(repository, local_head, upstream_head):
        raise OperationalError(
            "sync.behind_remote",
            "authoritative repository is behind origin/main; fetch or rebase before syncing",
            {"local": local_head, "remote": upstream_head},
        )
    if is_ancestor(repository, upstream_head, local_head):
        return
    raise OperationalError(
        "sync.diverged_remote",
        "authoritative repository diverged from origin/main; resolve history before syncing",
        {"local": local_head, "remote": upstream_head},
    )
