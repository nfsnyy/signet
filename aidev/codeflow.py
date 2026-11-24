"""T18 multi-project-support（REQ-5~REQ-8）：非内置项目代码闭环模块。

钉扎存储（服务端数据目录，与 clones/ 分离，不进任何状态仓）：

- ``code-mirrors/<project_id>.git``        每项目一个 bare 代码镜像（对象 + 远端
  命名空间，不作共享工作副本）；
- ``code-verifications/<task_id>/source``  精确源 commit 的 detached 验收
  worktree（HEAD == source_commit、status 干净），经 SIGNET_CODE_WORKTREE
  提供给机器验收、异源评审与人工走查；
- ``code-verifications/<task_id>/merge``   预合并 worktree（仅服务端使用）。

凭据纪律（AC-04）：全部远端操作显式 ``repo_kind="code"`` 走项目服务端 code 凭据
（state/code 不混用）；本模块不调用平台仓本机 ship 入口、不做全量暂存收集；
服务端 code 秘密不进响应/报告/事件/任务包/日志。
"""

import re
import shutil
import subprocess
from pathlib import Path

from . import projects as project_store
from .errors import OperationalError, PlatformError
from .gitrepo import is_ancestor

CODE_MIRRORS_DIR_NAME = "code-mirrors"
CODE_VERIFICATIONS_DIR_NAME = "code-verifications"
TASK_BRANCH_PREFIX = "task/"

CODE_FIELDS = ("code_repo_id", "branch", "commit_hash")
COMMIT_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
REMOTE_OP_TIMEOUT_SECONDS = 120
MERGE_MESSAGE = "Merge {branch} ({task_id})"

SUBMIT_ERROR_PREFIX = "submit"
VERIFY_ERROR_PREFIX = "verify"


def mirrors_root():
    return project_store.data_dir() / CODE_MIRRORS_DIR_NAME


def verifications_root():
    return project_store.data_dir() / CODE_VERIFICATIONS_DIR_NAME


def mirror_dir(project_id):
    return mirrors_root() / f"{project_id}.git"


def verification_dir(task_id):
    return verifications_root() / task_id


def source_worktree_dir(task_id):
    return verification_dir(task_id) / "source"


def merge_worktree_dir(task_id):
    return verification_dir(task_id) / "merge"


def code_task_context(repository, metadata):
    """非内置项目且 spec_pin 非空的实现任务 → (project_id, record)；否则 None。

    code_repo_id 恰为任务所属 project_id（T17 一项目一 code_repo）；内置项目与
    未钉扎起草任务返回 None（维持五段报告与旧验收路径）。"""
    if not isinstance(metadata, dict) or not isinstance(metadata.get("spec_pin"), dict):
        return None
    project_id = project_store.project_id_for_repository(repository)
    if not project_id:
        return None
    record = project_store.get_project(project_id)
    if not record or record.get("builtin"):
        return None
    return project_id, record


def _git(cwd, arguments, env=None, check=True):
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=REMOTE_OP_TIMEOUT_SECONDS,
        check=False,
    )
    if check and result.returncode != 0:
        raise OperationalError(
            "git.command_failed",
            f"git {arguments[0]} failed",
            {"stderr": (result.stderr or result.stdout).strip()[-300:], "exit_code": result.returncode},
        )
    return result


def _code_env(project_id):
    return project_store.git_auth_env(project_id, repo_kind="code")


def _auth_args():
    return project_store.remote_auth_args()


def _sanitize(result):
    return (result.stderr or result.stdout or "").strip()[-300:]


def ensure_mirror(project_id, record):
    """项目代码镜像在场（缺则建）：bare 仓 + origin=注册表 code_repo（明文 URL）。"""
    path = mirror_dir(project_id)
    if (path / "HEAD").exists():
        _git(path, ["remote", "set-url", "origin", record["code_repo"]], check=False)
        return path
    mirrors_root().mkdir(parents=True, exist_ok=True)
    _git(mirrors_root(), ["init", "--bare", "--initial-branch=main", str(path)])
    _git(path, ["remote", "add", "origin", record["code_repo"]])
    return path


