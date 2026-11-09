"""signet 平台管理入口（第三入口）：本地账号管理。

直接读写服务端数据目录 ~/.signet/（users.json、service-audit.jsonl），
不经过 HTTP 服务；新建账号下一次登录请求即被服务认可（服务每次登录
实时读取 users.json）。管理动作追加写 service-audit.jsonl。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aidev import authn
from aidev.errors import PlatformError


def emit(payload):
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="aidev-admin", description="signet 平台本地账号管理（服务端数据目录 ~/.signet/）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-user", help="新建账号")
    create.add_argument("--user", required=True)
    create.add_argument("--password", required=True)

    disable = subparsers.add_parser("disable-user", help="停用账号")
    disable.add_argument("--user", required=True)

    reset = subparsers.add_parser("reset-password", help="重置口令")
    reset.add_argument("--user", required=True)
    reset.add_argument("--password", required=True)

    subparsers.add_parser("list-users", help="列出账号")

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create-user":
            result = authn.create_user(arguments.user, arguments.password)
        elif arguments.command == "disable-user":
            result = authn.disable_user(arguments.user)
        elif arguments.command == "reset-password":
            result = authn.set_user_password(arguments.user, arguments.password)
        else:
            result = authn.list_users()
    except PlatformError as error:
        emit({"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details}})
        return 3
    emit({"ok": True, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
