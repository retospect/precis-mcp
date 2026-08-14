"""``precis.taproot.migrate`` — the Phase 0 (score) + Phase 1 (dry-run)
migration runner for pre-existing (pre-decomposition) claim hubs
(docs/backlog/taproot-atomic-claims.md §Strategy).

Two layers, mirroring ``tests/test_taproot_backfill.py``'s split:

* Pure scoring (``score_title`` / ``cohort_for_score``) — no DB, no model.
* DB-backed ``score_hubs`` / ``dry_run`` over real ``refs``/``chunks``/
  ``links`` via the ``store`` fixture, hubs seeded through the real write
  door (``mint_hub`` / ``link_claims``) so the compound/stamp exclusions
  exercise the actual predicate. ``dry_run``'s ``extract_fn`` is always
  injected (a deterministic fake) — no live LLM call anywhere in this file.
"""

from __future__ import annotations

from precis.store.store import Store
from precis.taproot.canon import CanonicalClaim, ClaimExtraction, NotClaim
from precis.taproot.hub import link_claims, mint_hub
from precis.taproot.migrate import (
    COHORTS,
    DryRunReport,
    cohort_for_score,
    dry_run,
    render_report,
    score_hubs,
    score_title,
)

# ── score_title / cohort_for_score — pure, no DB ────────────────────────


def test_score_title_no_signals_is_zero() -> None:
    score, signals = score_title("Pd/C catalyzes Suzuki coupling at RT")
    assert score == 0
    assert signals == ()


def test_score_title_conjunction_and_scores() -> None:
    score, signals = score_title("Graphene has high strength and high conductivity")
    assert "conjunction" in signals
    assert score >= 2


def test_score_title_word_boundary_does_not_match_band() -> None:
    """ "band" contains the substring "and" but must never trip the
    conjunction heuristic — the word-boundary regex is the whole point."""
    score, signals = score_title("Optical absorption band shifts with strain")
    assert "conjunction" not in signals
    assert score == 0


def test_score_title_but_and_while_both_trigger() -> None:
    _, signals_but = score_title("It is fast but not accurate")
    _, signals_while = score_title("It degrades while heating")
    assert "conjunction" in signals_but
    assert "conjunction" in signals_while


def test_score_title_long_title_scores() -> None:
    long_title = "A" * 161
    score, signals = score_title(long_title)
    assert "long-title" in signals
    assert score >= 2


def test_score_title_short_title_no_length_signal() -> None:
    _, signals = score_title("A" * 160)
    assert "long-title" not in signals


def test_score_title_semicolon_scores() -> None:
    score, signals = score_title("X increases yield; Y decreases side products")
    assert "semicolon" in signals
    assert score >= 2


def test_score_title_multi_comma_scores() -> None:
    score, signals = score_title("X, Y, and Z were measured")
    assert "multi-comma" in signals
    assert score >= 1


def test_score_title_single_comma_no_signal() -> None:
    _, signals = score_title("X, Y were measured")
    assert "multi-comma" not in signals


def test_score_title_combined_signals_sum() -> None:
    score, signals = score_title(
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


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


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
    into two atoms + a compound; "NOCLAIM" yields the empty extraction;
    anything else is treated as already-atomic (pass-through)."""
    if "NOCLAIM" in title:
        return ClaimExtraction(atoms=(), compound=None, not_claims=())
    if "SPLIT" in title:
        not_claims: tuple[NotClaim, ...] = (
            NotClaim(text="a vague forward-looking bit", reason="forward-looking"),
        )
        return ClaimExtraction(
            atoms=(_claim(f"{title} atom A"), _claim(f"{title} atom B")),
            compound=_claim(title),
            not_claims=not_claims,
        )
    return ClaimExtraction(atoms=(_claim(title),), compound=None, not_claims=())


def _fake_extract(chunk_text: str) -> ClaimExtraction:
    return _extraction_for(chunk_text)


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


def test_dry_run_samples_controls_from_atomic_tail(store: Store) -> None:
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
