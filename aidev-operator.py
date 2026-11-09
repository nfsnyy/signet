"""signet 平台 operator 面入口（薄壳）。

仅做参数解析、HTTP 请求与 JSON 透传；业务逻辑在服务端 aidev.web。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signet_client import run


if __name__ == "__main__":
    raise SystemExit(run("operator"))
