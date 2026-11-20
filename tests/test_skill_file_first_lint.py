"""Reject project file-first directions; explicit file-kind manuals are exempt."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src/precis/data/skills"
# These describe file addressing, code, or generated artifact formats.
ALLOWLIST = frozenset(
    {
        "precis-files-help",
        "precis-markdown-help",
        "precis-plaintext-help",
        "precis-tex-help",
        "precis-python-help",
        "precis-workspace-help",
        "precis-overview",
    }
)
FILE_FIRST = re.compile(
    r"under\s+`?PRECIS_ROOT|owns a workspace|real file access|"
    r"[Cc]reate a markdown file|[Dd]rop a new note file|"
    r"hand-maintained\s+`?\.(?:tex|md)",
    re.IGNORECASE,
)


def violations(name: str, text: str) -> set[str]:
    if name in ALLOWLIST:
        return set()
    return {f"{name}: {match.group(0)}" for match in FILE_FIRST.finditer(text)}


def test_no_new_file_first_wording() -> None:
    baseline = set(
        (ROOT / "tests/data/file_first_baseline.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    found: set[str] = set()
    for path in SKILLS.glob("*.md"):
        found.update(violations(path.stem, path.read_text(encoding="utf-8")))
    planner = ROOT / "src/precis/workers/planner_prompt.py"
    found.update(violations("planner_prompt", planner.read_text(encoding="utf-8")))
    assert not found - baseline, "File-first project guidance:\n" + "\n".join(
        sorted(found - baseline)
    )
    assert not baseline - found, "Remove obsolete file-first baseline entries"


@pytest.mark.parametrize(
    "phrase", ["under PRECIS_ROOT", "owns a workspace", "hand-maintained `.tex`"]
)
def test_synthetic_project_skill_is_rejected(phrase: str) -> None:
    assert violations("project-help", phrase)
    assert not violations("precis-tex-help", phrase)
