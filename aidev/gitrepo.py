import os
import subprocess
import tempfile
import time
from pathlib import Path

from .errors import OperationalError


def run_git(repository, arguments, check=True):
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
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


def commit_paths(repository, paths, message, push=False):
    repository = ensure_repository(repository)
    paths = sorted({Path(path).as_posix() for path in paths})
    lock_path = git_commit_lock(repository)
    try:
        staged = run_git(repository, ["diff", "--cached", "--name-only"]).stdout
        if staged.strip():
            raise OperationalError(
                "git.staged_changes_present",
                "refusing to commit while unrelated staged changes exist",
                {"paths": staged.splitlines()},
            )
        run_git(repository, ["add", "--", *paths])
        changed = run_git(repository, ["diff", "--cached", "--name-only", "--", *paths]).stdout.splitlines()
        if not changed:
            if push:
                run_git(repository, ["push", "origin", "main"])
            return None
        if any(path not in paths for path in changed):
            raise OperationalError("git.invalid_commit_scope", "refusing to commit files outside the allowed set")
        run_git(repository, ["commit", "--only", "-m", message, "--", *paths])
        committed = run_git(repository, ["show", "--pretty=", "--name-only", "HEAD"]).stdout.splitlines()
        if any(path not in paths for path in committed if path.strip()):
            raise OperationalError("git.invalid_commit_scope", "commit contains files outside the allowed set")
        if push:
            run_git(repository, ["push", "origin", "main"])
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
