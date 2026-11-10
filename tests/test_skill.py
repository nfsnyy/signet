import re
import subprocess
import sys
import unittest
from pathlib import Path

from aidev import collaboration, domain

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "aidev-agent" / "SKILL.md"
_VERB_CACHE = {}


def cli_verbs(role):
    if role in _VERB_CACHE:
        return _VERB_CACHE[role]
    script = ROOT / f"aidev-{role}.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{script.name} --help exited {result.returncode}: {(result.stderr or '')[-200:]}")
    groups = re.findall(r"\{([^{}]+)\}", result.stdout)
    if not groups:
        raise AssertionError(f"no subcommand choices found in {script.name} --help output")
    group = max(groups, key=lambda item: len(item.split(",")))
    _VERB_CACHE[role] = {item.strip() for item in group.split(",") if item.strip()}
    return _VERB_CACHE[role]


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with a frontmatter block delimited by ---")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("frontmatter block is not closed") from error
    fields = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if "\t" in line or ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        if key in fields:
            raise ValueError(f"duplicate frontmatter key: {key}")
        fields[key] = value.strip()
    return fields, "\n".join(lines[end + 1:])


def documented_agent_verbs(body):
    verbs = set()
    in_section = False
    for line in body.splitlines():
        if line.startswith("## "):
            in_section = line.startswith("## 1.")
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("| `"):
                first_cell = stripped.split("|")[1].strip()
                verbs.add(first_cell.strip("`").strip())
    return verbs


class SkillComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SKILL_PATH.is_file():
            raise AssertionError(f"skill file missing: {SKILL_PATH}")
        cls.text = SKILL_PATH.read_text(encoding="utf-8")
        cls.fields, cls.body = parse_frontmatter(cls.text)

    def test_frontmatter_required_fields(self):
        self.assertEqual("aidev-agent", self.fields.get("name"))
        self.assertEqual("aidev-agent", SKILL_PATH.parent.name)
        self.assertTrue(self.fields.get("description"), "description is required for directory-form skills")

    def test_frontmatter_type_and_when_to_use(self):
        self.assertIn(self.fields.get("type", "prompt"), {"prompt", "inline", "flow"})
        self.assertTrue(self.fields.get("whenToUse"), "whenToUse is required so the model can auto-invoke the skill")

    def test_body_not_empty(self):
        self.assertGreater(len(self.body.strip()), 500)


class SkillDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_PATH.read_text(encoding="utf-8")
        _, cls.body = parse_frontmatter(cls.text)

    def test_agent_verbs_match_cli(self):
        implemented = cli_verbs("agent")
        documented = documented_agent_verbs(self.body)
        self.assertEqual(implemented, documented, "skill verb table must match the CLI agent verbs exactly")

    def test_operator_verbs_flagged_as_not_agent_usable(self):
        line = next((line for line in self.body.splitlines() if "operator 面动词" in line), "")
        operator_only = cli_verbs("operator") - cli_verbs("agent")
        for verb in operator_only:
            self.assertIn(verb, line, f"operator verb {verb} must be listed as not usable by the agent")

    def test_report_sections_documented(self):
        for section in domain.REPORT_SECTIONS:
            self.assertIn(f'"{section}"', self.body)

    def test_actor_alias_regex_matches_domain(self):
        pattern = domain.ACTOR_PATTERN.pattern
        self.assertIn(pattern, self.body)

    def test_collab_actions_documented(self):
        for action in sorted(collaboration.ANSWER_ACTIONS):
            self.assertRegex(self.body, re.compile(re.escape(action)))

    def test_workspace_layout_documented(self):
        for name in ("task.md", "AGENTS.md"):
            self.assertIn(name, self.body)
        for directory in domain.WORKSPACE_DIRECTORIES:
            self.assertIn(directory, self.body)

    def test_handoff_flow_documented(self):
        for token in ("--handoff", ".signet-token", "lease.renewed"):
            self.assertIn(token, self.body)


if __name__ == "__main__":
    unittest.main()
