import argparse
import logging
import os

import uvicorn

import aidev
from .. import distribution, skillguard
from .app import create_app


def startup_skill_check(platform_root=None, log=None):
    """启动自检（REQ-4/AC-05 safety-veto）：仓内 skill 标识与运行版本
    （aidev.__version__ + 平台仓 HEAD）漂移时打 WARNING 级日志（不得静默）并返回
    漂移报告 dict；一致返回 None。

    与启动流程解耦——用例直调本函数验证，不依赖真实启动副作用。仓内文件不强制
    含标识（AC-04：标识由 read_skill 读取时注入，天然与运行版本一致），故仓内
    无标识 = 一致；自检目标是检出「仓内标识与运行版本漂移」（如过期分发件回拷入仓）。
    """
    logger = log if log is not None else logging.getLogger("signet.startup")
    root = distribution.resolve_platform_root(platform_root)
    source = root / distribution.SKILL_RELATIVE
    if not source.is_file():
        report = {
            "kind": "skill_missing",
            "reason": f"skill 事实源缺失: {source}",
            "expected": str(distribution.SKILL_RELATIVE),
            "actual": "missing",
        }
        logger.warning("signet-skill 启动自检漂移：%s", report["reason"])
        return report
    text = source.read_text(encoding="utf-8")
    first_line, _, _ = text.partition("\n")
    if not first_line.startswith("<!-- signet-skill"):
        return None
    report = skillguard.check_skill_version_alignment(
        text, aidev.__version__, distribution.platform_head(root))
    if report is not None:
        logger.warning(
            "signet-skill 启动自检漂移：%s（expected=%r actual=%r）",
            report.get("reason"), report.get("expected"), report.get("actual"),
        )
    return report


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
    startup_skill_check()
    uvicorn.run(create_app(default_repo=arguments.repo), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
