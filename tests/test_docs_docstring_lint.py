"""`scripts/docs-index --lint`: docstring ceiling + doc/skill restatement, one owner per fact."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/docs-index"

SENTENCE = (
    "the planner mints one worker mintable subtask per due tick and a succeeded "
    "child blocks re mint until the auto check flips it done again"
)


def _lint(root: Path) -> list[str]:
    fn = runpy.run_path(str(SCRIPT))["lint_docstrings"]
    fn.__globals__["ROOT"] = root
    return fn()


def _pkg(root: Path, name: str, doc: str) -> None:
    d = root / "src" / name
    d.mkdir(parents=True)
    (d / "__init__.py").write_text(f'"""{doc}"""\n', encoding="utf-8")


def test_repo_docstring_layer_is_clean() -> None:
    """The committed tree passes — the lint is a ratchet, not advice."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--lint"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_over_ceiling_docstring_is_flagged(tmp_path: Path) -> None:
    _pkg(tmp_path, "big", "Big.\n\n" + "word " * 1600)
    _pkg(tmp_path, "ok", "Short.\n\n" + "word " * 1400)
    findings = _lint(tmp_path)
    assert len(findings) == 1
    assert "src/big/__init__.py" in findings[0] and "ceiling 1500" in findings[0]


@pytest.mark.parametrize(
    "where", ["docs/guide.md", "src/precis/data/skills/precis-x-help.md"]
)
def test_restated_sentence_is_flagged_in_docs_and_skills(
    tmp_path: Path, where: str
) -> None:
    _pkg(tmp_path, "pkg", f"Owner.\n\n{SENTENCE}.")
    doc = tmp_path / where
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(f"# T\n\nSee below. {SENTENCE}. Done.\n", encoding="utf-8")
    findings = _lint(tmp_path)
    assert len(findings) == 1
    assert "src/pkg/__init__.py" in findings[0] and where in findings[0]


def test_short_overlap_and_exempt_files_are_quiet(tmp_path: Path) -> None:
    _pkg(tmp_path, "pkg", f"Owner.\n\n{SENTENCE}.")
    (tmp_path / "docs/backlog").mkdir(parents=True)
    (tmp_path / "docs/backlog/item.md").write_text(SENTENCE, encoding="utf-8")
    (tmp_path / "docs/short.md").write_text(
        "the planner mints one worker mintable subtask per due tick", encoding="utf-8"
    )
    assert _lint(tmp_path) == []
