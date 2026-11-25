"""T21 core-modularization 过渡承载模块（S1-S3 临时）：尚未迁入最终域模块的函数暂存于此，
随 S2/S3/S4 逐步搬空后删除。函数体与基线 aidev/projects.py 逐字节一致。
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import authn
from ..domain import make_event
from ..errors import OperationalError, PlatformError
from ..gitrepo import run_git
from ..jsonio import append_jsonl, new_id, utc_now

from .credentials import (
    ASKPASS_TEMPLATE,
    _shquote,
    apply_credential_to_clone,
    credential_required_for,
    git_auth_env,
    git_credential_metadata,
    remote_auth_args,
    upsert_git_credential,
    validate_credential,
)
from .clones import _git_template_dir
from .registry import (
    GIT_IDENTITY,
    PROJECT_MARKER,
    _read_project_json,
    clone_dir,
    put_project,
    validate_repo_url,
    write_project_marker,
)

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

# ---------------------------------------------------------------------------
# 引导骨架（AC-12/AC-13）
# ---------------------------------------------------------------------------

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