def _valid_ref_short_name(mirror, branch):
    if not branch or branch.startswith("-") or branch.endswith(".lock"):
        return False
    result = _git(mirror, ["check-ref-format", "--branch", branch], check=False)
    return result.returncode == 0


def _remote_branch_exists(mirror, project_id, branch, error_prefix):
    """ls-remote 探针：连通/认证失败 → code_fetch_failed；缺分支 → branch_not_found。"""
    result = _git(
        mirror,
        [*_auth_args(), "ls-remote", "origin", f"refs/heads/{branch}"],
        env=_code_env(project_id),
        check=False,
    )
    if result.returncode != 0:
        if error_prefix == SUBMIT_ERROR_PREFIX:
            raise PlatformError(
                "submit.code_fetch_failed",
                "项目代码仓不可达或 code 凭据认证失败",
                details={"stderr": _sanitize(result)},
            )
        raise OperationalError("git.fetch_failed", "项目代码仓 ls-remote 失败", {"stderr": _sanitize(result)})
    if not result.stdout.strip():
        if error_prefix == SUBMIT_ERROR_PREFIX:
            raise PlatformError("submit.branch_not_found", f"远端不存在所报分支: {branch}")
        raise OperationalError("git.branch_missing", f"远端不存在任务分支: {branch}")
    return result.stdout.splitlines()[0].split()[0]


def fetch_branch(mirror, project_id, branch, error_prefix):
    """明确 refspec 只 fetch 所报分支到镜像远端命名空间，返回分支 tip。"""
    refspec = f"refs/heads/{branch}:refs/remotes/origin/{branch}"
    result = _git(
        mirror,
        [*_auth_args(), "fetch", "origin", refspec],
        env=_code_env(project_id),
        check=False,
    )
    if result.returncode != 0:
        if error_prefix == SUBMIT_ERROR_PREFIX:
            if "find remote ref" in (result.stderr or ""):
                raise PlatformError("submit.branch_not_found", f"远端不存在所报分支: {branch}")
            raise PlatformError(
                "submit.code_fetch_failed",
                "项目代码仓 fetch 失败或 code 凭据认证失败",
                details={"stderr": _sanitize(result)},
            )
        raise OperationalError("git.fetch_failed", "项目代码仓 fetch 失败", {"stderr": _sanitize(result)})
    return _git(mirror, ["rev-parse", f"refs/remotes/origin/{branch}"]).stdout.strip()


def fetch_main(mirror, project_id):
    """fetch origin/main 到远端命名空间，返回当前主分支基点。"""
    result = _git(
        mirror,
        [*_auth_args(), "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"],
        env=_code_env(project_id),
        check=False,
    )
    if result.returncode != 0:
        raise OperationalError("git.fetch_failed", "项目代码仓 fetch main 失败", {"stderr": _sanitize(result)})
    return _git(mirror, ["rev-parse", "refs/remotes/origin/main"]).stdout.strip()


def _commit_exists(mirror, commit_hash):
    return _git(mirror, ["cat-file", "-e", f"{commit_hash}^{{commit}}"], check=False).returncode == 0


