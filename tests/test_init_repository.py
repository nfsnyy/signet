import json
from pathlib import Path

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class InitRepositoryTests(WorkflowTestCase):
    def test_init_on_pre_created_git_repository(self):
        repository = self.path("precreated-repo")
        repository.mkdir(parents=True)
        self.git(["init", "-b", "main", repository])

        result = self.operator([
            "init-repository", "--repo", repository,
            "--actor", "owner", "--name", "precreated", "--owner", "owner",
        ])
        project = result["result"]["project"]
        self.assertEqual("precreated", project["name"])

        on_disk = json.loads((repository / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(project["project_id"], on_disk["project_id"])
        self.assertTrue((repository / "events" / "events.jsonl").is_file())
        events = [json.loads(line) for line in (repository / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual("repository.initialized", events[0]["event_type"])
        self.assertEqual(project["project_id"], events[0]["project_id"])

        log = self.git(["-C", repository, "log", "--oneline"])
        self.assertEqual(1, len(log.strip().splitlines()))
        tracked = self.git(["-C", repository, "ls-files"])
        self.assertIn("project.json", tracked)
        self.assertIn("events/events.jsonl", tracked)

    def test_reinit_is_refused(self):
        repository, _ = self.init_state_repo()
        result = self.operator(["init-repository", "--repo", repository, "--actor", "owner"], expected_exit=2)
        self.assertEqual("repository.already_initialized", result["error"]["code"])

    def test_non_git_non_empty_directory_is_refused(self):
        repository = self.path("dirty-dir")
        repository.mkdir(parents=True)
        (repository / "stray.txt").write_text("x", encoding="utf-8")
        result = self.operator(["init-repository", "--repo", repository, "--actor", "owner"], expected_exit=3)
        self.assertEqual("git.repository_not_empty", result["error"]["code"])
