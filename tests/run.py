"""按层运行入口（T26/REQ-1）：python -m tests.run {quick|integration|all} [--collect-only]。

分层依据 = 各 TestCase 类的 layer 类属性（打标机制，helpers.WorkflowTestCase
不设缺省）；all 收集 ≡ python -m unittest discover -s tests（既有全量方式不变）。
末行 JSON 结算 {"ok","layer","count"}；本文件非 test*.py 命名，不进 discover 收集面。
"""

import argparse
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
LAYERS = ("quick", "integration", "all")


def iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


def collect_cases(layer):
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR))
    return [
        case
        for case in iter_cases(suite)
        if layer == "all" or getattr(case, "layer", None) == layer
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tests.run")
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args(argv)

    cases = collect_cases(args.layer)
    ok = True
    if not args.collect_only:
        result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(cases))
        ok = result.wasSuccessful()

    print(json.dumps(
        {"ok": ok, "layer": args.layer, "count": len(cases)},
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
