"""``precis.taproot.migrate`` — the Phase 0 (score) + Phase 1 (dry-run)
migration runner for pre-existing (pre-decomposition) claim hubs
(docs/backlog/taproot-atomic-claims.md §Strategy).

Two layers, mirroring ``tests/test_taproot_backfill.py``'s split:

* Pure scoring (``score_sentence`` / ``cohort_for_score``) — no DB, no model.
* DB-backed ``score_hubs`` / ``dry_run`` over real ``refs``/``chunks``/
  ``links`` via the ``store`` fixture, hubs seeded through the real write
  door (``mint_hub`` / ``link_claims``) so the compound/stamp exclusions
  exercise the actual predicate. ``dry_run``'s ``extract_fn`` is always
  injected (a deterministic fake) — no live LLM call anywhere in this file.
"""

from __future__ import annotations

import json

import pytest

from precis.store.store import Store
from precis.taproot.canon import CanonicalClaim, ClaimExtraction, NotClaim
from precis.taproot.hub import link_claims, mint_hub
from precis.taproot.migrate import (
    COHORTS,
    DryRunReport,
    classify_extraction,
    cohort_for_score,
    dry_run,
    dump_outcomes_jsonl,
    render_report,
    score_hubs,
    score_sentence,
)

# ── score_sentence / cohort_for_score — pure, no DB ────────────────────────


def test_score_sentence_no_signals_is_zero() -> None:
    score, signals = score_sentence("Pd/C catalyzes Suzuki coupling at RT")
    assert score == 0
    assert signals == ()


def test_score_sentence_conjunction_and_scores() -> None:
    score, signals = score_sentence("Graphene has high strength and high conductivity")
    assert "conjunction" in signals
    assert score >= 2


def test_score_sentence_word_boundary_does_not_match_band() -> None:
    """ "band" contains the substring "and" but must never trip the
    conjunction heuristic — the word-boundary regex is the whole point."""
    score, signals = score_sentence("Optical absorption band shifts with strain")
    assert "conjunction" not in signals
    assert score == 0


def test_score_sentence_but_and_while_both_trigger() -> None:
    _, signals_but = score_sentence("It is fast but not accurate")
    _, signals_while = score_sentence("It degrades while heating")
    assert "conjunction" in signals_but
    assert "conjunction" in signals_while


def test_score_sentence_long_title_scores() -> None:
    long_title = "A" * 161
    score, signals = score_sentence(long_title)
    assert "long-title" in signals
    assert score >= 2


def test_score_sentence_short_title_no_length_signal() -> None:
    _, signals = score_sentence("A" * 160)
    assert "long-title" not in signals


def test_score_sentence_semicolon_scores() -> None:
    score, signals = score_sentence("X increases yield; Y decreases side products")
    assert "semicolon" in signals
    assert score >= 2


def test_score_sentence_multi_comma_scores() -> None:
    score, signals = score_sentence("X, Y, and Z were measured")
    assert "multi-comma" in signals
    assert score >= 1


def test_score_sentence_single_comma_no_signal() -> None:
    _, signals = score_sentence("X, Y were measured")
    assert "multi-comma" not in signals


def test_score_sentence_combined_signals_sum() -> None:
    score, signals = score_sentence(
        "X shows A and B; also C, D, and E over a long qualifying tail " + "z" * 120
    )
    assert {"conjunction", "semicolon", "multi-comma", "long-title"} <= set(signals)
    assert score == 2 + 2 + 2 + 1


def test_cohort_for_score_zero_is_atomic() -> None:
    assert cohort_for_score(0) == "likely-atomic"


def test_cohort_for_score_high_is_compound() -> None:
    assert cohort_for_score(4) == "likely-compound"
    assert cohort_for_score(6) == "likely-compound"


def test_cohort_for_score_mid_is_uncertain() -> None:
    assert cohort_for_score(1) == "uncertain"
    assert cohort_for_score(2) == "uncertain"


