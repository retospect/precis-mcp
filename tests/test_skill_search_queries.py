"""SkillHandler ``queries=`` — multi-phrasing fused discovery (gr568491).

Each phrasing runs the same two retrieval streams as the single-q
path; tests stub ``_semantic_hits``/``_lexical_hits`` so fusion,
floors, fisheye caps and the rendered table stay deterministic.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.skill import SkillHandler
from precis.skill_index import SearchHit


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


def _hit(
    slug: str,
    idx: int,
    score: float,
    *,
    heading: str = "sec",
    variant: str = "structural",
) -> SearchHit:
    return SearchHit(
        slug=slug,
        chunk_idx=idx,
        heading=heading,
        score=score,
        snippet=f"{slug} chunk {idx} text",
        variant=variant,
    )


def _stub(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
    sem: dict[str, list[SearchHit]],
    lex: dict[str, list[tuple[str, int, str, bool, int]]] | None = None,
) -> None:
    monkeypatch.setattr(
        skill,
        "_semantic_hits",
        lambda q, *, page_size, query_vec=None: list(sem.get(q, []))[:page_size],
    )
    monkeypatch.setattr(skill, "_lexical_hits", lambda q: list((lex or {}).get(q, [])))


def _row_lines(body: str, slug: str) -> list[str]:
    """Table rows naming ``slug`` — the Next: hints embed the top id
    inside a ``get(kind='skill', …)`` call, so exclude call lines."""
    return [ln for ln in body.splitlines() if slug in ln and "kind='skill'" not in ln]


# ── the fused menu ───────────────────────────────────────────────────


def test_queries_fuses_phrasings_by_rank(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A section two phrasings agree on outranks a section only one
    phrasing hit — agreement is the signal (gr568491 item 1)."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq kxpt": [
                _hit("precis-get-help", 1, 0.9),
                _hit("precis-put-help", 2, 0.5),
            ],
            "wwwe mvbn": [
                _hit("precis-put-help", 2, 0.9),
                _hit("precis-link-help", 1, 0.8),
            ],
        },
    )
    out = skill.search(queries=["zzzq kxpt", "wwwe mvbn"])
    body = out.body
    assert "q1: zzzq kxpt" in body
    assert "q2: wwwe mvbn" in body
    assert "precis-put-help~2" in body
    assert "precis-get-help~1" in body
    assert "precis-link-help~1" in body
    # put~2 (both legs) leads; get~1 (leg1 rank1) precedes link~1
    # (leg2 rank2) in the RRF order.
    assert body.index("precis-put-help~2") < body.index("precis-get-help~1")
    assert body.index("precis-get-help~1") < body.index("precis-link-help~1")


def test_queries_rows_show_which_phrasings_hit(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``q`` column names the phrasing indices that voted for the
    row — the vocabulary cue the agent reads before fetching."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [_hit("precis-get-help", 1, 0.9)],
            "wwwe": [_hit("precis-get-help", 1, 0.8)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    [row] = _row_lines(out.body, "precis-get-help~1")
    assert "q1,q2" in row


def test_queries_rows_are_sections(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows carry the addressable ``slug~N`` id so step two is a
    plain ``get(kind='skill', id='slug~N')``."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [_hit("precis-get-help", 3, 0.9, heading="Install")],
            "wwwe": [_hit("precis-get-help", 3, 0.7, heading="Install")],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    assert "precis-get-help~3" in out.body
    [row] = _row_lines(out.body, "precis-get-help~3")
    assert "Install" in row


def test_queries_every_leg_keeps_its_top1(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A distinct sub-task's best row survives the fused caps — leg2's
    top-1 reserves a slot within the per-skill cap (gr568491 item 2)."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [_hit("precis-get-help", i, 0.9 - i * 0.05) for i in range(4)],
            "wwwe": [_hit("precis-get-help", 5, 0.9)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    # ~5 reserves a slot before the fused tail fills the cap.
    assert "precis-get-help~5" in out.body
    assert len(_row_lines(out.body, "precis-get-help~")) <= 3


def test_queries_per_skill_cap(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No skill owns more than _FUSED_PER_SKILL_CAP rows; the tail
    renders as "+N more — narrow" (gr568491 item 3)."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [_hit("precis-get-help", i, 0.9 - i * 0.05) for i in range(6)],
            "wwwe": [_hit("precis-put-help", 0, 0.9)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    assert len(_row_lines(out.body, "precis-get-help~")) == 3
    assert "more sections" in out.body


def test_queries_marks_empty_legs(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(
        skill,
        monkeypatch,
        {"zzzq": [_hit("precis-get-help", 1, 0.9)], "wwwe": []},
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    assert "q2: wwwe  (no leg hits)" in out.body


def test_queries_all_empty_legs_is_honest_empty(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(skill, monkeypatch, {"zzzq": [], "wwwe": []})
    out = skill.search(queries=["zzzq", "wwwe"])
    assert "no skill sections" in out.body or "no lexical matches" in out.body
    assert "get(kind='skill'" in out.body  # a second-step door


def test_queries_semantic_floor_drops_sub_floor_rows(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cosine row under _SKILL_SEMANTIC_FLOOR never reaches the
    fusion — the absolute per-leg floor (gr568491 item 4)."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [
                _hit("precis-get-help", 1, 0.9),
                _hit("precis-put-help", 2, 0.2),  # below the 0.35 floor
            ],
            "wwwe": [_hit("precis-get-help", 1, 0.8)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    assert "precis-put-help" not in out.body


def test_queries_lexical_floor_drops_weak_overlap(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare two-word overlap (score 0.14) is not leg evidence —
    under _SKILL_LEXICAL_FLOOR it drops before fusing."""
    _stub(
        skill,
        monkeypatch,
        {"zzzq": [], "wwwe": [_hit("precis-get-help", 1, 0.9)]},
        lex={
            "zzzq": [("precis-put-help", 2, "two words here", False, 0)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    assert "precis-put-help" not in out.body


def test_queries_lexical_counts_need_section_evidence(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-skill counts cannot substitute for addressable section evidence."""
    _stub(
        skill,
        monkeypatch,
        {"zzzq": [], "wwwe": [_hit("precis-get-help", 1, 0.9)]},
        lex={
            "zzzq": [("precis-put-help", 3, "three words here", False, 0)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"])
    rows = _row_lines(out.body, "precis-put-help")
    assert not rows, "whole-skill counts without section evidence must be dropped"


# ── arg contract ──────────────────────────────────────────────────────


def test_queries_dedupes_near_identical_phrasings(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-token-set rewordings fold — the legend numbers the kept
    phrasings, not the raw entries."""
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq one": [_hit("precis-get-help", 1, 0.9)],
            "wwwe two": [_hit("precis-put-help", 1, 0.9)],
        },
    )
    out = skill.search(queries=["zzzq one", "one zzzq", "zzzq one!", "wwwe two"])
    assert "q1: zzzq one" in out.body
    assert "q2: wwwe two" in out.body
    assert "q3:" not in out.body


def test_queries_q_counts_as_first_phrasing(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(
        skill,
        monkeypatch,
        {
            "zzzq": [_hit("precis-get-help", 1, 0.9)],
            "wwwe": [_hit("precis-put-help", 1, 0.9)],
        },
    )
    out = skill.search(q="zzzq", queries=["wwwe"])
    assert "q1: zzzq" in out.body
    assert "q2: wwwe" in out.body


def test_queries_single_effective_phrasing_still_uses_sections(
    skill: SkillHandler,
) -> None:
    """Even one queries= phrasing retains the section-only contract."""
    out = skill.search(queries=["memory search"])
    assert "skill section" in out.body or "no lexical matches" in out.body
    assert "q1: memory search" in out.body


def test_queries_refuses_over_raw_cap(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="max 8"):
        skill.search(queries=[f"zzzq phrasing {i}" for i in range(9)])


def test_queries_refuses_over_leg_cap(skill: SkillHandler) -> None:
    # Six genuinely distinct phrasings — near-identical strings would
    # dedupe before the cap applies.
    with pytest.raises(BadInput, match="caps at 5"):
        skill.search(
            queries=[
                "alpha bravo charlie",
                "delta echo foxtrot",
                "golf hotel india",
                "juliet kilo lima",
                "mike november oscar",
                "papa quebec romeo",
            ]
        )


def test_queries_refuses_bad_shape(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="queries= must be a list"):
        skill.search(queries=cast(Any, 42))


def test_queries_refuses_scope(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="doesn't combine with scope"):
        skill.search(q="zzzq", scope="precis-get-help", queries=["wwwe"])


def test_queries_refuses_page(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="not paged"):
        skill.search(queries=["zzzq", "wwwe"], page=2)


def test_queries_refuses_page_size(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="page_size"):
        skill.search(queries=["zzzq", "wwwe"], page_size=5)


def test_queries_lexical_mode_still_fuses(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mode='lexical' legs still fuse — the semantic stream is
    skipped, the word-overlap leg alone votes."""
    _stub(
        skill,
        monkeypatch,
        {"zzzq": [], "wwwe": []},
        lex={
            "zzzq": [("precis-put-help", 4, "four words matched", False, 1)],
            "wwwe": [("precis-put-help", 3, "three more", False, 0)],
        },
    )
    out = skill.search(queries=["zzzq", "wwwe"], mode="lexical")
    rows = _row_lines(out.body, "precis-put-help")
    assert not rows  # fabricated skill-level evidence has no matching section


def test_queries_eval_corpus_section_recall(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calibrate lexical floors with real gap-cut corpus retrieval scores.

    Isolate the named skill after retrieval so the measurement reflects
    flooring, rather than unrelated changes from the 20-row display cap.
    Hash mock embeddings cannot calibrate semantic cosine relevance.
    """
    from collections import Counter

    from precis.skill_eval.cases import load_cases

    retrieve = skill._ranked_rows_full
    misses: list[str] = []
    measured = 0
    for case in load_cases():
        rows, _, _, hits = retrieve(
            case.prompt,
            mode_norm="lexical",
            page_size=20,
        )
        target = [row for row in rows if row.slug == case.skill]
        if not target:
            continue
        measured += 1
        # The fused path whitespace-normalises each phrasing
        # (_dedupe_phrasings) before it reaches _ranked_rows_full —
        # most YAML prompts are multi-line, so match on the
        # normalised form or the stub never fires.
        norm_prompt = " ".join(case.prompt.split())
        with monkeypatch.context() as patch:
            patch.setattr(
                skill,
                "_ranked_rows_full",
                lambda q, target=target, norm=norm_prompt, hits=hits, **kw: (
                    target if q == norm else [],
                    Counter(),
                    set(),
                    hits,
                ),
            )
            result = skill.search(
                queries=[case.prompt, "zxqv unrelated"],
                mode="lexical",
            )
        if case.skill + "~" not in result.body:
            misses.append(case.case_id)
    assert measured > 0
    assert not misses, f"floored fusion lost named probe sections: {misses}"


def test_queries_refuses_incompatible_winner_cap(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins removed: real title pins (e.g. "alpha"→precis-math-help)
    # legitimately join legs as section candidates — the scenario needs
    # all four legs' top-1 inside ONE skill to trip the winner cap.
    monkeypatch.setattr(
        "precis.handlers.skill._skill_title_tokens",
        lambda slug: (frozenset(), frozenset()),
    )
    phrasings = ["alpha", "bravo", "charlie", "delta"]
    _stub(
        skill,
        monkeypatch,
        {q: [_hit("precis-get-help", i, 0.9)] for i, q in enumerate(phrasings)},
    )
    with pytest.raises(BadInput, match="three sections"):
        skill.search(queries=phrasings)


def test_queries_title_boost_needs_section_evidence(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.skill import _fused_leg_rows, _SkillSearchRow

    monkeypatch.setattr(
        "precis.handlers.skill._load_skill",
        lambda slug: "## Unrelated\nNothing matches here.",
    )
    row = _SkillSearchRow(
        slug="precis-get-help",
        score=2.5,
        source="title",
        section="title match",
        snippet="zzzq",
    )
    assert not _fused_leg_rows("zzzq", [row], [])
