import argparse
import os

import uvicorn

from .app import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aidev-web", description="signet 平台 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("SIGNET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIGNET_PORT", "8000")))
    arguments = parser.parse_args(argv)
    uvicorn.run(create_app(), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
