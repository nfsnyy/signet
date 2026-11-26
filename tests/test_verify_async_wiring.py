"""T16 返场回归：async verify 接线期间服务仍可响应其它请求。

T24/REQ-6 起 verify --execute 迁移为 operator 本机执行+上传（服务端净室退役，
execute=true 一律 400 verify.execute_retired）：冻结用例改在 CLI 侧子进程执行，
其 HTTP 自回调（/api/health）经 SIGNET_BASE_URL 指向被测服务——本测试经真实
HTTP CLI 走 verify --execute，用例回调只有在服务事件循环可响应时才能通过，
同时覆盖「本机执行 → 上传 → 服务端落账 Verified」全链路。服务端 verify_task
仍经线程池执行（run_in_threadpool，T16 接线不变）。
"""

import os

try:
    from .helpers import WorkflowTestCase, start_service
except ImportError:
    from helpers import WorkflowTestCase, start_service


class VerifyAsyncWiringTests(WorkflowTestCase):
    def test_verify_execute_keeps_service_responsive(self):
        repository, _ = self.init_state_repo()
        # 净室 allowed_env 从 CLI 进程环境取值（T24 起用例在 CLI 侧本机执行）；
        # 测试服务为同进程线程内实例，此处注入自引用地址供用例子进程回调。
        os.environ["SIGNET_BASE_URL"] = start_service()
        workspace, task_id, _ = self.init_task(repository, "verify-async")
        artifacts = self.path("verify-async-artifacts")
        artifacts.mkdir(parents=True, exist_ok=True)
        card = artifacts / "acceptance-card.json"
        contract = artifacts / "contract.json"
        mapping = artifacts / "mapping.json"
        self.write_json(card, {"criteria": [{"criterion_id": "AC-001", "text": "verify 期间服务可响应自回调", "strength": "must"}]})
        self.write_json(contract, {
            "contract_type": "target",
            "operation_set": None,
            "assets": {},
            "cases": [{
                "case_id": "CASE-CALLBACK",
                "command": ["python", "-c",
                            "import os,urllib.request;"
                            "print(urllib.request.urlopen(os.environ['SIGNET_BASE_URL']+'/api/health', timeout=15).status)"],
                "fixtures": [],
                "expect": {"exit_code": 0, "stdout_contains": "200"},
            }],
            "environment": {
                "runner": "python",
                "dependency_files": [],
                "endpoints": {},
                "allowed_env": ["SIGNET_BASE_URL", "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
                                "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP"],
            },
        })
        self.write_json(mapping, {"rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-CALLBACK"], "manual": False}]})
        self.operator([
            "freeze", "--repo", repository, "--task-id", task_id, "--workspace", workspace,
            "--acceptance-card", card, "--contract", contract, "--mapping", mapping,
            "--h1-actor", "owner", "--h2-actor", "architect",
        ])
        self.claim_and_sync(repository, workspace)
        self.submit_and_sync(repository, workspace)
        self.assertEqual("Implemented", self.task_state(repository, task_id))
        # 经真实 HTTP（CLI 子进程本机执行用例 → 上传 → async verify_endpoint →
        # 线程池 verify_task）：用例的 HTTP 自回调只有在服务可响应时才能成功。
        self.operator([
            "verify", "--repo", repository, "--task-id", task_id, "--execute",
            "--verifier", "verifier-a", "--confirmed-by", "owner",
        ])
        self.assertEqual("Verified", self.task_state(repository, task_id))


if __name__ == "__main__":
    import unittest
    unittest.main()
