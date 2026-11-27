"""AC-09 真实走查缺陷修复回归（reject bbffd47）：证据材料下载端点项目路由。

真实走查证实：T19 非内置项目任务包与规格资产可下载且哈希正确，但三个证据
下载均 400 task.not_found——task_evidence_endpoint 经 resolve_repo(None)
钉死绑定存储，与任务所属项目脱节。修复后端点与任务详情/资产端点同构：
可选 repo 查询参数 + task_repository(request, task_id, repo) 定位
（省略按绑定存储/活动项目 → 注册表全局定位；显式 repo 仅限该项目内）。

本案覆盖（不触碰 T18 冻结三件套/冻结用例）：
- 登录用户携 repo=<非内置 project_id> 下载真实证据：200 且字节一致；
- 错误/缺失项目定位不得泄露其它项目证据（404 且响应体为错误对象）；
- 同任务 sigt 200、他任务 sigt 403、匿名 401 的既有顺序不回归
  （名与路径解析先于授权：匿名 + 清单外名 → 404，匿名 + 有效名 → 401）。
"""
import json
import subprocess

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase

from starlette.testclient import TestClient

from aidev import authn, projects
from aidev.web.app import create_app

EVIDENCE_BYTES = b"t19-real-walkthrough-proof\x00\xac09\r\n"


class EvidenceProjectRoutingTests(WorkflowTestCase):
    layer = "integration"

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
            "title": title, "requirement": "req-ac09", "repo": project_id,
        }, headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]["task_id"]

    def plant_evidence(self, project_id, task_id, name="proof.bin", content=EVIDENCE_BYTES):
        """在服务端项目克隆的 tasks/<id>/evidence/ 落真实证据字节并本地提交
        （读路径 fetch_integrate 对本地超前容忍，服务本地现状）。"""
        record = projects.get_project(project_id)
        clone = projects.ensure_clone(record)
        evidence_dir = clone / "tasks" / task_id / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / name).write_bytes(content)
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-m", "ac09 evidence"],
                       check=True, capture_output=True)
        return clone / "tasks" / task_id / "evidence" / name

    def test_logged_in_downloads_project_task_evidence_with_repo_param(self):
        pid = self.register("ev-repo")
        task_id = self.make_task(pid, "ev-repo-task")
        planted = self.plant_evidence(pid, task_id)
        response = self.client.get(
            f"/api/tasks/{task_id}/evidence/proof.bin",
            params={"repo": pid}, headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(planted.read_bytes(), response.content)
        self.assertEqual(EVIDENCE_BYTES, response.content)

    def test_global_locate_without_repo_serves_project_task_evidence(self):
        pid = self.register("ev-global")
        task_id = self.make_task(pid, "ev-global-task")
        self.plant_evidence(pid, task_id)
        response = self.client.get(
            f"/api/tasks/{task_id}/evidence/proof.bin", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(EVIDENCE_BYTES, response.content)

    def test_wrong_project_repo_does_not_leak_evidence(self):
        pid_a = self.register("ev-a")
        pid_b = self.register("ev-b")
        task_id = self.make_task(pid_a, "ev-a-task")
        self.plant_evidence(pid_a, task_id)
        # 任务在 A，携 B 的 repo：404 task.not_found，绝不回显 A 的证据字节。
        response = self.client.get(
            f"/api/tasks/{task_id}/evidence/proof.bin",
            params={"repo": pid_b}, headers=self.headers)
        self.assertEqual(404, response.status_code, response.text[:200])
        self.assertEqual("task.not_found", response.json()["error"]["code"])
        self.assertNotEqual(EVIDENCE_BYTES, response.content)
        # 未知项目 id：404 repo.not_found。
        response = self.client.get(
            f"/api/tasks/{task_id}/evidence/proof.bin",
            params={"repo": "project-no-such"}, headers=self.headers)
        self.assertEqual(404, response.status_code, response.text[:200])
        self.assertEqual("repo.not_found", response.json()["error"]["code"])

    def test_builtin_task_evidence_without_repo_still_serves(self):
        task_id = self.make_task("project-bound-fixed", "ev-builtin-task")
        # 内置项目 = 服务端绑定存储本身（无独立克隆），证据直接落绑定仓。
        evidence_dir = self.bound / "tasks" / task_id / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        planted = evidence_dir / "proof.bin"
        planted.write_bytes(EVIDENCE_BYTES)
        subprocess.run(["git", "-C", str(self.bound), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.bound), "commit", "-m", "ac09 evidence"],
                       check=True, capture_output=True)
        response = self.client.get(
            f"/api/tasks/{task_id}/evidence/proof.bin", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(planted.read_bytes(), response.content)

    def test_auth_order_not_regressed(self):
        pid = self.register("ev-auth")
        task_id = self.make_task(pid, "ev-auth-task")
        self.plant_evidence(pid, task_id)
        url = f"/api/tasks/{task_id}/evidence/proof.bin"
        # 匿名 + 有效名：401（路径解析先于授权）。
        anonymous = self.client.get(url)
        self.assertEqual(401, anonymous.status_code, anonymous.text[:200])
        # 匿名 + 清单外名：404（名与路径解析先于授权的顺序不回退）。
        missing = self.client.get(f"/api/tasks/{task_id}/evidence/nope.bin")
        self.assertEqual(404, missing.status_code, missing.text[:200])
        self.assertEqual("task.evidence_not_found", missing.json()["error"]["code"])
        # 同任务 sigt：200。
        same_sigt = authn.mint_sigt(task_id, "owner-x", str(projects.ensure_clone(projects.get_project(pid))))
        same = self.client.get(url, headers={"Authorization": "sigt " + same_sigt})
        self.assertEqual(200, same.status_code, same.text[:200])
        self.assertEqual(EVIDENCE_BYTES, same.content)
        # 他任务 sigt：403 authz.task_mismatch。
        other_id = self.make_task(pid, "ev-auth-other")
        other_sigt = authn.mint_sigt(other_id, "owner-x", str(projects.ensure_clone(projects.get_project(pid))))
        other = self.client.get(url, headers={"Authorization": "sigt " + other_sigt})
        self.assertEqual(403, other.status_code, other.text[:200])
        self.assertEqual("authz.task_mismatch", other.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
