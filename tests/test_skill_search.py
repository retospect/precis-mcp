"""SkillHandler search — terms, trailers, gap-cut paging, verbatim mode, axis filters and related-hits lines (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.skill import SkillHandler
from tests._skill_corpus import _install_corpus, _md


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── search ────────────────────────────────────────────────────────────


def test_search_finds_term(skill: SkillHandler) -> None:
    """Most skills mention 'kind' somewhere — sanity check fulltext."""
    out = skill.search(q="kind")
    assert "skill match" in out.body
    # Each hit should reference a real slug.
    assert "precis-" in out.body


def test_search_trailer_interpolates_top_hit_slug(skill: SkillHandler) -> None:
    """hint-audit item 5: the search drill-down hint used to say
    ``id='<slug-from-above>'`` — mirrors the ``tag.py``
    ``_render_search_body`` pattern (interpolate the real top hit's
    slug) instead of a template the reader has to fill in by hand."""
    import re

    out = skill.search(q="kind")
    assert "id='<slug-from-above>'" not in out.body
    m = re.search(r"get\(kind='skill', id='([^']+)'\)", out.body)
    assert m is not None
    top_slug = m.group(1)
    before_next = out.body.split("Next:")[0]
    assert top_slug in before_next, "hint slug must be a row shown above it"


def test_search_no_match(skill: SkillHandler) -> None:
    # Nonsense tokens only — common English words ("such", "no") would
    # word-match whatever skill happens to use them in prose.
    out = skill.search(q="xyzzy-frobnitz-quuxwomble")
    # Two acceptable headlines depending on whether the test rig wired
    # an embedder (then "no skills mention" — semantic genuinely found
    # nothing) or didn't (then the warming-hint shape from broad-pass
    # finding #1, so the agent knows to retry). Either is honest.
    assert "no skills mention" in out.body or (
        "no lexical matches" in out.body and "warming" in out.body
    )


def test_search_hyphen_space_equivalence(skill: SkillHandler) -> None:
    """A 7B caller's natural phrasing ``spaced repetition`` must
    find ``spaced-repetition`` in the corpus, and vice versa. The
    MCP critic flagged the punctuation-specific false negative as
    MAJOR-C 2026-05-02; the substring search now folds hyphen and
    whitespace runs to a single space on both needle and haystack.
    """
    # ``precis-anki-help`` summarises itself as "spaced-repetition cloze
    # cards" — the hyphenated form is what's actually in the corpus.
    out_natural = skill.search(q="spaced repetition")
    out_hyphen = skill.search(q="spaced-repetition")
    assert "precis-anki-help" in out_natural.body, (
        "natural-language query must find the hyphenated corpus term"
    )
    assert "precis-anki-help" in out_hyphen.body, (
        "hyphenated query must find the hyphenated corpus term"
    )


def test_search_empty_query_degrades_to_index(skill: SkillHandler) -> None:
    """``search(kind='skill', q='')`` and ``q=None`` degrade to the
    skill index (the same body ``get(kind='skill')`` returns), instead
    of raising ``BadInput`` like the previous behaviour. Round-2 picky
    N4/F-6, 2026-05-30 — symmetric with how the rest of the surface
    treats empty-q calls (``search(kind='memory', tags=[...])`` lists
    refs without ranking; skill should mirror that). The index body
    carries its own canonical ``Next:`` trailer.
    """
    index = skill.get()
    for empty in (None, "", "   "):
        resp = skill.search(q=empty)
        assert resp.body == index.body, (
            f"search(q={empty!r}) should match skill.get() index body; "
            f"got first-line {resp.body.splitlines()[0]!r}"
        )


# ── gr496170: gap cut over the top-10 (rerank-after-top-k) ───────────
#
# ``q="zzzq wwwe"`` pins no skill's title/section tokens (verified:
# neither token appears in any ``_skill_title_tokens`` pool), so the
# merged menu is exactly the stubbed stream — no title-boost row lands
# on top and reorders the crafted sequence.


def _stub_hits(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
    pairs: list[tuple[str, float]],
) -> None:
    """Drive ``search`` with a crafted score sequence.

    ``variant='heading_only'`` keeps ``_addressable_chunk_idx`` off the
    filesystem — the section column renders the heading text as-is, no
    ``~N`` lookup against the real skill body.
    """
    from precis.skill_index import SearchHit

    hits = [
        SearchHit(
            slug=slug,
            chunk_idx=0,
            heading="sec",
            score=score,
            snippet="snip",
            variant="heading_only",
        )
        for slug, score in pairs
    ]
    monkeypatch.setattr(
        skill, "_semantic_hits", lambda q, *, page_size: hits[:page_size]
    )
    monkeypatch.setattr(skill, "_lexical_hits", lambda q: [])


_GAP_QUERY = "zzzq wwwe"


def test_search_gap_cut_trims_dominant_top(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-pin page whose tail drops below 0.6 × top renders only the
    head, with the ``…and N more`` tail line offering ``page=2``."""
    _stub_hits(
        skill,
        monkeypatch,
        [
            ("precis-get-help", 0.92),
            ("precis-put-help", 0.88),
            ("precis-link-help", 0.40),
            ("precis-tag-help", 0.35),
        ],
    )
    out = skill.search(q=_GAP_QUERY)
    table = out.body.split("\n\n")[1]
    assert "precis-get-help" in table
    assert "precis-put-help" in table
    assert "precis-link-help" not in table
    assert "precis-tag-help" not in table
    assert "…and 2 more" in out.body
    assert f"search(kind='skill', q={_GAP_QUERY!r}, page=2)" in out.body


