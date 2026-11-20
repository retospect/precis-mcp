"""gr568491: skill search's fused multi-question form — ``args={'queries': [...]}``.

One intent in several of the caller's own phrasings, one ranked
section-level index: per-phrasing legs floored at an absolute relevance
minimum, reciprocal-rank-fused with per-query attribution (``q1,q4``),
every phrasing's top-1 row guaranteed regardless of fusion, fisheye caps
(≤3 rows per skill, ~20 total) and never a ``page=``. Distinct from
memory's ``batch=`` (independent subjects, labelled envelope) on
purpose — see the gr568491 ruling and the module docstring of
``precis.handlers.skill``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.skill import SkillHandler
from precis.runtime import PrecisRuntime
from precis.skill_index import SearchHit


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── crafting helpers ──────────────────────────────────────────────────

# Nonsense leg phrasings: no real skill's identity tokens, so the shared
# intent-pin pass provably adds nothing and the legs are exactly the
# stubbed streams.
_Q1 = "zzzq aaab phrasing"
_Q2 = "zzzq cccd phrasing"
_Q3 = "zzzq eeef phrasing"


def _hit(slug: str, idx: int, score: float, heading: str = "sec") -> SearchHit:
    """A structural hit — ``_addressable_chunk_idx`` returns ``idx`` as-is,
    keeping the section key off the filesystem."""
    return SearchHit(
        slug=slug,
        chunk_idx=idx,
        heading=heading,
        score=score,
        snippet="snip",
        variant="structural",
    )


def _stub_legs(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
    legs: dict[str, list[SearchHit]],
) -> None:
    """Drive the fused form with crafted per-leg semantic streams and no
    lexical stream — the RRF order is then fully determined by the stubs."""
    monkeypatch.setattr(
        skill,
        "_semantic_hits",
        lambda q, *, page_size, query_vec=None: legs.get(q, [])[:page_size],
    )
    monkeypatch.setattr(skill, "_lexical_hits", lambda q: [])


def _rows(body: str) -> list[str]:
    """First-column cells (``slug~N — heading`` / ``slug``) of the table."""
    return [
        ln.split("\t")[0]
        for ln in body.splitlines()
        if ln.startswith(("precis-", "many-"))
    ]


def _row_queries(body: str, slug: str) -> str:
    """The ``queries`` attribution cell for the row whose section cell
    names ``slug`` (exactly, or as the handle before a `` — label``)."""
    for ln in body.splitlines():
        if "\t" not in ln:
            continue
        cells = ln.split("\t")
        handle = cells[0].split(" — ", 1)[0]
        if handle == slug or handle.split("~", 1)[0] == slug:
            return cells[1]
    raise AssertionError(f"no row for {slug!r} in:\n{body}")


# ── fusion + attribution ──────────────────────────────────────────────


def test_fused_queries_agreement_rises_and_attributes(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A section hit by both phrasings outranks a section hit once at a
    better raw score — agreement is the signal — and the row names which
    queries hit it."""
    _stub_legs(
        skill,
        monkeypatch,
        {
            _Q1: [
                _hit("precis-aaa-help", 0, 0.95),
                _hit("precis-aaa-help", 1, 0.90),
            ],
            _Q2: [
                _hit("precis-aaa-help", 1, 0.99),
                _hit("precis-bbb-help", 0, 0.98),
            ],
        },
    )
    out = skill.search(queries=[_Q1, _Q2])
    rows = _rows(out.body)
    # aaa~1: 1/61 + 1/61 (two legs) beats aaa~0's single 1/61 even though
    # aaa~0's raw score (0.95) tops aaa~1's best (0.99 in q2, 0.90 in q1).
    assert rows[0].startswith("precis-aaa-help~1")
    assert rows[1].startswith("precis-aaa-help~0")
    assert _row_queries(out.body, "precis-aaa-help~1") == "q1,q2"
    assert _row_queries(out.body, "precis-aaa-help~0") == "q1"
    assert _row_queries(out.body, "precis-bbb-help") == "q2"
    assert "across 2 phrasings" in out.body