def test_cohorts_constant_matches_literal_set() -> None:
    assert set(COHORTS) == {"likely-compound", "uncertain", "likely-atomic"}


# ── score_hubs — DB-backed ───────────────────────────────────────────────


def _claim(sentence: str, scope: dict[str, str] | None = None) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope=scope or {})


def test_score_hubs_scores_and_sorts_live_claim_hubs(store: Store) -> None:
    atomic = mint_hub(store, _claim("Pd/C catalyzes Suzuki coupling at RT"))
    compoundish = mint_hub(
        store,
        _claim(
            "Graphene shows high strength and high conductivity; also great "
            "flexibility, low weight, and low cost"
        ),
    )

    scores = score_hubs(store)
    by_id = {s.ref_id: s for s in scores}
    assert atomic in by_id
    assert compoundish in by_id
    assert by_id[atomic].cohort == "likely-atomic"
    assert by_id[atomic].score == 0
    assert by_id[compoundish].cohort == "likely-compound"
    assert by_id[compoundish].score > by_id[atomic].score

    # Sorted score descending (ties broken by ref_id ascending).
    assert scores == sorted(scores, key=lambda s: (-s.score, s.ref_id))


def test_score_hubs_excludes_compound_hubs(store: Store) -> None:
    """A hub with a live inbound ``conjunct-of`` edge (i.e. IS a compound)
    is never a migration candidate — nothing left to decompose."""
    atom = mint_hub(store, _claim("Atom claim one for compound exclusion test"))
    compound = mint_hub(
        store, _claim("Compound claim bundling atom one and atom two together")
    )
    link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )

    scores = score_hubs(store)
    ids = {s.ref_id for s in scores}
    assert atom in ids  # the atom itself is still a plain, scoreable hub
    assert compound not in ids  # the compound is excluded


def test_score_hubs_scores_full_claim_sentence(store: Store) -> None:
    """A claim sentence is stored full-length in ``refs.title`` (no cap —
    the title carries all the meaning) and mirrored in the ``finding_body``
    chunk; scoring reads the chunk (via :data:`_CANDIDATE_HUBS_SQL`'s JOIN)
    and both must agree even for a long sentence."""
    lead = "A" * 210  # would have crossed the old 200-char cap
    sentence = f"{lead} and this trailing clause only exists past the old cutoff"
    hub_id = mint_hub(store, _claim(sentence))

    scores = score_hubs(store)
    by_id = {s.ref_id: s for s in scores}
    assert by_id[hub_id].sentence == sentence
    assert by_id[hub_id].title == sentence  # full, never truncated
    assert "conjunction" in by_id[hub_id].signals
    assert by_id[hub_id].cohort == "likely-compound"


def test_score_hubs_excludes_already_stamped_hubs(store: Store) -> None:
    hub_id = mint_hub(store, _claim("Already migrated claim, stamped and done"))
    store.update_ref(
        hub_id, meta_patch={"taproot_decomposed_at": "2026-08-14T00:00:00Z"}
    )

    scores = score_hubs(store)
    assert hub_id not in {s.ref_id for s in scores}


# ── dry_run — DB-backed, fake extract_fn (no LLM) ───────────────────────


