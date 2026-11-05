import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class ProjectTests(WorkflowTestCase):
    def test_project_metadata_and_event_stamping(self):
        repository = self.path("proj-repo")
        result = self.operator([
            "init-repository", "--repo", repository,
            "--actor", "owner", "--name", "demo-project", "--owner", "owner",
        ])
        project = result["result"]["project"]
        self.assertEqual("demo-project", project["name"])
        project_id = project["project_id"]
        self.assertTrue(project_id.startswith("project-"))

        on_disk = json.loads((Path(repository) / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(project_id, on_disk["project_id"])
        self.assertEqual("owner", on_disk["owner"])
        self.assertTrue(on_disk["created_at"])

        default_repo = self.path("another-repo")
        default_result = self.operator(["init-repository", "--repo", default_repo, "--actor", "owner"])
        self.assertEqual("another-repo", default_result["result"]["project"]["name"])

        workspace, task_id, _ = self.init_task(repository, "proj")
        self.freeze_target(repository, workspace, task_id)
        events = [json.loads(line) for line in (Path(repository) / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertGreaterEqual(len(events), 4)
        for event in events:
            self.assertEqual(project_id, event.get("project_id"), f"event missing project_id: {event['event_type']}")

    def test_actor_alias_validation(self):
        bad = self.operator(["init-repository", "--repo", self.path("bad-repo"), "--actor", "聂方"], expected_exit=2)
        self.assertEqual("actor.invalid_alias", bad["error"]["code"])

        repository, _ = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "alias")
        self.freeze_target(repository, workspace, task_id)

        email = self.operator([
            "init-task", "--repo", repository, "--workspace", self.path("alias-w2"),
            "--title", "x", "--requirement", "y", "--owner", "owner",
            "--actor", "nfsnyy@example.com",
        ], expected_exit=2)
        self.assertEqual("actor.invalid_alias", email["error"]["code"])

        upper = self.agent(["claim", "--workspace", workspace, "--actor", "Agent A", "--on-behalf-of", "owner"], expected_exit=2)
        self.assertEqual("actor.invalid_alias", upper["error"]["code"])

        chinese = self.agent(["claim", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "张三"], expected_exit=2)
        self.assertEqual("actor.invalid_alias", chinese["error"]["code"])

        wildcard = self.agent(["claim", "--workspace", workspace, "--actor", "agent-*", "--on-behalf-of", "owner"])
        self.assertFalse(wildcard["result"]["authoritative"])
