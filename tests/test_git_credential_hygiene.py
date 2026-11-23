"""AC-19 防泄漏回归（operator 驳回 f8484cf 修复配套）。

Git PAT 不得进入 repo URL、.git/config、进程参数、日志或错误响应：远端认证一律
经 GIT_ASKPASS 环境供给（origin 恒为明文 URL），远端操作另带
``-c credential.helper=``（防全局 helper/GCM 持久化或弹窗）与禁终端提示
fail-closed。askpass 脚本是凭据存储的衍生运行时工件，随凭据增删刷新。
"""
import base64
import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase

from aidev import gitrepo, projects


class GitCredentialHygieneTests(WorkflowTestCase):
    def make_bare(self, name):
        remote = self.path(name)
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                       check=True, capture_output=True)
        return remote

    def url(self, target):
        return Path(target).resolve().as_uri()

    def register(self, name, credentials=None):
        state = self.make_bare(f"{name}-state.git")
        code = self.make_bare(f"{name}-code.git")
        record = projects.register_project(
            name, self.url(state), self.url(code), credentials,
            f"owner-{name}", str(self.path("platform-root")), str(self.path("bound-none")),
            "project-builtin-fixed",
        )
        return record, state, code

    def record_call(self, captured):
        real_run = subprocess.run

        def recorder(*args, **kwargs):
            argv = list(args[0]) if args else list(kwargs.get("args", []))
            captured.append((argv, kwargs.get("env")))
            return real_run(*args, **kwargs)

        return recorder

    def test_apply_credential_keeps_secret_out_of_clone_config(self):
        record, _, _ = self.register("hyg-1")
        secret = "t17-hyg1-secret-" + "a" * 20
        projects.upsert_git_credential(record["project_id"], "state", "https_pat", secret=secret)
        projects.apply_credential_to_clone(projects.clone_dir(record["project_id"]), record)

        clone = projects.clone_dir(record["project_id"])
        config_bytes = (clone / ".git" / "config").read_bytes()
        self.assertNotIn(secret.encode(), config_bytes)
        origin = projects.run_git(clone, ["remote", "get-url", "origin"]).stdout.strip()
        self.assertEqual(record["state_repo"], origin)
        self.assertNotIn(secret, origin)
        # 克隆全树（含 .git）字节扫无秘密；askpass 脚本在场且含秘密（数据目录只写落证）
        hits = [str(p.relative_to(clone)) for p in clone.rglob("*")
                if p.is_file() and secret.encode() in p.read_bytes()]
        self.assertEqual([], hits)
        script = projects.askpass_path(record["project_id"], "state")
        self.assertTrue(script.is_file())
        self.assertIn(secret, script.read_text(encoding="utf-8"))

    def test_apply_credential_migrates_legacy_secret_origin_url(self):
        clone = self.path("hyg-legacy-clone")
        clone.mkdir()
        subprocess.run(["git", "init", "--initial-branch=main", str(clone)], check=True, capture_output=True)
        legacy_secret = "t17-hyg-legacy-secret"
        projects.run_git(clone, ["remote", "add", "origin", f"https://{legacy_secret}@git.example.invalid/x.git"])
        pid = "project-hyg-legacy"
        projects.write_project_marker(clone, pid)
        record = {"project_id": pid, "state_repo": "https://git.example.invalid/x.git",
                  "code_repo": "https://git.example.invalid/y.git"}

        projects.apply_credential_to_clone(clone, record)

        self.assertNotIn(legacy_secret.encode(), (clone / ".git" / "config").read_bytes())
        self.assertEqual(record["state_repo"], projects.run_git(clone, ["remote", "get-url", "origin"]).stdout.strip())

    def test_askpass_script_prompt_dispatch(self):
        secret = "t17-hyg-askpass-secret"
        script = projects.write_askpass_script("project-hyg-ask", "state", secret)
        username = subprocess.run(["sh", str(script), "Username for 'https://git.example.invalid':"],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(secret, username.stdout.strip())
        password = subprocess.run(["sh", str(script), "Password for 'https://u@git.example.invalid':"],
                                  capture_output=True, text=True, check=True)
        self.assertEqual("", password.stdout.strip())

    def test_fetch_integrate_plumbs_askpass_env_and_helper_reset(self):
        record, _, _ = self.register("hyg-4")
        secret = "t17-hyg4-secret-" + "b" * 20
        projects.upsert_git_credential(record["project_id"], "state", "https_pat", secret=secret)
        clone = projects.clone_dir(record["project_id"])

        captured = []
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=self.record_call(captured)):
            projects.fetch_integrate(str(clone))

        fetch_calls = [call for call in captured if "fetch" in call[0]]
        self.assertTrue(fetch_calls, "fetch 未被执行")
        argv, env = fetch_calls[0]
        self.assertIn("credential.helper=", argv)
        self.assertNotIn(secret, " ".join(str(item) for item in argv))
        self.assertIsNotNone(env)
        self.assertEqual("0", env.get("GIT_TERMINAL_PROMPT"))
        self.assertEqual("never", env.get("GCM_INTERACTIVE"))
        self.assertEqual(str(projects.askpass_path(record["project_id"], "state")), env.get("GIT_ASKPASS"))

    def test_probe_clone_error_surface_has_no_secret(self):
        secret = "t17-hyg-probe-secret"
        with self.assertRaises(Exception) as ctx:
            projects._probe_clone("http://127.0.0.1:9/none.git", {"method": "https_pat", "secret": secret})
        exc = ctx.exception
        self.assertEqual("project.state_repo_unreachable", exc.code)
        self.assertNotIn(secret, str(exc))
        self.assertNotIn(secret, json.dumps(exc.details or {}, ensure_ascii=False))

    def test_auth_flow_end_to_end_and_fail_closed_after_removal(self):
        secret = "t17-hyg6-flow-secret"
        expected = "Basic " + base64.b64encode(f"{secret}:".encode()).decode()
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                auth = self.headers.get("Authorization")
                calls.append(auth)
                if auth != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="hyg"')
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not-a-git-repo")

            def log_message(self, *args):
                return None

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/x.git"
            # 手工项目克隆（register 流程需要真 git 远端，这里直接构造克隆面）
            clone = self.path("hyg-6-clone")
            clone.mkdir()
            subprocess.run(["git", "init", "--initial-branch=main", str(clone)], check=True, capture_output=True)
            pid = "project-hyg-flow"
            projects.write_project_marker(clone, pid)
            record = {"project_id": pid, "state_repo": url, "code_repo": url}
            projects.upsert_git_credential(pid, "state", "https_pat", secret=secret)
            projects.apply_credential_to_clone(clone, record)

            env = projects.git_auth_env(pid, "state")
            result = subprocess.run(
                ["git", *projects.remote_auth_args(), "ls-remote", url],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertIn(expected, calls, "认证流未携带 askpass 供给的凭据")
            self.assertNotIn(secret, " ".join(result.args))
            self.assertNotIn(secret, result.stderr)

            # 删除凭据后 fail-closed：无 askpass、快速失败、不再出示秘密
            projects.remove_git_credential(pid, "state")
            projects.apply_credential_to_clone(clone, record)
            self.assertFalse(projects.askpass_path(pid, "state").exists())
            calls.clear()
            denied = subprocess.run(
                ["git", *projects.remote_auth_args(), "ls-remote", url],
                env=projects.git_auth_env(pid, "state"), capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(0, denied.returncode)
            self.assertNotIn(secret, calls)
            self.assertNotIn(secret, denied.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_commit_paths_auto_push_plumbs_auth_env(self):
        record, _, _ = self.register("hyg-7")
        secret = "t17-hyg7-secret-" + "c" * 20
        projects.upsert_git_credential(record["project_id"], "state", "https_pat", secret=secret)
        clone = projects.clone_dir(record["project_id"])
        (clone / "hygiene.txt").write_text("hygiene\n", encoding="utf-8", newline="\n")

        captured = []
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=self.record_call(captured)):
            gitrepo.commit_paths(str(clone), ["hygiene.txt"], "hygiene commit")

        push_calls = [call for call in captured if "push" in call[0]]
        self.assertTrue(push_calls, "项目克隆写后自动 push 未执行")
        argv, env = push_calls[0]
        self.assertIn("credential.helper=", argv)
        self.assertNotIn(secret, " ".join(str(item) for item in argv))
        self.assertEqual(str(projects.askpass_path(record["project_id"], "state")), env.get("GIT_ASKPASS"))


if __name__ == "__main__":
    unittest.main()
