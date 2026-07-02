"""Byte-equivalence guard for the ADR 0038 step-2 prompt refactor.

``llm_summarize.build_messages`` (and ``briefing``'s summarize prompt) were
folded onto the shared ``utils.prompt`` assembler + the new
:class:`LiteLLMAdapter`. This is a *pure* refactor: the OpenAI ``messages``
posted to the litellm ``summarizer`` alias must be **byte-identical** to the
hand-rolled prompt they replaced.

The reference is a verbatim copy of the pre-refactor implementations
(``_legacy_build_messages`` / ``_legacy_briefing_messages``), captured here
as the known-good golden. Each test asserts the new path reproduces it
exactly for a representative input.

Documented mapping (see :class:`LiteLLMAdapter`): the three CACHED blocks
(instruction + examples + doc-header) join into the leading ``system``
message; the single VARIABLE block (the passage) is the trailing ``user``
message. The doc-header stays in ``system`` — as the shipped code and Shot 2
put it — because it is the stable per-document KV-cache prefix; routing it to
the ``user`` turn (as a naive "variable→user" reading would) would move bytes
and is *not* done here.
"""

from __future__ import annotations

from precis.workers.briefing import _SYSTEM_PROMPT, _build_briefing_messages
from precis.workers.llm_summarize import _BRIEF_MAX_WORDS, _Claimed, build_messages

# --------------------------------------------------------------------------
# golden reference — a verbatim copy of the PRE-refactor implementations
# --------------------------------------------------------------------------


def _kind_noun(ref_kind: str) -> str:
    return {
        "paper": "scientific paper",
        "patent": "patent",
        "conv": "conversation",
    }.get(ref_kind, "document")


def _legacy_build_messages(claim: _Claimed, *, doc_card: str) -> list[dict[str, str]]:
    noun = _kind_noun(claim.ref_kind)
    header = doc_card.strip() or f"Title: {claim.title}".strip()
    system = (
        "You summarize a single passage from a larger document, "
        "as a navigation gloss.\n"
        "Output EXACTLY two lines and nothing else:\n"
        f"BRIEF: <a self-contained gist in one clause, at most {_BRIEF_MAX_WORDS} words>\n"
        "DETAIL: <1-3 terse fragments adding specifics NOT already in BRIEF — "
        "quantities, named entities, method, caveats>\n"
        "DETAIL is always shown appended to BRIEF, never on its own, so it "
        "must read as a continuation and never repeat anything in BRIEF.\n"
        "Be faithful — never invent facts. Write both lines telegraphically: "
        "plain, no preamble, no markdown, and drop leading articles and "
        "pronouns. Spell out abbreviations when standard and unambiguous (keep "
        "unit/element symbols, DNA, pH); never reuse source-only labels. Put a "
        "space between a number and its unit and reproduce quantities verbatim.\n"
        "If the passage is not prose — a data table, coordinate dump, reference "
        "list, or copyright/masthead boilerplate — set BRIEF to a short "
        "parenthetical tag naming it (e.g. (tabular data), (atomic coordinates), "
        "(copyright notice), (publication metadata), (reference list)) and leave "
        "DETAIL empty.\n\n"
        "Seven examples (style only — do NOT summarize these):\n"
        "PASSAGE: We synthesized a cobalt complex bearing pendant amine groups "
        "and tested it for proton reduction in acidic acetonitrile. Cyclic "
        "voltammetry and controlled-potential electrolysis gave a turnover "
        "frequency of 12,000 h⁻¹ at 80 °C, roughly threefold the Pd benchmark "
        "under identical conditions, with full activity retained over 200 cycles.\n"
        "BRIEF: cobalt catalyst triples proton-reduction turnover over palladium, "
        "stable to 200 cycles\n"
        "DETAIL: 12,000 h⁻¹ at 80 °C in acidic acetonitrile; rate credited to "
        "pendant-amine proton relays.\n\n"
        "PASSAGE: Reviewing the quarter, we argue the budget shortfall stems "
        "from the Q3 hiring freeze rather than weaker sales. Revenue held flat "
        "against forecast — the top line in Table 2 is essentially unchanged — "
        "so the gap must originate on the cost side.\n"
        "BRIEF: attributes the budget shortfall to the Q3 hiring freeze, not "
        "weaker sales\n"
        "DETAIL: flat revenue vs forecast (unchanged top line, Table 2); gap is "
        "cost-side.\n\n"
        "PASSAGE: Contrary to our hypothesis, daily supplementation produced no "
        "significant change in composite cognitive scores relative to placebo "
        "(p = 0.42). We caution that the trial was underpowered, enrolling only "
        "38 participants, and ran for just eight weeks.\n"
        "BRIEF: supplementation gave no cognitive benefit over placebo, against "
        "the hypothesis\n"
        "DETAIL: non-significant (p = 0.42); underpowered at 38 participants, "
        "eight-week trial.\n\n"
        "PASSAGE: Immediately after collection, samples were flash-frozen in "
        "liquid nitrogen within 30 s to halt metabolic activity, then moved to "
        "long-term storage at −80 °C. Aliquots were thawed on ice only once, "
        "just before analysis, to avoid freeze–thaw degradation.\n"
        "BRIEF: samples flash-frozen then cold-stored to preserve them until "
        "analysis\n"
        "DETAIL: liquid nitrogen within 30 s of collection; stored at −80 °C; "
        "thawed on ice once.\n\n"
        "PASSAGE: Throughout this paper we define resilience as the capacity of "
        "a system to absorb disturbance and reorganize while undergoing change, "
        "so as to still retain essentially the same function, structure, "
        "identity, and feedbacks — departing from engineering notions of return "
        "time to a single equilibrium.\n"
        "BRIEF: defines resilience as absorbing disturbance while keeping core "
        "function\n"
        "DETAIL: also reorganizes yet retains structure, identity, feedbacks; "
        "rejects single-equilibrium view.\n\n"
        "PASSAGE: 4822.296 273.86 10489.05 295511.5 [8,8] 54514 241665 491010 "
        "41.07 354.3621 4309522 6228.624 352.84 13601.7 384269.5 [9,9] 68598 "
        "304587 618714 51.14 443.5323 5437062\n"
        "BRIEF: (tabular data)\n"
        "DETAIL:\n\n"
        "PASSAGE: Nature Energy February 2022 Copyright 2022 The Author(s), "
        "under exclusive licence to Springer Nature Limited. All Rights "
        "Reserved. Section: Pg. 130-143; Vol. 7; No. 2; ISSN: 2058-7546\n"
        "BRIEF: (publication metadata)\n"
        "DETAIL:\n\n"
        f"--- Document for context (a {noun}; do not summarize this header) ---\n"
        f"{header}"
    )

    parts: list[str] = []
    if claim.section_path:
        parts.append("Section: " + " › ".join(claim.section_path))
    if claim.keywords:
        parts.append("Keywords: " + ", ".join(claim.keywords))
    if claim.numerics:
        parts.append("Quantities: " + ", ".join(claim.numerics[:20]))
    prefix = ("\n".join(parts) + "\n\n") if parts else ""
    user = f"{prefix}Passage to summarize:\n{claim.text}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _legacy_briefing_messages(
    *, date: str, count: int, context: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (f"Date: {date}. {count} headlines overnight:\n\n{context}"),
        },
    ]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _claim(
    *,
    ref_kind: str = "paper",
    section_path: list[str] | None = None,
    keywords: list[str] | None = None,
    numerics: list[str] | None = None,
    text: str = "We synthesized MOF-5 and measured a band gap of 3.5 eV.",
    title: str = "A study of MOF-5",
) -> _Claimed:
    return _Claimed(
        chunk_id=1,
        ref_id=7,
        ord=0,
        chunk_kind="paragraph",
        text=text,
        section_path=section_path if section_path is not None else ["Results"],
        keywords=keywords if keywords is not None else ["mof-5", "band gap"],
        numerics=numerics if numerics is not None else ["3.5 eV"],
        ref_kind=ref_kind,
        title=title,
    )


