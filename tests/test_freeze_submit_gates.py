from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class FreezeSubmitGateTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.repository, _ = self.init_state_repo()
        self.artifacts = self.path("artifacts")
        self.card = self.artifacts / "acceptance-card.json"
        self.contract = self.artifacts / "contract.json"
        self.mapping = self.artifacts / "mapping.json"
        self.operations = self.artifacts / "operations.json"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.fixture = self.artifacts / "fixture.txt"
        self.fixture.write_text("fixture\n", encoding="utf-8", newline="\n")
        self.write_json(self.card, {"criteria": [{"criterion_id": "AC-001", "text": "标准", "strength": "must"}]})
        self.write_json(self.mapping, {"rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": False}]})
        self.write_json(self.operations, {"operations": ["op-a"]})
        self.write_json(self.contract, self.valid_contract())

    def valid_contract(self):
        return {
            "contract_type": "target",
            "operation_set": None,
            "assets": {},
            "cases": [{"case_id": "CASE-001", "command": ["python", "--version"], "fixtures": [], "expect": {"exit_code": 0}}],
            "environment": {"runner": "python", "dependency_files": [], "endpoints": {}, "allowed_env": []},
        }

    def freeze(self, task_id, workspace, operation_source=None):
        arguments = [
            "freeze",
            "--repo", self.repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", self.card,
            "--contract", self.contract,
            "--mapping", self.mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
        ]
        if operation_source:
            arguments.extend(["--operation-source", operation_source])
        return self.operator(arguments, expected_exit=2)

    def test_freeze_rejects_c1_c2_c3_asset_and_expectation_failures(self):
        cases = []

        contract = self.valid_contract()
        self.write_json(self.card, {"criteria": [
            {"criterion_id": "AC-001", "text": "标准", "strength": "must"},
            {"criterion_id": "AC-002", "text": "未映射标准", "strength": "must"},
        ]})
        cases.append((None, "c1.uncovered_criterion"))
        self.write_json(self.card, {"criteria": [{"criterion_id": "AC-001", "text": "标准", "strength": "must"}]})

        contract = self.valid_contract()
        contract["operation_set"] = ["op-a"]
        contract["cases"] = [dict(contract["cases"][0], operation="op-b")]
        self.write_json(self.contract, contract)
        cases.append((self.operations, "c2.operation_set_mismatch"))

        contract = self.valid_contract()
        contract["cases"] = [dict(contract["cases"][0], fixtures=["fixture.txt"])]
        self.write_json(self.contract, contract)
        cases.append((None, "c3.asset_hash_missing"))

        contract = self.valid_contract()
        contract["cases"] = [dict(contract["cases"][0], fixtures=["fixture.txt"])]
        contract["assets"] = {"fixture.txt": "0" * 64}
        self.write_json(self.contract, contract)
        cases.append((None, "c3.asset_hash_mismatch"))

        contract = self.valid_contract()
        contract["cases"] = [dict(contract["cases"][0], expect={})]
        self.write_json(self.contract, contract)
        cases.append((None, "c3.invalid_expectation"))

        for index, (operation_source, expected_code) in enumerate(cases):
            with self.subTest(index=index, expected_code=expected_code):
                workspace, task_id, _ = self.init_task(self.repository, f"gate-{index}")
                self.write_json(self.contract, self.valid_contract())
                self.write_json(self.card, {"criteria": [{"criterion_id": "AC-001", "text": "标准", "strength": "must"}]})
                if expected_code == "c1.uncovered_criterion":
                    self.write_json(self.card, {"criteria": [
                        {"criterion_id": "AC-001", "text": "标准", "strength": "must"},
                        {"criterion_id": "AC-002", "text": "未映射标准", "strength": "must"},
                    ]})
                elif expected_code == "c2.operation_set_mismatch":
                    contract = self.valid_contract()
                    contract["operation_set"] = ["op-a"]
                    contract["cases"] = [dict(contract["cases"][0], operation="op-b")]
                    self.write_json(self.contract, contract)
                elif expected_code == "c3.asset_hash_missing":
                    contract = self.valid_contract()
                    contract["cases"] = [dict(contract["cases"][0], fixtures=["fixture.txt"])]
                    self.write_json(self.contract, contract)
                elif expected_code == "c3.asset_hash_mismatch":
                    contract = self.valid_contract()
                    contract["cases"] = [dict(contract["cases"][0], fixtures=["fixture.txt"])]
                    contract["assets"] = {"fixture.txt": "0" * 64}
                    self.write_json(self.contract, contract)
                elif expected_code == "c3.invalid_expectation":
                    contract = self.valid_contract()
                    contract["cases"] = [dict(contract["cases"][0], expect={})]
                    self.write_json(self.contract, contract)

                result = self.freeze(task_id, workspace, operation_source)
                self.assertEqual(expected_code, result["error"]["code"])
                self.assertEqual("Draft", self.task_state(self.repository, task_id))

    def test_submit_rejects_invalid_report_and_empty_evidence(self):
        workspace, task_id, _ = self.init_task(self.repository, "submit")
        frozen = self.operator([
            "freeze",
            "--repo", self.repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", self.card,
            "--contract", self.contract,
            "--mapping", self.mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
        ])
        self.assertEqual("Ready", frozen["result"]["state"])
        self.claim_and_sync(self.repository, workspace)

        self.write_json(Path(workspace) / "report.json", {
            "implemented": "缺少 deviations",
            "executed": [],
            "not_executed": [],
            "collaboration_requests": [],
        })
        evidence = Path(workspace) / "evidence" / "valid.txt"
        evidence.write_text("evidence\n", encoding="utf-8", newline="\n")
        invalid = self.agent([
            "submit", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner",
            "--report", Path(workspace) / "report.json", "--evidence", evidence,
        ], expected_exit=2)
        self.assertEqual("submit.invalid_report", invalid["error"]["code"])

        self.write_json(Path(workspace) / "report.json", {
            "implemented": "完成",
            "executed": [{"command": ["python", "--version"], "output": "local"}],
            "not_executed": [],
            "deviations": [],
            "collaboration_requests": [],
        })
        empty = Path(workspace) / "evidence" / "empty.txt"
        empty.write_text("", encoding="utf-8")
        result = self.agent([
            "submit", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner",
            "--report", Path(workspace) / "report.json", "--evidence", empty,
        ], expected_exit=2)
        self.assertEqual("submit.evidence_empty", result["error"]["code"])

        submitted = self.submit_and_sync(self.repository, workspace)
        self.assertTrue(any(item.get("accepted") for item in submitted["result"]["results"]))
        self.assertEqual("Implemented", self.task_state(self.repository, task_id))

    def test_freeze_accepts_implemented_with_unfrozen_draft(self):
        # 起草流程：Draft → 编排 claim（claim_authoritative）→ 编排 submit
        # （submit_authoritative，sigt 直回流的服务端本体）→ Implemented 未钉扎
        # → operator 签署 freeze → Ready。
        from aidev.service import claim_authoritative, submit_authoritative
        workspace, task_id, _ = self.init_task(self.repository, "spec-draft")
        claim_authoritative(self.repository, task_id, "agent-a")
        self.assertEqual("In Progress", self.task_state(self.repository, task_id))
        report = Path(workspace) / "report.json"
        self.write_json(report, {
            "implemented": "起草三件套",
            "executed": [{"command": ["python", "--version"], "output": "local"}],
            "not_executed": [],
            "deviations": [],
            "collaboration_requests": [],
        })
        evidence = Path(workspace) / "evidence" / "spec.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("spec draft\n", encoding="utf-8", newline="\n")
        submit_authoritative(self.repository, task_id, "agent-a", report, [evidence], on_behalf_of="agent-a")
        self.assertEqual("Implemented", self.task_state(self.repository, task_id))

        frozen = self.operator([
            "freeze",
            "--repo", self.repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", self.card,
            "--contract", self.contract,
            "--mapping", self.mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
        ])
        self.assertEqual("Ready", frozen["result"]["state"])

        # 已钉扎的 Implemented 不得再走 freeze（须走 refreeze）。
        self.claim_and_sync(self.repository, workspace)
        self.submit_and_sync(self.repository, workspace)
        self.assertEqual("Implemented", self.task_state(self.repository, task_id))
        again = self.freeze(task_id, workspace)
        self.assertEqual("freeze.invalid_state", again["error"]["code"])
