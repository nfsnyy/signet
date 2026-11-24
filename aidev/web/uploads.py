import hashlib
import json
from pathlib import Path

from ..errors import PlatformError


def save_upload(upload, directory, fallback_name):
    if upload is None:
        return None
    filename = Path(upload.filename or fallback_name).name or fallback_name
    target = Path(directory) / filename
    target.write_bytes(upload.file.read())
    return str(target)


def parse_asset_manifest(manifest):
    """AC-05（api-remote-ready）：asset_manifest 目标形态——[{"path","sha256"}]
    对象列表；旧纯字符串列表或条目缺键一律 c3.invalid_fixture；路径越界
    c3.invalid_asset_path。返回 [(path, sha256), ...]。"""
    try:
        entries = json.loads(manifest or "[]")
    except json.JSONDecodeError:
        raise PlatformError("c3.invalid_fixture", "asset manifest is not valid JSON")
    if not isinstance(entries, list):
        raise PlatformError("c3.invalid_fixture", "asset manifest must be a list")
    parsed = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PlatformError("c3.invalid_fixture", "each asset manifest entry must be an object with path and sha256")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative.strip() or not isinstance(digest, str) or len(digest) != 64:
            raise PlatformError("c3.invalid_fixture", "each asset manifest entry must declare path and a 64-hex sha256")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise PlatformError("c3.invalid_asset_path", f"asset escapes the spec directory: {relative}")
        parsed.append((relative_path.as_posix(), digest))
    return parsed


def _contract_asset_declarations(contract_path):
    """宽容解析上传的 contract.json 取 assets 声明；解析失败返回 None——
    契约合法性由 freeze_task 的 c2.invalid_contract 统一裁决。"""
    try:
        document = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    assets = document.get("assets")
    return assets if isinstance(assets, dict) else {}


def materialize_assets(uploads, manifest, temporary, contract_path=None):
    """AC-04/AC-05：资产内容上传——逐件校验 sha256：manifest 摘要 vs 上传字节、
    contract 声明 vs 上传字节，任一不符 → c3.content_hash_mismatch 整次拒绝；
    通过后写入请求级临时目录。"""
    entries = parse_asset_manifest(manifest)
    declared = _contract_asset_declarations(contract_path) if contract_path else {}
    uploads = uploads or []
    if len(uploads) != len(entries):
        raise PlatformError("c3.invalid_fixture", "uploaded assets do not match the contract asset manifest")
    for (relative, digest), upload in zip(entries, uploads):
        content = upload.file.read()
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            raise PlatformError("c3.content_hash_mismatch", f"asset content hash mismatch: {relative}")
        declared_digest = declared.get(relative)
        if declared_digest is not None and declared_digest != actual:
            raise PlatformError(
                "c3.content_hash_mismatch",
                f"declared asset hash does not match the uploaded content: {relative}",
            )
        target = Path(temporary) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def verify_upload_digests(card_path, contract_path, mapping_path, card_sha256, contract_sha256, mapping_sha256):
    """AC-05：三件套随件摘要校验——任一摘要值与上传字节不符 →
    c3.content_hash_mismatch 整次拒绝（零副作用：不写规格目录/不落事件）。"""
    expected = {
        "card_sha256": (card_path, card_sha256),
        "contract_sha256": (contract_path, contract_sha256),
        "mapping_sha256": (mapping_path, mapping_sha256),
    }
    for field, (path, digest) in expected.items():
        if not digest or not path:
            raise PlatformError("request.invalid", f"{field} is required with the uploaded specification files")
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != digest:
            raise PlatformError(
                "c3.content_hash_mismatch",
                f"{field} does not match the uploaded content",
                details={"field": field, "expected": digest, "actual": actual},
            )
