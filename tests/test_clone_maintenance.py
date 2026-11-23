"""服务端克隆维护时延回归（AC-19 协同返场：case-git-credentials 单案时延）。

本机 git spawn 单价 0.2~1.2s（验收环境更慢），存量克隆维护必须免 spawn 直返：
- ensure_clone 对已就绪克隆凭 .git/signet-credstate 标记直返（零 git 调用）；
- 凭据增删使标记失效，下一次全量维护并重落标记；
- fetch_integrate 单次 for-each-ref 取齐本地/远端 HEAD（省一次 spawn）；
- 克隆使用空模板（无样板 hooks/description），缩减数据目录扫描体积。
"""
import subprocess
import unittest
from pathlib import Path as _Path
from unittest import mock


def Path_url(target):
    return _Path(target).resolve().as_uri()

try:
    from .helpers import WorkflowTestCase
except ImportError:
    from helpers import WorkflowTestCase

from aidev import projects


class CloneMaintenanceTests(WorkflowTestCase):
    def make_bare(self, name):
        remote = self.path(name)
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                       check=True, capture_output=True)
        return remote

    def register(self, name):
        state = self.make_bare(f"{name}-state.git")
        code = self.make_bare(f"{name}-code.git")
        record = projects.register_project(
            name, Path_url(state), Path_url(code), None,
            f"owner-{name}", str(self.path("platform-root")), str(self.path("bound-none")),
            "project-builtin-fixed",
        )
        return record

    def record_call(self, captured):
        real_run = subprocess.run

        def recorder(*args, **kwargs):
            argv = list(args[0]) if args else list(kwargs.get("args", []))
            captured.append(argv)
            return real_run(*args, **kwargs)

        return recorder

    def git_spawns(self, captured):
        return [argv for argv in captured if argv and argv[0] == "git"]

    def test_ensure_clone_existing_hit_skips_all_git_spawns(self):
        record = self.register("perf-1")
        clone = projects.ensure_clone(record)  # 建克隆 + 落标记
        self.assertTrue(projects._credstate_path(clone).is_file())

        captured = []
        recorder = self.record_call(captured)
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=recorder), \
                mock.patch("aidev.projects.subprocess.run", side_effect=recorder):
            again = projects.ensure_clone(record)
        self.assertEqual(str(clone), str(again))
        self.assertEqual([], self.git_spawns(captured),
                         "凭据状态标记一致的存量克隆不应产生任何 git spawn")

    def test_credential_change_invalidates_credstate_once(self):
        record = self.register("perf-2")
        clone = projects.ensure_clone(record)

        projects.upsert_git_credential(record["project_id"], "state", "https_pat",
                                       secret="t17-perf-secret")
        captured = []
        recorder = self.record_call(captured)
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=recorder), \
                mock.patch("aidev.projects.subprocess.run", side_effect=recorder):
            projects.ensure_clone(record)
        self.assertTrue(self.git_spawns(captured), "凭据变更后应触发一次全量维护")

        captured.clear()
        recorder = self.record_call(captured)
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=recorder), \
                mock.patch("aidev.projects.subprocess.run", side_effect=recorder):
            projects.ensure_clone(record)
        self.assertEqual([], self.git_spawns(captured), "维护后标记恢复、再次直返")

    def test_fetch_integrate_enumerates_refs_in_one_call(self):
        record = self.register("perf-3")
        clone = projects.ensure_clone(record)

        captured = []
        recorder = self.record_call(captured)
        with mock.patch("aidev.gitrepo.subprocess.run", side_effect=recorder), \
                mock.patch("aidev.projects.subprocess.run", side_effect=recorder):
            projects.fetch_integrate(str(clone))

        ref_calls = [argv for argv in self.git_spawns(captured) if "for-each-ref" in argv]
        rev_parse_calls = [argv for argv in self.git_spawns(captured) if "rev-parse" in argv]
        self.assertEqual(1, len(ref_calls), "HEAD/远端 HEAD 应单次 for-each-ref 取齐")
        self.assertEqual([], rev_parse_calls)

    def test_clone_uses_empty_template(self):
        record = self.register("perf-4")
        clone = projects.ensure_clone(record)
        samples = list((clone / ".git" / "hooks").glob("*.sample")) if (clone / ".git" / "hooks").is_dir() else []
        self.assertEqual([], samples, "克隆不应复制样板 hooks（数据目录扫描体积）")
        self.assertFalse((clone / ".git" / "description").exists())


if __name__ == "__main__":
    unittest.main()
