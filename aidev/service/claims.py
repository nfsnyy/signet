from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import projects as project_store
from ..authn import mint_sigt
from ..domain import apply_claim, event_path, lease_is_active, make_event, proposal_base, task_path
from ..errors import AuthorizationError, PlatformError, NotFoundError
from ..gitrepo import commit_paths, ensure_repository
from ..jsonio import append_jsonl, new_id, read_jsonl, utc_now, write_json
from ..taskfile import read_task, write_task
from .ledger import verify_frozen_integrity
from .packages import build_task_package, stamp_events, validate_workspace_pin


def claim(workspace, actor, on_behalf_of):
    workspace = Path(workspace).resolve()
    metadata, _ = read_task(workspace / "task.md")
    if metadata.get("state") != "Ready":
        raise PlatformError("claim.not_ready", "only a Ready task can be claimed", details={"state": metadata.get("state")})
    if lease_is_active(metadata.get("lease")):
        raise PlatformError("claim.lease_active", "task already has an active lease")
    validate_workspace_pin(workspace, metadata)
    proposal_id = new_id("proposal")
    proposal = {
        "proposal_id": proposal_id,
        "task_id": metadata["task_id"],
        "operation": "claim",
        "actor": actor,
        "on_behalf_of": on_behalf_of,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {},
        "workspace": str(workspace),
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    write_json(workspace / "outbox" / f"{proposal_id}.json", proposal)
    return {"proposal_id": proposal_id, "task_id": metadata["task_id"], "authoritative": False}


def claim_authoritative(repository, task_id, actor):
    """同步授权模式 claim：直接改写权威账本并签发 handoff 令牌。

    用于携带凭据、不带工作区的编排请求；经典提案模式（claim）保持不变。
    允许 Ready 或 Draft（T6 编排预冻结领取：仅无工作区路径，任务进入
    In Progress 并签发 sigt；带工作区的提案式 claim 仍要求 Ready）。
    """
    repository = ensure_repository(repository)
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    if metadata.get("state") not in ("Ready", "Draft"):
        raise PlatformError("claim.not_ready", "only a Ready or Draft task can be claimed", details={"state": metadata.get("state")})
    if lease_is_active(metadata.get("lease")):
        raise PlatformError("claim.lease_active", "task already has an active lease")
    verify_frozen_integrity(repository, metadata, actor, entrypoint="operator")
    proposal = {
        "proposal_id": new_id("proposal"),
        "task_id": task_id,
        "operation": "claim",
        "actor": actor,
        "on_behalf_of": actor,
        "created_at": utc_now(),
        "base": proposal_base(metadata),
        "parameters": {},
        "workspace": None,
        "local_validation": {"result": "passed", "checked_at": utc_now()},
    }
    accepted_event, outcome = apply_claim(proposal, metadata, allow_draft=True)
    if accepted_event is None:
        _rejected_event, result = outcome
        raise PlatformError(result.get("code", "claim.rejected"), result.get("reason", "claim was rejected"))
    events_file = event_path(repository)
    write_task(path, metadata, body)
    append_jsonl(events_file, stamp_events(repository, [accepted_event]))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository)],
        f"Claim {task_id}",
    )
    return {
        "task_id": task_id,
        "state": metadata["state"],
        "lease": metadata["lease"],
        "handoff_token": mint_sigt(task_id, actor, sigt_repo_field(repository)),
        "authoritative": True,
        "commit": commit,
    }


def sigt_repo_field(repository):
    """T17：sigt 的 repo 字段记项目 project_id（逻辑 id）；仓无项目标识时（未初始化
    临时仓/兼容旧形态）回落仓路径字符串。任务解析时校验该字段与任务所属项目一致。"""
    return project_store.project_id_for_repository(repository) or str(Path(repository).resolve())


def collect_expired_leases(repository, changed_tasks, new_events):
    tasks_directory = repository / "tasks"
    if not tasks_directory.is_dir():
        return
    for path in sorted(tasks_directory.glob("*/task.md")):
        task_id = path.parent.name
        if task_id in changed_tasks:
            metadata, body = changed_tasks[task_id]
        else:
            metadata, body = read_task(path)
        lease = metadata.get("lease")
        if lease and not lease_is_active(lease):
            metadata["lease"] = None
            metadata["updated_at"] = utc_now()
            new_events.append(make_event(
                "lease.expired",
                task_id,
                lease["claimed_by"],
                metadata["owner"],
                metadata["state"],
                metadata["state"],
                proposal_id=lease.get("proposal_id"),
                payload={"lease": lease},
                entrypoint="agent",
            ))
            changed_tasks[task_id] = (metadata, body)


