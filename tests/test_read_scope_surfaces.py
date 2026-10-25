"""gr512233 slice 3: toc/index/search/graph-footer write fences.

On a read-scope connection:

* ``get(kind='skill', id='toc')`` and the bare-get index omit
  ``scope: write`` skills and say how many went;
* ``get`` of a ``scope: write`` skill serves the stub (slice 1
  machinery, pinned here for the shipped verb skills);
* ``search(kind='skill')`` drops hits from ``scope: write`` skills
  and from fully dropped sections, and reports the hidden count;
* the graph footer and the search ``related:`` line never link a
  ``scope: write`` skill.

Write-scope output is byte-identical to today's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import precis.runtime.scope as scope_mod
from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from precis.skill_index import SearchHit
from tests._skill_corpus import _install_corpus


@pytest.fixture(autouse=True)
def _write_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        scope_mod.SERVE_READ_ONLY_ENV,
        "PRECIS_MCP_DENY_VERBS",
        "PRECIS_MCP_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


@pytest.fixture
def read_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")


@pytest.fixture
def skill() -> SkillHandler:
    return SkillHandler(hub=Hub())


_READ_SKILL = (
    "---\nid: read-skill\ntitle: R\nsummary: read things\n---\n"
    "# R\n\nhow to tag a ref the read way\n"
)
_WRITE_SKILL = (
    "---\nid: w-put\ntitle: W\nsummary: write things\nscope: write\n---\n"
    "# W\n\nhow to tag a ref by writing it\n"
)
_MIXED_SKILL = (
    "---\nid: mixed\ntitle: M\nsummary: mixed things\n---\n"
    "# M\n\n## Read part\n\nhow to tag a ref safely\n\n"
    "## Secret write part {write}\n\n"
    "how to tag a ref by mutating it: put(kind='x', text='tag')\n"
)


def _install(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_corpus(
        monkeypatch,
        {"read-skill": _READ_SKILL, "w-put": _WRITE_SKILL, "mixed": _MIXED_SKILL},
    )


# ── toc + index ──────────────────────────────────────────────────────


def test_toc_omits_write_only_skills_in_read_scope(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.get(id="toc").body
    assert "w-put" not in body
    assert "read-skill" in body
    assert "1 write-only skill hidden (read-only connection)" in body


def test_index_omits_write_only_skills_in_read_scope(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.get().body
    assert "w-put" not in body
    assert "1 write-only skill hidden (read-only connection)" in body


def test_toc_and_index_write_scope_is_unchanged(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    assert "w-put" in skill.get(id="toc").body
    assert "w-put" in skill.get().body
    assert "read-only connection" not in skill.get(id="toc").body


def test_real_toc_hides_the_verb_skills_in_read_scope(
    skill: SkillHandler, read_scope: None
) -> None:
    body = skill.get(id="toc").body
    for slug in (
        "precis-put-help",
        "precis-edit-help",
        "precis-delete-help",
        "precis-tag-help",
        "precis-link-help",
    ):
        assert slug not in body
    assert "write-only skills hidden (read-only connection)" in body


def test_verb_skill_stub_on_real_corpus(skill: SkillHandler, read_scope: None) -> None:
    body = skill.get(id="precis-put-help").body
    assert "summary:" in body
    assert "write recipes omitted" in body
    assert "put(kind='gripe', text='...')" in body
    # The stub carries no recipe lines — the only put( left is the
    # gripe carve-out inside the pointer.
    rest = body.replace("put(kind='gripe', text='...')", "")
    assert "put(" not in rest


def test_verb_skill_write_scope_serves_the_real_body(
    skill: SkillHandler,
) -> None:
    body = skill.get(id="precis-put-help").body
    assert "mint new refs" in body
    assert "write recipes omitted" not in body


# ── search ───────────────────────────────────────────────────────────


def test_search_drops_write_skill_and_dropped_chunk_hits(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="how to tag a ref").body
    assert "w-put" not in body
    # "mutating" evidence only lives in mixed's dropped section, so
    # mixed's overlap hit survives only on its read content — but the
    # dropped-line preview must never appear.
    assert "mutating it: put" not in body
    assert "write-scope" in body and "hidden" in body


def test_search_verbatim_hit_inside_dropped_section_is_hidden(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="how to tag a ref by mutating it").body
    # The only verbatim carrier is mixed's dropped section — its row
    # drops and the footer reports the hidden hit.
    assert "mixed" not in body
    assert "w-put" not in body
    assert "hidden" in body
    monkeypatch.delenv("PRECIS_MCP_DENY_VERBS")
    write_body = skill.search(q="how to tag a ref by mutating it").body
    assert "mixed" in write_body


def test_search_footer_counts_hidden_hits(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    # Verbatim query: one hit inside w-put, one inside mixed's dropped
    # section → both hidden, count reported.
    body = skill.search(q="tag a ref by", mode="verbatim").body
    assert "w-put" not in body
    assert "2 write-scope hits hidden — read-only connection" in body


def test_search_write_scope_is_unchanged(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="how to tag a ref").body
    assert "w-put" in body
    assert "write-scope" not in body


def test_real_search_add_a_tag_hides_verb_skill_hits(
    skill: SkillHandler, read_scope: None
) -> None:
    body = skill.search(q="add a tag").body
    assert "precis-tag-help" not in body
    assert "hidden" in body


# ── graph footer + related line ──────────────────────────────────────


def test_graph_footer_drops_write_only_neighbour(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "a": "---\nflavor: reference\n---\n# A\nsee [[w-put]] and [[b]]\n",
            "w-put": _WRITE_SKILL,
            "b": _READ_SKILL,
        },
    )
    body = skill.get(id="a").body
    # The body text still carries the expanded [[w-put]] wikilink —
    # only the footer's "linked:" row must not name a write skill.
    linked_line = next(l for l in body.splitlines() if "linked:" in l)
    assert "w-put" not in linked_line
    assert "b" in linked_line


def test_graph_footer_write_scope_keeps_neighbour(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "a": "---\nflavor: reference\n---\n# A\nsee [[w-put]]\n",
            "w-put": _WRITE_SKILL,
        },
    )
    assert "linked: w-put" in skill.get(id="a").body


# ── fence helpers (unit) ─────────────────────────────────────────────


def test_dropped_section_headings_finds_only_fully_dropped_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.skill import _dropped_section_headings
    from precis.utils.skill_scope import render

    text = (
        "# T\n\n## Read\n\nx\n\n## Gone {write}\n\ny\n\n"
        "## Inline\n\nkeep\n<!-- write -->\ndrop\n<!-- /write -->\n"
    )
    dropped = _dropped_section_headings(render(text, "read")[0])
    assert dropped == frozenset({"Gone"})


def test_fence_semantic_hits_drops_write_skill_and_dropped_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.skill import _fence_semantic_hits

    _install(monkeypatch)

    def read_render(slug: str):
        from precis.handlers.skill import (
            _dropped_section_headings,
            _norm_for_substr,
        )
        from precis.utils.skill_scope import render as scope_render

        text = {
            "read-skill": _READ_SKILL,
            "w-put": _WRITE_SKILL,
            "mixed": _MIXED_SKILL,
        }[slug]
        body, _ = scope_render(text, "read")
        search_body = "\n".join(
            line
            for line in body.splitlines()
            if not line.strip().startswith("(write recipe omitted")
        )
        return (
            search_body,
            _norm_for_substr(search_body),
            _dropped_section_headings(body),
        )

    hits = [
        SearchHit(
            slug="w-put", chunk_idx=0, heading="W", score=0.9, snippet="how to tag"
        ),
        SearchHit(
            slug="mixed",
            chunk_idx=1,
            heading="Secret write part",
            score=0.8,
            snippet="mutating",
        ),
        SearchHit(
            slug="read-skill",
            chunk_idx=0,
            heading="",
            score=0.7,
            snippet="how to tag",
        ),
    ]
    kept, hidden = _fence_semantic_hits(hits, read_render=read_render)
    assert [h.slug for h in kept] == ["read-skill"]
    assert hidden == {"w-put", "mixed"}


def test_fence_semantic_hits_dedups_hidden_by_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Land review 3: hidden counts distinct slugs — a skill whose two
    chunks hit reports once, not twice."""
    from precis.handlers.skill import _fence_semantic_hits

    _install(monkeypatch)
    hits = [
        SearchHit(slug="w-put", chunk_idx=i, heading="W", score=0.9, snippet="x")
        for i in range(3)
    ]
    kept, hidden = _fence_semantic_hits(
        hits, read_render=lambda s: ("", "", frozenset())
    )
    assert kept == []
    assert hidden == {"w-put"}


