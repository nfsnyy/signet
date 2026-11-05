import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aidev.taskfile import read_task

ROOT = Path(__file__).resolve().parents[1]
AGENT_CLI = ROOT / "aidev-agent.py"
OPERATOR_CLI = ROOT / "aidev-operator.py"

TEST_ENV = os.environ.copy()
TEST_ENV.update({
    "PYTHONDONTWRITEBYTECODE": "1",
    "GIT_AUTHOR_NAME": "nfsnyy",
    "GIT_AUTHOR_EMAIL": "12371954+nfsnyy@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "nfsnyy",
    "GIT_COMMITTER_EMAIL": "12371954+nfsnyy@users.noreply.github.com",
})


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="aidev-test-")
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def path(self, *parts):
        return self.root.joinpath(*parts)

    def run_json(self, script, arguments, expected_exit=0):
        result = subprocess.run(
            [sys.executable, str(script), *map(str, arguments)],
            cwd=ROOT,
            env=TEST_ENV,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != expected_exit:
            raise AssertionError(
                f"command failed: {script.name} {arguments}\n"
                f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return json.loads(result.stdout) if result.stdout.strip() else None

    def agent(self, arguments, expected_exit=0):
        return self.run_json(AGENT_CLI, arguments, expected_exit)

    def operator(self, arguments, expected_exit=0):
        return self.run_json(OPERATOR_CLI, arguments, expected_exit)

    def git(self, arguments, expected_exit=0):
        result = subprocess.run(
            ["git", *map(str, arguments)],
            cwd=ROOT,
            env=TEST_ENV,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != expected_exit:
            raise AssertionError(f"git failed: {arguments}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def init_state_repo(self, name="repo"):
        repository = self.path(name)
        remote = self.path(f"{name}-remote.git")
        self.operator(["init-repository", "--repo", repository, "--actor", "owner"])
        self.git(["init", "--bare", remote])
        self.git(["-C", repository, "remote", "add", "origin", remote])
        return repository, remote

    def init_task(self, repository, name):
        workspace = self.path(f"{name}-workspace")
        result = self.operator([
            "init-task",
            "--repo", repository,
            "--workspace", workspace,
            "--title", name,
            "--requirement", "test",
            "--owner", "owner",
            "--actor", "owner",
        ])
        return workspace, result["result"]["task_id"], result["result"]["spec_id"]

    def freeze_target(self, repository, workspace, task_id, push=False):
        artifacts = self.path(f"artifacts-{task_id}")
        card = artifacts / "acceptance-card.json"
        contract = artifacts / "contract.json"
        mapping = artifacts / "mapping.json"
        self.write_json(card, {"criteria": [{"criterion_id": "AC-001", "text": "标准可验收", "strength": "must"}]})
        self.write_json(contract, {
            "contract_type": "target",
            "operation_set": None,
            "assets": {},
            "cases": [{"case_id": "CASE-001", "command": ["python", "--version"], "fixtures": [], "expect": {"exit_code": 0}}],
            "environment": {"runner": "python", "dependency_files": [], "endpoints": {}, "allowed_env": []},
        })
        self.write_json(mapping, {"rows": [{"criterion_id": "AC-001", "case_ids": ["CASE-001"], "manual": False}]})
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
        return self.operator(arguments)

    def write_json(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def read_task(self, path):
        return read_task(path)

    def task_state(self, repository, task_id):
        return self.read_task(Path(repository) / "tasks" / task_id / "task.md")[0]["state"]

    def claim_and_sync(self, repository, workspace):
        self.agent(["claim", "--workspace", workspace, "--actor", "agent-a", "--on-behalf-of", "owner"])
        return self.operator(["sync", "--repo", repository, "--workspace", workspace, "--actor", "owner"])

    def submit_and_sync(self, repository, workspace):
        evidence = Path(workspace) / "evidence" / "submit.txt"
        evidence.write_text("submit evidence\n", encoding="utf-8", newline="\n")
        self.write_json(Path(workspace) / "report.json", {
            "implemented": "完成实现",
            "executed": [{"command": ["python", "--version"], "output": str(evidence)}],
            "not_executed": [],
            "deviations": [],
            "collaboration_requests": [],
        })
        self.agent([
            "submit",
            "--workspace", workspace,
            "--actor", "agent-a",
            "--on-behalf-of", "owner",
            "--report", Path(workspace) / "report.json",
            "--evidence", evidence,
        ])
        return self.operator(["sync", "--repo", repository, "--workspace", workspace, "--actor", "owner"])

    def copy_workspace(self, source, target):
        shutil.copytree(source, target)
        return target
