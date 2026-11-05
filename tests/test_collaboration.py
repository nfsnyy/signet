import json
import shutil
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class CollaborationTests(WorkflowTestCase):
    def two_frozen_tasks(self, prefix="collab"):
        repository, _ = self.init_state_repo()
        workspace_a, task_a, _ = self.init_task(repository, f"{prefix}-a")
        self.freeze_target(repository, workspace_a, task_a)
        workspace_b, task_b, _ = self.init_task(repository, f"{prefix}-b")
        self.freeze_target(repository, workspace_b, task_b)
        return repository, workspace_a, task_a, workspace_b, task_b

    def route_message(self, source_workspace, target_workspace, filename):
        shutil.copyfile(Path(source_workspace) / "outbox-collab" / filename, Path(target_workspace) / "inbox" / filename)

    def send(self, workspace, task_id, to_task, message_type="in-spec-request", expected_exit=0):
        result = self.agent([
            "collab-send",
            "--workspace", workspace,
            "--actor", "agent-a",
            "--on-behalf-of", "owner",
            "--type", message_type,
            "--to-task", to_task,
            "--summary", "请求协调",
            "--details", "需要对方任务配合调整",
        ], expected_exit=expected_exit)
        return result

    def answer(self, workspace, message_id, action, note=None, expected_exit=0):
        arguments = [
            "collab-answer",
            "--workspace", workspace,
            "--actor", "agent-b",
            "--on-behalf-of", "owner",
            "--message-id", message_id,
            "--action", action,
        ]
        if note is not None:
            arguments.extend(["--note", note])
        return self.agent(arguments, expected_exit=expected_exit)

    def archived_message(self, repository, message_id):
        path = Path(repository) / "discussions" / f"{message_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def event_types(self, repository, task_id):
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        return [event["event_type"] for event in events if event.get("task_id") == task_id]

    def test_in_spec_request_negotiation_and_idempotent_sync(self):
        repository, workspace_a, task_a, workspace_b, task_b = self.two_frozen_tasks("nego")

        sent = self.send(workspace_a, task_a, task_b)
        message_id = sent["result"]["message_id"]
        filename = f"{message_id}.json"
        first_sync = self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.assertTrue(any(item.get("message_id") == message_id and item.get("accepted") for item in first_sync["result"]["results"]))

        archived = self.archived_message(repository, message_id)
        self.assertIsNotNone(archived)
        self.assertEqual("open", archived["status"])
        self.assertEqual(task_a, archived["from_task"])
        self.assertEqual(task_b, archived["to_task"])
        self.assertIn("discussion.opened", self.event_types(repository, task_a))
        self.assertIn("discussion.received", self.event_types(repository, task_b))

        self.route_message(workspace_a, workspace_b, filename)
        countered = self.answer(workspace_b, message_id, "counter", note="可以部分配合")
        self.assertEqual("open", countered["result"]["status"])
        self.assertEqual(1, countered["result"]["round"])
        self.operator(["sync", "--repo", repository, "--workspace", workspace_b, "--actor", "owner"])
        self.assertIn("discussion.answered", self.event_types(repository, task_b))
        self.assertIn("discussion.updated", self.event_types(repository, task_a))

        self.route_message(workspace_b, workspace_a, filename)
        accepted = self.answer(workspace_a, message_id, "accept", note="按还价执行")
        self.assertEqual("accepted", accepted["result"]["status"])
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.assertEqual("accepted", self.archived_message(repository, message_id)["status"])

        inbox_b = self.operator(["inbox", "--repo", repository, "--task-id", task_b])
        self.assertEqual("accepted", inbox_b["result"]["messages"][0]["status"])
        inbox_a = self.operator(["inbox", "--repo", repository, "--task-id", task_a])
        self.assertEqual(1, len(inbox_a["result"]["messages"]))

        events_file = Path(repository) / "events" / "events.jsonl"
        before = len(events_file.read_text(encoding="utf-8").splitlines())
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        after = len(events_file.read_text(encoding="utf-8").splitlines())
        self.assertEqual(before, after)

    def test_definition_doubt_requires_blocked_and_new_scope_only_closes(self):
        repository, workspace_a, task_a, workspace_b, task_b = self.two_frozen_tasks("doubt")

        blocked = self.send(workspace_a, task_a, task_b, message_type="definition-doubt", expected_exit=2)
        self.assertEqual("collab.not_blocked", blocked["error"]["code"])

        self.claim_and_sync(repository, workspace_a)
        block_evidence = Path(workspace_a) / "evidence" / "block.txt"
        block_evidence.write_text("definition doubt evidence\n", encoding="utf-8", newline="\n")
        self.agent([
            "block", "--workspace", workspace_a, "--actor", "agent-a", "--on-behalf-of", "owner",
            "--reason", "定义与外部契约矛盾", "--evidence", block_evidence,
        ])
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.assertEqual("Blocked", self.task_state(repository, task_a))

        sent = self.send(workspace_a, task_a, task_b, message_type="definition-doubt")
        message_id = sent["result"]["message_id"]
        filename = f"{message_id}.json"
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.route_message(workspace_a, workspace_b, filename)

        no_counter = self.answer(workspace_b, message_id, "counter", note="试图还价", expected_exit=2)
        self.assertEqual("collab.invalid_action", no_counter["error"]["code"])
        closed = self.answer(workspace_b, message_id, "close")
        self.assertEqual("closed", closed["result"]["status"])

        scope = self.send(workspace_a, task_a, task_b, message_type="new-scope")
        scope_id = scope["result"]["message_id"]
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.route_message(workspace_a, workspace_b, f"{scope_id}.json")
        scope_accept = self.answer(workspace_b, scope_id, "accept", note="试图接受新需求", expected_exit=2)
        self.assertEqual("collab.invalid_action", scope_accept["error"]["code"])
        scope_close = self.answer(workspace_b, scope_id, "close")
        self.assertEqual("closed", scope_close["result"]["status"])

    def test_in_spec_request_round_limit_and_escalate(self):
        repository, workspace_a, task_a, workspace_b, task_b = self.two_frozen_tasks("rounds")

        sent = self.send(workspace_a, task_a, task_b)
        message_id = sent["result"]["message_id"]
        filename = f"{message_id}.json"
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.route_message(workspace_a, workspace_b, filename)

        self.answer(workspace_b, message_id, "counter", note="第一轮还价")
        self.operator(["sync", "--repo", repository, "--workspace", workspace_b, "--actor", "owner"])
        self.route_message(workspace_b, workspace_a, filename)
        self.answer(workspace_a, message_id, "counter", note="第二轮还价")
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.route_message(workspace_a, workspace_b, filename)

        limit = self.answer(workspace_b, message_id, "counter", note="第三轮被拒", expected_exit=2)
        self.assertEqual("collab.round_limit", limit["error"]["code"])
        escalated = self.answer(workspace_b, message_id, "escalate", note="升级给 operator 裁决")
        self.assertEqual("escalated", escalated["result"]["status"])
        self.operator(["sync", "--repo", repository, "--workspace", workspace_b, "--actor", "owner"])
        self.assertEqual("escalated", self.archived_message(repository, message_id)["status"])

    def test_submit_report_must_reference_archived_messages(self):
        repository, workspace_a, task_a, workspace_b, task_b = self.two_frozen_tasks("submit-collab")

        sent = self.send(workspace_a, task_a, task_b)
        message_id = sent["result"]["message_id"]
        self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])

        self.claim_and_sync(repository, workspace_a)
        evidence = Path(workspace_a) / "evidence" / "submit.txt"
        evidence.write_text("submit evidence\n", encoding="utf-8", newline="\n")

        self.write_json(Path(workspace_a) / "report-fake.json", {
            "implemented": "完成实现",
            "executed": [{"command": ["python", "--version"], "output": str(evidence)}],
            "not_executed": [],
            "deviations": [],
            "collaboration_requests": [{"message_id": "msg-nonexistent", "status": "open"}],
        })
        self.agent([
            "submit", "--workspace", workspace_a, "--actor", "agent-a", "--on-behalf-of", "owner",
            "--report", Path(workspace_a) / "report-fake.json", "--evidence", evidence,
        ])
        rejected = self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        fake_result = [item for item in rejected["result"]["results"] if item.get("code") == "submit.collaboration_unknown"]
        self.assertEqual(1, len(fake_result))
        self.assertEqual("In Progress", self.task_state(repository, task_a))

        self.write_json(Path(workspace_a) / "report-ok.json", {
            "implemented": "完成实现",
            "executed": [{"command": ["python", "--version"], "output": str(evidence)}],
            "not_executed": [],
            "deviations": [],
            "collaboration_requests": [{"message_id": message_id, "status": "open"}],
        })
        self.agent([
            "submit", "--workspace", workspace_a, "--actor", "agent-a", "--on-behalf-of", "owner",
            "--report", Path(workspace_a) / "report-ok.json", "--evidence", evidence,
        ])
        accepted = self.operator(["sync", "--repo", repository, "--workspace", workspace_a, "--actor", "owner"])
        self.assertTrue(any(item.get("accepted") for item in accepted["result"]["results"]))
        self.assertEqual("Implemented", self.task_state(repository, task_a))
