import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class CoreWorkflowTests(WorkflowTestCase):
    def test_draft_task_cannot_be_claimed(self):
        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "draft")
        result = self.agent([
            "claim",
            "--workspace", workspace,
            "--actor", "agent-a",
            "--on-behalf-of", "owner",
        ], expected_exit=2)
        self.assertEqual("claim.not_ready", result["error"]["code"])
        self.assertEqual("Draft", self.task_state(repository, task_id))

    def test_concurrent_claims_are_atomic_and_sync_is_idempotent(self):
        repository, remote = self.init_state_repo()
        workspace, task_id, spec_id = self.init_task(repository, "main")
        frozen = self.freeze_target(repository, workspace, task_id, push=True)
        self.assertEqual("Ready", frozen["result"]["state"])
        other_workspace = self.copy_workspace(workspace, self.path("other-workspace"))

        first = self.agent(["claim", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner"])
        second = self.agent(["claim", "--workspace", other_workspace, "--actor", "agent-b", "--on-behalf-of", "owner"])
        self.assertFalse(first["result"]["authoritative"])
        self.assertFalse(second["result"]["authoritative"])

        unrelated = Path(repository) / "unrelated.txt"
        unrelated.write_text("must not be committed\n", encoding="utf-8", newline="\n")
        synced = self.operator([
            "sync",
            "--repo", repository,
            "--workspace", workspace,
            "--workspace", other_workspace,
            "--actor", "owner",
            "--push",
        ])
        results = synced["result"]["results"]
        self.assertEqual(1, sum(item.get("accepted") is True for item in results))
        self.assertEqual(1, sum(item.get("accepted") is False and item.get("code") == "claim.not_ready" for item in results))

        metadata, body = self.read_task(Path(repository) / "tasks" / task_id / "task.md")
        self.assertEqual("In Progress", metadata["state"])
        self.assertIn(metadata["lease"]["claimed_by"], {"agent-a", "agent-b"})
        self.assertEqual("# main\n\n## 任务说明\n\n待补充。\n", body)

        workspace_metadata, _ = self.read_task(Path(workspace) / "task.md")
        self.assertEqual("In Progress", workspace_metadata["state"])

        events_file = Path(repository) / "events" / "events.jsonl"
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line]
        task_events = [event for event in events if event.get("task_id") == task_id]
        self.assertEqual(1, sum(event["event_type"] == "task.claimed" for event in task_events))
        self.assertEqual(1, sum(event["event_type"] == "proposal.rejected" for event in task_events))
        claimed = [event for event in task_events if event["event_type"] == "task.claimed"][0]
        self.assertEqual("agent", claimed["entrypoint"])

        committed = self.git(["-C", repository, "show", "--pretty=", "--name-only", "HEAD"]).splitlines()
        self.assertEqual({f"tasks/{task_id}/task.md", "events/events.jsonl"}, set(committed))
        self.assertEqual("", self.git(["-C", repository, "ls-files", "--", "unrelated.txt"]).strip())
        self.assertEqual("must not be committed\n", unrelated.read_text(encoding="utf-8"))

        local_head = self.git(["-C", repository, "rev-parse", "HEAD"]).strip()
        remote_head = self.git(["--git-dir", remote, "rev-parse", "main"]).strip()
        self.assertEqual(local_head, remote_head)

        repeated = self.operator(["sync", "--repo", repository, "--workspace", workspace, "--workspace", other_workspace, "--actor", "owner"])
        self.assertTrue(all(item.get("duplicate") for item in repeated["result"]["results"]))
        self.assertEqual(local_head, self.git(["-C", repository, "rev-parse", "HEAD"]).strip())
        self.assertTrue((Path(repository) / "specs" / spec_id / "1" / "contract.json").is_file())
