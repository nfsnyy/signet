"""skill / norms / 客户端包分发读取（REQ-1 唯一事实源、REQ-6 平台可获取性）。

skill 唯一事实源 = 平台仓 skills/aidev-agent/SKILL.md；read_skill 读取时注入
标识行 <!-- signet-skill v{version}@{commit} -->（AC-04：解决 commit 自指
悖论，仓内文件不强制含标识）。norms 事实源 = 平台仓 doc/，v1 名册 =
三件套编写规范.md / 页面呈现规范.md / operator手册.md + prototype/ 全部
.html；read_norm 白名单 + 防穿越（AC-07：404 norm.not_found /
400 norm.invalid），并携原文字节 sha256（T22/REQ-3：任务包材料清单核验用）。

T22/REQ-1：build_client_package 构建版本化 zip 薄客户端包（EP-01 下载体、
EP-02 版本面）——钉名内容 = 双角色入口 + signet_client/ 七件 + manifest.json
+ 空 workspaces/ 骨架；确定性字节（固定条目序与时间戳），同一工作副本多次
构建字节一致；包内不含 skill/服务端 aidev/ 包/服务端密钥/需求规范材料。
"""
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

import aidev
from .errors import NotFoundError, PlatformError

SKILL_RELATIVE = Path("skills") / "aidev-agent" / "SKILL.md"
NORM_DOC_NAMES = ("三件套编写规范.md", "页面呈现规范.md", "operator手册.md")
MARKER_TEMPLATE = "<!-- signet-skill v{version}@{commit} -->"
MARKER_RE = re.compile(r"^<!-- signet-skill v([^@]+)@([0-9a-f]{7,40}) -->\n?$")


def resolve_platform_root(explicit=None):
    """平台仓根定位：显式入参 → SIGNET_PLATFORM_REPO → aidev 包自身检出。"""
    if explicit:
        return Path(explicit)
    env_root = os.environ.get("SIGNET_PLATFORM_REPO")
    if env_root:
        return Path(env_root)
    return Path(aidev.__file__).resolve().parent.parent


def platform_head(platform_root):
    """平台仓 HEAD 短哈希（>=7 位十六进制）。

    夹具副本（无 .git，用例硬编码 PLATFORM 路径对照真仓）回落到 aidev 包
    自身检出——被测代码即平台仓工作副本，标识 commit 取真仓 HEAD。
    """
    candidates = (Path(platform_root), Path(aidev.__file__).resolve().parent.parent)
    for candidate in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        head = (result.stdout or "").strip()
        if result.returncode == 0 and head:
            return head[:7]
    raise PlatformError(
        "platform.head_unavailable",
        f"无法解析平台仓 HEAD（{platform_root} 及其代码检出均无 git HEAD）",
    )


def read_skill(platform_root=None):
    """分发 skill 全文：标识行 + 仓内原文字节（AC-01/AC-04 钉名函数）。

    返回 {"version", "commit", "content", "source"}；content 首行为标识行，
    其余部分与仓内文件逐字节一致。
    """
    root = resolve_platform_root(platform_root)
    source = root / SKILL_RELATIVE
    if not source.is_file():
        raise NotFoundError("skill.not_found", f"skill 事实源缺失: {source}")
    raw = source.read_text(encoding="utf-8")
    version = aidev.__version__
    commit = platform_head(root)
    content = MARKER_TEMPLATE.format(version=version, commit=commit) + "\n" + raw
    return {"version": version, "commit": commit, "content": content, "source": str(source)}


def norm_roster(platform_root=None):
    """v1 名册（AC-07）：doc/ 三规范 md + prototype/ 全部 .html（POSIX 相对名）。"""
    root = resolve_platform_root(platform_root)
    doc_root = root / "doc"
    roster = []
    for name in NORM_DOC_NAMES:
        if (doc_root / name).is_file():
            roster.append(name)
    prototype_dir = doc_root / "prototype"
    if prototype_dir.is_dir():
        roster.extend(
            "prototype/" + path.name
            for path in sorted(prototype_dir.glob("*.html"))
            if path.is_file()
        )
    return roster