# ── land-review fixes ────────────────────────────────────────────────


def test_taproot_mint_keeps_rubric_and_drops_only_recipe_sections(
    skill: SkillHandler, read_scope: None
) -> None:
    """Land review 1: precis-taproot-mint-help is NOT whole-skill write
    — the claim rubric/notation policy stays readable; only the two
    mint recipe sections drop to the pointer."""
    body = skill.get(id="precis-taproot-mint-help").body
    assert "Claim admissibility" in body
    assert "Notation canon" in body
    assert "## Mint a claim hub" in body  # the slot's heading stays
    assert "write recipe omitted" in body
    assert "trimodal" not in body  # the recipe body is gone
    assert "precis taproot mint --spec" not in body


def test_taproot_mint_write_scope_serves_full_body(skill: SkillHandler) -> None:
    body = skill.get(id="precis-taproot-mint-help").body
    assert "trimodal" in body
    assert "Claim admissibility" in body
    assert "{write}" not in body  # markers strip on serve


def test_taproot_mint_write_render_is_byte_identical() -> None:
    """The marked file's write render equals the pre-marking blob —
    only the ``{write}`` suffixes differ, and they strip."""
    from precis.handlers.skill import _load_skills_map
    from precis.utils.skill_scope import render as scope_render

    text = _load_skills_map()["precis-taproot-mint-help"]
    write, _ = scope_render(text, "write")
    unmarked = text.replace(" {write}\n", "\n")
    assert write == unmarked