def last_claim_delegate(repository, task_id):
    """最近一次 task.claimed 的执行者（on_behalf_of 优先，缺省 actor）——sigt 续期判定键。"""
    for event in reversed(read_jsonl(event_path(repository))):
        if event.get("task_id") == task_id and event.get("event_type") == "task.claimed":
            return event.get("on_behalf_of") or event.get("actor")
    return None


def claim_with_sigt(repository, task_id, actor, on_behalf_of=None, sigt_repo=None):
    """sigt 幂等接管 + 续期：直接改写权威账本并提交，响应携带完整任务包。

    - In Progress 且同执行者（actor 且 on_behalf_of 与最近一次 claim 一致）持活跃租约：
      续期（lease.renewed 事件，expires_at 顺延 24h）；
    - Ready（含 refreeze 释放租约后）或执行者变更（委托转交另一 agent）：重 claim
      （task.claimed 事件，状态进入 In Progress）；
    - 他人持活跃租约（actor 不同）：拒绝（claim.lease_active）。
    AC-16 委托流归因：事件 actor=签发人（问责锁定），on_behalf_of=执行者（缺省回落 actor）。
    T17：sigt 的 repo 字段（项目 project_id 或兼容的仓路径）必须与任务所属项目
    一致，不符即拒（403 authz.project_denied，零副作用）。
    """
    repository = ensure_repository(repository)
    on_behalf_of = on_behalf_of or actor
    if sigt_repo is not None:
        expected = {sigt_repo_field(repository), str(Path(repository).resolve())}
        if sigt_repo not in expected:
            raise AuthorizationError(
                "authz.project_denied",
                f"sigt 绑定项目 {sigt_repo} 与任务所属项目不符",
                details={"sigt_project": sigt_repo, "task_project": sigt_repo_field(repository)},
            )
    path = task_path(repository, task_id)
    if not path.is_file():
        raise PlatformError("task.not_found", f"task does not exist: {task_id}")
    metadata, body = read_task(path)
    state = metadata.get("state")
    lease = metadata.get("lease") or {}
    now = datetime.now(timezone.utc)
    events = []
    takeover = False
    verify_frozen_integrity(repository, metadata, actor, entrypoint="agent")
    if state == "In Progress" and lease_is_active(lease):
        if lease.get("claimed_by") != actor:
            raise PlatformError("claim.lease_active", "task already has an active lease held by another actor")
        if last_claim_delegate(repository, task_id) != on_behalf_of:
            # 委托转交：同签发人令牌换一个执行者接管——落新的 task.claimed 事件（执行者归因）。
            takeover = True
        else:
            previous_expiry = lease.get("expires_at")
            lease["expires_at"] = (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            metadata["lease"] = lease
            metadata["updated_at"] = utc_now()
            events.append(make_event(
                "lease.renewed",
                task_id,
                actor,
                on_behalf_of,
                "In Progress",
                "In Progress",
                payload={"lease": lease, "previous_expires_at": previous_expiry},
                entrypoint="agent",
            ))
    if state in ("Ready", "In Progress") and not events:
        if not takeover and lease_is_active(lease) and lease.get("claimed_by") != actor:
            raise PlatformError("claim.lease_active", "task already has an active lease held by another actor")
        before = "In Progress" if takeover else state
        metadata["lease"] = {
            "claimed_by": actor,
            "claimed_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "proposal_id": None,
        }
        metadata["state"] = "In Progress"
        metadata["updated_at"] = utc_now()
        events.append(make_event(
            "task.claimed",
            task_id,
            actor,
            on_behalf_of,
            before,
            "In Progress",
            payload={"lease": metadata["lease"]},
            entrypoint="agent",
        ))
    elif not events:
        raise PlatformError(
            "claim.not_ready",
            "only a Ready or In Progress task can be re-claimed with a handoff token",
            details={"state": state},
        )
    write_task(path, metadata, body)
    events_file = event_path(repository)
    append_jsonl(events_file, stamp_events(repository, events))
    commit = commit_paths(
        repository,
        [path.relative_to(repository), events_file.relative_to(repository)],
        f"Reclaim {task_id}",
    )
    return {
        "task_id": task_id,
        "state": metadata["state"],
        "lease": metadata["lease"],
        "lease_expires_at": metadata["lease"]["expires_at"],
        "package": build_task_package(repository, metadata, body),
        "authoritative": True,
        "commit": commit,
    }
