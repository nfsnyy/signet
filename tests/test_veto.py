import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class VetoTests(WorkflowTestCase):
    def veto_artifacts(self, task_id, safety_strength="safety-veto", safety_text="安全否决项：不得记录敏感信息", must_text="功能正确"):
        artifacts = self.path(f"artifacts-veto-{task_id}")
        card = artifacts / "acceptance-card.json"
        contract = artifacts / "contract.json"
        mapping = artifacts / "mapping.json"
        self.write_json(card, {"criteria": [
            {"criterion_id": "AC-001", "text": safety_text, "strength": safety_strength},
            {"criterion_id": "AC-002", "text": must_text, "strength": "must"},
        ]})
        self.write_json(contract, {
            "contract_type": "target",
            "operation_set": None,
            "assets": {},
            "cases": [
                {"case_id": "CASE-001", "command": ["python", "--version"], "fixtures": [], "expect": {"exit_code": 0}},
                {"case_id": "CASE-002", "command": ["python", "--version"], "fixtures": [], "expect": {"exit_code": 0}},
            ],
            "environment": {"runner": "python", "dependency_files": [], "endpoints": {}, "allowed_env": []},
        })
        self.write_json(mapping, {"rows": [
            {"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": False},
            {"criterion_id": "AC-002", "case_ids": ["CASE-002"], "manual": False},
        ]})
        return card, contract, mapping

    def freeze_with_artifacts(self, repository, workspace, task_id, card, contract, mapping):
        return self.operator([
            "freeze",
            "--repo", repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", card,
            "--contract", contract,
            "--mapping", mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
        ])

    def refreeze_with(self, repository, workspace, task_id, card, contract, mapping, expected_exit=0):
        return self.operator([
            "refreeze",
            "--repo", repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", card,
            "--contract", contract,
            "--mapping", mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
            "--reason", "受控变更",
        ], expected_exit=expected_exit)

    def propose_veto(self, repository, task_id, card, contract, mapping, actor="architect", expected_exit=0):
        return self.operator([
            "veto", "--propose",
            "--repo", repository,
            "--actor", actor,
            "--task-id", task_id,
            "--acceptance-card", card,
            "--contract", contract,
            "--mapping", mapping,
            "--reason", "安全面降级申请",
        ], expected_exit=expected_exit)

    def decide_veto(self, repository, veto_id, action, actor, reason=None, expected_exit=0):
        arguments = ["veto", f"--{action}", "--repo", repository, "--actor", actor, "--veto-id", veto_id]
        if reason:
            arguments.extend(["--reason", reason])
        return self.operator(arguments, expected_exit=expected_exit)

    def latest_veto(self, repository, veto_id):
        records = [json.loads(line) for line in (Path(repository) / "vetoes" / "vetoes.jsonl").read_text(encoding="utf-8").splitlines() if line]
        matches = [record for record in records if record.get("veto_id") == veto_id]
        return matches[-1] if matches else None

    def task_events(self, repository, task_id):
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        return [event for event in events if event.get("task_id") == task_id]

    def test_refreeze_touching_safety_surface_requires_veto(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "veto-gate")
        card, contract, mapping = self.veto_artifacts(task_id)
        self.freeze_with_artifacts(repository, workspace, task_id, card, contract, mapping)

        weakened_card, weakened_contract, weakened_mapping = self.veto_artifacts(task_id, safety_strength="must")
        blocked = self.refreeze_with(repository, workspace, task_id, weakened_card, weakened_contract, weakened_mapping, expected_exit=2)
        self.assertEqual("refreeze.veto_required", blocked["error"]["code"])
        self.assertEqual(["AC-001"], blocked["error"]["details"]["safety_criteria"])
        self.assertFalse((Path(repository) / "specs" / spec_id / "2").exists())

        unchanged_card, unchanged_contract, unchanged_mapping = self.veto_artifacts(task_id, must_text="功能正确（修订措辞）")
        not_required = self.propose_veto(repository, task_id, unchanged_card, unchanged_contract, unchanged_mapping, expected_exit=2)
        self.assertEqual("veto.not_required", not_required["error"]["code"])

    def test_veto_approve_then_refreeze_applies_invalidation(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "veto-approve")
        card, contract, mapping = self.veto_artifacts(task_id)
        self.freeze_with_artifacts(repository, workspace, task_id, card, contract, mapping)
        weakened_card, weakened_contract, weakened_mapping = self.veto_artifacts(task_id, safety_strength="must")

        proposed = self.propose_veto(repository, task_id, weakened_card, weakened_contract, weakened_mapping)
        veto_id = proposed["result"]["veto_id"]
        self.assertEqual("proposed", proposed["result"]["status"])
        self.assertEqual(["AC-001"], proposed["result"]["analysis"]["safety_criteria"])
        self.assertEqual("Ready", self.task_state(repository, task_id))

        self.assertIn("veto.self_approve", self.decide_veto(repository, veto_id, "approve", "architect", expected_exit=2)["error"]["code"])
        approved = self.decide_veto(repository, veto_id, "approve", "owner")
        self.assertEqual("approved", approved["result"]["status"])

        result = self.refreeze_with(repository, workspace, task_id, weakened_card, weakened_contract, weakened_mapping)
        self.assertEqual("2", result["result"]["version"])
        self.assertEqual("Ready", self.task_state(repository, task_id))

        marker = json.loads((Path(repository) / "specs" / spec_id / "1" / "INVALID.json").read_text(encoding="utf-8"))
        self.assertEqual("veto-downgrade", marker["reason"])
        self.assertEqual("2", marker["supersedes"])
        self.assertEqual(veto_id, marker["veto_id"])
        for filename in ("acceptance-card.json", "contract.json", "mapping.json"):
            self.assertTrue((Path(repository) / "specs" / spec_id / "1" / filename).is_file())

        resolved = self.latest_veto(repository, veto_id)
        self.assertEqual("approved", resolved["status"])
        self.assertIsNotNone(resolved["resolution"])
        self.assertEqual("2", resolved["resolution"]["new_version"])

        event_types = {event["event_type"] for event in self.task_events(repository, task_id)}
        self.assertIn("veto.proposed", event_types)
        self.assertIn("veto.approved", event_types)
        self.assertIn("veto.applied", event_types)
        applied = [event for event in self.task_events(repository, task_id) if event["event_type"] == "veto.applied"]
        self.assertEqual(veto_id, applied[0]["payload"]["veto_id"])

    def test_veto_expired_effective_settles_on_next_operator_command(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "veto-expire")
        card, contract, mapping = self.veto_artifacts(task_id)
        self.freeze_with_artifacts(repository, workspace, task_id, card, contract, mapping)
        weakened_card, weakened_contract, weakened_mapping = self.veto_artifacts(task_id, safety_strength="must")

        proposed = self.propose_veto(repository, task_id, weakened_card, weakened_contract, weakened_mapping)
        veto_id = proposed["result"]["veto_id"]

        log_path = Path(repository) / "vetoes" / "vetoes.jsonl"
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
        records[0]["delay_until"] = "2000-01-01T00:00:00.000Z"
        log_path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records), encoding="utf-8")

        self.operator(["log", "--repo", repository, "--task-id", task_id])
        settled = self.latest_veto(repository, veto_id)
        self.assertEqual("expired_effective", settled["status"])
        self.assertEqual("2000-01-01T00:00:00.000Z", settled["effective_at"])

        result = self.refreeze_with(repository, workspace, task_id, weakened_card, weakened_contract, weakened_mapping)
        self.assertEqual("2", result["result"]["version"])
        self.assertTrue((Path(repository) / "specs" / spec_id / "1" / "INVALID.json").is_file())
        event_types = {event["event_type"] for event in self.task_events(repository, task_id)}
        self.assertIn("veto.expired_effective", event_types)
        self.assertIn("veto.applied", event_types)

    def test_veto_withdraw_and_reject_rules(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "veto-decide")
        card, contract, mapping = self.veto_artifacts(task_id)
        self.freeze_with_artifacts(repository, workspace, task_id, card, contract, mapping)
        weakened_card, weakened_contract, weakened_mapping = self.veto_artifacts(task_id, safety_strength="must")

        proposed = self.propose_veto(repository, task_id, weakened_card, weakened_contract, weakened_mapping)
        veto_id = proposed["result"]["veto_id"]
        self.assertIn("veto.not_proposer", self.decide_veto(repository, veto_id, "withdraw", "owner", expected_exit=2)["error"]["code"])
        withdrawn = self.decide_veto(repository, veto_id, "withdraw", "architect")
        self.assertEqual("withdrawn", withdrawn["result"]["status"])
        self.assertIn("veto.invalid_state", self.decide_veto(repository, veto_id, "approve", "owner", expected_exit=2)["error"]["code"])

        second = self.propose_veto(repository, task_id, weakened_card, weakened_contract, weakened_mapping)
        second_id = second["result"]["veto_id"]
        self.assertIn("veto.self_reject", self.decide_veto(repository, second_id, "reject", "architect", reason="不应自我驳回", expected_exit=2)["error"]["code"])
        self.assertIn("veto.reason_required", self.decide_veto(repository, second_id, "reject", "owner", expected_exit=2)["error"]["code"])
        rejected = self.decide_veto(repository, second_id, "reject", "owner", reason="降级理由不充分")
        self.assertEqual("rejected", rejected["result"]["status"])

        still_blocked = self.refreeze_with(repository, workspace, task_id, weakened_card, weakened_contract, weakened_mapping, expected_exit=2)
        self.assertEqual("refreeze.veto_required", still_blocked["error"]["code"])
