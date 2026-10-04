"""Frozen-grounding sweep (``precis.nanopub.grounding_sweep``, D3 of the
signed-grounding recurrence): shallow grounding, method coverage gaps, better
passages and the summary. The core (:func:`assess`) is DB-free; the last
tests drive the loader and the ``precis nanopub sweep-grounding`` command
against the ``store`` fixture."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Any

import pytest

from precis.cli import nanopub as cli_nanopub
from precis.nanopub import grounding_sweep as gs
from precis.nanopub.grounding_sweep import GroundingPassage, HubInput
from precis.nanopub.term_coverage import PaperChunk
from tests.test_nanopub_term_coverage import _hub_with_method_paper

_NANOBUD = (
    "TEM and STS measurements identify a nanobud as a fullerene bonded to the "
    "outside of a carbon nanotube."
)


def _chunk(
    chunk_id: int,
    ord_: int,
    text: str,
    section: list[str] | None = None,
    *,
    ref_id: int = 1,
    kind: str = "paragraph",
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        ref_id=ref_id,
        handle=f"pc{chunk_id}",
        ord=ord_,
        kind=kind,
        section_path=section if section is not None else ["Results"],
        text=text,
    )


def _passages(*chunks: PaperChunk) -> list[GroundingPassage]:
    return [GroundingPassage(text=c.text, chunk=c) for c in chunks]


def _hub(
    sentence: str,
    grounding: list[PaperChunk],
    chunks: list[PaperChunk],
    abstracts: dict[int, str] | None = None,
    state: str = "anchored",
    hub: int = 100,
) -> HubInput:
    return HubInput(
        hub_ref_id=hub,
        state=state,
        sentence=sentence,
        passages=_passages(*grounding),
        chunks=chunks,
        abstracts=abstracts or {},
    )


def _nanobud_paper() -> tuple[list[PaperChunk], PaperChunk, PaperChunk]:
    """fi189535 in miniature: the grounding is the abstract plus a definition
    sentence; the TEM caption and the STS passage sit in the same paper."""
    abstract = _chunk(
        1,
        1,
        "We have discovered a novel hybrid material that combines fullerenes "
        "and nanotubes into a single structure.",
        ["Abstract"],
    )
    definition = _chunk(
        2,
        8,
        "The term nanobud is used to describe a fullerene covalently attached "
        "to a nanotube.",
        ["1. Introduction"],
    )
    caption = _chunk(
        3, 12, "Figure 1 TEM image of NanoBud structures.", ["Results"], kind="figure"
    )
    sts = _chunk(
        4,
        14,
        "STS measurements show the local density of states on the bud.",
        ["Results"],
    )
    return [abstract, definition, caption, sts], abstract, definition


# ---------------------------------------------------------------- rules


def test_abstract_plus_definition_grounding_is_shallow_and_suggests_caption() -> None:
    chunks, abstract, definition = _nanobud_paper()
    rep = gs.assess(_hub(_NANOBUD, [abstract, definition], chunks))
    assert rep.needs_body and rep.shallow and rep.flagged
    assert [v.shallow for v in rep.passages] == ["front", "definition"]
    gap_terms = {g.term for g in rep.gaps}
    assert {"TEM", "STS"} <= gap_terms
    tem = next(g for g in rep.gaps if g.term == "TEM")
    assert tem.suggestions[0].handle == "pc3" and tem.suggestions[0].tier == "caption"
    # the supersede candidates: caption first, the STS passage next, never
    # a chunk that is already grounding
    assert [b.handle for b in rep.better][:2] == ["pc3", "pc4"]
    assert {"pc1", "pc2"}.isdisjoint(b.handle for b in rep.better)


def test_one_body_passage_clears_shallow() -> None:
    chunks, abstract, definition = _nanobud_paper()
    rep = gs.assess(_hub(_NANOBUD, [abstract, definition, chunks[3]], chunks))
    assert not rep.shallow
    # STS is carried now, TEM still is not: a gap, so still flagged
    assert [g.term for g in rep.gaps] == ["TEM"] and rep.flagged


def test_methods_passage_is_not_shallow() -> None:
    methods = _chunk(
        5,
        6,
        "DFT calculations used a 10 x 10 k-point mesh.",
        ["2. Computational methods"],
    )
    chunks = [_chunk(i, i, f"filler paragraph {i}", ["Results"]) for i in (7, 8)]
    rep = gs.assess(
        _hub("DFT calculations find a gap of 0.07 eV.", [methods], [methods, *chunks])
    )
    assert rep.passages[0].tier == "methods" and rep.passages[0].shallow is None
    assert not rep.shallow


def test_claim_that_needs_no_body_is_never_shallow() -> None:
    abstract = _chunk(1, 1, "Nanobuds are hybrids.", ["Abstract"])
    rep = gs.assess(_hub("Nanobuds exist as hybrids.", [abstract], [abstract]))
    assert not rep.needs_body and not rep.shallow


def _kroto_like(extra_chunks: int = 0) -> tuple[list[PaperChunk], PaperChunk]:
    """A short letter: title block, opening paragraph, a few more; headings
    are an author line only (no methods/results)."""
    sec = ["H. W. Kroto, J. R. Heath & R. E. Smalley"]
    opening = _chunk(
        11,
        1,
        "Time-of-flight mass spectrometry of laser-vaporized graphite shows a "
        "remarkably stable cluster of 60 carbon atoms.",
        sec,
    )
    chunks = [_chunk(10, 0, "Title block", sec), opening]
    chunks += [
        _chunk(20 + i, 2 + i, f"Paragraph {i} of the letter.", sec) for i in range(3)
    ]
    chunks += [
        _chunk(100 + i, 10 + i, f"Appendix paragraph {i}.", sec)
        for i in range(extra_chunks)
    ]
    return chunks, opening


def test_short_letter_opening_paragraph_is_the_body() -> None:
    chunks, opening = _kroto_like()
    assert gs.is_short_letter(chunks)
    rep = gs.assess(
        _hub(
            "Time-of-flight mass spectrometry of laser-vaporized graphite finds a "
            "stable cluster of 60 carbon atoms.",
            [opening],
            chunks,
        )
    )
    assert rep.passages[0].letter and rep.passages[0].tier == "body"
    assert not rep.shallow


def test_same_opening_paragraph_in_a_long_paper_is_front_matter() -> None:
    chunks, opening = _kroto_like(extra_chunks=gs.LETTER_MAX_CHUNKS)
    assert not gs.is_short_letter(chunks)
    rep = gs.assess(
        _hub(
            "Time-of-flight mass spectrometry of laser-vaporized graphite finds a "
            "stable cluster of 60 carbon atoms.",
            [opening],
            chunks,
        )
    )
    assert rep.passages[0].tier == "front" and rep.shallow


def test_a_results_heading_makes_a_short_paper_not_a_letter() -> None:
    chunks, _opening = _kroto_like()
    chunks.append(_chunk(30, 9, "We observed it.", ["Results and discussion"]))
    assert not gs.is_short_letter(chunks)
    assert not gs.is_short_letter([])


def test_heading_over_the_whole_paper_is_not_front_matter() -> None:
    """Every chunk filed under 'ABSTRACT' (an extraction fault): a passage
    from the middle is a body passage."""
    chunks = [
        _chunk(i, i, f"Paragraph {i} of the study.", ["ABSTRACT"]) for i in range(9)
    ]
    chunks += [
        _chunk(50 + i, 20 + i, f"Method {i}.", ["Experimental"]) for i in range(3)
    ]
    mid = chunks[5]
    rep = gs.assess(_hub("DFT calculations find a gap of 0.07 eV.", [mid], chunks))
    assert rep.passages[0].tier == "body" and not rep.shallow
    # a genuinely short abstract heading still counts
    short = [
        _chunk(1, 0, "Title", ["Title"]),
        _chunk(2, 3, "Abstract text here.", ["Abstract"]),
        *[_chunk(60 + i, 10 + i, f"Method {i}.", ["Experimental"]) for i in range(3)],
    ]
    rep = gs.assess(_hub("DFT calculations find a gap of 0.07 eV.", [short[1]], short))
    assert rep.passages[0].tier == "front" and rep.shallow


@pytest.mark.parametrize(
    "text",
    [
        "The term graphene nanobud is used to describe a hybrid of C60 and graphene.",
        "A nanobud is defined as a fullerene bonded to a nanotube.",
        "Fullerite refers to the solid form of C60.",
        "A nanobud is a hybrid structure that combines a fullerene and a nanotube.",
        "We call this structure a nanobud.",
        "This so-called bud is a covalent attachment.",
    ],
)
def test_definition_cues(text: str) -> None:
    assert gs.is_definition(text)


@pytest.mark.parametrize(
    "text",
    [
        "Figure 2 shows a typical STM topographic image of the NanoBud.",
        "The bud height is 0.7 nm and the bias is 1.0 V.",
        # a long passage that merely contains a definition clause is body text
        "Nanobuds are hybrids that combine fullerenes and nanotubes. "
        + "The measured conductance drops by a factor of three. " * 8,
    ],
)
def test_not_a_definition(text: str) -> None:
    assert not gs.is_definition(text)


def test_bibliography_items_do_not_count_as_better_passages() -> None:
    chunks, abstract, definition = _nanobud_paper()
    chunks.append(
        _chunk(
            9, 40, "- (19) Meng, T. Z.; Wang, C. Y. TEM of nanobuds. 2007.", ["Results"]
        )
    )
    chunks.append(
        _chunk(
            10,
            41,
            '- <span id="x"></span>[6] K. Culik, STS and TEM of tiles, 1996.',
            ["Results"],
        )
    )
    rep = gs.assess(_hub(_NANOBUD, [abstract, definition], chunks))
    shown = [b.handle for b in rep.better] + [
        s.handle for g in rep.gaps for s in g.suggestions
    ]
    assert "pc9" not in shown and "pc10" not in shown


def test_unresolved_passage_chunk_is_not_front_matter() -> None:
    rep = gs.assess(
        HubInput(
            hub_ref_id=1,
            state="signed",
            sentence="DFT calculations find a gap of 0.07 eV.",
            passages=[GroundingPassage(text="The gap is 0.07 eV (DFT).", chunk=None)],
            chunks=[],
        )
    )
    assert rep.passages[0].handle == "(chunk gone)" and not rep.shallow


# -------------------------------------------------------- summary / JSON


def _three_reports() -> list[gs.HubReport]:
    chunks, abstract, definition = _nanobud_paper()
    shallow = gs.assess(_hub(_NANOBUD, [abstract, definition], chunks, hub=3))
    kchunks, opening = _kroto_like()
    clean = gs.assess(
        _hub(
            "Time-of-flight mass spectrometry of laser-vaporized graphite finds a "
            "stable cluster of 60 carbon atoms.",
            [opening],
            kchunks,
            state="signed",
            hub=2,
        )
    )
    gap_only = gs.assess(
        _hub(
            _NANOBUD,
            [abstract, definition, chunks[3]],
            chunks,
            state="published",
            hub=1,
        )
    )
    return [shallow, clean, gap_only]


def test_summary_counts_and_rates() -> None:
    reports = _three_reports()
    s = gs.summarise(reports)
    assert s["hubs"] == 3
    assert s["by_state"] == {"anchored": 1, "published": 1, "signed": 1}
    assert s["shallow"] == 1 and s["flagged"] == 2 and s["gap_hubs"] == 2
    # shallow hub: TEM + STS acronyms; gap-only hub: TEM acronym
    assert s["gap_terms_by_kind"]["acronym"] == 3
    assert s["gap_hubs_by_kind"]["acronym"] == 2
    assert s["per_100_hubs"]["shallow"] == pytest.approx(33.3)
    assert s["per_100_hubs"]["flagged"] == pytest.approx(66.7)
    assert gs.summarise([])["per_100_hubs"]["shallow"] == 0.0


def test_json_shape() -> None:
    out = gs.to_dict(_three_reports())
    assert json.loads(json.dumps(out)) == out  # plain JSON
    assert set(out) == {"summary", "hubs"}
    assert [h["hub"] for h in out["hubs"]] == ["fi1", "fi2", "fi3"]
    hub = out["hubs"][2]
    assert set(hub) == {
        "hub",
        "state",
        "sentence",
        "needs_body",
        "flagged",
        "shallow",
        "passages",
        "gaps",
        "better",
    }
    assert hub["shallow"] is True
    assert set(hub["passages"][0]) == {
        "chunk",
        "tier",
        "shallow",
        "short_letter",
        "text",
    }
    assert hub["passages"][0]["shallow"] == "front"
    gap = hub["gaps"][0]
    assert set(gap) == {"kind", "term", "expansion", "suggestions"}
    assert gap["suggestions"][0]["chunk"] == "pc3"
    assert set(hub["better"][0]) == {"chunk", "tier", "text"}
    assert out["hubs"][1]["flagged"] is False and out["hubs"][1]["better"] == []


def test_markdown_lists_only_flagged_hubs() -> None:
    md = gs.render_md(_three_reports())
    assert "3 hubs" in md and "shallow grounding: 1 (33.3 per 100 hubs)" in md
    assert "## fi3 [anchored] shallow, gap" in md
    assert "## fi1 [published] gap" in md
    assert "## fi2" not in md
    assert "better pc3 [caption]" in md


# ------------------------------------------------------- DB loader / CLI


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli_nanopub.add_parser(sub)
    return parser.parse_args(argv)


def test_sweep_reads_a_reviewed_row_and_never_writes(store: Any) -> None:
    hub, chunks = _hub_with_method_paper(store)
    before = store.nanopub_publish_row(hub)
    reports = gs.sweep(store)
    assert [r.hub_ref_id for r in reports] == [hub]
    rep = reports[0]
    assert rep.state == "reviewed" and rep.flagged
    tem = next(g for g in rep.gaps if g.term == "TEM")
    assert tem.suggestions[0].handle == f"pc{chunks['caption']}"
    assert tem.suggestions[0].tier == "caption"
    assert gs.sweep(store, ("anchored",)) == []
    assert store.nanopub_publish_row(hub) == before


def test_cli_sweep_grounding_json_and_state_filter(store: Any) -> None:
    hub, chunks = _hub_with_method_paper(store)
    args = _parse(["nanopub", "sweep-grounding", "--format", "json"])
    assert args.state is None
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_nanopub._sweep_grounding(args, store)
    out = json.loads(buf.getvalue())
    assert out["summary"]["hubs"] == 1 and out["summary"]["by_state"] == {"reviewed": 1}
    assert out["hubs"][0]["hub"] == f"fi{hub}"
    assert (
        out["hubs"][0]["gaps"][0]["suggestions"][0]["chunk"] == f"pc{chunks['caption']}"
    )

    args = _parse(["nanopub", "sweep-grounding", "--state", "anchored"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_nanopub._sweep_grounding(args, store)
    assert "0 hubs" in buf.getvalue()


def test_cli_state_choices_are_the_frozen_states() -> None:
    with pytest.raises(SystemExit):
        _parse(["nanopub", "sweep-grounding", "--state", "candidate"])
