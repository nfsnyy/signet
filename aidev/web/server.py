import argparse
import os

import uvicorn

from .app import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aidev-web", description="signet 平台 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("SIGNET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIGNET_PORT", "8000")))
    parser.add_argument(
        "--repo",
        default=None,
        help="服务端状态仓绑定（等同 SIGNET_STATE_REPO；请求未提供可用 repo 时的落点）",
    )
    arguments = parser.parse_args(argv)
    uvicorn.run(create_app(default_repo=arguments.repo), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
