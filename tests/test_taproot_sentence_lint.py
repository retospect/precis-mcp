"""Advisory admissibility/grammar lint (`src/precis/taproot/sentence_lint.py`).

Pure-function unit tests for `lint_claim_sentence` and `lint_scope` -- no DB
fixture, mirroring the pure-function section of `tests/test_taproot_notation.py`.
Covers every code named in `docs/backlog/nanopub-corpus-remediation.md`
Phase 1, plus the explicit carve-outs (clean sentence, `S/cm`-style
denominators are out of this module's scope but a fully compliant sentence
must still pass clean).
"""

from __future__ import annotations

from precis.taproot.sentence_lint import lint_claim_sentence, lint_scope

_CLEAN_SENTENCE = (
    "DFT predicts a 1.2 eV band gap for monolayer MoS2 under 2% biaxial strain."
)


# ── never raises ─────────────────────────────────────────────────────────


def test_lint_claim_sentence_empty_string_returns_empty_list() -> None:
    assert lint_claim_sentence("") == []


def test_lint_claim_sentence_never_raises_on_odd_input() -> None:
    for s in ["\x00", "}{[", "~" * 50, "≈" * 20, ";" * 20, "a" * 5000]:
        assert isinstance(lint_claim_sentence(s), list)


def test_lint_scope_empty_dict_returns_scope_empty() -> None:
    warnings = lint_scope({})
    assert any("scope-empty" in w for w in warnings)


def test_lint_scope_never_raises_on_odd_values() -> None:
    for scope in [{"material": ""}, {"material": None}, {"material": 123}]:
        assert isinstance(lint_scope(scope), list)  # type: ignore[arg-type]


# ── lint_claim_sentence: each code fires, named specifically ────────────


def test_not_falsifiable_fires_on_label_style() -> None:
    warnings = lint_claim_sentence(
        "Meir & Wingreen 1992 — Landauer formula for interacting electrons."
    )
    assert any("not-falsifiable" in w for w in warnings)


def test_not_falsifiable_fires_on_bibliography_lead() -> None:
    warnings = lint_claim_sentence(
        "Meir and Wingreen 1992 introduced a formula for interacting electrons."
    )
    assert any("not-falsifiable" in w for w in warnings)


def test_not_falsifiable_fires_on_copula_definition() -> None:
    warnings = lint_claim_sentence(
        "NUPACK is a software suite for nucleic acid design."
    )
    assert any("not-falsifiable" in w for w in warnings)


def test_not_falsifiable_fires_on_study_happened_phrasing() -> None:
    warnings = lint_claim_sentence(
        "Surface interactions between graphene nanobuds and cerium(III) "
        "were investigated."
    )
    assert any("not-falsifiable" in w for w in warnings)


# ── not-falsifiable: verbless recall (prod lint run, 2026-08-19) ─────────
#
# Nine real corpus titles from the coordinator's prod run over 1,524 live
# hubs -- four already caught by the four shape-based sub-checks above,
# three that the `verbless` addition below now catches (none of the four
# shape checks fire on them), and three genuine claims that must stay
# UNflagged even though one carries an author name and a year.

_PROD_MUST_FLAG: tuple[str, ...] = (
    "EDRR chloride-medium electrohydrometallurgy: Au recovery from "
    "refractory telluride ore",
    "Dennard 1974: constant-field scaling rules for MOSFETs",
    "NUPACK is a software suite for nucleic acid design",
    "Surface interactions between graphene nanobuds and cerium(III) were investigated",
    # verbless misses -- caught only by _has_finite_verb, no shape-based
    # sub-check fires on these:
    "MOF/PPy + MnO2 hybrid CDI defluorination: 55.12 mg-F/g at 1.2 V, 5 cycle regen",
    "PCR cartridge design with planar laminated card",
    "Landauer 1957/1970 - conductance as transmission",
)

_PROD_MUST_NOT_FLAG: tuple[str, ...] = (
    "SWRO brine spreads up to 5 km along seabed; impairs benthic ecosystems",
    "DFT predicts nanobuds adsorb Li more strongly than graphene.",
    "Haber 1927 gold-from-seawater program failed because real Au "
    "concentrations were ≈1000× lower than reported",
)