def submit_code_validator(project_id, task_id):
    """apply_submit 代码校验钩子（AC-02 钉名拒绝码全集）。

    domain.validate_report 已完成 code 形状校验（submit.code_required）；本钩子按序
    执行项目绑定 → hash 形态 → ref 形态 → 任务分支绑定 → 远端存在性 → commit 存在
    性 → 分支携带关系。成功时归一化 report["code"]（恰三键）并钉入任务 frontmatter
    的 metadata["code_submission"]，与事件 payload 保持同值（fail-closed 对账基准）。
    """
    def validate(report, metadata):
        code = report.get("code") or {}
        if code.get("code_repo_id") != project_id:
            raise PlatformError(
                "submit.code_repo_mismatch",
                "code_repo_id 必须恰为任务所属 project_id（不接受 URL、路径或未知 id）",
                details={"code_repo_id": code.get("code_repo_id"), "project_id": project_id},
            )
        commit_hash = code.get("commit_hash")
        if not COMMIT_HASH_RE.match(commit_hash or ""):
            raise PlatformError(
                "submit.commit_invalid",
                "commit_hash 必须是 40 位小写 hex",
                details={"commit_hash": commit_hash},
            )
        branch = code.get("branch")
        record = project_store.get_project(project_id)
        if record is None:
            raise PlatformError(
                "submit.code_repo_mismatch",
                "任务所属项目不存在",
                details={"project_id": project_id},
            )
        mirror = ensure_mirror(project_id, record)
        if not _valid_ref_short_name(mirror, branch or ""):
            raise PlatformError(
                "submit.branch_invalid",
                "branch 不是合法 Git ref 短名",
                details={"branch": branch},
            )
        expected_branch = TASK_BRANCH_PREFIX + task_id
        if branch != expected_branch and not branch.startswith(expected_branch + "-"):
            raise PlatformError(
                "submit.branch_task_mismatch",
                f"branch 必须是本任务分支 {expected_branch}",
                details={"branch": branch, "expected": expected_branch},
            )
        _remote_branch_exists(mirror, project_id, branch, SUBMIT_ERROR_PREFIX)
        tip = fetch_branch(mirror, project_id, branch, SUBMIT_ERROR_PREFIX)
        if not _commit_exists(mirror, commit_hash):
            raise PlatformError(
                "submit.commit_not_found",
                "所报 commit 在所报代码仓中不存在或非 commit 对象",
                details={"commit_hash": commit_hash},
            )
        # 所报 commit 必须携带所报分支 tip：main 祖先/旁支对象不得冒充任务分支工作。
        if not is_ancestor(mirror, tip, commit_hash):
            raise PlatformError(
                "submit.commit_not_on_branch",
                "所报 commit 不是所报远端分支的工作提交",
                details={"commit_hash": commit_hash, "branch": branch, "branch_tip": tip},
            )
        normalized = {"code_repo_id": project_id, "branch": branch, "commit_hash": commit_hash}
        report["code"] = dict(normalized)
        metadata["code_submission"] = dict(normalized)
        return normalized

    return validate


def latest_submitted_code(events):
    """最近一次 task.submitted 事件的 report.code（缺失返回 None）。"""
    for event in reversed(list(events)):
        if event.get("event_type") == "task.submitted":
            report = (event.get("payload") or {}).get("report") or {}
            code = report.get("code")
            if isinstance(code, dict):
                return code
            return None
    return None


def latest_code_verification(events):
    """最近一次 verification.initial 的 code_verification 片段（缺失返回 None）。"""
    for event in reversed(list(events)):
        if event.get("event_type") == "verification.initial":
            verification = (event.get("payload") or {}).get("code_verification")
            if isinstance(verification, dict):
                return verification
    return None


def reconcile_source_pin(metadata, submitted_code):
    """任务 frontmatter code_submission 与最近 task.submitted.report.code 对账。

    任一缺失或不等 → 409 verify.source_commit_mismatch（fail-closed，既有 Verified
    不被覆盖）。"""
    pinned = metadata.get("code_submission")
    if not isinstance(pinned, dict) or not submitted_code or pinned != submitted_code:
        raise PlatformError(
            "verify.source_commit_mismatch",
            "任务元数据 code_submission 与最近 task.submitted.report.code 不一致",
            exit_code=3,
            details={"code_submission": pinned, "submitted_code": submitted_code},
        )
    return pinned


def _remove_worktrees(mirror, task_id):
    """移除本任务陈旧验收 worktree 元数据并重建（AC-03：不复用任何旧副本）。"""
    for path in (source_worktree_dir(task_id), merge_worktree_dir(task_id)):
        if path.exists():
            _git(mirror, ["worktree", "remove", "--force", str(path)], check=False)
            shutil.rmtree(path, ignore_errors=True)
    _git(mirror, ["worktree", "prune"], check=False)


def _worktree_add(mirror, path, commit):
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(mirror, ["worktree", "add", "--detach", str(path), commit])


