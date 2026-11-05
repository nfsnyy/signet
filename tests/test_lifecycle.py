import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class LifecycleTests(WorkflowTestCase):
    def verification_document(self, repository, task_id, verifier, passed=True):
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        return {
            "verifier": verifier,
            "spec_pin": metadata["spec_pin"],
            "results": [{
                "criterion_id": "AC-001",
                "passed": passed,
                "evidence": [{"path": "verification.txt", "sha256": "x" * 64}],
            }],
        }

    def test_block_verify_close_and_reject_paths(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "lifecycle")
        self.freeze_target(repository, workspace, task_id)
        self.claim_and_sync(repository, workspace)

        enhancement = self.agent([
            "enhancement",
            "--workspace", workspace,
            "--actor", "agent-a",
            "--on-behalf-of", "owner",
            "--title", "后续事项",
            "--details", "不应改变当前范围",
        ])
        self.assertFalse(enhancement["result"]["authoritative"])
        synced = self.operator(["sync", "--repo", repository, "--workspace", workspace, "--actor", "owner"])
        self.assertTrue(any(item.get("accepted") for item in synced["result"]["results"]))
        self.assertEqual("In Progress", self.task_state(repository, task_id))

        block_evidence = Path(workspace) / "evidence" / "block.txt"
        block_evidence.write_text("block evidence\n", encoding="utf-8", newline="\n")
        self.agent([
            "block",
            "--workspace", workspace,
            "--actor", "agent-a",
            "--on-behalf-of", "owner",
            "--reason", "定义与现实矛盾",
            "--evidence", block_evidence,
        ])
        blocked = self.operator(["sync", "--repo", repository, "--workspace", workspace, "--actor", "owner"])
        self.assertTrue(any(item.get("accepted") for item in blocked["result"]["results"]))
        self.assertEqual("Blocked", self.task_state(repository, task_id))
        metadata, _ = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        self.assertIsNone(metadata["lease"])
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(any(event.get("task_id") == task_id and event.get("event_type") == "discussion.opened" for event in events))

        self.operator([
            "adjudicate",
            "--repo", repository,
            "--task-id", task_id,
            "--category", "implementation",
            "--reason", "实现问题",
            "--actor", "architect",
            "--workspace", workspace,
        ])
        self.assertEqual("Ready", self.task_state(repository, task_id))
        workspace_metadata, _ = self.read_task(Path(workspace) / "task.md")
        self.assertEqual("Ready", workspace_metadata["state"])

        self.claim_and_sync(repository, workspace)
        submitted = self.submit_and_sync(repository, workspace)
        self.assertTrue(any(item.get("accepted") for item in submitted["result"]["results"]))
        self.assertEqual("Implemented", self.task_state(repository, task_id))

        self.write_json(Path(workspace) / "verify-self.json", self.verification_document(repository, task_id, "agent-a"))
        self_review = self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--results", Path(workspace) / "verify-self.json",
            "--verifier", "agent-a", "--confirmed-by", "owner",
        ], expected_exit=2)
        self.assertEqual("verify.self_review", self_review["error"]["code"])

        self.write_json(Path(workspace) / "verify-failed.json", self.verification_document(repository, task_id, "verifier-a", passed=False))
        failed = self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--results", Path(workspace) / "verify-failed.json",
            "--verifier", "verifier-a", "--confirmed-by", "owner",
        ], expected_exit=2)
        self.assertEqual("verify.failed_criterion", failed["error"]["code"])
        self.assertEqual("Implemented", self.task_state(repository, task_id))

        self.write_json(Path(workspace) / "verify.json", self.verification_document(repository, task_id, "verifier-a"))
        self.operator([
            "verify", "--repo", repository, "--task-id", task_id,
            "--results", Path(workspace) / "verify.json",
            "--verifier", "verifier-a", "--confirmed-by", "owner",
        ])
        self.assertEqual("Verified", self.task_state(repository, task_id))
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        task_events = [event for event in events if event.get("task_id") == task_id]
        initial = [event for event in task_events if event["event_type"] == "verification.initial"]
        confirm = [event for event in task_events if event["event_type"] == "verification.confirm"]
        self.assertEqual(1, len(initial))
        self.assertEqual(1, len(confirm))
        self.assertEqual("verifier-a", initial[0]["actor"])
        self.assertEqual("owner", confirm[0]["actor"])

        self.operator([
            "close", "--repo", repository, "--task-id", task_id,
            "--decision", "reject", "--actor", "stakeholder", "--reason", "证据摘要不充分",
        ])
        self.assertEqual("Verified", self.task_state(repository, task_id))
        self.operator([
            "close", "--repo", repository, "--task-id", task_id,
            "--decision", "accept", "--actor", "stakeholder",
        ])
        self.assertEqual("Done", self.task_state(repository, task_id))

        reject_workspace, reject_task, _ = self.init_task(repository, "reject")
        self.freeze_target(repository, reject_workspace, reject_task)
        self.claim_and_sync(repository, reject_workspace)
        self.submit_and_sync(repository, reject_workspace)
        self.operator([
            "reject", "--repo", repository, "--task-id", reject_task,
            "--category", "implementation", "--reason", "实现未满足标准",
            "--actor", "verifier-a", "--workspace", reject_workspace,
        ])
        self.assertEqual("Ready", self.task_state(repository, reject_task))
        self.assertEqual("Ready", self.read_task(Path(reject_workspace) / "task.md")[0]["state"])

        self.claim_and_sync(repository, reject_workspace)
        self.submit_and_sync(repository, reject_workspace)
        self.operator([
            "reject", "--repo", repository, "--task-id", reject_task,
            "--category", "input", "--reason", "输入前提无效", "--actor", "architect",
        ])
        self.assertEqual("Void", self.task_state(repository, reject_task))

    def test_reject_input_is_allowed_from_ready_and_in_progress_only(self):
        repository, _ = self.init_state_repo()
        ready_workspace, ready_task, _ = self.init_task(repository, "reject-ready")
        self.freeze_target(repository, ready_workspace, ready_task)
        self.operator([
            "reject", "--repo", repository, "--task-id", ready_task,
            "--category", "input", "--reason", "Ready 输入无效", "--actor", "architect",
        ])
        self.assertEqual("Void", self.task_state(repository, ready_task))

        progress_workspace, progress_task, _ = self.init_task(repository, "reject-progress")
        self.freeze_target(repository, progress_workspace, progress_task)
        self.claim_and_sync(repository, progress_workspace)
        self.assertEqual("In Progress", self.task_state(repository, progress_task))
        result = self.operator([
            "reject", "--repo", repository, "--task-id", progress_task,
            "--category", "implementation", "--reason", "错误分类", "--actor", "verifier-a",
        ], expected_exit=2)
        self.assertEqual("reject.invalid_state", result["error"]["code"])
        self.operator([
            "reject", "--repo", repository, "--task-id", progress_task,
            "--category", "input", "--reason", "In Progress 输入无效", "--actor", "architect",
            "--workspace", progress_workspace,
        ])
        metadata, _ = self.read_task(Path(repository) / "tasks" / progress_task / "task.md")
        self.assertEqual("Void", metadata["state"])
        self.assertIsNone(metadata["lease"])
        self.assertEqual("Void", self.read_task(Path(progress_workspace) / "task.md")[0]["state"])