def test_not_falsifiable_prod_must_flag_sentences() -> None:
    for sentence in _PROD_MUST_FLAG:
        warnings = lint_claim_sentence(sentence)
        assert any("not-falsifiable" in w for w in warnings), sentence


def test_not_falsifiable_prod_must_not_flag_sentences() -> None:
    for sentence in _PROD_MUST_NOT_FLAG:
        warnings = lint_claim_sentence(sentence)
        assert not any("not-falsifiable" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_not_falsifiable_author_name_may_fire_but_not_verbless() -> None:
    # The Haber 1927 sentence carries an author name and a year -- it is
    # still a claim (it has a finite verb, 'failed'), so author-name may
    # fire independently but not-falsifiable/verbless must not.
    sentence = (
        "Haber 1927 gold-from-seawater program failed because real Au "
        "concentrations were ≈1000× lower than reported"
    )
    warnings = lint_claim_sentence(sentence)
    assert any("author-name" in w for w in warnings)
    assert not any("not-falsifiable" in w for w in warnings)


def test_not_falsifiable_label_style_handles_ascii_hyphen_variant() -> None:
    # Real corpus text uses '-', '--', and em-dash interchangeably as a
    # label separator.
    for sep in ("-", "--", "—", "–"):
        sentence = f"Landauer 1957/1970 {sep} conductance as transmission"
        warnings = lint_claim_sentence(sentence)
        assert any("not-falsifiable" in w for w in warnings), sentence


def test_verbless_does_not_fire_on_nomenclature_or_units() -> None:
    # Sanity: the generic verb-inflection-shape fallback must not treat
    # ordinary plural units/nomenclature as verb evidence in a sentence
    # that otherwise has no verb -- these still correctly fire verbless,
    # they just must not be *misread* as containing a verb for the wrong
    # reason (asserted indirectly: the sentence is still flagged).
    warnings = lint_claim_sentence("A stack of 500 nm films and 10 layers")
    assert any("not-falsifiable" in w for w in warnings)


def test_verbless_stoplist_words_do_not_count_as_verb_evidence() -> None:
    # Each of these ends in s/ed/es (the generic fallback's shape) but is
    # a plural noun or attributive adjective in this corpus, not a verb --
    # a sentence built ONLY from stoplist words plus non-verb filler must
    # still read as verbless. ('results' and 'printed' are deliberately
    # excluded: both are also genuine verb forms and sit in
    # `_FINITE_VERB_RE`'s whitelist, which -- correctly, per the
    # false-negative bias -- outranks this stoplist.)
    for word in [
        "applications",
        "properties",
        "materials",
        "rules",
        "studies",
        "devices",
        "films",
        "cells",
        "electrodes",
        "series",
        "analysis",
        "laminated",
        "integrated",
    ]:
        sentence = f"A survey of {word} for next-generation devices"
        warnings = lint_claim_sentence(sentence)
        assert any("not-falsifiable" in w for w in warnings), (word, sentence)


def test_dangling_reference_fires_on_the_same_group() -> None:
    warnings = lint_claim_sentence(
        "The same group demonstrated ultra-sensitive detection of "
        "vitamins B9 and B12 using SERS."
    )
    assert any("dangling-reference" in w for w in warnings)


def test_dangling_reference_fires_on_each_phrase() -> None:
    for phrase in [
        "This work reports a new synthesis route.",
        "As above, the mobility exceeds 10^4.",
        "The authors report a novel mechanism.",
        "These results confirm the earlier assignment.",
        "The former shows a larger shift than the latter.",
        "The best performing device reached 90% efficiency.",
    ]:
        warnings = lint_claim_sentence(phrase)
        assert any("dangling-reference" in w for w in warnings), phrase


def test_no_evidence_verb_fires_when_absent() -> None:
    warnings = lint_claim_sentence("Mobility increases with encapsulation.")
    assert any("no-evidence-verb" in w for w in warnings)


def test_no_evidence_verb_does_not_fire_on_controlled_verb_inflections() -> None:
    for sentence in [
        "DFT predicts a larger gap under strain.",
        "TEM finds a 5-8-5 defect pair in the lattice.",
        "Raman shows a redshift of the G peak.",
        "STM measures a 0.3 nm step height.",
        "AFM observed a 2 nm roughness increase.",
        "XRD demonstrated a phase transition at 400 K.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("no-evidence-verb" in w for w in warnings), sentence


def test_no_epistemic_mode_fires_when_absent() -> None:
    warnings = lint_claim_sentence("Mobility increases with encapsulation.")
    assert any("no-epistemic-mode" in w for w in warnings)


def test_no_epistemic_mode_does_not_fire_on_method_tokens() -> None:
    for sentence in [
        "DFT predicts a larger gap under strain.",
        "Spin-polarized DFT predicts a magnetic moment of 2 uB per atom.",
        "Molecular dynamics finds a diffusion barrier of 0.4 eV.",
        "MD predicts a melting point near 1200 K.",
        "NEGF predicts a conductance quantum plateau.",
        "TEM finds a 5-8-5 defect pair in the lattice.",
        "c-AFM measures a local conductance drop at the grain boundary.",
        "Raman shows a redshift of the G peak.",
        "First-principles calculations predict a direct band gap.",
        "Monte Carlo predicts a critical temperature near 300 K.",
        "Nanoindentation measures a hardness of 12 GPa.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("no-epistemic-mode" in w for w in warnings), sentence


def test_multi_assertion_fires_on_and_join() -> None:
    warnings = lint_claim_sentence(
        "DFT shows the band gap increases under strain, and Raman confirms "
        "the associated redshift."
    )
    assert any("multi-assertion" in w for w in warnings)


def test_multi_assertion_fires_on_semicolon_join() -> None:
    warnings = lint_claim_sentence(
        "DFT predicts a 1.2 eV gap; TEM confirms a 5-8-5 defect pair."
    )
    assert any("multi-assertion" in w for w in warnings)


def test_multi_assertion_does_not_fire_on_single_clause() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("multi-assertion" in w for w in warnings)


def test_multi_assertion_ignores_serial_comma_in_noun_list() -> None:
    # gr245400: `A, B, and C` is one assertion. Splitting on the serial
    # comma stranded the subject on one side of the cut and the verb on the
    # other, so the finite-verb count read one claim as two.
    for sentence in [
        "X-ray diffraction shows that CD-MOF-1 (K+), CD-MOF-2 (Rb+), and "
        "CD-MOF-3 (Cs+) are all isostructural.",
        "Infrared spectroscopy observes absorption bands at 2159, 2025, and "
        "1977 cm-1 in the calcined material.",
        "DFT calculations show that the atom-to-atom, bond-to-bond, and "
        "bond-to-ring configurations differ in stability.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("multi-assertion" in w for w in warnings), sentence


def test_multi_assertion_still_fires_when_verb_set_misses_the_predicate() -> None:
    # The length cap, not the verb set, is what catches this one: the span
    # before `, and` carries a predicate (`cannot be switched off`) that
    # `_FINITE_VERB_RE` does not know, but it is far too long to be a list
    # item, so the join is still read as a coordination.
    warnings = lint_claim_sentence(
        "Because graphene has no gap between its valence and conduction "
        "bands, a graphene field-effect transistor channel cannot be "
        "switched off, and the device's on/off current ratio hardly "
        "exceeds 100."
    )
    assert any("multi-assertion" in w for w in warnings)


def test_multi_assertion_digit_grouping_comma_does_not_open_a_list() -> None:
    # The comma in `10,000` separates nothing; if it were taken as the
    # comma that opened an enumeration, the "item" it closes would be the
    # tail of a number ("000 cm2/Vs") and the real coordination would slip.
    warnings = lint_claim_sentence(
        "Measurements find mobilities of 10,000 cm2/Vs, and transport "
        "remains ballistic at submicron distances."
    )
    assert any("multi-assertion" in w for w in warnings)


def test_no_terminal_period_fires_when_missing() -> None:
    warnings = lint_claim_sentence("DFT predicts a 1.2 eV band gap for monolayer MoS2")
    assert any("no-terminal-period" in w for w in warnings)


def test_no_terminal_period_does_not_fire_when_present() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("no-terminal-period" in w for w in warnings)


def test_over_long_fires_past_budget() -> None:
    long_sentence = "DFT predicts a value of " + ("x" * 240) + " for the gap."
    warnings = lint_claim_sentence(long_sentence)
    assert any("over-long" in w for w in warnings)


def test_over_long_does_not_fire_on_clean_sentence() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("over-long" in w for w in warnings)


def test_author_name_fires_on_surname_year() -> None:
    warnings = lint_claim_sentence(
        "Collins 2006 reports a mobility above 10 000 cm2/Vs."
    )
    assert any("author-name" in w for w in warnings)


def test_author_name_fires_on_et_al() -> None:
    warnings = lint_claim_sentence(
        "Novoselov et al. reports isolation of monolayer graphene."
    )
    assert any("author-name" in w for w in warnings)


def test_author_name_does_not_fire_on_clean_sentence() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("author-name" in w for w in warnings)


# ── em-dash: dash used as a clause/label separator ───────────────────────


def test_em_dash_fires_on_each_banned_form() -> None:
    for sep in ("—", "--", "-"):
        sentence = f"DFT predicts a 1.2 eV gap {sep} Raman confirms a redshift."
        warnings = lint_claim_sentence(sentence)
        assert any("em-dash" in w for w in warnings), sentence


def test_em_dash_fires_on_unspaced_em_dash() -> None:
    # No legitimate parenthetical use exists in this corpus (90/90 hubs
    # with an em dash use it as a separator), so the em dash fires
    # whether spaced or not -- unlike the ASCII stand-ins, which only
    # fire spaced.
    warnings = lint_claim_sentence(
        "DFT predicts a 1.2 eV gap—Raman confirms a redshift."
    )
    assert any("em-dash" in w for w in warnings)


def test_em_dash_does_not_fire_on_en_dash_numeric_ranges() -> None:
    for sentence in [
        "DFT predicts a mobility gain over the 5–10 nm range.",
        "TEM measures a growth window of 240–280 °C for the film.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("em-dash" in w for w in warnings), sentence


def test_em_dash_does_not_fire_on_en_dash_compound_names() -> None:
    for sentence in [
        "DFT–NEGF predicts a conductance plateau near the Dirac point.",
        "XPS finds a Cu–Zn alloy shift in the binding energy.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("em-dash" in w for w in warnings), sentence


def test_em_dash_does_not_fire_on_in_word_hyphens() -> None:
    for sentence in [
        "DFT predicts a chloride-medium reaction pathway shift.",
        "TEM measures a sub-10-nm grain size in the UiO-66 sample.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("em-dash" in w for w in warnings), sentence


def test_em_dash_does_not_fire_on_clean_sentence() -> None:
    assert not any("em-dash" in w for w in lint_claim_sentence(_CLEAN_SENTENCE))


def test_em_dash_and_not_falsifiable_both_fire_on_leading_label() -> None:
    # `_LABEL_STYLE_RE` catches the leading `Surname YYYY — topic` shape
    # as not-falsifiable; `em-dash` independently flags the punctuation.
    # These are two different, independently actionable facts about the
    # same string -- both must fire, not just one.
    sentence = "Meir & Wingreen 1992 — Landauer formula for interacting electrons."
    warnings = lint_claim_sentence(sentence)
    assert any("not-falsifiable" in w for w in warnings), warnings
    assert any("em-dash" in w for w in warnings), warnings


def test_em_dash_fires_without_not_falsifiable_when_mid_sentence() -> None:
    # Past the leading-label window (60 chars) or not colon/dash-anchored
    # at the start, `_LABEL_STYLE_RE` does not fire -- but the em dash is
    # still a banned separator anywhere in the sentence.
    sentence = (
        "DFT predicts a 1.2 eV band gap for monolayer MoS2 under strain "
        "— a result consistent with prior Raman measurements."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("em-dash" in w for w in warnings), warnings
    assert not any("not-falsifiable" in w for w in warnings), warnings


# ── tense: past-passive (blocking), past-tense / present-perfect ─────────
#
# 2026-08-19 tense standard (Reto): simple present is the default, never
# flagged; simple past is correct only when the claim's subject is itself
# a historical event (advisory, machine-undecidable); present perfect is
# allowed only for existence/achievement claims (advisory, also
# machine-undecidable); past passive with no result stated is a bare
# history-of-science/activity report and is banned outright (blocking).


def test_past_passive_fires_on_kirkpatrick_true_positive() -> None:
    sentence = (
        "Simulated annealing as a combinatorial-optimization metaheuristic "
        "was proposed by Kirkpatrick et al."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("past-passive" in w for w in warnings), warnings


def test_past_passive_fires_on_nanobud_investigated_true_positive() -> None:
    sentence = (
        "Surface interactions between graphene nanobuds and cerium(III) "
        "were investigated."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("past-passive" in w for w in warnings), warnings


def test_past_passive_does_not_fire_on_attributive_participle() -> None:
    # 'mounted' sits in `_VERB_SHAPE_EXCEPTIONS` -- an attributive
    # past-participle-as-adjective, not a passive main verb -- so a
    # was/were construction using it must never fire past-passive
    # (bias toward false negatives, per the module docstring).
    for sentence in [
        "PCR cartridge design with planar laminated card.",
        "The sensor was mounted on a flexible substrate.",
    ]:
        warnings = lint_claim_sentence(sentence)
        assert not any("past-passive" in w for w in warnings), (sentence, warnings)


def test_past_passive_does_not_fire_on_present_perfect() -> None:
    # 'has been engineered' is present perfect, not past passive -- must
    # not be misread (the two codes are structurally disjoint: past-passive
    # matches only was/were, present-perfect only has/have).
    sentence = "Carbon NanoBud material has been engineered into printed touch sensors."
    warnings = lint_claim_sentence(sentence)
    assert not any("past-passive" in w for w in warnings), warnings
    assert any("present-perfect" in w for w in warnings), warnings


def test_past_passive_does_not_fire_on_active_present_sentence() -> None:
    warnings = lint_claim_sentence(
        "DFT predicts nanobuds adsorb Li more strongly than graphene."
    )
    assert not any("past-passive" in w for w in warnings), warnings


def test_past_passive_suppresses_past_tense_on_the_same_sentence() -> None:
    # past-passive is the most specific of the three tense codes -- a
    # sentence matching it must never also emit past-tense.
    sentence = (
        "Simulated annealing as a combinatorial-optimization metaheuristic "
        "was proposed by Kirkpatrick et al."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("past-passive" in w for w in warnings), warnings
    assert not any("past-tense" in w for w in warnings), warnings


def test_past_tense_fires_on_historical_event_subject() -> None:
    # Rule 2's own worked example: rewriting to present ("...fails
    # because...") would make the sentence false/absurd, so past is
    # correct here -- flagged only as advisory, never blocking.
    sentence = (
        "Haber's 1927 gold-from-seawater program failed because real Au "
        "concentrations were ~1000x lower than reported."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("past-tense" in w for w in warnings), warnings
    assert not any("past-passive" in w for w in warnings), warnings


def test_past_tense_does_not_fire_on_simple_present_sentence() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("past-tense" in w for w in warnings)


def test_present_perfect_fires_on_achievement_claim() -> None:
    sentence = (
        "Room-temperature coherence has been demonstrated in this material via NMR."
    )
    warnings = lint_claim_sentence(sentence)
    assert any("present-perfect" in w for w in warnings), warnings


def test_present_perfect_does_not_fire_on_simple_present_sentence() -> None:
    warnings = lint_claim_sentence(_CLEAN_SENTENCE)
    assert not any("present-perfect" in w for w in warnings)


# ── lint_claim_sentence: fully compliant sentence ────────────────────────


def test_fully_admissible_sentence_produces_no_warnings() -> None:
    assert lint_claim_sentence(_CLEAN_SENTENCE) == []


# ── lint_scope ────────────────────────────────────────────────────────────


def test_scope_unknown_key_fires() -> None:
    warnings = lint_scope({"notes": "misc"})
    assert any("scope-unknown-key" in w for w in warnings)


def test_scope_unknown_key_does_not_fire_on_enumerated_keys() -> None:
    warnings = lint_scope(
        {
            "material": "graphene",
            "method": "DFT",
            "regime": "low-temperature",
            "system": "MoS2 monolayer",
            "quantity": "band gap",
            "substrate": "SiO2",
            "temperature": "300 K",
        }
    )
    assert not any("scope-unknown-key" in w for w in warnings)


def test_scope_free_text_fires_on_prose_fragment() -> None:
    warnings = lint_scope({"quantity": "engineered into printed"})
    assert any("scope-free-text" in w for w in warnings)


def test_scope_free_text_fires_on_longer_prose_fragment() -> None:
    warnings = lint_scope({"quantity": "engineered into printed touch sensors"})
    assert any("scope-free-text" in w for w in warnings)


def test_scope_free_text_does_not_fire_on_short_tokens() -> None:
    warnings = lint_scope(
        {
            "material": "graphene",
            "method": "DFT",
            "temperature": "300 K",
            "system": "MoS2 monolayer",
        }
    )
    assert not any("scope-free-text" in w for w in warnings)


def test_scope_empty_is_advisory_not_paired_with_other_codes() -> None:
    assert lint_scope({}) == [
        "scope-empty: scope is empty -- advisory only, not an error."
    ]


# ── mixed-point-range (2026-08-20 numeric-value policy) ──────────────────
# Deliberately conservative: this module has no access to the source, so it
# flags only the false-precision *shape* -- a bare point beside a range
# sharing its unit, no typical-value marker nearby -- and every negative
# case below pins a way that shape must NOT be over-read.


def test_mixed_point_range_fires_on_bare_point_beside_a_range() -> None:
    warnings = lint_claim_sentence(
        "Nanoindentation measures a hardness of 3.2 GPa, within a reported "
        "range of 3–3.4 GPa."
    )
    assert any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_typical_plus_range() -> None:
    # The preferred shape when the source designates a typical value --
    # must never be flagged as false precision.
    warnings = lint_claim_sentence(
        "Nanoindentation measures a bulk modulus of approximately 9 GPa "
        "across a reported 9–12 GPa range."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_approx_symbol_marker() -> None:
    warnings = lint_claim_sentence(
        "Nanoindentation measures a bulk modulus of ≈9 GPa across a "
        "reported 9–12 GPa range."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_range_alone() -> None:
    warnings = lint_claim_sentence(
        "Nanoindentation measures a bulk modulus of 9–12 GPa across samples."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_point_alone() -> None:
    warnings = lint_claim_sentence(
        "Nanoindentation measures a bulk modulus of 9.4 GPa for the sample."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_different_units() -> None:
    # The point value and the range share no unit -- not the same quantity.
    warnings = lint_claim_sentence(
        "DFT predicts a 1.2 eV band gap for a device operating at 9–12 GPa."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_two_ranges_same_unit() -> None:
    # Two side-by-side ranges for two different regimes -- neither
    # endpoint is a stray point, and this must never cross-fire.
    warnings = lint_claim_sentence(
        "The modulus measures 9–12 GPa for the annealed sample and 3–5 GPa "
        "for the as-deposited one."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_hyphenated_compound_word() -> None:
    # A hyphenated compound adjective must never be misread as a range.
    warnings = lint_claim_sentence(
        "DFT-computed values of 3.2 GPa are reported for the single-walled case."
    )
    assert not any("mixed-point-range" in w for w in warnings)


def test_mixed_point_range_does_not_fire_on_negative_number() -> None:
    # A single negative value has no second number to form a range with --
    # must never be mistaken for one.
    warnings = lint_claim_sentence(
        "DFT predicts a stress of −3.2 GPa near the interface."
    )
    assert not any("mixed-point-range" in w for w in warnings)
