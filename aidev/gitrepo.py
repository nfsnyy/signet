import subprocess
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
    if any(repository.iterdir()):
        raise OperationalError("git.repository_not_empty", f"directory is not empty: {repository}")
    run_git(repository, ["init", "-b", "main"])
    return repository


def commit_paths(repository, paths, message, push=False):
    repository = ensure_repository(repository)
    paths = sorted({Path(path).as_posix() for path in paths})
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
