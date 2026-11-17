"""skill 版本自检纯函数（REQ-4/AC-05 safety-veto：漂移不得静默）。

check_skill_version_alignment(skill_text, version, commit)：分发全文首行标识
与期望 version@commit 一致 → None；漂移 → 漂移报告 dict（含 kind/reason/
expected/actual），类别覆盖：版本不符 / commit 不符 / 标识缺失 / 标识不可解析。
"""
import re

MARKER_RE = re.compile(r"^<!-- signet-skill v([^@]+)@([0-9a-f]{7,40}) -->\n?$")
EXPECTED_FORMAT = "<!-- signet-skill v{version}@{commit} -->"


def check_skill_version_alignment(skill_text, version, commit):
    """校验分发 skill 全文首行标识与运行版本的一致性（纯函数，钉名）。

    参数 skill_text 为分发全文（首行应含标识行）；version 期望版本
    （aidev.__version__）；commit 期望平台仓 HEAD 短哈希。一致返回 None；
    漂移返回报告 dict：kind ∈ version_mismatch / commit_mismatch /
    marker_missing / marker_unparseable。
    """
    text = skill_text or ""
    first_line, _, _ = text.partition("\n")
    match = MARKER_RE.match(first_line)
    if match is None:
        if "signet-skill" in first_line:
            return {
                "kind": "marker_unparseable",
                "reason": f"标识行不可解析: {first_line!r}",
                "expected": EXPECTED_FORMAT,
                "actual": first_line,
            }
        return {
            "kind": "marker_missing",
            "reason": "标识行缺失：分发全文首行不含 signet-skill 标识",
            "expected": EXPECTED_FORMAT.format(version=version, commit=commit),
            "actual": first_line or "(空行)",
        }
    actual_version, actual_commit = match.group(1), match.group(2)
    if actual_version != version:
        return {
            "kind": "version_mismatch",
            "reason": f"版本不符：标识 v{actual_version} ≠ 运行版本 v{version}",
            "expected": version,
            "actual": actual_version,
        }
    if actual_commit != commit:
        return {
            "kind": "commit_mismatch",
            "reason": f"commit 不符：标识 @{actual_commit} ≠ 平台仓 HEAD @{commit}",
            "expected": commit,
            "actual": actual_commit,
        }
    return None
