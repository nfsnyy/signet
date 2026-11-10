import json
from pathlib import Path

from aidev.jsonio import sha256_file

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase

SPEC_KEYS = {
    "acceptance-card.json": "acceptance_card",
    "contract.json": "contract",
    "mapping.json": "mapping",
}


class RefreezeTests(WorkflowTestCase):
    def revised_artifacts(self, task_id, criterion_text="修订后的标准"):
        artifacts = self.path(f"artifacts-v2-{task_id}")
        card = artifacts / "acceptance-card.json"
        contract = artifacts / "contract.json"
        mapping = artifacts / "mapping.json"
        self.write_json(card, {"criteria": [{"criterion_id": "AC-001", "text": criterion_text, "strength": "must"}]})
        self.write_json(contract, {
            "contract_type": "target",
            "operation_set": None,
            "assets": {},
            "cases": [{"case_id": "CASE-001", "command": ["python", "--version"], "fixtures": [], "expect": {"exit_code": 0}}],
            "environment": {"runner": "python", "dependency_files": [], "endpoints": {}, "allowed_env": []},
        })
        self.write_json(mapping, {"rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": False}]})
        return card, contract, mapping

    def refreeze(self, repository, workspace, task_id, card, contract, mapping, reason="规格修订", expected_exit=0):
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
            "--reason", reason,
        ], expected_exit=expected_exit)

    def test_refreeze_from_in_progress_supersedes_old_version_and_releases_lease(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "refreeze")
        self.freeze_target(repository, workspace, task_id)
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        old_pin = metadata["spec_pin"]
        self.claim_and_sync(repository, workspace)
        self.assertEqual("In Progress", self.task_state(repository, task_id))

        card, contract, mapping = self.revised_artifacts(task_id)
        result = self.refreeze(repository, workspace, task_id, card, contract, mapping)

        self.assertEqual("1", result["result"]["old_version"])
        self.assertEqual("2", result["result"]["version"])
        self.assertEqual("1", result["result"]["supersedes"])
        self.assertEqual("Ready", result["result"]["state"])

        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        self.assertEqual("Ready", metadata["state"])
        self.assertIsNone(metadata["lease"])
        new_pin = metadata["spec_pin"]
        self.assertEqual("2", new_pin["acceptance_card"]["version"])
        self.assertNotEqual(old_pin["acceptance_card"]["sha256"], new_pin["acceptance_card"]["sha256"])

        for filename, key in SPEC_KEYS.items():
            old_file = Path(repository) / "specs" / spec_id / "1" / filename
            self.assertTrue(old_file.is_file(), f"old version must be kept: {old_file}")
            self.assertEqual(old_pin[key]["sha256"], sha256_file(old_file))

        version_document = json.loads((Path(repository) / "specs" / spec_id / "2" / "version.json").read_text(encoding="utf-8"))
        self.assertEqual("1", version_document["supersedes"])
        self.assertEqual("规格修订", version_document["reason"])

        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        refrozen = [event for event in events if event.get("task_id") == task_id and event.get("event_type") == "specification.refrozen"]
        self.assertEqual(1, len(refrozen))
        self.assertEqual("1", refrozen[0]["payload"]["old_version"])
        self.assertEqual("2", refrozen[0]["payload"]["version"])
        self.assertEqual("In Progress", refrozen[0]["before"])
        self.assertEqual("Ready", refrozen[0]["after"])

        workspace_metadata, _ = self.read_task(Path(workspace) / "task.md")
        self.assertEqual("Ready", workspace_metadata["state"])
        self.assertEqual(new_pin, workspace_metadata["spec_pin"])
        for filename, key in SPEC_KEYS.items():
            self.assertEqual(new_pin[key]["sha256"], sha256_file(Path(workspace) / "contract" / filename))

        self.claim_and_sync(repository, workspace)
        self.assertEqual("In Progress", self.task_state(repository, task_id))

    def test_refreeze_draft_task_without_pin_freezes_as_v2(self):
        # refreeze v2 补卡（T6）：Draft 未冻结任务允许 refreeze——以虚拟 v1 基线
        # 直接冻结为 v2，事件与元数据口径与常规 refreeze 一致。
        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "refreeze-draft")
        card, contract, mapping = self.revised_artifacts(task_id)
        result = self.refreeze(repository, workspace, task_id, card, contract, mapping)
        pin = result["result"]["spec_pin"]
        self.assertEqual("2", pin["acceptance_card"]["version"])
        self.assertEqual("1", result["result"]["supersedes"])
        self.assertEqual("Ready", self.task_state(repository, task_id))
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        self.assertEqual(pin, metadata["spec_pin"])
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        confirmed = [e for e in events if e.get("task_id") == task_id and e["event_type"] == "specification.content_confirmed"]
        self.assertTrue(any(e.get("payload", {}).get("version") == "2" for e in confirmed))

    def test_refreeze_rejects_invalid_three_piece_and_keeps_old_version(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "refreeze-invalid")
        self.freeze_target(repository, workspace, task_id)
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        old_pin = metadata["spec_pin"]

        card, contract, mapping = self.revised_artifacts(task_id)
        self.write_json(mapping, {"rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-404"], "manual": False}]})
        result = self.refreeze(repository, workspace, task_id, card, contract, mapping, expected_exit=2)
        self.assertEqual("c1.unknown_case", result["error"]["code"])

        self.assertFalse((Path(repository) / "specs" / spec_id / "2").exists())
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        self.assertEqual(old_pin, metadata["spec_pin"])
        self.assertEqual("Ready", metadata["state"])

    def test_refreeze_rejects_final_states(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "refreeze-final")
        self.freeze_target(repository, workspace, task_id)
        self.operator([
            "reject", "--repo", repository, "--task-id", task_id,
            "--category", "input", "--reason", "输入前提无效", "--actor", "architect",
        ])
        self.assertEqual("Void", self.task_state(repository, task_id))
        card, contract, mapping = self.revised_artifacts(task_id)
        result = self.refreeze(repository, workspace, task_id, card, contract, mapping, expected_exit=2)
        self.assertEqual("refreeze.invalid_state", result["error"]["code"])