def read_norm(platform_root, name):
    """按白名单读取 doc/ 规范材料原文字节（AC-07 钉名函数）。

    未知名 → norm.not_found（404）；绝对路径 / .. 穿越 / 越出 doc/ 根 →
    norm.invalid（400，不读仓外文件）。
    """
    root = resolve_platform_root(platform_root)
    label = str(name or "")
    relative = Path(label)
    if not label.strip() or relative.is_absolute() or ".." in relative.parts:
        raise PlatformError("norm.invalid", f"norm 名不合法（空/绝对路径/穿越）: {name!r}")
    doc_root = root / "doc"
    if label not in set(norm_roster(root)):
        # 穿越已在上游拦截：这里区分「doc/ 命名空间内的未知名」与「doc/ 外路径」。
        try:
            candidate = (doc_root / relative).resolve()
            doc_resolved = doc_root.resolve()
        except OSError:
            raise PlatformError("norm.invalid", f"norm 路径不可解析: {name!r}")
        if candidate != doc_resolved and doc_resolved not in candidate.parents:
            raise PlatformError("norm.invalid", f"norm 路径越过 doc/ 根（非白名单）: {name!r}")
        raise NotFoundError("norm.not_found", f"norm 不在 v1 名册: {name}")
    target = doc_root / label
    raw = target.read_bytes()
    return {
        "name": label,
        "version": aidev.__version__,
        "commit": platform_head(root),
        "content": raw.decode("utf-8"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "path": str(target),
    }


# ---------------------------------------------------------------------------
# T22/REQ-1：版本化 zip 薄客户端包（EP-01 下载体 / EP-02 版本面）
# ---------------------------------------------------------------------------

CLIENT_ENTRY_FILES = ("aidev-agent.py", "aidev-operator.py")
CLIENT_PACKAGE_DIRECTORY = "signet_client"
# T24/REQ-6/7（AC-01/AC-09）：钉名成员清单同步——本机执行器 local_verify.py 入包
# （verify --execute 本机执行+上传），本机 ship 模块 legacy_ship.py 退役出包。
CLIENT_PACKAGE_MEMBERS = (
    "__init__.py",
    "http.py",
    "credentials.py",
    "projection.py",
    "local_verify.py",
    "requests.py",
    "cli.py",
)
CLIENT_WORKSPACES_ENTRY = "workspaces/"
CLIENT_MANIFEST_ENTRY = "manifest.json"
_ZIP_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def _zip_write_entry(archive, name, data, directory=False):
    """固定时间戳/权限位写入条目——同一输入字节流多次构建产物逐字节一致。"""
    info = zipfile.ZipInfo(name, date_time=_ZIP_FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 << 16) | 0x10) if directory else (0o644 << 16)
    archive.writestr(info, data)


def build_client_package(platform_root=None):
    """构建版本化 zip 薄客户端包（AC-01/AC-02 钉名函数）。

    钉名内容：双角色入口 aidev-agent.py / aidev-operator.py、signet_client/
    七件钉名成员、manifest.json（client_version/api_version/files[] 逐文件
    sha256，只覆盖文件条目）、空 workspaces/ 骨架。包内不含 skill、服务端
    aidev/ 包、服务端密钥与需求/规范材料。返回 {"content", "client_version",
    "api_version", "sha256", "size"}；content 确定性（固定条目序 + 固定时间戳），
    版本端点 sha256/size 与下载端点字节因此恒等。
    """
    root = resolve_platform_root(platform_root)
    files = []
    for entry in CLIENT_ENTRY_FILES:
        source = root / entry
        if not source.is_file():
            raise PlatformError("client.source_missing", f"客户端入口文件缺失: {source}")
        files.append((entry, source.read_bytes()))
    for member in CLIENT_PACKAGE_MEMBERS:
        source = root / CLIENT_PACKAGE_DIRECTORY / member
        if not source.is_file():
            raise PlatformError("client.source_missing", f"客户端模块成员缺失: {source}")
        files.append((f"{CLIENT_PACKAGE_DIRECTORY}/{member}", source.read_bytes()))
    files.sort(key=lambda item: item[0])
    manifest = {
        "client_version": aidev.__version__,
        "api_version": aidev.API_VERSION,
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _zip_write_entry(archive, f"{CLIENT_PACKAGE_DIRECTORY}/", b"", directory=True)
        _zip_write_entry(archive, CLIENT_WORKSPACES_ENTRY, b"", directory=True)
        for name, data in files:
            _zip_write_entry(archive, name, data)
        _zip_write_entry(
            archive,
            CLIENT_MANIFEST_ENTRY,
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    content = buffer.getvalue()
    return {
        "content": content,
        "client_version": manifest["client_version"],
        "api_version": manifest["api_version"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
