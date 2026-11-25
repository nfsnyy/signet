import json
from pathlib import Path

from .. import distribution
from ..collaboration import collaboration_overview
from ..domain import SPEC_FILES, workspace_agents
from ..errors import NotFoundError, PlatformError
from ..jsonio import read_json, sha256_file
from ..readmodel import parse_requirement_text, pinned_acceptance_card


def project_metadata(repository):
    path = Path(repository) / "project.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    project_id = data.get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def stamp_events(repository, events):
    project_id = project_metadata(repository)
    if not project_id:
        return events
    for event in events:
        event.setdefault("project_id", project_id)
    return events


def normalize_req_items(req_items):
    """承载条目编号规范化：去空白、去重、升序存放（缺省 []）。"""
    return sorted({str(item).strip() for item in (req_items or []) if str(item or "").strip()})


def validate_workspace_pin(workspace, metadata):
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict):
        raise PlatformError("claim.spec_not_pinned", "task has no spec_pin")
    for key, filename in SPEC_FILES.items():
        expected = pin.get(key, {}).get("sha256")
        path = Path(workspace) / "contract" / filename
        if not expected or not path.is_file() or sha256_file(path) != expected:
            raise PlatformError("claim.spec_hash_mismatch", f"frozen file hash mismatch: {filename}")


def requirement_body(repository, requirement_id):
    """requirements/{id}/requirement.md 去 frontmatter 后的原文；无此需求返回 None。
    目录形态优先，缺失回落文件形态 requirements/<id>.md（T17 骨架引导需求）。"""
    if not requirement_id:
        return None
    file_path = Path(repository) / "requirements" / requirement_id / "requirement.md"
    if not file_path.is_file():
        file_path = Path(repository) / "requirements" / f"{requirement_id}.md"
        if not file_path.is_file():
            return None
    try:
        text = file_path.read_text(encoding="utf-8")
        _fields, body = parse_requirement_text(text)
    except (OSError, ValueError):
        return None
    return body


def spec_version_directory(repository, metadata):
    pin = metadata.get("spec_pin")
    if not isinstance(pin, dict) or not metadata.get("spec_id"):
        return None
    return Path(repository) / "specs" / metadata["spec_id"] / pin["acceptance_card"]["version"]


def spec_asset_manifest(repository, metadata):
    """钉扎规格版本目录内全部文件的下载清单（name=相对 POSIX 路径, sha256）。"""
    spec_directory = spec_version_directory(repository, metadata)
    if spec_directory is None or not spec_directory.is_dir():
        return []
    manifest = []
    for path in sorted(spec_directory.rglob("*")):
        if path.is_file():
            manifest.append({
                "name": path.relative_to(spec_directory).as_posix(),
                "sha256": sha256_file(path),
            })
    return manifest


def resolve_spec_asset(repository, metadata, name):
    """按名定位钉扎规格目录内的资产文件（拒绝目录穿越），不存在时 404。"""
    spec_directory = spec_version_directory(repository, metadata)
    relative = Path(name or "")
    if spec_directory is None:
        raise NotFoundError("task.asset_not_found", f"task has no frozen spec assets: {name}")
    if relative.is_absolute() or ".." in relative.parts or not str(name).strip():
        raise PlatformError("task.asset_invalid", f"invalid asset name: {name}")
    root = spec_directory.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise PlatformError("task.asset_invalid", f"asset escapes the spec directory: {name}")
    if not path.is_file():
        raise NotFoundError("task.asset_not_found", f"asset does not exist: {name}")
    return path


def task_inbox(repository, task_id):
    """发给本任务的协作消息（package.inbox；无协作面时为空列表）。"""
    try:
        messages = collaboration_overview(repository, task_id).get("messages") or []
    except (OSError, PlatformError):
        return []
    return [message for message in messages if message.get("to_task") == task_id]


def task_materials(repository, metadata):
    """任务包材料清单（REQ-6/AC-08）：[{name, via}]——via="package" 已含于本任务包
    （skill/requirement→requirement_body/acceptance_card/assets/hints→workspace_files），
    via 为绝对路径者经对应读端点匿名获取（AC-07 名册内规范材料）。T22/REQ-3（AC-08）：
    /api/norms/ 条目携 sha256（规范原文字节哈希），客户端落盘逐字节核验。"""
    materials = [
        {"name": "skill", "via": "package"},
        {"name": "requirement", "via": "package"},
        {"name": "acceptance_card", "via": "package"},
        {"name": "assets", "via": "package"},
    ]
    if (Path(repository) / "hints.md").is_file():
        materials.append({"name": "hints", "via": "package"})
    for norm in distribution.norm_roster():
        name = norm[:-3] if norm.endswith(".md") else norm
        document = distribution.read_norm(None, norm)
        materials.append({"name": name, "via": "/api/norms/" + norm, "sha256": document["sha256"]})
    return materials


def build_task_package(repository, metadata, body):
    """sigt 接管响应携带的完整任务包（AC-01）。

    workspace_files 由 CLI 原样落进工作区根：AGENTS.md 恒有；状态仓根存在
    hints.md 时一并注入（不存在则不注入、不报错——防御语义）；T15 起增
    .signet-skill.md（=skill.content，与 /api/skill、任务包 skill 字段三处同源）。
    """
    repository = Path(repository)
    workspace_files = {"AGENTS.md": workspace_agents(metadata)}
    hints_source = repository / "hints.md"
    if hints_source.is_file():
        try:
            workspace_files["hints.md"] = hints_source.read_text(encoding="utf-8")
        except OSError:
            pass
    # REQ-2/AC-02 + REQ-4/AC-04：skill 自举——恰两键 version/content，content 全文
    # 含注入标识行；三处分发同源同字节（read_skill 唯一事实源）。
    skill_document = distribution.read_skill()
    skill = {"version": skill_document["version"], "content": skill_document["content"]}
    workspace_files[".signet-skill.md"] = skill_document["content"]
    return {
        "task_id": metadata["task_id"],
        "title": metadata.get("title"),
        "state": metadata.get("state"),
        "metadata": metadata,
        "task_md": body,
        "acceptance_card": pinned_acceptance_card(repository, metadata),
        "requirement_body": requirement_body(repository, metadata.get("requirement")),
        "assets": spec_asset_manifest(repository, metadata),
        "inbox": task_inbox(repository, metadata["task_id"]),
        "lease_expires_at": (metadata.get("lease") or {}).get("expires_at"),
        "skill": skill,
        "materials": task_materials(repository, metadata),
        "workspace_files": workspace_files,
    }
