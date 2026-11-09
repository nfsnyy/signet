"""环境自检脚本：输出平台运行环境摘要（JSON）。

用法：
    python scripts/check_env.py [--require-version X.Y[.Z]]

正常情况 exit 0，stdout 为：
    {"ok": true, "checks": {"python": "<版本>", "git": "<版本>", "project_json": "<project_id 或 null>"}}
环境不满足（如 --require-version 高于本机 Python）时 exit 非零，stdout 为：
    {"ok": false, "reason": "<非空说明>"}

仓库根定位：优先取环境变量 SIGNET_PLATFORM_REPO，否则取本脚本所在仓库根。
"""
import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("SIGNET_PLATFORM_REPO")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def emit(payload):
    print(json.dumps(payload, ensure_ascii=True))


def fail(reason):
    emit({"ok": False, "reason": reason})
    sys.exit(1)


def check_required_version(spec):
    parts = spec.split(".")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        fail(f"--require-version 格式无效: {spec!r}（应为 X.Y 或 X.Y.Z）")
    required = tuple(int(p) for p in parts)
    current = tuple(sys.version_info[: len(required)])
    if current < required:
        fail(
            f"Python 版本不满足要求: 当前 {platform.python_version()} < 要求 {spec}"
        )


def _git_candidates():
    import shutil

    found = shutil.which("git")
    if found:
        yield found
    if platform.system() == "Windows":
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\git.exe"
                ) as key:
                    yield winreg.QueryValueEx(key, None)[0]
            except OSError:
                pass
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                local_appdata = winreg.QueryValueEx(key, "Local AppData")[0]
            yield str(Path(local_appdata) / "Programs" / "Git" / "cmd" / "git.exe")
        except OSError:
            pass
        for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
            yield str(Path(base) / "Git" / "cmd" / "git.exe")
    else:
        for candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
            yield candidate


def git_version():
    for candidate in _git_candidates():
        try:
            proc = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            fail(f"git --version 退出码非零: {proc.stderr.strip()[:120]}")
        tokens = proc.stdout.strip().split()
        if len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "version":
            return tokens[2]
        fail(f"无法解析 git --version 输出: {proc.stdout.strip()[:120]}")
    fail("未找到 git（PATH 及常见安装位置均不可用）")


def project_id(root):
    try:
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        fail(f"project.json 读取或解析失败: {exc}")
    value = data.get("project_id")
    return value if isinstance(value, str) and value else None


def main():
    parser = argparse.ArgumentParser(description="平台环境自检")
    parser.add_argument(
        "--require-version",
        metavar="X.Y[.Z]",
        help="要求的最低 Python 版本，不满足则非零退出",
    )
    args = parser.parse_args()

    if args.require_version:
        check_required_version(args.require_version)

    checks = {
        "python": platform.python_version(),
        "git": git_version(),
        "project_json": project_id(repo_root()),
    }
    emit({"ok": True, "checks": checks})
    sys.exit(0)


if __name__ == "__main__":
    main()