def build_code_verification(project_id, task_id, pinned):
    """重建验收副本并预合并（AC-03/AC-05）。

    返回 code_verification 片段（含 clean 实际检查结果）。冲突 → 409
    verify.merge_conflict（任务保持 Implemented，零验收事件、零远端写）。
    """
    record = project_store.get_project(project_id)
    if record is None:
        raise OperationalError("project.not_found", f"project does not exist: {project_id}")
    mirror = ensure_mirror(project_id, record)
    branch = pinned["branch"]
    source_commit = pinned["commit_hash"]
    tip = fetch_branch(mirror, project_id, branch, VERIFY_ERROR_PREFIX)
    if not _commit_exists(mirror, source_commit) or not is_ancestor(mirror, source_commit, tip):
        raise PlatformError(
            "verify.source_commit_mismatch",
            "源 commit 不在所报远端分支上",
            exit_code=3,
            details={"source_commit": source_commit, "branch": branch},
        )
    target_base = fetch_main(mirror, project_id)
    _remove_worktrees(mirror, task_id)
    source_wt = source_worktree_dir(task_id)
    _worktree_add(mirror, source_wt, source_commit)
    head = _git(source_wt, ["rev-parse", "HEAD"]).stdout.strip()
    clean = head == source_commit and _git(source_wt, ["status", "--porcelain"]).stdout == ""
    if not clean:
        raise PlatformError(
            "verify.source_commit_mismatch",
            "源 commit 验收副本 HEAD 与状态检查未通过",
            exit_code=3,
            details={"head": head, "source_commit": source_commit},
        )
    merge_wt = merge_worktree_dir(task_id)
    _worktree_add(mirror, merge_wt, target_base)
    _git(merge_wt, ["config", "user.name", project_store.GIT_IDENTITY[0]])
    _git(merge_wt, ["config", "user.email", project_store.GIT_IDENTITY[1]])
    merge = _git(
        merge_wt,
        ["merge", "--no-ff", "--no-edit", "-m", MERGE_MESSAGE.format(branch=branch, task_id=task_id), source_commit],
        check=False,
    )
    if merge.returncode != 0:
        raise PlatformError(
            "verify.merge_conflict",
            "任务分支与主分支冲突，须人工处理后重新提交",
            exit_code=3,
            details={"branch": branch, "target_base": target_base, "stderr": _sanitize(merge)},
        )
    merge_commit = _git(merge_wt, ["rev-parse", "HEAD"]).stdout.strip()
    return {
        "project_id": project_id,
        "code_repo_id": project_id,
        "branch": branch,
        "source_commit": source_commit,
        "target_base": target_base,
        "merge_commit": merge_commit,
        "worktree": str(source_wt),
        "clean": True,
    }


def close_push_state(project_id, merge_commit, target_base):
    """close --ship 的代码远端对账 + 推送（AC-06/AC-07）。

    返回 (current_main, pushed)：远端 main 已等于 merge_commit（账本失败恢复重试）
    → (current_main, False)，视为代码副作用已完成、只补账本。main 已前进且不等于
    merge_commit → 409 ship.reverification_required。远端任何失败（不可达/认证/
    push 被拒）统一 → 409 ship.push_failed（main 由单 ref 非强制 push 保证不前进）。
    """
    record = project_store.get_project(project_id)
    if record is None:
        raise OperationalError("project.not_found", f"project does not exist: {project_id}")
    mirror = ensure_mirror(project_id, record)
    fetch = _git(
        mirror,
        [*_auth_args(), "fetch", "origin", "refs/heads/main:refs/remotes/origin/main"],
        env=_code_env(project_id),
        check=False,
    )
    if fetch.returncode != 0:
        raise PlatformError(
            "ship.push_failed",
            "代码远端不可达或认证失败，push 无法完成；恢复后重试同一 close",
            exit_code=3,
            details={"stderr": _sanitize(fetch)},
        )
    current_main = _git(mirror, ["rev-parse", "refs/remotes/origin/main"]).stdout.strip()
    if current_main == merge_commit:
        return current_main, False
    if current_main != target_base:
        raise PlatformError(
            "ship.reverification_required",
            "主分支已在 verify 后前进，须显式 verify --reverify 后再 close",
            exit_code=3,
            details={"current_main": current_main, "target_base": target_base},
        )
    push = _git(
        mirror,
        [*_auth_args(), "push", "origin", f"{merge_commit}:refs/heads/main"],
        env=_code_env(project_id),
        check=False,
    )
    if push.returncode != 0:
        raise PlatformError(
            "ship.push_failed",
            "代码仓 push 失败，main 未前进；恢复后重试同一 close",
            exit_code=3,
            details={"stderr": _sanitize(push)},
        )
    return current_main, True
