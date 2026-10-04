"""Thread navigation keeps declared activity and validated handoffs in one view."""

import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/docs-index"


def _fixture(root: Path) -> Path:
    fleet = root / ".claude/fleet"
    fleet.mkdir(parents=True)
    (fleet / "threads.tsv").write_text("alpha\thigh\tno\tsonnet\n", encoding="utf-8")
    directory = root / "docs/backlog/threads"
    directory.mkdir(parents=True)
    for slug in ("alpha", "beta"):
        (directory / f"{slug}.md").write_text(
            "# Thread\n\n## Resume\n\n"
            "- **Pillar:** platform\n"
            "- **Next:** Inspect [work](#do-next).\n"
            "- **Blocked by:** None.\n"
            "- **Unblocks:** Reliable releases.\n"
            "- **Acceptance:** Green gate.\n"
            f"- **Worktree:** `{slug}`\n"
            "- **Builds:** One.\n"
            "- **Detail:** [Work](#do-next).\n\n",
            encoding="utf-8",
        )
        path = directory / f"{slug}.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Do next\n\nWork.\n",
            encoding="utf-8",
        )
    return directory


def test_activity_comes_from_roster_and_resume_is_bounded(tmp_path: Path) -> None:
    directory = _fixture(tmp_path)
    path = directory / "alpha.md"
    text = path.read_text(encoding="utf-8").replace(
        "\n## Do next", "\n## Thread context\n\n" + "History " * 500 + "\n## Do next"
    )
    path.write_text(text, encoding="utf-8")
    rows = runpy.run_path(str(SCRIPT))["thread_priorities"](tmp_path)
    assert any("[alpha](alpha.md#resume) | platform | active" in row for row in rows)
    assert any("[beta](beta.md#resume) | platform | dormant" in row for row in rows)
    assert any("Inspect [work](alpha.md#do-next)." in row for row in rows)
    assert any("Inspect [work](beta.md#do-next)." in row for row in rows)
    assert not any("](#do-next)" in row for row in rows)
    assert not any("History" in row for row in rows)


@pytest.mark.parametrize("roster", ["missing\thigh\n", "alpha\thigh\nalpha\thigh\n"])
def test_invalid_roster_rejected(tmp_path: Path, roster: str) -> None:
    _fixture(tmp_path)
    (tmp_path / ".claude/fleet/threads.tsv").write_text(roster, encoding="utf-8")
    with pytest.raises(ValueError, match="fleet target"):
        runpy.run_path(str(SCRIPT))["thread_priorities"](tmp_path)


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        ("**Builds:** One.", "**Builds missing:** One.", "expected Resume fields"),
        ("platform", "unknown", "unknown pillar"),
        ("#do-next", "missing.md", "missing Resume link"),
        ("#do-next", "#absent", "missing Resume anchor"),
        ("**Next:** Inspect", "**Next:** " + "word " * 351 + "Inspect", "exceeds 350"),
    ],
)
def test_invalid_resume_rejected(
    tmp_path: Path, before: str, after: str, error: str
) -> None:
    directory = _fixture(tmp_path)
    path = directory / "alpha.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(before, after), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=error):
        runpy.run_path(str(SCRIPT))["thread_priorities"](tmp_path)


def test_repository_navigation_is_complete() -> None:
    root = SCRIPT.parent.parent
    rows = runpy.run_path(str(SCRIPT))["thread_priorities"](root)
    threads = {p.stem for p in (root / "docs/backlog/threads").glob("*.md")} - {
        "README",
        "INDEX",
        "PRIORITIES",
    }
    rendered = {
        row.split("](", 1)[0].removeprefix("| [")
        for row in rows
        if row.startswith("| [")
    }
    assert rendered == threads
