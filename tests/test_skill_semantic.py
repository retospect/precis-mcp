"""SkillHandler semantic rows — anchors, chunk addresses and the chunk-id round-trip through get (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


def test_semantic_row_question_only_uses_skill_title_anchor() -> None:
    """A ``question_only`` hit has no heading of its own (it matched a
    front-matter question, not any one H2 section) — its display
    anchor is the skill's title so the row reads as "this skill", not
    a blank/empty section cell."""
    from precis.handlers.skill import _semantic_row, _skill_title
    from precis.skill_index import SearchHit

    hit = SearchHit(
        slug="precis-overview",
        chunk_idx=9,
        heading="",
        score=0.9,
        snippet="top-level orientation",
        variant="question_only",
    )
    row = _semantic_row(hit)
    assert row.section == _skill_title("precis-overview")
    assert row.section != ""


def test_semantic_row_heading_only_uses_its_own_heading_anchor() -> None:
    """A ``heading_only`` hit already carries the right heading text —
    it *is* the heading — so display doesn't need special-casing.

    gr259666: the hit's *own* ``chunk_idx`` (its position among the
    embedded twins, appended after every structural chunk — see
    :mod:`precis.skill_index.chunker`) is NOT a valid ``slug~N``
    address; ``_semantic_row`` resolves the twin back to its sibling
    structural chunk's real index by heading match, so the rendered
    section is prefixed with that resolved ``~N`` (here ``~2``, "Verb
    cheat-sheet"'s actual structural position in
    ``precis-overview.md``) rather than the twin's own out-of-range
    position (``3``).
    """
    from precis.handlers.skill import _semantic_row
    from precis.skill_index import SearchHit

    hit = SearchHit(
        slug="precis-overview",
        chunk_idx=3,
        heading="Verb cheat-sheet",
        score=0.9,
        snippet="Verb cheat-sheet",
        variant="heading_only",
    )
    row = _semantic_row(hit)
    assert row.section == "~2 — Verb cheat-sheet"


def test_semantic_row_structural_hit_prefixes_own_chunk_idx() -> None:
    """A ``structural`` hit's ``chunk_idx`` already IS the section's
    real position in ``chunk_by_h2(text)`` — no resolution needed, just
    rendering (gr259666)."""
    from precis.handlers.skill import _semantic_row
    from precis.skill_index import SearchHit

    hit = SearchHit(
        slug="precis-overview",
        chunk_idx=2,
        heading="Verb cheat-sheet",
        score=0.9,
        snippet="Verb cheat-sheet",
        variant="structural",
    )
    row = _semantic_row(hit)
    assert row.section == "~2 — Verb cheat-sheet"


def test_semantic_row_question_only_has_no_chunk_address() -> None:
    """A ``question_only`` hit matches no single section (front-matter
    question, not an H2), so it gets no ``~N`` prefix — only the
    skill-title anchor (gr259666; no address is honest here, a wrong
    one would round-trip to the wrong chunk)."""
    from precis.handlers.skill import _semantic_row
    from precis.skill_index import SearchHit

    hit = SearchHit(
        slug="precis-overview",
        chunk_idx=9,
        heading="",
        score=0.9,
        snippet="top-level orientation",
        variant="question_only",
    )
    row = _semantic_row(hit)
    assert not row.section.startswith("~")


def test_search_table_chunk_id_round_trips_through_get(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr259666: the search table's section column must render a
    copy-pasteable ``~N`` chunk id, and that id must actually resolve
    via ``get(kind='skill', id='<slug>~<N>')`` — the failure mode was
    an agent pasting the heading text into ``id='slug~Heading'`` and
    getting a ``BadInput`` (``_SKILL_ID_RE`` requires ``~\\d+``).
    """
    import re

    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.skill_index import index as skill_index_mod

    monkeypatch.setenv("PRECIS_CACHE_DIR", str(tmp_path))
    # The fresh cache makes the index build cold; on a loaded CI runner it
    # outlasts the 5 s interactive wait and the semantic ~N rows vanish.
    monkeypatch.setattr(skill_index_mod, "_BUILD_WAIT_S", 120.0)
    hub = Hub(embedder=MockEmbedder(dim=64))
    handler = SkillHandler(hub=hub)
    handler.hub = hub
    out = handler.search(q="precis-overview")
    m = re.search(r"~(\d+) — ", out.body)
    assert m is not None, f"expected a ~N chunk id in the table; got:\n{out.body}"
    n = int(m.group(1))
    # The row line containing the match must start with a real slug.
    line = next(ln for ln in out.body.splitlines() if f"~{n} — " in ln)
    slug = line.split("\t", 1)[0].strip()
    # Round-trip: the address get() accepts and returns real content.
    chunk = handler.get(id=f"{slug}~{n}")
    assert chunk.body.strip()


def test_semantic_search_distinct_section_count_skips_twin_variants(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``more`` column counts additional matching *sections* for a
    slug. A ``body_only``/``heading_only``/``question_only`` hit is an
    embedding-surface affordance, not a distinct section, so none of
    them may inflate the count — extending the pre-existing
    ``body_only`` exclusion to the v4 variants."""
    from precis.skill_index import SearchHit

    slug = "precis-overview"
    hits = [
        SearchHit(
            slug=slug,
            chunk_idx=0,
            heading="A",
            score=0.9,
            snippet="s",
            variant="structural",
        ),
        SearchHit(
            slug=slug,
            chunk_idx=1,
            heading="B",
            score=0.8,
            snippet="s",
            variant="structural",
        ),
        SearchHit(
            slug=slug,
            chunk_idx=2,
            heading="B",
            score=0.7,
            snippet="s",
            variant="body_only",
        ),
        SearchHit(
            slug=slug,
            chunk_idx=3,
            heading="B",
            score=0.6,
            snippet="s",
            variant="heading_only",
        ),
        SearchHit(
            slug=slug,
            chunk_idx=4,
            heading="",
            score=0.5,
            snippet="s",
            variant="question_only",
        ),
    ]
    monkeypatch.setattr(skill, "_semantic_hits", lambda q, *, page_size: hits)

    out = skill.search(q="zzzznonexistentqueryzzzz")
    rows = [ln for ln in out.body.splitlines() if ln.startswith(f"{slug}\t")]
    assert rows, f"expected a {slug} row; got body:\n{out.body}"
    # Two structural hits for this slug → one *additional* beyond the
    # row shown → "+1", not "+3" (which double-counts the three twins).
    assert "+1" in rows[0], rows[0]
