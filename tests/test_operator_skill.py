import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "aidev-operator" / "SKILL.md"
STANDARD_ENV = [
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
]

sys.path.insert(0, str(ROOT / "tests"))
from test_skill import cli_verbs, parse_frontmatter  # noqa: E402


def documented_operator_verbs(body):
    verbs = set()
    in_section = False
    for line in body.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## 2.")
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("| `"):
                first_cell = stripped.split("|")[1].strip()
                verbs.add(first_cell.strip("`").strip())
    return verbs


class OperatorSkillComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SKILL_PATH.is_file():
            raise AssertionError(f"skill file missing: {SKILL_PATH}")
        cls.text = SKILL_PATH.read_text(encoding="utf-8")
        cls.fields, cls.body = parse_frontmatter(cls.text)

    def test_frontmatter_required_fields(self):
        self.assertEqual("aidev-operator", self.fields.get("name"))
        self.assertEqual("aidev-operator", SKILL_PATH.parent.name)
        self.assertTrue(self.fields.get("description"), "description is required for directory-form skills")

    def test_frontmatter_type_and_when_to_use(self):
        self.assertIn(self.fields.get("type", "prompt"), {"prompt", "inline", "flow"})
        self.assertTrue(self.fields.get("whenToUse"), "whenToUse is required so the model can auto-invoke the skill")

    def test_body_not_empty(self):
        self.assertGreater(len(self.body.strip()), 500)

    def test_never_implement_boundary(self):
        self.assertIn("永远不实现任务", self.body)


class OperatorSkillDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_PATH.read_text(encoding="utf-8")
        _, cls.body = parse_frontmatter(cls.text)

    def test_operator_verbs_match_cli(self):
        implemented = cli_verbs("operator")
        documented = documented_operator_verbs(self.body)
        self.assertEqual(implemented, documented, "skill verb table must match the CLI operator verbs exactly")

    def test_agent_verbs_flagged_as_not_operator_usable(self):
        line = next((line for line in self.body.splitlines() if "agent 面动词" in line), "")
        agent_only = cli_verbs("agent") - cli_verbs("operator")
        for verb in sorted(agent_only):
            self.assertIn(verb, line, f"agent verb {verb} must be listed as not usable by the operator")

    def test_standard_allowed_env_documented(self):
        for name in STANDARD_ENV:
            self.assertIn(name, self.body)

    def test_ceremony_commands_documented(self):
        for token in ("--h1-actor", "--manual-results", "--on-behalf-of", "close --decision accept", "refreeze"):
            self.assertIn(token, self.body)

    def test_sigt_machinery_documented(self):
        for token in ("/claims", "mint_sigt", "handoff_token", "单行勿折行"):
            self.assertIn(token, self.body)

    def test_card_template_sections_documented(self):
        for token in ("角色", "入场", "输入材料", "收尾协议"):
            self.assertIn(token, self.body)

    def test_reconcile_checklist_documented(self):
        for token in ("一案一行", "sha256", "__pycache__"):
            self.assertIn(token, self.body)

    def test_freeze_door_semantics_documented(self):
        self.assertIn("Implemented 未钉扎", self.body)


if __name__ == "__main__":
    unittest.main()
