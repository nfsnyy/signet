import hashlib
import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class ExecutionAndBaselineTests(WorkflowTestCase):
    def write_executable_contract(self, name, command, stdout_contains, contract_type="target", fixture_text=None):
        artifacts = self.path(f"{name}-artifacts")
        fixture = artifacts / "fixtures" / "hello.txt"
        fixtures = []
        assets = {}
        if fixture_text is not None:
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(fixture_text, encoding="utf-8", newline="\n")
            fixtures = ["fixtures/hello.txt"]
            assets = {"fixtures/hello.txt": hashlib.sha256(fixture.read_bytes()).hexdigest()}
        card = self.write_json(artifacts / "acceptance-card.json", {
            "criteria": [{"criterion_id": "AC-001", "text": "用例通过", "strength": "must"}]
        })
        contract = self.write_json(artifacts / "contract.json", {
            "contract_type": contract_type,
            "operation_set": None,
            "assets": assets,
            "cases": [{
                "case_id": "CASE-001",
                "command": ["python", "-c", command],
                "fixtures": fixtures,
                "expect": {"exit_code": 0, "stdout_contains": stdout_contains},
            }],
            "environment": {
                "runner": "python",
                "dependency_files": [],
                "endpoints": {},
                "allowed_env": ["SystemRoot"],
            },
        })
        mapping = self.write_json(artifacts / "mapping.json", {
            "rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": False}]
        })
        return card, contract, mapping

    def freeze_with_artifacts(self, repository, workspace, task_id, artifacts, push=False, expected_exit=0):
        card, contract, mapping = artifacts
        arguments = [
            "freeze",
            "--repo", repository,
            "--task-id", task_id,
            "--workspace", workspace,
            "--acceptance-card", card,
            "--contract", contract,
            "--mapping", mapping,
            "--h1-actor", "owner",
            "--h2-actor", "architect",
        ]
        if push:
            arguments.append("--push")
        return self.operator(arguments, expected_exit=expected_exit)

    def prepare_implemented_task(self, name, artifacts, repo_name="repo"):
        repository, _ = self.init_state_repo(repo_name)
        workspace, task_id, spec_id = self.init_task(repository, name)
        frozen = self.freeze_with_artifacts(repository, workspace, task_id, artifacts)
        self.assertEqual("Ready", frozen["result"]["state"])
        self.claim_and_sync(repository, workspace)
        submitted = self.submit_and_sync(repository, workspace)
        self.assertTrue(any(item.get("accepted") for item in submitted["result"]["results"]))
        return repository, workspace, task_id, spec_id

    def test_verify_execute_uses_frozen_assets_and_writes_machine_evidence(self):
        artifacts = self.write_executable_contract(
            "execute-success",
            "from pathlib import Path; print('fixture-ok:' + Path('fixtures/hello.txt').read_text().strip())",
            "fixture-ok:hello",
            fixture_text="hello\n",
        )
        repository, workspace, task_id, spec_id = self.prepare_implemented_task("execute-success", artifacts)
        polluted = Path(workspace) / "fixtures" / "hello.txt"
        polluted.parent.mkdir(exist_ok=True)
        polluted.write_text("workspace pollution\n", encoding="utf-8", newline="\n")

        self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--execute", "--verifier", "verifier-a", "--confirmed-by", "owner",
        ])
        self.assertEqual("Verified", self.task_state(repository, task_id))
        log = Path(repository) / "tasks" / task_id / "verification" / "CASE-001.log"
        self.assertTrue(log.is_file())
        self.assertIn("fixture-ok:hello", log.read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        initial = [event for event in events if event.get("task_id") == task_id and event["event_type"] == "verification.initial"]
        self.assertEqual(1, len(initial))
        self.assertTrue(initial[0]["payload"]["executed"])

    def test_verify_execute_rejects_failed_case_and_tampered_asset(self):
        failure_artifacts = self.write_executable_contract("execute-failure", "print('actual-output')", "expected-missing")
        repository, workspace, task_id, _ = self.prepare_implemented_task("execute-failure", failure_artifacts)
        failed = self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--execute", "--verifier", "verifier-a", "--confirmed-by", "owner",
        ], expected_exit=2)
        self.assertEqual("verify.failed_criterion", failed["error"]["code"])
        self.assertEqual("Implemented", self.task_state(repository, task_id))

        tamper_artifacts = self.write_executable_contract(
            "execute-tamper",
            "print('fixture-ok:' + Path('fixtures/hello.txt').read_text().strip())",
            "fixture-ok:hello",
            fixture_text="hello\n",
        )
        repository, workspace, task_id, spec_id = self.prepare_implemented_task("execute-tamper", tamper_artifacts, repo_name="tamper-repo")
        frozen_fixture = Path(repository) / "specs" / spec_id / "1" / "fixtures" / "hello.txt"
        frozen_fixture.write_text("tampered\n", encoding="utf-8", newline="\n")
        tampered = self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--execute", "--verifier", "verifier-a", "--confirmed-by", "owner",
        ], expected_exit=2)
        self.assertEqual("verify.asset_hash_mismatch", tampered["error"]["code"])

    def test_baseline_freeze_saves_evidence_and_failed_baseline_is_cleaned(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "baseline-success")
        success = self.write_executable_contract("baseline-success", "print('base-ok')", "base-ok", contract_type="baseline")
        result = self.freeze_with_artifacts(repository, workspace, task_id, success)
        self.assertEqual("Ready", result["result"]["state"])
        evidence = Path(repository) / "specs" / spec_id / "1" / "baseline-evidence" / "CASE-001.log"
        self.assertTrue(evidence.is_file())
        self.assertIn("base-ok", evidence.read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(any(event.get("task_id") == task_id and event.get("event_type") == "specification.baseline_verified" for event in events))
        committed = self.git(["-C", repository, "show", "--pretty=", "--name-only", "HEAD"]).splitlines()
        self.assertTrue(any(path.endswith("baseline-evidence/CASE-001.log") for path in committed))

        failure_workspace, failure_task, failure_spec = self.init_task(repository, "baseline-failure")
        failure = self.write_executable_contract("baseline-failure", "print('actual')", "expected-missing", contract_type="baseline")
        before = self.git(["-C", repository, "rev-parse", "HEAD"]).strip()
        result = self.freeze_with_artifacts(repository, failure_workspace, failure_task, failure, expected_exit=2)
        self.assertEqual("c5.case_failed", result["error"]["code"])
        self.assertFalse((Path(repository) / "specs" / failure_spec).exists())
        self.assertEqual(before, self.git(["-C", repository, "rev-parse", "HEAD"]).strip())