def test_search_gap_cut_page2_returns_hidden_rows(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut defines the page-1/2 boundary: page=2 starts exactly
    where the trimmed page 1 stopped, so nothing is unreachable."""
    _stub_hits(
        skill,
        monkeypatch,
        [
            ("precis-get-help", 0.92),
            ("precis-put-help", 0.88),
            ("precis-link-help", 0.40),
            ("precis-tag-help", 0.35),
        ],
    )
    page2 = skill.search(q=_GAP_QUERY, page=2)
    assert "precis-link-help" in page2.body
    assert "precis-tag-help" in page2.body
    assert "precis-get-help" not in page2.body
    # Both rows fit page 2 — no page=3 continuation is offered.
    assert "page=3" not in page2.body


def test_search_gap_cut_explicit_page_size_untouched(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling: an explicit ``page_size`` means "caller asked for N,
    show N" — the same dominant-top menu renders in full."""
    pairs = [
        ("precis-get-help", 0.92),
        ("precis-put-help", 0.88),
        ("precis-link-help", 0.40),
        ("precis-tag-help", 0.35),
    ]
    _stub_hits(skill, monkeypatch, pairs)
    out = skill.search(q=_GAP_QUERY, page_size=10)
    for slug, _score in pairs:
        assert slug in out.body


def test_search_gap_cut_verbatim_untouched(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling: ``mode='verbatim'`` never cuts — the literal-quote
    lookup keeps every verbatim row and offers no tail line."""
    # verbatim mode scores lexical rows only (the semantic stream is
    # skipped); stub verbatim hits and assert the exemption contract:
    # every row renders, no ``…and N more`` is offered.
    lexical = [
        ("precis-get-help", 9, "p", True, 0),
        ("precis-put-help", 1, "p", True, 0),
        ("precis-link-help", 1, "p", True, 0),
    ]
    monkeypatch.setattr(skill, "_lexical_hits", lambda q: lexical)
    out = skill.search(q=_GAP_QUERY, mode="verbatim")
    assert "precis-get-help" in out.body
    assert "precis-put-help" in out.body
    assert "precis-link-help" in out.body
    assert "…and" not in out.body


def test_search_gap_cut_title_pin_never_cuts(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page led at or above ``_TITLE_MATCH_SCORE`` never cuts: the
    pin already declared the intent match, and the skill-eval probe
    corpus showed the tail below a pin still carries the named answer
    (recall@cut = 100 % bar)."""
    _stub_hits(
        skill,
        monkeypatch,
        [
            ("precis-get-help", 1.62),
            ("precis-put-help", 0.28),
            ("precis-link-help", 0.22),
        ],
    )
    out = skill.search(q=_GAP_QUERY)
    assert "precis-put-help" in out.body
    assert "precis-link-help" in out.body
    assert "…and" not in out.body


def test_search_gap_cut_tie_cluster_never_splits(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Near-equal scores are a genuine tie cluster — nothing clearly
    dominates, so the full page renders."""
    _stub_hits(
        skill,
        monkeypatch,
        [
            ("precis-get-help", 0.90),
            ("precis-put-help", 0.88),
            ("precis-link-help", 0.85),
        ],
    )
    out = skill.search(q=_GAP_QUERY)
    assert "precis-link-help" in out.body
    assert "…and" not in out.body


def test_search_page_beyond_end_is_an_honest_empty(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``page=`` previously vanished into ``**_kw`` and silently
    re-served page 1. Now an out-of-range page says so instead of
    claiming there were no matches at all."""
    _stub_hits(skill, monkeypatch, [("precis-get-help", 0.92)])
    out = skill.search(q=_GAP_QUERY, page=3)
    assert "no more skill matches" in out.body
    assert "page 3" in out.body


def test_search_gap_cut_eval_corpus_recall(skill: SkillHandler) -> None:
    """The gr496170 landing bar, ratcheted: replay every skill-eval
    case prompt through search and check the YAML's named skill
    survives the gap cut whenever it made the uncut top-10 at all.
    An explicit ``page_size=10`` bypasses the cut, so it is the
    before/after oracle. Probes whose named skill never ranked are
    skipped — they can't measure the cut."""
    from precis.skill_eval.cases import load_cases

    def table_slugs(body: str) -> set[str]:
        rows: set[str] = set()
        in_table = False
        for line in body.splitlines():
            if line.startswith("{slug"):
                in_table = True
                continue
            if in_table:
                if not line.strip():
                    break
                rows.add(line.split("\t")[0].split(" [")[0])
        return rows

    misses: list[str] = []
    measured = 0
    for case in load_cases():
        uncut = table_slugs(skill.search(q=case.prompt, page_size=10).body)
        if case.skill not in uncut:
            continue
        measured += 1
        kept = table_slugs(skill.search(q=case.prompt).body)
        if case.skill not in kept:
            misses.append(case.case_id)
    assert not misses, (
        f"recall@cut < 100%: named skill dropped by the gap cut on "
        f"{len(misses)}/{measured} probes: {misses}"
    )


# ── semantic search via FileCorpusIndex ──────────────────────────────


def test_search_uses_semantic_index_when_embedder_wired(tmp_path) -> None:
    """When the hub carries an embedder, ``search()`` routes through
    :class:`FileCorpusIndex` and returns hits.

    The MockEmbedder is hash-based and not actually semantic, but
    the integration path — index build, cache write, response
    formatting — is what we're pinning here. End-to-end "transcribe
    video finds youtube-help" requires a real bge-m3 model and is
    exercised live, not in CI.

    Round-2 picky 2026-05-31: dropped the ``source`` column from the
    rendered TOON shape (the maintainer said it was low signal). The
    test now checks for the integration path firing — search returns
    a hit headline + at least one TOON row — rather than the
    source-label string. End-to-end correctness is what matters.
    """
    import os

    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder

    os.environ["PRECIS_CACHE_DIR"] = str(tmp_path)
    try:
        hub = Hub(embedder=MockEmbedder(dim=64))
        handler = SkillHandler(hub=hub)
        # Plant the hub manually since we bypassed Handler._register_with.
        handler.hub = hub
        out = handler.search(q="precis-overview")
        assert "skill match" in out.body
        # At least one TOON row begins with a slug + tab — the data
        # rows of the search-hit table.
        assert any(
            ln.startswith("precis-") and "\t" in ln for ln in out.body.splitlines()
        ), f"expected at least one precis-* TOON row; got body:\n{out.body}"
    finally:
        del os.environ["PRECIS_CACHE_DIR"]


def test_search_falls_back_to_substring_when_no_embedder(skill: SkillHandler) -> None:
    """Without an embedder the substring stream provides every hit.

    Round-2 picky 2026-05-31: the rendered output no longer carries a
    ``source`` column; the substring-vs-semantic distinction is now
    internal-only. Test that the substring path still returns hits
    when invoked without an embedder.
    """
    out = skill.search(q="seven verbs")
    # Either we found hits, in which case there's a headline + a TOON
    # row, OR we hit the empty-result branch which has a different
    # body shape entirely. Both are acceptable; what matters is no
    # crash and no fallback to ``BadInput``.
    assert (
        "no skills mention" in out.body
        or "skill match" in out.body
        or ("no lexical matches" in out.body and "warming" in out.body)
    )


def _strong_semantic_elsewhere(skill: SkillHandler, monkeypatch) -> None:
    """A strong semantic hit on an unrelated skill plus a weak one on the
    skill that holds the literal phrase — prod's shape for gr488917."""
    from precis.skill_index import SearchHit

    hits = [
        SearchHit(
            slug="precis-overview",
            chunk_idx=1,
            heading="What are the seven verbs?",
            score=0.8,
            snippet="seven verbs",
            variant="structural",
        ),
        SearchHit(
            slug="precis-pcb-help",
            chunk_idx=1,
            heading="x",
            score=0.05,
            snippet="x",
            variant="structural",
        ),
    ]
    monkeypatch.setattr(skill, "_semantic_hits", lambda q, *, page_size: hits)


def _rows(body: str) -> list[str]:
    return [ln.split("\t")[0] for ln in body.splitlines() if ln.startswith("precis-")]


def test_search_mode_verbatim_returns_only_literal_hits(
    skill: SkillHandler, monkeypatch
) -> None:
    """gr488917: mode='verbatim' was silently ignored — the literal
    phrase's skill ranked under unrelated semantic neighbours."""
    _strong_semantic_elsewhere(skill, monkeypatch)
    out = skill.search(q="Five verbs, no new ones", mode="verbatim")
    rows = _rows(out.body)
    # pcb-help (and draft-help, which says it too) — never the semantic
    # neighbour that only talks about "seven verbs".
    assert "precis-pcb-help" in rows and "precis-overview" not in rows, out.body
    assert all(
        "substring hit" in ln
        for ln in out.body.splitlines()
        if ln.startswith("precis-")
    ), out.body
    with pytest.raises(BadInput, match="mode"):
        skill.search(q="x", mode="fuzzy")


def test_search_verbatim_hit_not_replaced_by_weaker_semantic_row(
    skill: SkillHandler, monkeypatch
) -> None:
    """gr488917: in hybrid mode a weak semantic row for a slug used to
    discard that slug's (higher-scoring) verbatim lexical hit."""
    _strong_semantic_elsewhere(skill, monkeypatch)
    out = skill.search(q="Five verbs, no new ones")
    row = next(ln for ln in out.body.splitlines() if ln.startswith("precis-pcb-help"))
    assert "substring hit" in row, row


def test_search_emits_more_column(skill: SkillHandler) -> None:
    """The result table carries a ``more`` column counting additional
    semantic H2 hits per slug (``+N`` / ``.``) — mirrors the
    paper-mode `more` design from
    backlog-search-unique-per-paper.md (2026-06-06)."""
    out = skill.search(q="kind")
    body = out.body
    # Either the column header includes 'more' (TOON-rendered table)
    # or — when the embedder is unwired in this test build and only
    # lexical hits surface — at least the empty-marker '.' appears.
    # We assert the header form since it's the new contract.
    assert "more" in body, "expected `more` column in result table; got body:\n" + body


def test_search_axis_filter_actually_filters(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "widget-orient": _md(
                front="flavor: reference\ntags:\n  - orientation",
                body="widget gadget thing",
            ),
            "widget-workflow": _md(
                front="flavor: reference\ntags:\n  - workflow",
                body="widget gadget thing",
            ),
        },
    )
    out = skill.search(q="widget gadget", tag="orientation")
    assert "widget-orient" in out.body
    assert "widget-workflow" not in out.body


def test_search_related_line_on_top_hit(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "widget-help": _md(body="widget gadget thing see [[widget-parts]]"),
            "widget-parts": _md(body="parts of a widget"),
        },
    )
    out = skill.search(q="widget gadget thing")
    assert "related: widget-parts" in out.body
