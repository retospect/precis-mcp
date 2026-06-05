"""Tests for the skill ingest scan-and-plan stage.

Pure tests against tmp_path directories — no DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.ingest.skill_ingest import (
    DEFAULT_CHUNK_BUDGET_CHARS,
    IngestFailure,
    IngestPlan,
    scan_skill_dir,
)
from precis.ingest.skill_template import DocResolver, Includer

# ── basic shape ──────────────────────────────────────────────────────


def test_scan_nonexistent_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_skill_dir(tmp_path / "does-not-exist")


def test_scan_empty_dir_returns_empty(tmp_path: Path) -> None:
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    assert r.failures == ()


def _write(p: Path, name: str, text: str) -> Path:
    """Helper: write ``text`` to ``p / name`` and return the path."""
    f = p / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


# ── happy paths ──────────────────────────────────────────────────────


def test_scan_single_valid_skill(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "precis-search-help.md",
        (
            "---\n"
            "id: precis-search-help\n"
            "flavor: reference\n"
            "---\n"
            "# precis-search-help\n\n"
            "## Find a paper by topic when you don't know the title\n\n"
            "Use `search(kind='paper', q='...')`.\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    assert len(r.plans) == 1

    p = r.plans[0]
    assert p.slug == "precis-search-help"
    assert p.frontmatter.flavor == "reference"
    assert "FLAVOR:reference" in p.tags
    assert len(p.chunks) == 2  # head + the one H2 section
    assert p.file_sha256  # non-empty hex


def test_scan_recurses_into_subdirs(tmp_path: Path) -> None:
    _write(tmp_path, "precis-a.md", "---\nflavor: concept\n---\n# A\nbody\n")
    _write(
        tmp_path,
        "personas/precis-b.md",
        "---\nflavor: persona\n---\n# B\n## Adopt this persona\nbe B\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    slugs = {p.slug for p in r.plans}
    assert slugs == {"precis-a", "precis-b"}


def test_scan_emits_requires_tag(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "p.md",
        (
            "---\n"
            "flavor: reference\n"
            "available-when: PRECIS_EPO_KEY\n"
            "---\n# title\n## op\nbody\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    [p] = r.plans
    assert "FLAVOR:reference" in p.tags
    assert "requires:PRECIS_EPO_KEY" in p.tags


# ── failures ─────────────────────────────────────────────────────────


def test_scan_invalid_flavor_becomes_failure(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "---\nflavor: vibes\n---\n# x\n")
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    assert f.slug == "bad"
    assert "flavor" in f.reason


def test_scan_oversized_chunk_becomes_failure(tmp_path: Path) -> None:
    big_body = "x" * (DEFAULT_CHUNK_BUDGET_CHARS + 100)
    _write(tmp_path, "fat.md", f"---\nflavor: reference\n---\n# t\n## op\n{big_body}\n")
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    assert "chunk-size budget" in f.reason
    assert "Split the section" in f.reason


def test_scan_oversized_chunk_under_custom_budget(tmp_path: Path) -> None:
    # When the caller raises the budget, the same content passes.
    big_body = "x" * 5000
    _write(tmp_path, "fat.md", f"---\nflavor: reference\n---\n# t\n## op\n{big_body}\n")
    r = scan_skill_dir(tmp_path, chunk_budget_chars=10000)
    assert r.failures == ()
    assert len(r.plans) == 1


def test_scan_empty_file_becomes_failure(tmp_path: Path) -> None:
    _write(tmp_path, "empty.md", "")
    r = scan_skill_dir(tmp_path)
    [f] = r.failures
    assert "no chunks produced" in f.reason


def test_scan_unresolved_include_becomes_failure(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "uses-include.md",
        (
            "---\nflavor: reference\n---\n"
            "# title\n## op\n"
            "{{include doc:does-not-exist#section}}\n"
        ),
    )
    includer = Includer(resolvers={"doc": DocResolver(docs={})})
    r = scan_skill_dir(tmp_path, includer=includer)
    assert r.plans == ()
    [f] = r.failures
    assert "include" in f.reason
    assert "does-not-exist" in f.reason


# ── include integration ─────────────────────────────────────────────


def test_scan_expands_includes_and_hash_reflects_resolved_text(
    tmp_path: Path,
) -> None:
    # Two scans with different included content → different hashes.
    skill_md = (
        "---\nflavor: reference\n---\n"
        "# title\n## op\n"
        "Use these conventions:\n\n"
        "{{include doc:precis-common#address-grammar}}\n"
    )
    _write(tmp_path, "skill.md", skill_md)

    common_v1 = "## Address grammar\nUse `slug~N`.\n"
    common_v2 = "## Address grammar\nUse `slug~N` or `slug#anchor`.\n"

    inc1 = Includer(resolvers={"doc": DocResolver(docs={"precis-common": common_v1})})
    inc2 = Includer(resolvers={"doc": DocResolver(docs={"precis-common": common_v2})})

    r1 = scan_skill_dir(tmp_path, includer=inc1)
    r2 = scan_skill_dir(tmp_path, includer=inc2)
    [p1] = r1.plans
    [p2] = r2.plans
    assert p1.file_sha256 != p2.file_sha256
    assert "Use `slug~N`." in p1.expanded_text
    assert "slug#anchor" in p2.expanded_text


# ── cross-reference: invokes_personas ────────────────────────────────


def test_scan_runbook_with_resolved_personas_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "personas/precis-reviewer-a.md",
        ("---\nflavor: persona\n---\n# A\n## Adopt this persona\nbe A\n"),
    )
    _write(
        tmp_path,
        "personas/precis-reviewer-b.md",
        ("---\nflavor: persona\n---\n# B\n## Adopt this persona\nbe B\n"),
    )
    _write(
        tmp_path,
        "precis-polish.md",
        (
            "---\n"
            "flavor: runbook\n"
            "invokes-personas:\n"
            "  - precis-reviewer-a\n"
            "  - precis-reviewer-b\n"
            "---\n"
            "# polish\n## Run a polish pass\nbody\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    assert len(r.plans) == 3


def test_scan_runbook_with_missing_persona_becomes_failure(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "personas/precis-reviewer-a.md",
        ("---\nflavor: persona\n---\n# A\n## Adopt this persona\nbe A\n"),
    )
    _write(
        tmp_path,
        "precis-polish.md",
        (
            "---\n"
            "flavor: runbook\n"
            "invokes-personas:\n"
            "  - precis-reviewer-a\n"
            "  - precis-reviewer-ghost\n"
            "---\n"
            "# polish\n## op\nbody\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    # Persona alone passes; runbook fails cross-validation.
    assert {p.slug for p in r.plans} == {"precis-reviewer-a"}
    [f] = r.failures
    assert f.slug == "precis-polish"
    assert "precis-reviewer-ghost" in f.reason


def test_scan_runbook_pointing_at_non_persona_becomes_failure(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "precis-not-a-persona.md",
        ("---\nflavor: reference\n---\n# r\n## op\nbody\n"),
    )
    _write(
        tmp_path,
        "precis-polish.md",
        (
            "---\n"
            "flavor: runbook\n"
            "invokes-personas:\n"
            "  - precis-not-a-persona\n"
            "---\n"
            "# polish\n## op\nbody\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert {p.slug for p in r.plans} == {"precis-not-a-persona"}
    [f] = r.failures
    assert "not FLAVOR:persona" in f.reason


def test_scan_runbook_without_invokes_personas_passes(tmp_path: Path) -> None:
    # Runbooks without persona orchestration are still legal.
    _write(tmp_path, "p.md", ("---\nflavor: runbook\n---\n# title\n## op\nbody\n"))
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    [p] = r.plans
    assert p.frontmatter.invokes_personas == ()


# ── shape checks ─────────────────────────────────────────────────────


def test_ingest_plan_is_immutable_dataclass() -> None:
    # Sanity: IngestPlan is frozen.
    fm_kwargs = dict(
        slug="x",
        file_path=Path("/tmp/x.md"),
        file_sha256="abc",
        frontmatter=__import__(
            "precis.handlers._skill_common", fromlist=["SkillFrontmatter"]
        ).SkillFrontmatter(),
        chunks=(),
        tags=(),
        expanded_text="",
    )
    plan = IngestPlan(**fm_kwargs)
    with pytest.raises((AttributeError, Exception)):
        plan.slug = "y"  # type: ignore[misc]


def test_ingest_failure_str_format() -> None:
    path = Path("/tmp/x.md")
    f = IngestFailure(slug="x", file_path=path, reason="oops")
    s = str(f)
    assert "[x]" in s
    assert "oops" in s
    assert str(path) in s