def _extraction_for(title: str) -> ClaimExtraction:
    """Deterministic fake ``extract_fn``: a title containing "SPLIT" splits
    into two atoms (a genuine word-halves partition of the title, so the
    P0-2/P0-3 gates see a realistic, fully-covered, non-nested split — not
    two atoms that each restate the whole compound plus a suffix, which
    would itself look nested/lossy) + a compound; "NOCLAIM" yields the
    empty extraction; anything else is treated as already-atomic
    (pass-through). The rejected conjunct's text is drawn from the title
    itself — a real not-claim quotes the sentence it rejects, and the
    round-2 precision gate correctly flags invented not-claim wording as
    added material (`lossy`)."""
    if "NOCLAIM" in title:
        return ClaimExtraction(atoms=(), compound=None, not_claims=())
    if "SPLIT" in title:
        words = title.split()
        not_claims: tuple[NotClaim, ...] = (
            NotClaim(text=" ".join(words[-2:]), reason="forward-looking"),
        )
        mid = max(1, len(words) // 2)
        return ClaimExtraction(
            atoms=(_claim(" ".join(words[:mid])), _claim(" ".join(words[mid:]))),
            compound=_claim(title),
            not_claims=not_claims,
        )
    return ClaimExtraction(atoms=(_claim(title),), compound=None, not_claims=())


def _fake_extract(chunk_text: str) -> ClaimExtraction:
    return _extraction_for(chunk_text)


# ── classify_extraction — pure, no DB (full calibration against the ────
# labelled 25-hub fixture lives in test_taproot_migrate_gates.py) ────────


def test_classify_extraction_no_claim_skips_gates() -> None:
    verdict, gate_meta = classify_extraction(
        "Some sentence.", ClaimExtraction(atoms=(), compound=None, not_claims=())
    )
    assert verdict == "no-claim"
    assert gate_meta == {}


def test_classify_extraction_pass_through_with_full_recall() -> None:
    sentence = "Palladium on carbon catalyzes Suzuki coupling at room temperature."
    extraction = ClaimExtraction(
        atoms=(_claim(sentence),), compound=None, not_claims=()
    )
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "pass-through"
    assert gate_meta["recall"] == 1.0
    assert gate_meta["missing_numbers"] == ()


def test_classify_extraction_pass_through_dropping_content_is_lossy() -> None:
    """A single atom that keeps only a fragment of a long compound sentence
    (P0-2's headline pilot defect — 6 of 11 pass-throughs did exactly
    this) must gate to `lossy`, never `pass-through`."""
    sentence = (
        "Widget alloys self-assemble from base metals and rare-earth dopants via "
        "a slow annealing process; larger crystals based on cubic and hexagonal "
        "lattices have been grown, and defect clustering in mixtures of dopant "
        "species produces multiple distinct grain boundary types with high yield."
    )
    kept = "Defect clustering in mixtures of dopant species produces multiple distinct grain boundary types with high yield."
    extraction = ClaimExtraction(atoms=(_claim(kept),), compound=None, not_claims=())
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert gate_meta["recall"] < 0.73


def test_classify_extraction_dropped_number_is_hard_lossy() -> None:
    """Even with otherwise-decent recall, a dropped number-bearing token
    (a measurement, not a catalog name) is a hard `lossy` (P0-2's "n-type
    409 uA/um" pilot control)."""
    sentence = (
        "The device achieves on-state currents of 800 uA/um for p-type and "
        "409 uA/um for n-type applications, exceeding prior results."
    )
    kept = "The device achieves on-state currents of 800 uA/um for p-type applications."
    extraction = ClaimExtraction(atoms=(_claim(kept),), compound=None, not_claims=())
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert "409" in gate_meta["missing_numbers"]


def test_classify_extraction_dropped_number_never_hides_in_larger_number() -> None:
    """A dropped short number must not evade the coverage gate by being a
    digit-substring of a retained larger number: "9" dropped while "409"
    is kept is still hard `lossy` (exact token membership, not substring —
    reviewer catch on the P0-2 gate, 2026-08-14)."""
    sentence = (
        "The sample shows 9 distinct grain boundary types and reaches "
        "on-state currents of 409 uA/um at room temperature."
    )
    kept = "The sample reaches on-state currents of 409 uA/um at room temperature."
    extraction = ClaimExtraction(atoms=(_claim(kept),), compound=None, not_claims=())
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert "9" in gate_meta["missing_numbers"]


def test_classify_extraction_catalog_name_digit_is_not_a_number_token() -> None:
    """A material name that merely contains a digit ("MOF-5") is not a
    number-bearing token — dropping it from an "(e.g., ...)" example list
    when a split generalizes it away must not hard-fail the coverage
    gate (fi176435)."""
    sentence = (
        "Rigid frameworks (e.g., MOF-5, ZIF-8) have moduli of 2-30 GPa, while "
        "flexible frameworks can reach anisotropy ratios up to 400:1."
    )
    atom1 = _claim(
        "Rigid frameworks have moduli of 2-30 GPa.", scope={"quantity": "2-30 GPa"}
    )
    atom2 = _claim(
        "Flexible frameworks can reach anisotropy ratios up to 400:1.",
        scope={"quantity": "400:1"},
    )
    extraction = ClaimExtraction(
        atoms=(atom1, atom2), compound=_claim(sentence), not_claims=()
    )
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "split"
    assert gate_meta["missing_numbers"] == ()


def test_classify_extraction_nested_atoms_is_nested() -> None:
    """P0-3: one atom's content strictly contained in another's — a fake
    split, not three facts (fi176441's A1⊂A2⊂A3 pattern)."""
    a1 = _claim("Conductive frameworks exhibit strong electronic coupling.")
    a2 = _claim(
        "Conductive frameworks exhibit strong electronic coupling enabled by "
        "mixed valency."
    )
    compound = _claim(
        "Conductive frameworks exhibit strong electronic coupling enabled by "
        "mixed valency, supporting charge transport."
    )
    extraction = ClaimExtraction(atoms=(a1, a2), compound=compound, not_claims=())
    verdict, gate_meta = classify_extraction(compound.sentence, extraction)
    assert verdict == "nested"
    assert gate_meta["containment"]


def test_classify_extraction_nested_checked_before_lossy() -> None:
    """A nested extraction that would *also* fail the coverage gate must
    still report `nested` — containment runs first (docs/backlog/
    taproot-migration-extraction-quality-gates.md item 3)."""
    original = "A rare, dropped clause plus: conductive frameworks couple strongly."
    a1 = _claim("Conductive frameworks couple strongly.")
    a2 = _claim("Conductive frameworks couple strongly indeed.")
    extraction = ClaimExtraction(
        atoms=(a1, a2), compound=_claim(a2.sentence), not_claims=()
    )
    verdict, _ = classify_extraction(original, extraction)
    assert verdict == "nested"


def test_classify_extraction_pass_through_missing_word_cap() -> None:
    """Round 2 (fi176441, verbatim from the labelled-25 A/B re-run): a
    truncated single atom that drops an entire predicate yet clears the
    recall *ratio* (0.765 > 0.73 — short sentences give the ratio too
    little resolution) must still gate `lossy` via the absolute
    missing-content-word cap."""
    sentence = (
        "Conductive 2D metal-organic frameworks exhibit strong electronic "
        "coupling between metals and ligands, enabled by mixed valency of "
        "both, supporting charge transport."
    )
    kept = (
        "Conductive 2D metal-organic frameworks exhibit strong electronic "
        "coupling enabled by mixed valency of metals."
    )
    extraction = ClaimExtraction(
        atoms=(
            _claim(kept, scope={"material": "Conductive 2D metal-organic frameworks"}),
        ),
        compound=None,
        not_claims=(),
    )
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert gate_meta["recall"] > 0.73, "cap must be the firing gate, not the ratio"
    assert len(gate_meta["missing_content"]) >= 4
    assert "transport" in gate_meta["missing_content"]


def test_classify_extraction_split_is_exempt_from_missing_word_cap() -> None:
    """The missing-content-word cap applies to pass-throughs only: a sound
    split legitimately drops 4+ connective/summarizing words when the
    compound's framing is redistributed across atoms (fi176427/fi176435)."""
    sentence = (
        "Perovskite films crystallize rapidly under thermal annealing, and "
        "remarkably, the resulting devices thereby achieve collectively "
        "efficiencies above twenty percent overall."
    )
    atom1 = _claim("Perovskite films crystallize rapidly under thermal annealing.")
    atom2 = _claim("The resulting devices achieve efficiencies above twenty percent.")
    extraction = ClaimExtraction(
        atoms=(atom1, atom2), compound=_claim(sentence), not_claims=()
    )
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "split"
    assert "missing_content" not in gate_meta


def test_classify_extraction_invented_number_is_hard_lossy() -> None:
    """Round 2's mirror of the dropped-number gate: a number-bearing token
    the extraction *invented* (fi201713 hallucinated "10^208") is hard
    `lossy` even at full recall — a fabricated measurement in a mint-bound
    atom is worse than a lost one."""
    sentence = (
        "The layered cathode material retains structural stability across "
        "repeated charge cycles under ambient operating conditions."
    )
    kept = (
        "The layered cathode material retains structural stability across "
        "92% of repeated charge cycles under ambient operating conditions."
    )
    extraction = ClaimExtraction(atoms=(_claim(kept),), compound=None, not_claims=())
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert gate_meta["recall"] == 1.0
    assert "92%" in gate_meta["invented_numbers"]


def test_classify_extraction_low_precision_added_content_is_lossy() -> None:
    """Round 2's hallucination backstop: an extraction whose union is
    heavily padded with content words the sentence never said (precision
    < 0.8) is `lossy` even with perfect recall — recall only checks what
    was kept, never what was added (fi176275's invented framing)."""
    sentence = "Zeolite catalysts convert methanol into gasoline-range hydrocarbons."
    kept = (
        "Zeolite catalysts convert methanol into gasoline-range hydrocarbons, "
        "revolutionizing sustainable industrial commodity chemistry worldwide."
    )
    extraction = ClaimExtraction(atoms=(_claim(kept),), compound=None, not_claims=())
    verdict, gate_meta = classify_extraction(sentence, extraction)
    assert verdict == "lossy"
    assert gate_meta["recall"] == 1.0
    assert gate_meta["precision"] < 0.8
    assert gate_meta["invented_numbers"] == ()


def _table_counts(store: Store) -> dict[str, int]:
    counts: dict[str, int] = {}
    with store.pool.connection() as conn:
        for table in ("refs", "chunks", "links", "ref_tags"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            counts[table] = int(row[0])
    return counts


def test_dry_run_classifies_split_pass_through_and_no_claim(store: Store) -> None:
    split_hub = mint_hub(store, _claim("SPLIT bundled claim about two facts"))
    pass_hub = mint_hub(store, _claim("A single atomic claim"))
    noclaim_hub = mint_hub(store, _claim("NOCLAIM this asserts nothing groundable"))

    report = dry_run(store, limit=100, extract_fn=_fake_extract)
    by_id = {o.hub.ref_id: o for o in report.outcomes}

    assert by_id[split_hub].verdict == "split"
    extraction = by_id[split_hub].extraction
    assert extraction is not None
    assert len(extraction.atoms) == 2
    assert extraction.compound is not None
    assert extraction.not_claims

    assert by_id[pass_hub].verdict == "pass-through"
    assert by_id[noclaim_hub].verdict == "no-claim"


def test_dry_run_respects_limit_and_cohort_filter(store: Store) -> None:
    compound_titles = [
        f"SPLIT claim {i} with and or but; also, comma, comma" for i in range(3)
    ]
    for t in compound_titles:
        mint_hub(store, _claim(t))

    report = dry_run(store, limit=1, cohort="likely-compound", extract_fn=_fake_extract)
    assert len(report.outcomes) == 1
    assert report.outcomes[0].hub.cohort == "likely-compound"
    assert report.cohort_filter == "likely-compound"


def test_dry_run_samples_controls_from_atomic_cohort(store: Store) -> None:
    top_hub = mint_hub(store, _claim("SPLIT top scored claim and another; also, x, y"))
    atomic_hub = mint_hub(store, _claim("A plain atomic control claim"))

    report = dry_run(
        store,
        limit=1,
        cohort="likely-compound",
        controls=1,
        extract_fn=_fake_extract,
    )
    ids_by_control = {o.hub.ref_id: o.is_control for o in report.outcomes}
    assert ids_by_control.get(top_hub) is False
    assert ids_by_control.get(atomic_hub) is True


def test_dry_run_control_sample_is_deterministic_per_seed(store: Store) -> None:
    """P2-11: the control draw is a uniform random sample, but a fixed
    ``control_seed`` (default 0) must reproduce the same draw against an
    unchanged population — a re-run isn't a re-roll."""
    for i in range(10):
        mint_hub(store, _claim(f"Atomic control candidate number {i} for seed test"))

    first = dry_run(store, limit=0, controls=4, extract_fn=_fake_extract)
    second = dry_run(store, limit=0, controls=4, extract_fn=_fake_extract)
    assert {o.hub.ref_id for o in first.outcomes} == {
        o.hub.ref_id for o in second.outcomes
    }

    different_seed = dry_run(
        store, limit=0, controls=4, control_seed=1, extract_fn=_fake_extract
    )
    # Not asserting inequality (a different seed *could* coincidentally draw
    # the same set) — just that the seed parameter is threaded through and
    # this call succeeds with a full-size sample from the 10-hub pool.
    assert len(different_seed.outcomes) == 4


def test_dry_run_flags_junk_candidate_on_non_control_no_claim(store: Store) -> None:
    """P2-12: a NO-CLAIM on a hub that was NOT sampled as a pass-through
    control means the hub isn't a claim at all (a research note, task
    prose, ...) — route to junk-triage, never treat as an atomic hub with
    nothing to do."""
    noclaim_hub = mint_hub(store, _claim("NOCLAIM this asserts nothing groundable"))

    report = dry_run(store, limit=100, extract_fn=_fake_extract)
    by_id = {o.hub.ref_id: o for o in report.outcomes}

    assert by_id[noclaim_hub].verdict == "no-claim"
    assert by_id[noclaim_hub].junk_candidate is True


def test_dry_run_does_not_flag_junk_candidate_on_control_no_claim(store: Store) -> None:
    """A NO-CLAIM control hub is the *expected* pass-through-control
    outcome shape when the control itself isn't a claim — it's still
    surfaced (verdict stays no-claim) but must not be marked
    ``junk_candidate`` twice-over as if it were an unexpected top-scored
    finding."""
    top_hub = mint_hub(store, _claim("SPLIT top scored claim and another; also, x, y"))
    noclaim_control = mint_hub(store, _claim("NOCLAIM control that is not a claim"))

    report = dry_run(
        store,
        limit=1,
        cohort="likely-compound",
        controls=1,
        control_seed=0,
        extract_fn=_fake_extract,
    )
    by_id = {o.hub.ref_id: o for o in report.outcomes}
    assert by_id[top_hub].junk_candidate is False
    assert by_id[noclaim_control].verdict == "no-claim"
    assert by_id[noclaim_control].junk_candidate is False


def test_dry_run_escalates_lossy_nested_and_junk_candidate_outcomes(
    store: Store,
) -> None:
    """P2-10: ``escalate_fn`` re-runs exactly the three dangerous outcome
    shapes and keeps both results — a good escalated re-extraction can flip
    a lossy pass-through into a sound split without discarding the
    original for the reviewer."""
    lossy_sentence = (
        "Widget alloys self-assemble from base metals and rare-earth dopants "
        "via slow annealing; larger crystals based on cubic lattices have "
        "been grown, and defect clustering produces distinct grain types."
    )
    lossy_hub = mint_hub(store, _claim(lossy_sentence))
    noclaim_hub = mint_hub(store, _claim("NOCLAIM this asserts nothing groundable"))
    fine_hub = mint_hub(store, _claim("A single atomic claim that is fine"))

    def _first_extract(sentence: str) -> ClaimExtraction:
        # The primary pass keeps only the trailing clause of the compound
        # sentence — the recurring pilot defect (P0-2) — everything else
        # falls straight through unchanged (already-atomic, full recall).
        if sentence == lossy_sentence:
            return ClaimExtraction(
                atoms=(_claim("Defect clustering produces distinct grain types."),),
                compound=None,
                not_claims=(),
            )
        return _fake_extract(sentence)

    def _escalate(sentence: str) -> ClaimExtraction:
        # A better extractor: full coverage this time.
        return ClaimExtraction(atoms=(_claim(sentence),), compound=None, not_claims=())

    report = dry_run(store, limit=100, extract_fn=_first_extract, escalate_fn=_escalate)
    by_id = {o.hub.ref_id: o for o in report.outcomes}

    lossy_outcome = by_id[lossy_hub]
    assert lossy_outcome.verdict == "lossy"
    assert lossy_outcome.escalated_verdict == "pass-through"
    assert lossy_outcome.escalated_extraction is not None
    assert lossy_outcome.escalation_error is None

    noclaim_outcome = by_id[noclaim_hub]
    assert noclaim_outcome.junk_candidate is True
    assert noclaim_outcome.escalated_extraction is not None

    # A verdict escalation never touches an outcome that didn't need it.
    fine_outcome = by_id[fine_hub]
    assert fine_outcome.verdict == "pass-through"
    assert fine_outcome.escalated_extraction is None
    assert fine_outcome.escalated_verdict is None


def test_dry_run_isolates_a_per_hub_escalation_error(store: Store) -> None:
    """An escalation dispatch failure is caught per-hub — it must not
    abort the run or trip the primary extractor's consecutive-error
    breaker (that breaker watches ``extract_fn``, not ``escalate_fn``)."""
    noclaim_hub = mint_hub(store, _claim("NOCLAIM this asserts nothing groundable"))

    def _boom_escalate(sentence: str) -> ClaimExtraction:
        raise RuntimeError("escalation dispatch exploded")

    report = dry_run(
        store, limit=100, extract_fn=_fake_extract, escalate_fn=_boom_escalate
    )
    outcome = next(o for o in report.outcomes if o.hub.ref_id == noclaim_hub)
    assert outcome.junk_candidate is True
    assert outcome.escalation_error == "escalation dispatch exploded"
    assert outcome.escalated_extraction is None


def test_dry_run_isolates_a_per_hub_extract_error(store: Store) -> None:
    ok_hub = mint_hub(store, _claim("A single atomic claim that is fine"))
    boom_hub = mint_hub(store, _claim("BOOM this one blows up in extract_fn"))

    def _flaky(text: str) -> ClaimExtraction:
        if "BOOM" in text:
            raise RuntimeError("dispatch exploded")
        return _extraction_for(text)

    report = dry_run(store, limit=100, extract_fn=_flaky)
    by_id = {o.hub.ref_id: o for o in report.outcomes}
    assert by_id[boom_hub].verdict == "error"
    assert by_id[boom_hub].error == "dispatch exploded"
    assert by_id[ok_hub].verdict == "pass-through"


def test_dry_run_aborts_after_consecutive_infra_failures(store: Store) -> None:
    """An ``extract_fn`` that always raises (infra dead, e.g. ECONNREFUSED)
    must abort the whole run rather than silently reporting every hub as
    an error/no-claim — the melchior-incident guard. Only a handful of
    hubs should have been evaluated before the abort."""
    for i in range(8):
        mint_hub(store, _claim(f"Always-fails claim number {i} for breaker test"))

    calls: list[str] = []

    def _always_fails(text: str) -> ClaimExtraction:
        calls.append(text)
        raise RuntimeError("dispatch exploded: ECONNREFUSED")

    with pytest.raises(RuntimeError, match="ECONNREFUSED"):
        dry_run(store, limit=100, extract_fn=_always_fails)

    # 3-failure breaker: aborts on the 3rd consecutive error, not partway
    # through the full 8-hub pool.
    assert len(calls) <= 5


def test_dry_run_breaker_resets_on_a_non_error_outcome(store: Store) -> None:
    """A flaky ``extract_fn`` that fails twice then recovers must NOT trip
    the breaker — the counter resets on any non-error outcome, so sporadic
    infra blips stay isolated per-hub."""
    for i in range(6):
        mint_hub(store, _claim(f"Flaky claim number {i} for breaker reset test"))

    state = {"calls": 0}

    def _fails_twice_then_succeeds(text: str) -> ClaimExtraction:
        state["calls"] += 1
        if state["calls"] in (1, 2):
            raise RuntimeError("transient dispatch failure")
        return _extraction_for(text)

    report = dry_run(store, limit=100, extract_fn=_fails_twice_then_succeeds)
    counts = report.counts
    assert counts.get("error", 0) == 2
    assert len(report.outcomes) == 6


def test_dry_run_performs_no_store_writes(store: Store) -> None:
    mint_hub(store, _claim("SPLIT another bundled claim and more; also, x, y"))
    mint_hub(store, _claim("A stable atomic claim"))

    before = _table_counts(store)
    dry_run(store, limit=100, controls=5, extract_fn=_fake_extract)
    after = _table_counts(store)

    assert before == after


# ── render_report — smoke test ──────────────────────────────────────────


def test_render_report_is_readable_markdown(store: Store) -> None:
    mint_hub(store, _claim("SPLIT rendering claim and other; also, a, b"))
    mint_hub(store, _claim("A plain rendering claim"))
    mint_hub(store, _claim("NOCLAIM rendering nothing groundable here"))

    report = dry_run(store, limit=100, extract_fn=_fake_extract)
    rendered = render_report(report)

    assert isinstance(rendered, str)
    assert "# Taproot migration dry-run report" in rendered
    assert "| Verdict | Count |" in rendered
    for outcome in report.outcomes:
        assert f"fi{outcome.hub.ref_id}" in rendered
        assert outcome.verdict.upper() in rendered
    # Split hub renders its atoms + compound + not-claims.
    split_outcomes = [o for o in report.outcomes if o.verdict == "split"]
    assert split_outcomes
    for o in split_outcomes:
        assert o.extraction is not None
        for atom in o.extraction.atoms:
            assert atom.sentence in rendered
        assert o.extraction.compound is not None
        assert o.extraction.compound.sentence in rendered


def test_render_report_empty_report_still_renders(store: Store) -> None:
    empty_report = DryRunReport(outcomes=[], requested_limit=0, cohort_filter=None)
    rendered = render_report(empty_report)
    assert "Evaluated**: 0 hub(s)" in rendered


def test_render_report_shows_junk_candidate_count_and_marker(store: Store) -> None:
    mint_hub(store, _claim("NOCLAIM rendering nothing groundable here"))

    report = dry_run(store, limit=100, extract_fn=_fake_extract)
    rendered = render_report(report)

    assert "Junk candidates" in rendered
    assert "JUNK CANDIDATE" in rendered
    assert "| lossy |" in rendered
    assert "| nested |" in rendered


# ── dump_outcomes_jsonl — persistence (P0-4) ─────────────────────────────


def test_dump_outcomes_jsonl_round_trips_every_outcome(store: Store) -> None:
    mint_hub(store, _claim("SPLIT rendering claim and other; also, a, b"))
    mint_hub(store, _claim("A plain rendering claim"))
    mint_hub(store, _claim("NOCLAIM rendering nothing groundable here"))

    report = dry_run(store, limit=100, extract_fn=_fake_extract)
    dumped = dump_outcomes_jsonl(report)
    lines = dumped.splitlines()
    assert len(lines) == len(report.outcomes)

    records = [json.loads(line) for line in lines]
    by_hub = {r["hub"]: r for r in records}
    for outcome in report.outcomes:
        record = by_hub[outcome.hub.ref_id]
        assert record["sentence"] == outcome.claim_sentence
        assert record["verdict"] == outcome.verdict
        assert record["junk_candidate"] == outcome.junk_candidate
        assert record["control"] == outcome.is_control
        if outcome.extraction is not None:
            assert record["extraction"] is not None
            assert len(record["extraction"]["atoms"]) == len(outcome.extraction.atoms)


def test_dump_outcomes_jsonl_empty_report_is_empty_string() -> None:
    empty_report = DryRunReport(outcomes=[], requested_limit=0, cohort_filter=None)
    assert dump_outcomes_jsonl(empty_report) == ""
