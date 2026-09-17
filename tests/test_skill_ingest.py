"""Tests for the skill ingest scan-and-plan stage.

Pure tests against tmp_path directories — no DB.
"""

from __future__ import annotations

import re
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


def test_scan_multiple_oversized_chunks_reported_in_one_pass(tmp_path: Path) -> None:
    # gr344818: a file with several over-budget H2s used to surface them
    # one re-run at a time (the gate raised on the first hit and stopped).
    # Both oversized sections must be named — with their sizes — in the
    # single failure this file produces.
    ops_body = "x" * (DEFAULT_CHUNK_BUDGET_CHARS + 100)
    views_body = "y" * (DEFAULT_CHUNK_BUDGET_CHARS + 250)
    _write(
        tmp_path,
        "fat.md",
        (
            f"---\nflavor: reference\n---\n# t\n"
            f"## Ops\n{ops_body}\n"
            f"## Views\n{views_body}\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    # Both oversized sections named — with distinct sizes — in one failure,
    # not just the first one hit.
    assert "'Ops'" in f.reason
    assert "'Views'" in f.reason
    sizes = [int(n) for n in re.findall(r"\((\d+) > \d+ chars\)", f.reason)]
    assert len(sizes) == 2
    assert all(n > DEFAULT_CHUNK_BUDGET_CHARS for n in sizes)
    assert sizes[0] != sizes[1]
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


# ── graph gates: [[slug]] links, tags:, kinds: (hard-fail, slice 2) ────


def test_scan_result_default_warnings_empty(tmp_path: Path) -> None:
    r = scan_skill_dir(tmp_path)
    assert r.warnings == ()


def test_scan_extracts_wikilinks(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "---\nflavor: reference\n---\n# A\n## op\nsee [[b]]\n")
    _write(tmp_path, "b.md", "---\nflavor: reference\n---\n# B\n## op\nbody\n")
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    by_slug = {p.slug: p for p in r.plans}
    assert by_slug["a"].links == ("b",)
    assert by_slug["b"].links == ()


def test_scan_wikilinks_deduplicated_and_self_link_dropped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\n---\n# A\n## op\n[[b]] [[a]] [[b]] again\n",
    )
    _write(tmp_path, "b.md", "---\nflavor: reference\n---\n# B\n## op\nbody\n")
    r = scan_skill_dir(tmp_path)
    [a] = [p for p in r.plans if p.slug == "a"]
    assert a.links == ("b",)


def test_scan_dangling_wikilink_is_hard_failure_by_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\n---\n# A\n## op\nsee [[does-not-exist]]\n",
    )
    r = scan_skill_dir(tmp_path)
    # Hard-fail by default (GRAPH_GATES_HARD_FAIL is True, slice 2): the
    # plan does not ship.
    assert r.plans == ()
    assert r.warnings == ()
    [f] = r.failures
    assert f.slug == "a"
    assert "dangling" in f.reason
    assert "does-not-exist" in f.reason


