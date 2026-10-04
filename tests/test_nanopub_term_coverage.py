"""Term coverage at approve/sign (``precis.nanopub.term_coverage``): a hub
whose grounding lacks a method the claim names gets a non-blocking
``term-coverage`` preflight issue, with chunks that carry it from the
hub's papers — a figure caption ranked ahead of the abstract. DB-backed."""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.nanopub import evidence, mint, preflight, term_coverage
from precis.nanopub.keys import generate_keypair
from precis.taproot.hub import attach_evidence
from tests.test_nanopub_gates_mint import _QUOTE, _payload, _seed_hub, _seed_paper

_SENTENCE = "TEM shows MOFs can be anisotropic up to 400:1."


@pytest.fixture(autouse=True)
def _bot_key(monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)


def _add_chunk(
    store: Any, ref_id: int, ord_: int, text: str, section: list[str]
) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', %s, 'paragraph', %s, %s) "
            "RETURNING chunk_id",
            (ref_id, ord_, text, section),
        ).fetchone()
    return int(row[0])


def _hub_with_method_paper(store: Any) -> tuple[int, dict[str, int]]:
    """An approved (reviewed) hub whose grounding quote never says TEM, plus
    a second evidence paper that defines TEM in its abstract (ord 0) and
    shows it in a figure caption (ord 5, results section)."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)

    method_paper, abstract_chunk, _sha = _seed_paper(
        store,
        title="Imaging MOF crystals",
        chunk_text=(
            "We image the crystals by transmission electron microscopy (TEM) "
            "and report their anisotropy."
        ),
        section=["Abstract"],
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=method_paper,
        role="corroborates",
        meta={"source_handle": f"pc{abstract_chunk}"},
        check_retraction=False,
    )
    caption_chunk = _add_chunk(
        store,
        method_paper,
        5,
        "Figure 3 TEM image of the MOF crystals along the [001] axis.",
        ["Results"],
    )
    mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    return hub, {"abstract": abstract_chunk, "caption": caption_chunk}


def test_missing_method_warns_and_suggests_caption_before_abstract(
    store: Any,
) -> None:
    hub, chunks = _hub_with_method_paper(store)
    row = store.nanopub_publish_row(hub)
    assert row is not None and row.state == "reviewed"

    items = term_coverage.term_coverage(store, hub, _SENTENCE, row.grounding)
    by_text = {i.term.text: i for i in items}
    assert set(by_text) == {"TEM"}  # 400:1 and "anisotropic" are in the quote
    handles = [s.chunk_handle for s in by_text["TEM"].suggestions]
    assert handles == [f"pc{chunks['caption']}", f"pc{chunks['abstract']}"]
    tiers = [s.tier for s in by_text["TEM"].suggestions]
    assert tiers == [term_coverage.TIER_CAPTION, term_coverage.TIER_FRONT]
    # The abstract defines TEM, so the expansion rides along for display.
    assert by_text["TEM"].term.expansion == "transmission electron microscopy"


def test_preflight_carries_a_non_blocking_term_coverage_issue(store: Any) -> None:
    hub, chunks = _hub_with_method_paper(store)
    issues = preflight.publish_preflight(store, hub)
    cov = [i for i in issues if i.check == "term-coverage"]
    assert len(cov) == 1
    assert cov[0].blocking is False
    assert "TEM" in cov[0].message
    assert f"pc{chunks['caption']}" in cov[0].message
    assert cov[0].message.index(f"pc{chunks['caption']}") < cov[0].message.index(
        f"pc{chunks['abstract']}"
    )


def test_warning_never_blocks_sign(store: Any) -> None:
    hub, _chunks = _hub_with_method_paper(store)
    assert mint.sign(store, hub).state == "signed"


def test_covered_claim_raises_no_issue(store: Any) -> None:
    quote = f"{_QUOTE} by DFT"
    paper, chunk, sha = _seed_paper(
        store, chunk_text=f"Tensorial analysis. {quote}, in stark contrast."
    )
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    payload = _payload(chunk, sha)
    payload["passages"][0]["quote"] = quote  # grounding now names DFT
    mint.approve(store, hub, payload=payload, interactive=True)
    issues = preflight.publish_preflight(store, hub)
    assert [i for i in issues if i.check == "term-coverage"] == []


def test_blank_quote_falls_back_to_the_pinned_chunk_text(store: Any) -> None:
    paper, chunk, _sha = _seed_paper(store, chunk_text="TEM of the crystals.")
    texts = term_coverage.passage_texts(
        store, {"passages": [{"quote": "", "chunk_id": chunk}]}
    )
    assert texts == ["TEM of the crystals."]


def test_unfound_term_says_so(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    items = term_coverage.term_coverage(
        store,
        hub,
        _SENTENCE,
        _payload(chunk, sha),
        bundle=evidence.load_bundle(store, hub),
    )
    assert [i.term.text for i in items] == ["TEM"]
    assert items[0].suggestions == ()
    assert "not found in the evidence papers" in term_coverage.format_message(items)


@pytest.mark.parametrize(
    ("kind", "section", "ord_", "text", "abstract", "tier"),
    [
        ("figure", [], 9, "anything", None, 0),
        ("paragraph", [], 9, "Table 2 lists the lattice constants.", None, 0),
        ("paragraph", ["Methods"], 9, "We annealed the film.", None, 1),
        ("paragraph", ["Results and discussion"], 9, "The film cracked.", None, 1),
        ("paragraph", ["Introduction"], 9, "Prior work is large.", None, 2),
        ("paragraph", ["Abstract"], 9, "We study films.", None, 3),
        ("paragraph", ["A paper title"], 1, "Title and authors.", None, 3),
        (
            "paragraph",
            ["A paper title"],
            9,
            "We study the films of this paper in great depth here, and more still.",
            "We study the films of this paper in great depth here, and more still. Then more.",
            3,
        ),
    ],
)
def test_chunk_tier(
    kind: str,
    section: list[str],
    ord_: int,
    text: str,
    abstract: str | None,
    tier: int,
) -> None:
    assert (
        term_coverage.chunk_tier(
            kind=kind, section_path=section, ord_=ord_, text=text, abstract=abstract
        )
        == tier
    )


def test_front_heading_over_a_whole_paper_is_not_front_matter() -> None:
    """A reader that files every chunk under 'ABSTRACT' must not turn the
    whole paper into front matter: past FRONT_SECTION_MAX_CHUNKS the
    heading is ignored (position and abstract containment still count)."""
    cap = term_coverage.FRONT_SECTION_MAX_CHUNKS
    kw: dict[str, Any] = {
        "kind": "paragraph",
        "section_path": ["ABSTRACT"],
        "ord_": 9,
        "text": "We annealed the film.",
        "abstract": None,
    }
    assert (
        term_coverage.chunk_tier(**kw, heading_chunks=cap) == term_coverage.TIER_FRONT
    )
    assert term_coverage.chunk_tier(**kw) == term_coverage.TIER_FRONT  # unknown
    assert (
        term_coverage.chunk_tier(**kw, heading_chunks=cap + 1)
        == term_coverage.TIER_BODY
    )
    # a long Results heading keeps its methods tier
    kw["section_path"] = ["Results"]
    assert (
        term_coverage.chunk_tier(**kw, heading_chunks=cap + 20)
        == term_coverage.TIER_METHODS
    )
    # position still counts under a sprawling heading
    kw.update(section_path=["ABSTRACT"], ord_=1)
    assert (
        term_coverage.chunk_tier(**kw, heading_chunks=cap + 1)
        == term_coverage.TIER_FRONT
    )


def test_suggestions_from_a_paper_filed_under_abstract_are_body() -> None:
    """G2: the TEM chunk of a paper whose every chunk sits under 'ABSTRACT'
    is suggested as body text, not front matter."""
    chunks = [
        _pc(i, 5 + i, f"Paragraph {i} about the film.", ["ABSTRACT"]) for i in range(8)
    ]
    chunks.append(_pc(20, 20, "A TEM image shows the film is flat.", ["ABSTRACT"]))
    items = term_coverage.analyse(
        "TEM shows the film is flat.", ["The film is flat."], chunks, {}, {1}
    )
    assert [i.term.text for i in items] == ["TEM"]
    assert items[0].suggestions[0].chunk_handle == "pc20"
    assert items[0].suggestions[0].tier == term_coverage.TIER_BODY


# ── D2: the approve prefill ranks body passages ahead of the abstract ────


def _pc(
    chunk_id: int,
    ord_: int,
    text: str,
    section: list[str],
    kind: str = "paragraph",
    ref_id: int = 1,
) -> term_coverage.PaperChunk:
    return term_coverage.PaperChunk(
        chunk_id=chunk_id,
        ref_id=ref_id,
        handle=f"pc{chunk_id}",
        ord=ord_,
        kind=kind,
        section_path=section,
        text=text,
    )


def test_rank_for_claim_tiers_then_term_count() -> None:
    sentence = "TEM and STS show a nanobud is a fullerene bonded to a nanotube."
    abstract = "We study nanobuds by TEM and STS. They are fullerenes on tubes."
    chunks = [
        _pc(1, 0, abstract, ["Abstract"]),
        _pc(
            2, 4, "Nanobuds are common in samples of this kind of material.", ["Intro"]
        ),
        _pc(3, 7, "STS spectra of the bud region show a gap.", ["Results"]),
        _pc(4, 8, "Figure 2 TEM image of a nanobud on a nanotube.", ["Results"]),
        _pc(5, 9, "Figure 3 TEM and STS maps of a nanobud.", ["Results"]),
    ]
    order = term_coverage.rank_for_claim(sentence, chunks, {1: abstract})
    # captions first (more terms first), then results, then body, abstract last
    assert [chunks[i].chunk_id for i in order] == [5, 4, 3, 2, 1]
    assert sorted(order) == list(range(len(chunks)))  # nothing dropped


def test_rank_for_claim_sprawling_abstract_heading_is_not_demoted() -> None:
    sentence = "STS shows a gap in the nanobud."
    chunks = [_pc(1, 2, "Prior work on nanobuds is large.", ["Introduction"])]
    chunks += [
        _pc(10 + i, 10 + i, f"Paragraph {i} of the study.", ["ABSTRACT"])
        for i in range(8)
    ]
    chunks[4] = _pc(14, 14, "STS spectra show a gap in the bud.", ["ABSTRACT"])
    order = term_coverage.rank_for_claim(sentence, chunks, {})
    assert chunks[order[0]].chunk_id == 14  # same tier as the intro, more terms


def test_rank_for_claim_ties_prefer_the_grounding_papers() -> None:
    sentence = "TEM shows a nanobud."
    chunks = [
        _pc(1, 5, "TEM of a nanobud, other paper.", ["Results"], ref_id=2),
        _pc(2, 5, "TEM of a nanobud, grounding paper.", ["Results"], ref_id=1),
        _pc(3, 6, "TEM of a nanobud, other paper again.", ["Results"], ref_id=2),
    ]
    assert term_coverage.rank_for_claim(sentence, chunks, {}) == [0, 1, 2]
    assert term_coverage.rank_for_claim(sentence, chunks, {}, grounding_refs={1}) == [
        1,
        0,
        2,
    ]
    # tier and term count still beat the grounding preference
    chunks[0] = _pc(1, 5, "Figure 1 TEM of a nanobud.", ["Results"], ref_id=2)
    assert term_coverage.rank_for_claim(sentence, chunks, {}, grounding_refs={1}) == [
        0,
        1,
        2,
    ]


def test_names_method() -> None:
    assert term_coverage.names_method(_SENTENCE)
    assert not term_coverage.names_method("Nanobuds exist in some samples.")


def _prefill_hub(store: Any, sentence: str) -> tuple[int, int, int]:
    """An unminted hub with an abstract chunk attached first and a figure
    caption of the same paper attached second."""
    paper, abstract, _sha = _seed_paper(
        store,
        chunk_text="We image nanobuds by TEM and report their structure here.",
        section=["Abstract"],
    )
    hub = _seed_hub(store, sentence, paper, abstract)
    caption = _add_chunk(
        store, paper, 5, "Figure 3 TEM image of a nanobud on a nanotube.", ["Results"]
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{caption}"},
        check_retraction=False,
    )
    return hub, abstract, caption


def test_prefill_puts_the_caption_before_the_abstract_for_a_method_claim(
    store: Any,
) -> None:
    from precis_web.nanopub_render import _suggested_payload

    hub, abstract, caption = _prefill_hub(
        store, "TEM shows a nanobud sits on a nanotube."
    )
    bundle = evidence.load_bundle(store, hub)
    # the edge order is abstract first — the reorder is what lifts the caption
    assert [c.chunk_id for c in bundle.grounding_chunks] == [abstract, caption]
    payload = json.loads(
        _suggested_payload(store, store.nanopub_publish_row(hub), bundle, {})
    )
    assert [p["chunk_id"] for p in payload["passages"]] == [caption, abstract]


def test_prefill_order_unchanged_without_method_terms(store: Any) -> None:
    from precis_web.nanopub_render import _prefill_chunk_order

    hub, _abstract, _caption = _prefill_hub(
        store, "nanobuds sit on the surface of nanotubes."
    )
    bundle = evidence.load_bundle(store, hub)
    assert [c.chunk_id for c in _prefill_chunk_order(store, bundle)] == [
        c.chunk_id for c in bundle.grounding_chunks
    ]


def test_prefill_leaves_a_frozen_grounding_untouched(store: Any) -> None:
    from precis_web.nanopub_render import _suggested_payload

    hub, _chunks = _hub_with_method_paper(store)
    row = store.nanopub_publish_row(hub)
    bundle = evidence.load_bundle(store, hub)
    assert json.loads(_suggested_payload(store, row, bundle, {})) == row.grounding


def test_prefill_does_not_demote_a_paper_filed_under_abstract(store: Any) -> None:
    """DB half of the sprawl rule: the heading counts come from the whole
    paper, not just the attached chunks."""
    from precis_web.nanopub_render import _suggested_payload

    paper, first, _sha = _seed_paper(
        store, chunk_text="Overview.", section=["Introduction"]
    )
    intro = _add_chunk(
        store, paper, 4, "Prior work on nanobuds is large.", ["Introduction"]
    )
    tem = 0
    for i in range(8):
        text = "A TEM image of a nanobud." if i == 2 else f"Paragraph {i} of the study."
        cid = _add_chunk(store, paper, 10 + i, text, ["ABSTRACT"])
        tem = cid if i == 2 else tem
    hub = _seed_hub(store, "TEM shows a nanobud sits on a nanotube.", paper, intro)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{tem}"},
        check_retraction=False,
    )
    bundle = evidence.load_bundle(store, hub)
    assert [c.chunk_id for c in bundle.grounding_chunks] == [intro, tem]
    payload = json.loads(
        _suggested_payload(store, store.nanopub_publish_row(hub), bundle, {})
    )
    assert [p["chunk_id"] for p in payload["passages"]] == [tem, intro]
    assert first  # the ord-0 chunk exists but is not attached


def test_bibliography_runs_are_never_suggestions():
    from precis.nanopub.term_coverage import is_bib_text

    # prod 10-04, fi263188: a chunk cut mid reference list
    glued = (
        "…nal tilings in Z d . Discrete Analysis. (2021:16). 1-28. - [9] R. "
        "Greenfeld, T. Tao, Undecidable translational tilings with only two "
        "tiles. - [10] B. Grünbaum, G. C. Shephard, Tilings and Patterns."
    )
    assert is_bib_text(glued)
    assert is_bib_text("- [6] Culik K. An aperiodic set of 13 Wang tiles.")
    assert is_bib_text("(19) Meng, X. Graphene nanobuds.")
    assert not is_bib_text(
        "TEM images in [9] show the bud junction at 2 nm resolution."
    )
    assert not is_bib_text("The analysis of ref. [3] is extended here.")
