"""Deterministic chemistry-to-spoken-text transform (gripe 168609) — the
code-owned pronunciation layer for formulas pulled verbatim from paper
titles/abstracts. Pure, no TTS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.draft.chem_speak import speak_chemistry
from precis.draft.narrate import markdown_segments, render_narration
from precis.draft.verbalize import verbalize_numbers

# ── the gripe's own examples ─────────────────────────────────────────────


def test_hydrate_salt_formula():
    assert speak_chemistry("Zn(NO3)2·6H2O") == "zinc nitrate hexahydrate"


def test_hydrate_salt_formula_in_prose():
    out = speak_chemistry("the precursor was Zn(NO3)2·6H2O in solution")
    assert out == "the precursor was zinc nitrate hexahydrate in solution"


def test_designator_drops_hyphen_for_downstream_number_verbalization():
    # chem_speak only drops the hyphen; the pipeline's number verbalizer
    # (run downstream in narrate.py) spells the bare number out.
    assert speak_chemistry("MOF-74") == "MOF 74"
    assert verbalize_numbers(speak_chemistry("MOF-74")) == "MOF seventy-four"


def test_designator_generic_pattern_not_a_name_table():
    assert speak_chemistry("ZIF-8") == "ZIF 8"
    assert speak_chemistry("UiO-66") == "UiO 66"
    assert verbalize_numbers(speak_chemistry("ZIF-8")) == "ZIF eight"
    assert verbalize_numbers(speak_chemistry("UiO-66")) == "UiO sixty-six"


# ── curated exact-formula table ──────────────────────────────────────────


def test_common_formula_table():
    assert speak_chemistry("H2O") == "water"
    assert speak_chemistry("CO2") == "carbon dioxide"
    assert speak_chemistry("NH3") == "ammonia"
    assert speak_chemistry("H2SO4") == "sulfuric acid"
    assert speak_chemistry("NaCl") == "sodium chloride"
    assert speak_chemistry("TiO2") == "titanium dioxide"


def test_formula_word_boundary_in_prose():
    out = speak_chemistry("dissolved in H2O at 25C, releasing CO2 gas")
    assert out == "dissolved in water at 25C, releasing carbon dioxide gas"


# ── conservative no-op cases ──────────────────────────────────────────────


def test_unknown_formula_left_untouched():
    # Not in the curated table, not a recognized hydrate salt.
    assert speak_chemistry("C60") == "C60"
    assert speak_chemistry("La0.8Sr0.2MnO3") == "La0.8Sr0.2MnO3"


def test_substituent_label_not_mistaken_for_formula():
    # CO2R is a substituent label (R group), not the molecule CO2 — the
    # trailing R blocks the word-boundary match.
    assert speak_chemistry("CO2R") == "CO2R"
    assert speak_chemistry("the CO2R group") == "the CO2R group"


def test_hydrate_with_unknown_ion_left_untouched():
    # Cd is not in the curated cation table in this made-up combination
    # test — an unrecognized anion inside the parens must not be guessed.
    assert speak_chemistry("Zn(XYZ)2·6H2O") == "Zn(XYZ)2·6H2O"


def test_hydrate_bare_anion_shape_out_of_scope():
    # CuSO4·5H2O uses the bare (no-parens) salt grammar, which this v1
    # transform deliberately does not parse (ambiguous cation/anion split
    # without guessing) — left untouched rather than guessed at.
    assert speak_chemistry("CuSO4·5H2O") == "CuSO4·5H2O"


def test_designator_denylist_known_non_chemistry_pairs():
    assert speak_chemistry("GPT-4") == "GPT-4"
    assert speak_chemistry("COVID-19") == "COVID-19"


def test_plain_english_prose_untouched():
    text = "The catalyst improved selectivity by a wide margin."
    assert speak_chemistry(text) == text


# ── lexicon-override precedence (wired at the narrate.py layer) ─────────


class _RefMeta:
    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta


@dataclass
class _Chunk:
    chunk_kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class _Store:
    drafts = property(lambda self: self)

    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def reading_order(self, _ref_id: int) -> list[_Chunk]:
        return self._chunks


class _Ref:
    id = 1


def test_lexicon_overrides_chem_transform_in_render_narration():
    store = _Store([_Chunk("paragraph", "sample of MOF-74 was analyzed")])
    segs = render_narration(
        store,
        _Ref(),
        default_voice="af_heart",
        default_lang="en-us",
        lexicon={"MOF-74": "em oh eff seventy four"},
    )
    assert segs[0].text == "sample of em oh eff seventy four was analyzed"
    assert "MOF" not in segs[0].text


def test_lexicon_overrides_chem_formula_table():
    store = _Store([_Chunk("paragraph", "add H2O slowly")])
    segs = render_narration(
        store,
        _Ref(),
        default_voice="af_heart",
        default_lang="en-us",
        lexicon={"H2O": "aitch two oh"},
    )
    assert segs[0].text == "add aitch two oh slowly"


def test_no_lexicon_still_applies_chem_transform():
    store = _Store([_Chunk("paragraph", "add H2O slowly")])
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    assert segs[0].text == "add water slowly"


# ── wired end-to-end through narrate.py ──────────────────────────────────


def test_render_narration_applies_chem_transform():
    store = _Store(
        [_Chunk("paragraph", "The MOF-74 sample was soaked in Zn(NO3)2·6H2O.")]
    )
    segs = render_narration(
        store, _Ref(), default_voice="af_heart", default_lang="en-us"
    )
    assert segs[0].text == (
        "The MOF seventy-four sample was soaked in zinc nitrate hexahydrate."
    )


def test_markdown_segments_applies_chem_transform():
    segs = markdown_segments(
        "Synthesized using Zn(NO3)2·6H2O and calcined MOF-74 crystals.",
        voice="af_heart",
        lang="en-us",
    )
    assert segs[0].text == (
        "Synthesized using zinc nitrate hexahydrate and calcined "
        "MOF seventy-four crystals."
    )