def test_scan_synth_slug_wikilink_is_not_dangling(tmp_path: Path) -> None:
    # precis-help / precis-status / precis-toc / toc are synthesised at
    # runtime (handlers/skill.py), never a file — a [[slug]] targeting
    # one is a valid resolvable link, not a dangling one.
    _write(
        tmp_path,
        "a.md",
        (
            "---\nflavor: reference\n---\n# A\n## op\n"
            "see [[precis-help]], [[precis-status]], [[precis-toc]], [[toc]]\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    assert r.warnings == ()


def test_scan_unknown_tag_is_hard_failure_by_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\ntags:\n  - not-a-real-tag\n---\n# A\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    assert "not-a-real-tag" in f.reason
    assert "graph gate" in f.reason


def test_scan_kind_named_tag_is_hard_failure_by_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\ntags:\n  - paper\n---\n# A\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    assert "paper" in f.reason
    assert "graph gate" in f.reason


def test_scan_empty_kinds_is_hard_failure_by_default(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\nkinds:\nstatus: active\n---\n# A\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.plans == ()
    [f] = r.failures
    assert "kinds" in f.reason
    assert "empty" in f.reason


def test_scan_absent_kinds_is_not_a_warning(tmp_path: Path) -> None:
    # kinds: entirely absent (not yet migrated) is legal — only a
    # present-but-empty kinds: is a finding.
    _write(tmp_path, "a.md", "---\nflavor: reference\n---\n# A\n## op\nbody\n")
    r = scan_skill_dir(tmp_path)
    assert r.warnings == ()


def test_scan_valid_graph_axes_produce_no_warnings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        (
            "---\nflavor: reference\ntags:\n  - orientation\n"
            "kinds:\n  - paper\n---\n# A\n## op\nsee [[b]]\n"
        ),
    )
    # A second skill shares the tag so the (always-on, separate)
    # singleton-tag lint doesn't fire and pollute this "clean" case.
    _write(
        tmp_path,
        "b.md",
        "---\nflavor: reference\ntags:\n  - orientation\n---\n# B\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    assert r.warnings == ()


def test_scan_singleton_tag_is_always_a_warning(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\ntags:\n  - orientation\n---\n# A\n## op\nbody\n",
    )
    _write(
        tmp_path,
        "b.md",
        "---\nflavor: reference\ntags:\n  - orientation\n---\n# B\n## op\nbody\n",
    )
    _write(
        tmp_path,
        "c.md",
        "---\nflavor: reference\ntags:\n  - workflow\n---\n# C\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    # "orientation" is shared by two skills — no lint. "workflow" is used
    # by only "c" — singleton lint fires.
    assert len(r.warnings) == 1
    assert "[c]" in r.warnings[0]
    assert "workflow" in r.warnings[0]
    assert "only this one skill" in r.warnings[0]


def test_scan_graph_gates_can_be_downgraded_to_warn_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GRAPH_GATES_HARD_FAIL is True by default (slice 2's last step); the
    # WARN-mode code path (slice 1's ship mode, while the 160-file sweep
    # was in flight) stays reachable/tested via an explicit downgrade.
    import precis.ingest.skill_ingest as skill_ingest_mod

    monkeypatch.setattr(skill_ingest_mod, "GRAPH_GATES_HARD_FAIL", False)
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\n---\n# A\n## op\nsee [[does-not-exist]]\n",
    )
    r = scan_skill_dir(tmp_path)
    assert r.failures == ()
    assert len(r.plans) == 1
    [w] = r.warnings
    assert "[a]" in w
    assert "dangling" in w
    assert "does-not-exist" in w


# ── DB-side tag parity: KIND: + topic tags (item 6) ────────────────────


def test_scan_emits_kind_tags(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        (
            "---\nflavor: reference\nkinds:\n  - paper\n  - patent\n---\n"
            "# A\n## op\nbody\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    [p] = r.plans
    assert "KIND:paper" in p.tags
    assert "KIND:patent" in p.tags


def test_scan_emits_topic_tags(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "---\nflavor: reference\ntags:\n  - orientation\n---\n# A\n## op\nbody\n",
    )
    r = scan_skill_dir(tmp_path)
    [p] = r.plans
    assert "topic:orientation" in p.tags


def test_scan_no_kind_or_topic_tags_when_axes_absent(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "---\nflavor: reference\n---\n# A\n## op\nbody\n")
    r = scan_skill_dir(tmp_path)
    [p] = r.plans
    assert p.tags == ("FLAVOR:reference",)


# ── the real shipped corpus (docs/backlog/skill-graph.md slice 2 accept-
# ance criterion) ───────────────────────────────────────────────────────


def test_shipped_skill_corpus_has_zero_gate_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard-fail gate (:data:`GRAPH_GATES_HARD_FAIL`) against the real
    ``src/precis/data/skills/`` tree: every ``[[slug]]`` wikilink resolves
    (file or synth), every ``tags:``/``kinds:`` entry is valid, and no
    chunk exceeds the size budget.

    Plugin kinds (``se``/``route``/``protein``/…) are validated against
    their real ``handle_codes`` modules directly rather than the
    installed dist-info: a dev image bakes ``pyproject.toml`` entry
    points at build time, so it can lag a freshly-added plugin
    registration (see "new core dep: image + UV_WITH" in project
    memory) — this test asserts against the current source tree's
    promise, not a possibly-stale build artifact.
    """
    from importlib.resources import files

    from precis.ingest.skill_template import DocResolver, Includer
    from precis.utils import handle_registry as hr
    from precis_bio import handles as bio_handles
    from precis_chem import handles as chem_handles
    from precis_estimate import handles as estimate_handles
    from precis_pathway import handles as pathway_handles
    from precis_se import handles as se_handles

    plugin_kind_codes: dict[str, str] = {}
    plugin_chunk_codes: dict[str, str] = {}
    for mod in (
        pathway_handles,
        estimate_handles,
        se_handles,
        chem_handles,
        bio_handles,
    ):
        plugin_kind_codes.update(mod.RECORD_CODES)
        plugin_chunk_codes.update(mod.CHUNK_CODES)
    monkeypatch.setattr(hr, "_plugins_loaded", True)
    monkeypatch.setattr(hr, "_plugin_kind_codes", plugin_kind_codes)
    monkeypatch.setattr(hr, "_plugin_chunk_codes", plugin_chunk_codes)

    skills_dir = Path(str(files("precis.data.skills")))
    docs = {p.stem: p.read_text(encoding="utf-8") for p in skills_dir.rglob("*.md")}
    includer = Includer(resolvers={"doc": DocResolver(docs=docs)})

    r = scan_skill_dir(skills_dir, includer=includer)
    assert r.failures == (), "\n".join(str(f) for f in r.failures)


def test_link_to_gate_failed_file_does_not_cascade(tmp_path: Path) -> None:
    """A file that fails a prior gate is still a valid wikilink target —
    one bad file must not turn an unrelated ``[[link]]`` dangling."""
    _write(
        tmp_path,
        "precis-broken.md",
        # runbook whose invokes_personas target doesn't exist → fails
        # the cross-reference gate, before the graph gates run.
        (
            "---\nflavor: runbook\ninvokes-personas: precis-nope\n---\n"
            "# broken\n## Run it\nbody\n"
        ),
    )
    _write(
        tmp_path,
        "precis-fine.md",
        (
            "---\nflavor: reference\ntags: workflow\n---\n"
            "# fine\n## Use it\nSee [[precis-broken]] for the runbook.\n"
        ),
    )
    r = scan_skill_dir(tmp_path)
    assert {f.slug for f in r.failures} == {"precis-broken"}
    assert {p.slug for p in r.plans} == {"precis-fine"}
