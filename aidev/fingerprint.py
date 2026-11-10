"""工作区指纹：平台仓（SIGNET_PLATFORM_REPO）跟踪文件的聚合哈希。

算法与验收夹具 t6_common.workspace_fingerprint 逐字节一致：
git ls-files 输出按 path 排序，取 {path: sha256} 结构包一层
{"files": {...}} 后以紧凑分隔符 JSON 序列化，再做 sha256。
verify 落账、ship 比对共用本函数，保证同一份事实源。
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path


def workspace_fingerprint(platform_repo=None):
    root = Path(platform_repo or os.environ.get("SIGNET_PLATFORM_REPO") or "")
    if not str(root) or not (root / ".git").exists():
        return None
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if listing.returncode != 0:
        return None
    files = {}
    for line in sorted((listing.stdout or "").splitlines()):
        if line.strip():
            full = root / line.strip()
            if full.is_file():
                files[line.strip()] = hashlib.sha256(full.read_bytes()).hexdigest()
    canonical = json.dumps({"files": files}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
