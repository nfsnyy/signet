"""T16 返场回归：async verify 接线期间服务仍可响应其它请求。

verify_endpoint 是 async def，而 verify_task 为阻塞调用（git 提交 + 净室子
进程用例会经 HTTP 回调同一服务）。阻塞调用留在事件循环时，净室用例的自回调
全部 TimeoutError（operator 复现：verify-debug-service/*.log 全超时，直接运行
器 verify-debug/*.log 13/13 绿）。本测试经真实 HTTP CLI 走 verify --execute，
冻结用例子进程回调 /api/health，只有在事件循环可响应时才能通过——回归防护
verify_task 必须经线程池执行（run_in_threadpool）。
"""

import os

try:
    from .helpers import WorkflowTestCase, start_service
except ImportError:
    from helpers import WorkflowTestCase, start_service


class VerifyAsyncWiringTests(WorkflowTestCase):
    def test_verify_execute_keeps_service_responsive(self):
        repository, _ = self.init_state_repo()
        # 净室 allowed_env 从服务端进程环境取值；测试服务为同进程线程内实例，
        # 此处注入自引用地址供用例子进程回调。
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
        # 经真实 HTTP（CLI 子进程 → async verify_endpoint → 线程池 verify_task）：
        # 净室用例的 HTTP 自回调只有在事件循环未被阻塞时才能成功。
        self.operator([
            "verify", "--repo", repository, "--task-id", task_id, "--execute",
            "--verifier", "verifier-a", "--confirmed-by", "owner",
        ])
        self.assertEqual("Verified", self.task_state(repository, task_id))


if __name__ == "__main__":
    import unittest
    unittest.main()
