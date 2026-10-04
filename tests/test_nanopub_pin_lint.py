"""Draft pin lint (``precis.nanopub.pin_lint``): G3 coverage and G4 the pin
rule, at draft edit (only on change) and at export (every pin). Warning
only. The fi189535 shape: a sentence naming TEM and STS re-pinned to an
abstract chunk that names neither."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.export import latex
from precis.export.docx import export_docx
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.nanopub import mint, pin_lint
from precis.nanopub.keys import generate_keypair
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from tests.test_nanopub_gates_mint import _QUOTE, _payload, _seed_paper

_SENTENCE = "TEM and STS measurements show the MOF film is conductive"
_ABSTRACT = "We study conductive MOF films and report their charge transport."
_DEFAULT = (
    "Scanning tunnelling microscopy (STM) and spectroscopy (STS) and "
    "transmission electron microscopy (TEM) of the MOF film show conduction. "
    f"Tensorial analysis. {_QUOTE}, in stark contrast."
)


@pytest.fixture(autouse=True)
def _bot_key(monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)


def _chunk(store: Any, ref_id: int, ord_: int, text: str, section: list[str]) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', %s, 'paragraph', %s, %s) "
            "RETURNING chunk_id",
            (ref_id, ord_, text, section),
        ).fetchone()
    return int(row[0])


def _world(store: Any) -> dict[str, Any]:
    """A hub whose default grounding chunk names TEM and STS, in a paper that
    also has an abstract chunk naming neither; plus a second paper."""
    paper, default, sha = _seed_paper(store, chunk_text=_DEFAULT, section=["Results"])
    abstract = _chunk(store, paper, 1, _ABSTRACT, ["Abstract"])
    other, other_chunk, _sha2 = _seed_paper(
        store, title="Another paper", chunk_text="Some unrelated text."
    )
    hub = mint_hub(store, CanonicalClaim(sentence=f"{_SENTENCE}.", scope={}))
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{default}"},
        check_retraction=False,
    )
    return {
        "hub": hub,
        "fi": handle_registry.format_handle("finding", hub),
        "paper": paper,
        "sha": sha,
        "default_id": default,
        "default": f"pc{default}",
        "abstract": f"pc{abstract}",
        "other": other,
        "other_chunk": f"pc{other_chunk}",
    }


def _freeze(store: Any, w: dict[str, Any]) -> None:
    """Approve the hub (a ``reviewed`` publish row) with the default chunk
    as the grounding passage."""
    mint.approve(
        store, w["hub"], payload=_payload(w["default_id"], w["sha"]), interactive=True
    )


# ── pure parsing ──────────────────────────────────────────────────────


def test_find_pins_takes_the_sentence_holding_the_token() -> None:
    text = (
        "Earlier work is cited elsewhere. TEM and STS show it [fi5>pc9]. "
        "A later sentence [fi6+pc10][fi7] stands apart."
    )
    pins = pin_lint.find_pins(text)
    assert [(p.hub_ref_id, p.op, p.handles) for p in pins] == [
        (5, ">", ("pc9",)),
        (6, "+", ("pc10",)),
    ]
    assert pins[0].sentence == "TEM and STS show it."
    assert pins[1].sentence == "A later sentence"  # own span: up to its token


def test_a_token_after_the_full_stop_belongs_to_the_sentence_before() -> None:
    pins = pin_lint.find_pins("TEM and STS show it.[fi5>pc9] Next sentence here.")
    assert pins[0].sentence == "TEM and STS show it."


def test_a_pin_in_a_multi_cite_sentence_vouches_for_its_own_span() -> None:
    text = (
        "Raman shows a D band [fi5>pc1], TEM shows the bud [fi6>pc2] "
        "and STS the gap [fi7>pc3][fi8>pc4]."
    )
    spans = [p.sentence for p in pin_lint.find_pins(text)]
    assert spans == [
        "Raman shows a D band",
        "TEM shows the bud",
        "and STS the gap",
        # no words between fi7 and fi8: back over the previous span
        "and STS the gap",
    ]


def test_a_multi_cite_sentence_with_no_words_falls_back_to_the_whole_sentence() -> None:
    (pin,) = pin_lint.find_pins("[fi5][fi6>pc1]")
    assert pin.sentence == ""


def test_single_cite_sentence_keeps_the_whole_sentence() -> None:
    (pin,) = pin_lint.find_pins("TEM first, then STS [fi5>pc9], as shown. Next one.")
    assert pin.sentence == "TEM first, then STS, as shown."


def test_bare_years_are_history_not_values() -> None:
    sent = "Kroto et al. reported C60 in 1985 and Iijima the tubes in 1991 at 2000 K."
    assert pin_lint._bare_year("1985", sent)
    assert pin_lint._bare_year("1991", sent)
    assert not pin_lint._bare_year("2000", sent)  # a unit follows
    assert not pin_lint._bare_year("1.5", sent)
    assert not pin_lint._bare_year("2150", "in 2150 it ends")  # outside 1800-2099
    assert not pin_lint._bare_year("1985", "a gap of 1985.5 eV")  # decimal


def test_unpinned_and_non_finding_citations_are_not_pins() -> None:
    assert pin_lint.find_pins("Plain [fi5] and [pc9] and [pa1+pc2].") == []


def test_newly_uncovered_diffs_by_term() -> None:
    from precis.nanopub.term_coverage import UncoveredTerm
    from precis.taproot.coverage import KIND_ACRONYM, Term

    def u(text: str) -> UncoveredTerm:
        return UncoveredTerm(term=Term(KIND_ACRONYM, text), suggestions=())

    assert pin_lint.newly_uncovered([u("TEM"), u("STS")], None) == [u("TEM"), u("STS")]
    assert pin_lint.newly_uncovered([u("TEM"), u("STS")], [u("TEM")]) == [u("STS")]
    assert pin_lint.newly_uncovered([u("TEM")], [u("TEM")]) == []


# ── G3 coverage ───────────────────────────────────────────────────────


def test_pin_to_an_abstract_that_names_neither_method_warns(store: Any) -> None:
    w = _world(store)
    text = f"{_SENTENCE} [{w['fi']}>{w['abstract']}]."
    (finding,) = pin_lint.export_findings(store, text)
    assert {u.term.text for u in finding.uncovered} == {"TEM", "STS"}
    # G2's suggestions ride along: the default (Results) chunk carries both.
    for item in finding.uncovered:
        assert [s.chunk_handle for s in item.suggestions][0] == w["default"]
    (line,) = pin_lint.render_finding(finding)
    assert "TEM" in line and "STS" in line and w["default"] in line


def test_pin_to_a_passage_that_carries_the_terms_is_silent(store: Any) -> None:
    w = _world(store)
    text = f"{_SENTENCE} [{w['fi']}>{w['default']}]."
    assert pin_lint.export_findings(store, text) == []


def test_supplement_pin_is_judged_with_the_hubs_default_passages(store: Any) -> None:
    w = _world(store)
    # '+' keeps the default passage that names TEM and STS: covered.
    assert (
        pin_lint.export_findings(store, f"{_SENTENCE} [{w['fi']}+{w['abstract']}].")
        == []
    )
    # '>' drops it: uncovered.
    assert pin_lint.export_findings(store, f"{_SENTENCE} [{w['fi']}>{w['abstract']}].")


def test_a_pin_on_a_whole_paper_has_nothing_to_compare(store: Any) -> None:
    w = _world(store)
    pa = handle_registry.format_handle("paper", w["paper"])
    assert pin_lint.export_findings(store, f"{_SENTENCE} [{w['fi']}>{pa}].") == []


# ── G4 the pin rule ───────────────────────────────────────────────────


def test_pin_outside_the_grounding_papers_warns(store: Any) -> None:
    w = _world(store)
    _freeze(store, w)
    text = f"The film is conductive [{w['fi']}>{w['other_chunk']}]."
    (finding,) = pin_lint.export_findings(store, text)
    assert finding.rule is not None
    assert w["other_chunk"] in finding.rule  # names the pin
    other = handle_registry.format_handle("paper", w["other"])
    own = handle_registry.format_handle("paper", w["paper"])
    assert other in finding.rule and own in finding.rule  # its paper, the grounding's


def test_pin_to_a_non_grounding_chunk_of_a_grounding_paper_is_fine(store: Any) -> None:
    w = _world(store)
    _freeze(store, w)
    text = f"The film is conductive [{w['fi']}>{w['abstract']}]."  # not a grounding passage
    assert pin_lint.export_findings(store, text) == []


def test_hub_without_a_frozen_grounding_is_not_checked(store: Any) -> None:
    w = _world(store)
    text = f"The film is conductive [{w['fi']}>{w['other_chunk']}]."
    assert pin_lint.export_findings(store, text) == []


# ── at edit: only on change ───────────────────────────────────────────


def test_edit_warns_on_a_new_pin_and_on_a_narrowed_sentence_only(store: Any) -> None:
    w = _world(store)
    pin = f"[{w['fi']}>{w['default']}]"
    bad = f"[{w['fi']}>{w['abstract']}]"
    base = f"TEM shows the MOF film is conductive {pin}. Other prose."

    # Brand-new pin to a passage that does not carry the terms.
    (new,) = pin_lint.edit_findings(store, f"{_SENTENCE} {bad}.", "")
    assert {u.term.text for u in new.uncovered} == {"TEM", "STS"}

    # The same text re-saved: untouched pin, no warning (even though uncovered).
    assert (
        pin_lint.edit_findings(store, f"{_SENTENCE} {bad}.", f"{_SENTENCE} {bad}.")
        == []
    )
    # An edit elsewhere in the chunk leaves the pin's sentence alone.
    assert pin_lint.edit_findings(store, base.replace("Other", "Changed"), base) == []
    # Covered pin stays silent after an unrelated sentence change.
    assert pin_lint.edit_findings(store, base, "") == []

    # The sentence now names something the pin no longer carries: only the
    # newly uncovered term is reported.
    narrowed = base.replace("TEM shows", "TEM and the HITP lattice show")
    (finding,) = pin_lint.edit_findings(store, narrowed, base)
    assert {u.term.text for u in finding.uncovered} == {"HITP"}


def test_edit_that_swaps_the_pin_is_a_new_pin(store: Any) -> None:
    w = _world(store)
    good = f"{_SENTENCE} [{w['fi']}>{w['default']}]."
    bad = f"{_SENTENCE} [{w['fi']}>{w['abstract']}]."
    (finding,) = pin_lint.edit_findings(store, bad, good)
    assert {u.term.text for u in finding.uncovered} == {"TEM", "STS"}


def test_edit_applies_g4_to_new_pins_only(store: Any) -> None:
    w = _world(store)
    _freeze(store, w)
    text = f"The film is conductive [{w['fi']}>{w['other_chunk']}]."
    (finding,) = pin_lint.edit_findings(store, text, "")
    assert finding.rule is not None
    assert pin_lint.edit_findings(store, text + " More.", text) == []


# ── the surfaces ──────────────────────────────────────────────────────


def _draft(hub: Hub) -> tuple[DraftHandler, str]:
    draft = DraftHandler(hub=hub)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    draft.put(id="dpin", title="T", project=pid)
    return draft, "dpin"


def test_put_and_edit_responses_carry_the_warning_once(hub: Hub) -> None:
    w = _world(hub.live_store)
    draft, slug = _draft(hub)
    bad = f"{_SENTENCE} [{w['fi']}>{w['abstract']}]."
    r = draft.put(id=slug, chunk_kind="paragraph", text=bad, at={"last": True})
    assert "⚠ pin:" in r.body and "TEM" in r.body and "STS" in r.body
    dc = r.body.split("added 1 chunk to dpin: ")[1].split()[0]

    # Re-saving the same text, with only trailing prose added, stays quiet.
    r2 = draft.edit(id=dc, text=bad + " More prose follows.")
    assert "⚠ pin:" not in r2.body


def test_export_warns_on_every_pin_docx_and_latex(hub: Hub, tmp_path: Path) -> None:
    w = _world(hub.live_store)
    draft, slug = _draft(hub)
    bad = f"{_SENTENCE} [{w['fi']}>{w['abstract']}]."
    draft.put(id=slug, chunk_kind="paragraph", text=bad, at={"last": True})
    # An unrelated edit would be silent at edit time; export still warns.
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    res = export_docx(hub.live_store, ref, target_path=tmp_path / "d.docx")
    pin_warnings = [x for x in res.warnings if x.startswith("pin:")]
    assert len(pin_warnings) == 1 and "TEM" in pin_warnings[0]

    ctx = latex._Ctx(keymap={}, known_handles=set(), store=hub.live_store)
    latex._render_inline(bad, ctx)
    assert any(x.startswith("pin:") and "STS" in x for x in ctx.warnings)
    latex._render_inline(bad, ctx)  # a second render does not duplicate it
    assert len([x for x in ctx.warnings if x.startswith("pin:")]) == 1