# --------------------------------------------------------------------------
# summarizer equivalence
# --------------------------------------------------------------------------


def test_summarizer_full_claim_with_card_is_byte_identical() -> None:
    """A representative chunk (doc header + section/keywords/quantities)."""
    claim = _claim()
    card = "Title: A study of MOF-5\nAbstract: We report a metal-organic framework."
    assert build_messages(claim, doc_card=card) == _legacy_build_messages(
        claim, doc_card=card
    )


def test_summarizer_title_fallback_no_context_parts() -> None:
    """No card (title fallback) and no section/keywords/numerics → empty prefix."""
    claim = _claim(section_path=[], keywords=[], numerics=[])
    assert build_messages(claim, doc_card="") == _legacy_build_messages(
        claim, doc_card=""
    )


def test_summarizer_second_chunk_of_same_doc() -> None:
    """A second, distinct passage of the same document (the multi-chunk case).

    Its ``system`` prefix must equal the first chunk's (the KV-cache reuse
    the whole layering exists for) and the whole pair must match the golden."""
    card = "Title: A study of MOF-5\nAbstract: We report a metal-organic framework."
    c1 = _claim()
    c2 = _claim(
        text="Thermogravimetric analysis showed stability to 400 C.",
        section_path=["Results", "Thermal stability"],
        keywords=["tga", "stability"],
        numerics=["400 C"],
    )
    m1 = build_messages(c1, doc_card=card)
    m2 = build_messages(c2, doc_card=card)
    assert m1 == _legacy_build_messages(c1, doc_card=card)
    assert m2 == _legacy_build_messages(c2, doc_card=card)
    # the stable system prefix is byte-identical across the two chunks
    assert m1[0]["content"] == m2[0]["content"]
    assert m1[1]["content"] != m2[1]["content"]


def test_summarizer_kind_noun_variants_byte_identical() -> None:
    """The doc-header noun varies by ref_kind; each stays byte-identical."""
    for ref_kind in ("paper", "patent", "conv", "book"):
        claim = _claim(ref_kind=ref_kind)
        assert build_messages(claim, doc_card="") == _legacy_build_messages(
            claim, doc_card=""
        )


def test_summarizer_two_message_shape_preserved() -> None:
    """Still exactly one system + one user message, in that order."""
    msgs = build_messages(_claim(), doc_card="")
    assert [m["role"] for m in msgs] == ["system", "user"]
    # doc-header rides the system (cached) message, not the user turn
    assert "--- Document for context" in msgs[0]["content"]
    assert "--- Document for context" not in msgs[1]["content"]


# --------------------------------------------------------------------------
# briefing equivalence
# --------------------------------------------------------------------------


def test_briefing_messages_byte_identical() -> None:
    context = "- [reuters] Something happened (2026-07-02 06:00 UTC) https://x"
    got = _build_briefing_messages(date="2026-07-02", count=3, context=context)
    assert got == _legacy_briefing_messages(date="2026-07-02", count=3, context=context)


def test_briefing_two_message_shape_preserved() -> None:
    msgs = _build_briefing_messages(date="2026-07-02", count=1, context="- item")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == _SYSTEM_PROMPT
