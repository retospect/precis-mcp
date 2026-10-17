"""Thread navigation keeps declared activity and validated handoffs in one view.

The fleet roster is each thread file's front matter (`roster`, `pillar`,
`effort`, optional `design-review`); there is no separate roster table.
"""

import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/docs-index"
THREADS = ROOT / "docs/backlog/threads"
NOT_THREADS = {"README", "INDEX", "PRIORITIES"}
# The retired roster table's file name, spelled so this file does not match.
RETIRED_ROSTER = "threads" + ".tsv"


def _pillars() -> set[str]:
    lint = (ROOT / "scripts/backlog-lint").read_text(encoding="utf-8")
    match = re.search(r"^PILLARS='([^']+)'", lint, re.M)
    assert match, "scripts/backlog-lint lost its PILLARS line"
    return set(match.group(1).split("|"))


def _front(roster: str, effort: str = "high", pillar: str = "platform") -> str:
    return f"---\nroster: {roster}\npillar: {pillar}\neffort: {effort}\n---\n"


def _fixture(root: Path) -> Path:
    directory = root / "docs/backlog/threads"
    directory.mkdir(parents=True)
    for slug, roster in (("alpha", "active"), ("beta", "dormant")):
        (directory / f"{slug}.md").write_text(
            _front(roster) + "# Thread\n\n## Resume\n\n"
            "- **Next:** Inspect [work](#do-next).\n"
            "- **Blocked by:** None.\n"
            "- **Unblocks:** Reliable releases.\n"
            "- **Acceptance:** Green gate.\n"
            f"- **Worktree:** `{slug}`\n"
            "- **Builds:** One.\n"
            "- **Detail:** [Work](#do-next).\n\n"
            "\n## Do next\n\nWork.\n",
            encoding="utf-8",
        )
    return directory


def _render(root: Path) -> list[str]:
    return runpy.run_path(str(SCRIPT))["thread_priorities"](root)


def test_activity_comes_from_front_matter_and_resume_is_bounded(
    tmp_path: Path,
) -> None:
    directory = _fixture(tmp_path)
    path = directory / "alpha.md"
    text = path.read_text(encoding="utf-8").replace(
        "\n## Do next", "\n## Thread context\n\n" + "History " * 500 + "\n## Do next"
    )
    path.write_text(text, encoding="utf-8")
    rows = _render(tmp_path)
    assert any("[alpha](alpha.md#resume) | platform | active" in row for row in rows)
    assert any("[beta](beta.md#resume) | platform | dormant" in row for row in rows)
    assert any("Inspect [work](alpha.md#do-next)." in row for row in rows)
    assert any("Inspect [work](beta.md#do-next)." in row for row in rows)
    assert not any("](#do-next)" in row for row in rows)
    assert not any("History" in row for row in rows)


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        ("roster: active", "roster: busy", "roster must be"),
        ("roster: active\n", "", "roster must be"),
        ("pillar: platform", "pillar: unknown", "unknown pillar"),
        ("effort: high", "effort: xhigh", "effort must be"),
        ("effort: high\n", "", "effort must be"),
        ("effort: high\n", "effort: high\ndesign-review: no\n", "design-review"),
    ],
)
def test_invalid_front_matter_rejected(
    tmp_path: Path, before: str, after: str, error: str
) -> None:
    path = _fixture(tmp_path) / "alpha.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(before, after, 1), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=error):
        _render(tmp_path)


def test_missing_front_matter_rejected(tmp_path: Path) -> None:
    path = _fixture(tmp_path) / "alpha.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text[text.index("# Thread") :], encoding="utf-8")
    with pytest.raises(ValueError, match="roster must be"):
        _render(tmp_path)


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        ("**Builds:** One.", "**Builds missing:** One.", "expected Resume fields"),
        ("#do-next", "missing.md", "missing Resume link"),
        ("#do-next", "#absent", "missing Resume anchor"),
        ("**Next:** Inspect", "**Next:** " + "word " * 351 + "Inspect", "exceeds 350"),
    ],
)
def test_invalid_resume_rejected(
    tmp_path: Path, before: str, after: str, error: str
) -> None:
    path = _fixture(tmp_path) / "alpha.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(before, after), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=error):
        _render(tmp_path)


def test_repository_navigation_is_complete() -> None:
    rows = _render(ROOT)
    threads = {p.stem for p in THREADS.glob("*.md")} - NOT_THREADS
    rendered = {
        row.split("](", 1)[0].removeprefix("| [")
        for row in rows
        if row.startswith("| [")
    }
    assert rendered == threads


def test_every_thread_file_carries_valid_roster_fields() -> None:
    front_matter = runpy.run_path(str(SCRIPT))["front_matter"]
    pillars = _pillars()
    bad = []
    for path in sorted(THREADS.glob("*.md")):
        if path.stem in NOT_THREADS:
            continue
        fm, _ = front_matter(path.read_text(encoding="utf-8"))
        if fm.get("roster") not in {"active", "dormant"}:
            bad.append(f"{path.name}: roster={fm.get('roster')!r}")
        if fm.get("pillar") not in pillars:
            bad.append(f"{path.name}: pillar={fm.get('pillar')!r}")
        if fm.get("effort") not in {"low", "medium", "high"}:
            bad.append(f"{path.name}: effort={fm.get('effort')!r}")
        if fm.get("design-review", "yes") != "yes":
            bad.append(f"{path.name}: design-review={fm.get('design-review')!r}")
        if "model" in fm:
            bad.append(f"{path.name}: model is a launch-time choice, not a field")
    assert not bad, bad


def test_docs_index_pillars_match_backlog_lint() -> None:
    order = runpy.run_path(str(SCRIPT))["_PILLAR_ORDER"]
    assert set(order) == _pillars()
    assert "operations" in order


def test_no_file_references_the_retired_roster_table() -> None:
    assert not (ROOT / ".claude/fleet" / RETIRED_ROSTER).exists()
    roots = [
        ROOT / "docs",
        ROOT / "scripts",
        ROOT / "tests",
        ROOT / ".claude/commands",
        ROOT / ".claude/fleet",
        ROOT / "src/precis/data/skills",
    ]
    hits = []
    for base in roots:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".png", ".pdf"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if RETIRED_ROSTER in text:
                hits.append(str(path.relative_to(ROOT)))
    for name in ("CLAUDE.md", "AGENTS.md"):
        if RETIRED_ROSTER in (ROOT / name).read_text(encoding="utf-8"):
            hits.append(name)
    assert not hits, hits
