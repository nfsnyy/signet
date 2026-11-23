"""AC-03 回归（operator 驳回 f8484cf 修复配套）：省略 repo 的全局任务定位
不得绕过项目状态边界——停用项目的任务读写（读面/claims 写面）必须
409 project.disabled（先于任何读写副作用）；活动项目全局定位保持可达（正对照）。

PAT 的跨项目边界在 API 面由中间件先行裁决（authz.project_denied），
全局定位分支内另保留同源防线（含未命名的直达场景）， Frozen 锚案
case-cross-project-denial(-b) 已覆盖 API 面探针，本案聚焦 operator 留证的
停用项目省略 repo 读写绕过（disabled-global-read-proof.json）。
"""
import json
import subprocess

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase

from starlette.testclient import TestClient

from aidev import authn
from aidev.web.app import create_app


class GlobalTaskLocationBoundaryTests(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.bound = self.path("bound-state")
        self.bound.mkdir()
        subprocess.run(["git", "init", "--initial-branch=main", str(self.bound)],
                       check=True, capture_output=True)
        (self.bound / "project.json").write_text(
            json.dumps({"project_id": "project-bound-fixed", "name": "bound", "owner": "owner-x"}) + "\n",
            encoding="utf-8")
        subprocess.run(["git", "-C", str(self.bound), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.bound), "commit", "-m", "init"],
                       check=True, capture_output=True)
        self.client = TestClient(create_app(str(self.bound)))
        self.headers = {"Authorization": "Bearer " + authn.create_session("owner-x")}

    def register(self, name):
        state = self.path(f"{name}-state.git")
        code = self.path(f"{name}-code.git")
        for target in (state, code):
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(target)],
                           check=True, capture_output=True)
        response = self.client.post("/api/projects", json={
            "name": name,
            "state_repo": state.resolve().as_uri(),
            "code_repo": code.resolve().as_uri(),
        }, headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]["project"]["project_id"]

    def make_task(self, project_id, title):
        response = self.client.post("/api/tasks", json={
            "title": title, "requirement": "req-boundary", "repo": project_id,
        }, headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]["task_id"]

    def test_disabled_project_task_read_global_locate_rejected(self):
        pid = self.register("bnd-read")
        task_id = self.make_task(pid, "bnd-read-task")
        before = self.client.get(f"/api/tasks/{task_id}", headers=self.headers)
        self.assertEqual(200, before.status_code)

        self.client.post(f"/api/projects/{pid}/disable", headers=self.headers)
        response = self.client.get(f"/api/tasks/{task_id}", headers=self.headers)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("project.disabled", response.json()["error"]["code"])

    def test_disabled_project_task_anonymous_read_rejected(self):
        pid = self.register("bnd-anon")
        task_id = self.make_task(pid, "bnd-anon-task")
        self.client.post(f"/api/projects/{pid}/disable", headers=self.headers)
        response = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("project.disabled", response.json()["error"]["code"])

    def test_disabled_project_task_write_global_locate_rejected(self):
        pid = self.register("bnd-write")
        task_id = self.make_task(pid, "bnd-write-task")
        self.client.post(f"/api/projects/{pid}/disable", headers=self.headers)
        response = self.client.post(
            f"/api/tasks/{task_id}/claims", json={"on_behalf_of": "owner-x"}, headers=self.headers)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("project.disabled", response.json()["error"]["code"])

    def test_active_project_global_locate_still_serves(self):
        pid = self.register("bnd-active")
        task_id = self.make_task(pid, "bnd-active-task")
        response = self.client.get(f"/api/tasks/{task_id}", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        explicit = self.client.get(f"/api/tasks/{task_id}", params={"repo": pid}, headers=self.headers)
        self.assertEqual(200, explicit.status_code, explicit.text)

    def test_disabled_project_explicit_repo_read_rejected(self):
        pid = self.register("bnd-explicit")
        task_id = self.make_task(pid, "bnd-explicit-task")
        self.client.post(f"/api/projects/{pid}/disable", headers=self.headers)
        response = self.client.get(f"/api/tasks/{task_id}", params={"repo": pid}, headers=self.headers)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("project.disabled", response.json()["error"]["code"])

    def test_pat_cannot_ride_global_locate(self):
        pid = self.register("bnd-pat")
        task_id = self.make_task(pid, "bnd-pat-task")
        scope = list(authn.PAT_SCOPES)[0]
        issued = self.client.post("/api/pats", json={
            "project_id": pid, "label": "bnd-agent",
            "scope": [scope], "expires_in": "24h",
        }, headers=self.headers)
        self.assertEqual(200, issued.status_code, issued.text)
        token = issued.json()["result"]["token"]
        # PAT 省略 repo：目标回落内置项目 ≠ PAT 绑定项目 → 中间件边界先拒 403；
        # 全局定位分支内亦留同源防线（含凭据直达场景）。
        response = self.client.get(f"/api/tasks/{task_id}",
                                   headers={"Authorization": "Bearer " + token})
        self.assertEqual(403, response.status_code, response.text)
        self.assertIn(response.json()["error"]["code"],
                      ("authz.project_denied", "authz.pat_scope_denied"))


if __name__ == "__main__":
    unittest.main()
