import json
from pathlib import Path

from ..domain import SPEC_FILES
from ..errors import PlatformError
from ..jsonio import read_json, sha256_file


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