def test_fused_queries_q_rides_as_first_leg(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``q=`` plus ``queries=`` fuses with q as q1 — one leg list in the
    order given."""
    _stub_legs(
        skill,
        monkeypatch,
        {
            "zzzq primary": [_hit("precis-aaa-help", 0, 0.9)],
            _Q2: [_hit("precis-aaa-help", 0, 0.8), _hit("precis-bbb-help", 0, 0.7)],
        },
    )
    out = skill.search(q="zzzq primary", queries=[_Q2])
    assert _row_queries(out.body, "precis-aaa-help~0") == "q1,q2"
    assert "across 2 phrasings" in out.body


# ── per-leg floor ─────────────────────────────────────────────────────


def test_fused_queries_floor_drops_junk_leg(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leg whose only rows sit under the absolute floor contributes
    nothing — not even its top-1 — and the response names the dead leg."""
    _stub_legs(
        skill,
        monkeypatch,
        {
            _Q1: [_hit("precis-aaa-help", 0, 0.90)],
            # 0.20 < _SKILL_SEMANTIC_FLOOR (0.35): junk even at rank 1.
            _Q2: [_hit("precis-bbb-help", 0, 0.20)],
        },
    )
    out = skill.search(queries=[_Q1, _Q2])
    rows = _rows(out.body)
    assert rows[0].startswith("precis-aaa-help")
    assert "precis-bbb-help" not in out.body
    assert _row_queries(out.body, "precis-aaa-help~0") == "q1"
    assert "q2: no confident match" in out.body


def test_fused_queries_lexical_floor(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lexical regime floor is absolute too: a 2-word overlap
    (0.14 < _FUSED_LEXICAL_FLOOR) drops, a 5-word overlap (0.20) stays."""
    weak = [("precis-aaa-help", 2, "preview line", False, 0)]
    strong = [("precis-bbb-help", 5, "preview line", False, 0)]
    monkeypatch.setattr(skill, "_semantic_hits", lambda *a, **k: [])
    monkeypatch.setattr(skill, "_lexical_hits", lambda q: weak + strong)
    out = skill.search(queries=[_Q1])
    assert "precis-aaa-help" not in out.body
    assert "precis-bbb-help" in out.body
    # The kept row shows its honest evidence label.
    assert "5 word matches" in out.body


# ── top-1 guarantee + fisheye caps ────────────────────────────────────


def test_fused_queries_keeps_every_winner_with_three_rows_per_skill(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = [_Q1, _Q2, _Q3, "zzzq www phrasing", "zzzq vvv phrasing"]
    _stub_legs(
        skill,
        monkeypatch,
        {
            q: [_hit("precis-aaa-help", i, 0.9 - i * 0.01)]
            for i, q in enumerate(questions)
        },
    )
    out = skill.search(queries=questions)
    assert len(_rows(out.body)) == 3
    for n in range(3):
        assert _row_queries(out.body, f"precis-aaa-help~{n}") == f"q{n + 1}"
    assert "q4: get(kind='skill', id='precis-aaa-help~3')" in out.body
    assert "q5: get(kind='skill', id='precis-aaa-help~4')" in out.body


def test_fused_queries_reserves_eligible_winner_before_caps(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import skill as skill_mod

    _stub_legs(
        skill,
        monkeypatch,
        {
            _Q1: [
                _hit("precis-unavailable-help", 0, 0.99),
                _hit("precis-aaa-help", 1, 0.6),
            ],
            _Q2: [_hit("precis-bbb-help", 0, 0.9), _hit("precis-bbb-help", 1, 0.85)],
            _Q3: [_hit("precis-bbb-help", 0, 0.9), _hit("precis-bbb-help", 1, 0.85)],
        },
    )
    monkeypatch.setattr(skill_mod, "_FUSED_TOTAL_ROWS", 2)
    monkeypatch.setattr(
        skill_mod,
        "_availability_gap_parts",
        lambda slug, **kw: (
            ("missing tool", "missing tool")
            if slug == "precis-unavailable-help"
            else None
        ),
    )
    out = skill.search(queries=[_Q1, _Q2, _Q3], configured_only=True)
    assert len(_rows(out.body)) == 2
    assert _row_queries(out.body, "precis-aaa-help~1") == "q1"
    assert _row_queries(out.body, "precis-bbb-help~0") == "q2,q3"
    assert "precis-unavailable-help" not in out.body


def test_fused_queries_more_line_when_capped(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows dropped by the fisheye caps are counted and the narrowing
    door is named — never a page= continuation."""
    import precis.handlers.skill as skill_mod

    _stub_legs(
        skill,
        monkeypatch,
        {_Q1: [_hit(f"many-hits-{i:02d}", 0, 0.9 - i * 0.01) for i in range(8)]},
    )
    monkeypatch.setattr(skill_mod, "_FUSED_TOTAL_ROWS", 5)
    out = skill.search(queries=[_Q1])
    assert len(_rows(out.body)) == 5
    assert "…and 3 more section matches" in out.body
    assert "page=" not in out.body.split("Next:")[0]
    assert "never paginates" in out.body


# ── validation ────────────────────────────────────────────────────────


def test_fused_queries_near_duplicate_phrasings_fold(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case/punctuation-only differences are one leg, first occurrence
    wins; blanks drop silently."""
    _stub_legs(
        skill,
        monkeypatch,
        {_Q2: [_hit("precis-bbb-help", 0, 0.9)]},
    )
    out = skill.search(
        queries=[f"  {_Q2.upper()}! ", "   ", "tag a thought"],
    )
    # _Q2 folds with its punctuated/case-shifted twin → 2 legs survive.
    assert "across 2 phrasings" in out.body


def test_fused_queries_cap_refuses(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="at most 5"):
        skill.search(
            queries=["aa one", "bb two", "cc three", "dd four", "ee five", "ff six"]
        )
    with pytest.raises(BadInput, match="at most 5"):
        skill.search(
            q="ff six", queries=["aa one", "bb two", "cc three", "dd four", "ee five"]
        )


def test_fused_queries_type_refusals(skill: SkillHandler) -> None:
    from typing import Any

    not_a_list: Any = 42
    not_all_strings: list[Any] = ["ok phrasing", ["not", "a", "string"]]
    with pytest.raises(BadInput, match="list of phrasing"):
        skill.search(queries=not_a_list)
    with pytest.raises(BadInput, match=r"queries=\[1\]"):
        skill.search(queries=not_all_strings)


def test_fused_queries_paging_refused(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="never"):
        skill.search(queries=[_Q1], page=2)
    with pytest.raises(BadInput, match="never"):
        skill.search(queries=[_Q1], page_size=30)


def test_fused_queries_scope_refused(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="doesn't combine"):
        skill.search(q="zzzq", scope="precis-overview", queries=[_Q1])


def test_fused_queries_vacuous(skill: SkillHandler) -> None:
    """No usable phrasing and no q is a BadInput; vacuous queries= beside
    a real q degrades to the ordinary single-q search."""
    with pytest.raises(BadInput, match="no usable phrasings"):
        skill.search(queries=["", "   "])
    out = skill.search(q="kind", queries=["", "   "])
    assert "skill match" in out.body


def test_fused_queries_bare_string_coerced(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone-string queries= is one phrasing, not an iteration trap."""
    _stub_legs(skill, monkeypatch, {_Q1: [_hit("precis-aaa-help", 0, 0.9)]})
    out = skill.search(queries=_Q1)
    assert "across 1 phrasing" in out.body


# ── real-corpus behaviour ─────────────────────────────────────────────


def test_fused_queries_real_corpus_finds_skill(skill: SkillHandler) -> None:
    """End to end on the shipped corpus (no stubs, no embedder): the
    lexical leg carries the phrasing and the row is a copy-pasteable
    section handle with attribution."""
    out = skill.search(queries=["spaced repetition"])
    assert "precis-anki-help" in out.body
    assert "precis-anki-help~" in out.body
    assert _row_queries(out.body, "precis-anki-help").startswith("q1")


@pytest.mark.parametrize("nested", [False, True])
def test_fused_queries_dispatch_accepts_both_spellings(
    runtime_with_store: PrecisRuntime, nested: bool
) -> None:
    queries = {"queries": ["remember what I read"]}
    payload = {"__extras__": queries} if nested else queries
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "skill", "q": "spaced repetition", **payload},
    )
    assert "[error" not in out, out
    assert "section matches across 2 phrasings" in out, out


@pytest.mark.nightly(reason="replays the skill-eval corpus against the lexical index")
def test_fused_queries_eval_corpus_recall(skill: SkillHandler) -> None:
    """The real lexical floor retains supported targets and removes weak distractors."""
    from precis.handlers.skill import _lexical_row, _passes_fused_floor
    from precis.skill_eval.cases import load_cases

    named = rejected = 0
    for case in load_cases():
        for slug, count, preview, verbatim, compounds in skill._lexical_hits(
            case.prompt
        ):
            row = _lexical_row(
                slug, count, preview, verbatim=verbatim, compounds=compounds
            )
            kept = _passes_fused_floor(row)
            if slug == case.skill:
                named += 1
                assert kept, (case.case_id, slug, row.score)
            elif not kept:
                rejected += 1
    assert named >= 70
    assert rejected > 0


def test_semantic_floor_on_labelled_corpus_probes(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay labelled probes with controlled cosine evidence on either side of the cut."""
    from precis.skill_eval.cases import load_cases

    monkeypatch.setattr(skill, "_lexical_hits", lambda q: [])
    probes = load_cases()[:8]
    assert probes
    for case in probes:
        _stub_legs(
            skill,
            monkeypatch,
            {
                case.prompt: [
                    _hit(case.skill, 0, 0.36),
                    _hit("precis-off-topic-help", 0, 0.34),
                ],
            },
        )
        rows = skill._fused_leg_rows(case.prompt, mode_norm="semantic")
        assert [key for key, _row in rows] == [(case.skill, 0)]
