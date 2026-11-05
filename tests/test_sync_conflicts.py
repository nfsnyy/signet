try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase


class SyncConflictTests(WorkflowTestCase):
    def push_external_commit(self, remote, name):
        clone = self.path(name)
        self.git(["init", "-b", "main", clone])
        self.git(["-C", clone, "remote", "add", "origin", remote])
        self.git(["-C", clone, "pull", "origin", "main"])
        external = clone / "EXTERNAL.txt"
        external.write_text("external commit\n", encoding="utf-8", newline="\n")
        self.git(["-C", clone, "add", "EXTERNAL.txt"])
        self.git(["-C", clone, "commit", "-m", "External commit"])
        self.git(["-C", clone, "push", "origin", "main"])
        return clone

    def test_sync_rejects_when_local_repository_is_behind_remote(self):
        repository, remote = self.init_state_repo()
        workspace, task_id, _ = self.init_task(repository, "behind")
        self.freeze_target(repository, workspace, task_id)
        self.git(["-C", repository, "push", "origin", "main"])
        self.agent(["claim", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner"])
        self.push_external_commit(remote, "behind-clone")

        result = self.operator([
            "sync", "--repo", repository, "--workspace", workspace, "--actor", "owner",
        ], expected_exit=3)
        self.assertEqual("sync.behind_remote", result["error"]["code"])
        self.assertEqual("Ready", self.task_state(repository, task_id))

    def test_sync_rejects_when_repository_history_diverged(self):
        repository, remote = self.init_state_repo()
        self.push_external_commit(remote, "diverged-clone")
        workspace, task_id, _ = self.init_task(repository, "diverged")
        self.freeze_target(repository, workspace, task_id)
        self.agent(["claim", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner"])

        result = self.operator([
            "sync", "--repo", repository, "--workspace", workspace, "--actor", "owner",
        ], expected_exit=3)
        self.assertEqual("sync.diverged_remote", result["error"]["code"])
        self.assertEqual("Ready", self.task_state(repository, task_id))