def test_related_line_filters_before_capping(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Land review 2: with write-only neighbours ahead of the readable
    one in sort order, the line must still show the readable one —
    filter first, then cap."""
    files = {"top": "---\nid: top\n---\n# T\nneedle phrase here\n"}
    for i in range(6):
        files[f"a-w{i}"] = f"---\nid: a-w{i}\nscope: write\n---\n# W{i}\nsee [[top]]\n"
    files["z-read"] = "---\nid: z-read\n---\n# Z\nsee [[top]]\n"
    _install_corpus(monkeypatch, files)
    body = skill.search(q="needle phrase").body
    related_line = next(l for l in body.splitlines() if l.startswith("related:"))
    assert "z-read" in related_line
    assert "a-w" not in related_line


def test_lexical_probe_never_matches_a_pointer_line(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Land review 4: pointer text is stripped before the probe check —
    a query matching only pointer words keeps no skill and quotes no
    pointer."""
    _install(monkeypatch)
    body = skill.search(q="write recipe omitted", mode="verbatim").body
    assert "mixed" not in body
    assert "w-put" not in body
    # No hit rows at all — the pointer is renderer output, not evidence.
    assert "skill match" not in body.split("\n")[0] or "0 " in body.split("\n")[0]
    assert "hidden" not in body  # nothing was evidence-hidden either


# ── corpus guard: every scope:write skill serves the stub in read ────

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"


def _write_only_slugs() -> list[str]:
    from precis.utils.skill_common import parse_frontmatter

    return sorted(
        p.stem
        for p in _SKILLS_DIR.glob("*.md")
        if parse_frontmatter(p.read_text(encoding="utf-8")).scope == "write"
    )


@pytest.mark.parametrize("slug", _write_only_slugs())
def test_every_write_only_skill_stubs_in_read_scope(
    skill: SkillHandler, read_scope: None, slug: str
) -> None:
    body = skill.get(id=slug).body
    assert "write recipes omitted" in body


@pytest.mark.parametrize("slug", _write_only_slugs())
def test_write_only_skill_write_scope_serves_body(
    skill: SkillHandler, slug: str
) -> None:
    """Write scope is byte-identical to the pre-marking serve — the
    frontmatter line rides along, no stub, no pointer."""
    text = (_SKILLS_DIR / f"{slug}.md").read_text(encoding="utf-8")
    body = skill.get(id=slug).body
    assert "write recipes omitted" not in body
    assert text.split("---", 2)[-1].strip().splitlines()[0] in body
