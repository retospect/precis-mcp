"""The LLM batch reword sweep (`src/precis/taproot/reword.py`).

DB-backed (real `refs`/`chunks`/`links`/`nanopub_publish` via the `store`
fixture) but never networked: every test injects `propose_fn` (or
monkeypatches `propose_reword`), so the MEDIUM reword call is a local
stub while the post-validation belt — blocking lints, numeric survival,
citation ban, length budget — runs for real. The load-bearing assertions
are (a) dry-run writes NOTHING, and (b) apply goes through
`refine_claim_sentence`, so `refs.title` and the `finding_body` chunk
change together.
"""

from __future__ import annotations

import json
from typing import Any

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.taproot.reword import (
    _ungrounded_modes,
    _ungrounded_quantities,
    propose_reword,
    run_reword_sweep,
    select_reword_cohort,
)
from tests.workers._helpers import seed_chunk, seed_ref

#: Fails exactly the two dominant blocking codes (no-evidence-verb +
#: no-epistemic-mode) and nothing else — the audit's modal cohort member.
_FAILING = "Graphene has a tensile strength of 130 GPa."
#: A second, different failing claim (mint_hub converges identical
#: sentences onto one hub, so multi-hub tests need distinct claims).
_FAILING_2 = "Silicon carbide sublimes at 2700 K."
#: Passes every blocking lint: evidence verb ("show"), epistemic mode
#: ("Nanoindentation" / "measurements"), one assertion, terminal period.
_ADMISSIBLE = (
    "Nanoindentation measurements show graphene has a tensile strength of 130 GPa."
)
#: Lint-clean but drops the original's 130 GPa — the numeric belt's case.
_NUM_DROPPED = (
    "Nanoindentation measurements show graphene has a very high tensile strength."
)
#: Keeps 130 GPa but still fails the evidence-verb/mode pair.
_STILL_LINTING = "Graphene possesses a tensile strength of 130 GPa."


def _mint(store: Any, sentence: str, **kwargs: Any) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}), **kwargs)


def _stub(new_sentence: str) -> Any:
    def fn(sentence: str, scope: dict[str, Any], codes: Any) -> dict[str, Any] | None:
        return {"verdict": "reword", "sentence": new_sentence, "reason": "test"}

    return fn


def _never(sentence: str, scope: dict[str, Any], codes: Any) -> dict[str, Any] | None:
    raise AssertionError("the LLM must not be called for an excluded hub")


def _hub_state(store: Any, hub: int) -> tuple[str, str | None]:
    """(refs.title, live finding_body text) — both retitle-door surfaces."""
    with store.pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
        body = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 "
            "AND chunk_kind = 'finding_body' AND retired_at IS NULL",
            (hub,),
        ).fetchone()
    assert title is not None
    return str(title[0]), (str(body[0]) if body else None)


# ── cohort ───────────────────────────────────────────────────────────────


def test_cohort_selects_lint_failing_hubs_only(store: Any) -> None:
    failing = _mint(store, _FAILING)
    _mint(store, _ADMISSIBLE)  # lint-clean — must not enter

    cohort = select_reword_cohort(store)

    assert [c.hub_ref_id for c in cohort] == [failing]
    assert cohort[0].sentence == _FAILING
    assert "no-evidence-verb" in cohort[0].lint_codes
    assert "no-epistemic-mode" in cohort[0].lint_codes


def test_cohort_excludes_hypothesis_hubs(store: Any) -> None:
    # Same marker the widening pass reads (refs.meta.artifact_type) — a
    # conjecture must not be reworded into naming a method that never ran.
    _mint(store, _FAILING, extra_meta={"artifact_type": "hypothesis"})

    assert select_reword_cohort(store) == []
    summary = run_reword_sweep(store, propose_fn=_never)
    assert summary["cohort"] == 0
    assert summary["processed"] == 0


def test_cohort_excludes_disputed_hubs(store: Any) -> None:
    hub = _mint(store, _FAILING)
    paper = seed_ref(store, title="Dissenting paper", kind="paper")
    store.add_link(src_ref_id=paper, dst_ref_id=hub, relation="contradicts")

    assert select_reword_cohort(store) == []
    summary = run_reword_sweep(store, propose_fn=_never)
    assert summary["processed"] == 0


def test_cohort_excludes_publish_rows_past_candidate(store: Any) -> None:
    hub = _mint(store, _FAILING)
    row = store.nanopub_create_publish_row(hub)
    # A still-candidate row is pre-review: reword stays allowed.
    assert [c.hub_ref_id for c in select_reword_cohort(store)] == [hub]

    assert store.nanopub_transition(row.id, to_state="reviewed", expect=("candidate",))
    # Past candidate the claim sha is frozen — reword would be a re-review.
    assert select_reword_cohort(store) == []


def test_cohort_excludes_rejected_memo_hubs(store: Any) -> None:
    hub = _mint(store, _FAILING)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (json.dumps({"taproot_rejected": {"r123": "not supported"}}), hub),
        )
        conn.commit()
    assert select_reword_cohort(store) == []

    # An emptied memo (a sha-reopen's leftover {}) is falsy — exactly the
    # mint gate's truthiness — so the hub stays rewordable.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (json.dumps({"taproot_rejected": {}}), hub),
        )
        conn.commit()
    assert [c.hub_ref_id for c in select_reword_cohort(store)] == [hub]


def test_cohort_hub_and_limit(store: Any) -> None:
    first = _mint(store, _FAILING)
    second = _mint(store, _FAILING_2)

    assert [c.hub_ref_id for c in select_reword_cohort(store)] == [first, second]
    assert [c.hub_ref_id for c in select_reword_cohort(store, limit=1)] == [first]
    assert [c.hub_ref_id for c in select_reword_cohort(store, hub=second)] == [second]


# ── dry run ──────────────────────────────────────────────────────────────


def test_dry_run_reports_proposal_and_writes_nothing(
    store: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)
    monkeypatch.setattr("precis.taproot.reword.propose_reword", _stub(_ADMISSIBLE))
    out = tmp_path / "reword.jsonl"

    summary = run_reword_sweep(store, out=out)

    assert summary["apply"] is False
    assert summary["cohort"] == 1
    assert summary["applied"] == 0
    assert summary["counts"] == {"reworded": 1}
    assert summary["out"] == str(out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["hub"] == hub
    assert rows[0]["old"] == _FAILING
    assert rows[0]["new"] == _ADMISSIBLE
    assert rows[0]["status"] == "reworded"
    assert rows[0]["applied"] is False
    # The whole point of a dry run: the hub is untouched.
    assert _hub_state(store, hub) == before


# ── apply ────────────────────────────────────────────────────────────────


def test_apply_rewords_through_the_retitle_door(store: Any) -> None:
    hub = _mint(store, _FAILING)

    summary = run_reword_sweep(store, apply=True, propose_fn=_stub(_ADMISSIBLE))

    assert summary["counts"] == {"reworded": 1}
    assert summary["applied"] == 1
    title, body = _hub_state(store, hub)
    # refine_claim_sentence keeps title + finding_body in sync — asserting
    # both proves the write went through the retitle door, not a bare
    # UPDATE refs.
    assert title == _ADMISSIBLE
    assert body == _ADMISSIBLE
    # The reworded sentence now lint-clean: a re-run finds an empty cohort.
    assert select_reword_cohort(store) == []


# ── post-validation belt ─────────────────────────────────────────────────


def test_reword_dropping_a_numeric_token_is_rejected(store: Any) -> None:
    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)

    summary = run_reword_sweep(store, apply=True, propose_fn=_stub(_NUM_DROPPED))

    assert summary["counts"] == {"rejected": 1}
    assert summary["applied"] == 0
    assert _hub_state(store, hub) == before


def test_numeric_rejection_names_the_dropped_token(store: Any, tmp_path: Any) -> None:
    _mint(store, _FAILING)
    out = tmp_path / "r.jsonl"
    run_reword_sweep(store, out=out, propose_fn=_stub(_NUM_DROPPED))
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["status"] == "rejected"
    assert "numeric" in row["checks_failed"]
    assert "130" in row["reason"]


def test_still_linting_reword_is_rejected(store: Any, tmp_path: Any) -> None:
    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)
    out = tmp_path / "r.jsonl"

    summary = run_reword_sweep(
        store, apply=True, out=out, propose_fn=_stub(_STILL_LINTING)
    )

    assert summary["counts"] == {"rejected": 1}
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["checks_failed"] == ["lint"]
    assert "no-evidence-verb" in row["reason"]
    assert _hub_state(store, hub) == before


def test_reword_introducing_a_citation_is_rejected(store: Any, tmp_path: Any) -> None:
    _mint(store, _FAILING)
    out = tmp_path / "r.jsonl"
    cited = _ADMISSIBLE[:-1] + " [12]."

    summary = run_reword_sweep(store, apply=True, out=out, propose_fn=_stub(cited))

    assert summary["counts"] == {"rejected": 1}
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["checks_failed"] == ["citation"]
    assert "[12]" in row["reason"]


def test_over_long_reword_is_rejected(store: Any, tmp_path: Any) -> None:
    _mint(store, _FAILING)
    out = tmp_path / "r.jsonl"
    padded = _ADMISSIBLE[:-1] + (
        " under ambient laboratory conditions across a broad set of independently"
        " prepared graphene specimens measured with the same calibrated instrument"
        " in the same laboratory over several months of continuous testing."
    )
    assert len(padded) > 250

    summary = run_reword_sweep(store, apply=True, out=out, propose_fn=_stub(padded))

    assert summary["counts"] == {"rejected": 1}
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # The explicit budget check AND the over-long lint code both fire —
    # the belt survives a future re-scoping of the blocking set.
    assert "over-long" in row["checks_failed"]
    assert "lint" in row["checks_failed"]


# ── grounding (proposal vs the hub's own pinned passages) ───────────


def _with_passage(store: Any, sentence: str, passage: str) -> int:
    """A cohort hub carrying ONE live pinned evidence passage."""
    hub = _mint(store, sentence)
    paper = seed_ref(store, title="source", kind="paper")
    seed_chunk(store, ref_id=paper, text=passage, ord=0)
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        src_pos=0,
        meta={},
    )
    return hub


def test_quantity_absent_from_the_passage_is_rejected(
    store: Any, tmp_path: Any
) -> None:
    """The motivating defect: the sentence carries a number its own paper
    never states, and the old-to-new preservation check protects it."""
    hub = _with_passage(
        store, _FAILING, "We measured a tensile strength of 120 GPa for graphene."
    )
    before = _hub_state(store, hub)
    out = tmp_path / "r.jsonl"

    summary = run_reword_sweep(
        store, apply=True, out=out, propose_fn=_stub(_ADMISSIBLE)
    )

    assert summary["counts"] == {"rejected": 1}
    assert summary["applied"] == 0
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["checks_failed"] == ["numeric-grounding"]
    assert "130" in row["reason"]
    assert _hub_state(store, hub) == before


def test_grounded_reword_applies(store: Any) -> None:
    hub = _with_passage(
        store, _FAILING, "We measured a tensile strength of 130 GPa for graphene."
    )

    summary = run_reword_sweep(store, apply=True, propose_fn=_stub(_ADMISSIBLE))

    assert summary["counts"] == {"reworded": 1}
    assert summary["applied"] == 1
    assert _hub_state(store, hub)[0] == _ADMISSIBLE


def test_hub_with_no_pinned_passage_warns_but_never_blocks(
    store: Any, tmp_path: Any
) -> None:
    """Nothing to check against is reported, not silently called grounded."""
    _mint(store, _FAILING)
    out = tmp_path / "r.jsonl"

    summary = run_reword_sweep(
        store, apply=True, out=out, propose_fn=_stub(_ADMISSIBLE)
    )

    assert summary["counts"] == {"reworded": 1}
    assert summary["warned"] == 1
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["checks_failed"] == []
    assert row["warnings"] == ["no pinned evidence: grounding unchecked"]


def test_nomenclature_digits_are_not_quantities(store: Any) -> None:
    """``6-311G``/``C60`` are names, not measurements -- demanding the
    passage restate them would re-run the hyphen-numeric-range treadmill."""
    hub = _with_passage(
        store,
        "The 6-311G basis set describes C60 adsorption on graphene.",
        "Density functional theory calculations model fullerene binding"
        " on graphene with a Pople basis set.",
    )
    grounded = (
        "Density functional theory calculations with the 6-311G basis set"
        " show C60 binds to graphene."
    )

    summary = run_reword_sweep(store, apply=True, propose_fn=_stub(grounded))

    assert summary["counts"] == {"reworded": 1}
    assert summary["warned"] == 0
    assert _hub_state(store, hub)[0] == grounded


def test_unseen_epistemic_mode_warns_but_still_applies(
    store: Any, tmp_path: Any
) -> None:
    """Mode grounding is advisory: the lint DEMANDS a method, so a model
    that supplies one the passage never names is reported, not blocked."""
    hub = _with_passage(
        store, _FAILING, "We measured a tensile strength of 130 GPa for graphene."
    )
    out = tmp_path / "r.jsonl"

    summary = run_reword_sweep(
        store, apply=True, out=out, propose_fn=_stub(_ADMISSIBLE)
    )

    assert summary["counts"] == {"reworded": 1}
    assert summary["warned"] == 1
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["checks_failed"] == []
    (warning,) = row["warnings"]
    # "measurements" stems onto the passage's "measured"; "Nanoindentation"
    # appears nowhere -- only the latter is reported.
    assert warning.startswith("epistemic mode absent from every pinned passage")
    assert "Nanoindentation" in warning
    assert "measurements" not in warning
    assert _hub_state(store, hub)[0] == _ADMISSIBLE


def test_the_motivating_defect_a_number_from_another_paper() -> None:
    """fi189543: the sentence said 19 degrees; its own paper says 20."""
    assert _ungrounded_quantities(
        "Euler's-rule analysis identifies an opening angle of approximately 19°.",
        ["The cone with five pentagons has an opening angle of about 20°."],
    ) == ["19"]


def test_scientific_notation_mantissa_is_not_a_quantity() -> None:
    """``10¹¹`` grounds on a passage that writes the value out -- the bare
    ten is notation, not a measurement."""
    assert (
        _ungrounded_quantities(
            "Crossbar measurements show a density of 10¹¹ bits/cm².",
            ["The circuit reaches 100,000,000,000 bits per square centimetre."],
        )
        == []
    )


def test_trailing_fractional_zeros_ground_each_other() -> None:
    assert (
        _ungrounded_quantities(
            "Nanoindentation measurements show a Zener ratio of 0.7.",
            ["The measured Zener ratio is 0.70 for this framework."],
        )
        == []
    )


def test_a_decimal_grounds_on_a_longer_reading() -> None:
    """Rounding a reported value is not a fabricated number."""
    assert (
        _ungrounded_quantities(
            "Measurements show a Zener ratio reaching 1.3 in rigid MOFs.",
            ["We obtain 1.33 for this framework."],
        )
        == []
    )


def test_an_integer_never_grounds_on_a_longer_run() -> None:
    """No prefix tolerance for integers -- 19 is not evidence of 1900, and
    the motivating defect is an integer."""
    assert _ungrounded_quantities(
        "Measurements show a cone opening angle of 19°.",
        ["The cone opening angle is 1900 mrad."],
    ) == ["19"]


def test_mode_stem_grounds_an_inflection() -> None:
    passage = "A cone wall model reproduces the observed opening angles."
    assert _ungrounded_modes("Cone-wall modelling shows the angles.", [passage]) == []


def test_a_specific_technique_grounds_a_generic_head() -> None:
    """ "calculations" against a passage saying "the SCC-DFTB algorithm" is
    not a gap -- the head noun is superordinate to the technique."""
    assert (
        _ungrounded_modes(
            "Calculations show the binding is weak.",
            ["In the present work we use the SCC-DFTB algorithm."],
        )
        == []
    )


def test_a_generic_head_never_grounds_a_specific_technique() -> None:
    """Not the reverse: *which* method is the part a reader must trust."""
    assert _ungrounded_modes(
        "Molecular dynamics simulations show six configurations.",
        ["Our simulations show six configurations."],
    ) == ["Molecular dynamics"]


def test_mode_grounding_matches_a_short_acronym_whole() -> None:
    assert _ungrounded_modes("DFT predicts the gap.", ["DFT gives 1.2 eV."]) == []
    assert _ungrounded_modes("DFT predicts the gap.", ["We measured the gap."]) == [
        "DFT"
    ]


# ── model verdicts ───────────────────────────────────────────────────────


def test_no_reword_verdict_is_reported_not_written(store: Any, tmp_path: Any) -> None:
    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)
    out = tmp_path / "r.jsonl"

    def declines(
        sentence: str, scope: dict[str, Any], codes: Any
    ) -> dict[str, Any] | None:
        return {"verdict": "no-reword", "reason": "definition, not a claim"}

    summary = run_reword_sweep(store, apply=True, out=out, propose_fn=declines)

    assert summary["counts"] == {"no-reword": 1}
    assert summary["applied"] == 0
    (row,) = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert row["status"] == "no-reword"
    assert row["reason"] == "definition, not a claim"
    assert row["new"] is None
    assert _hub_state(store, hub) == before


def test_llm_failure_is_a_status_not_a_crash(store: Any) -> None:
    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)

    def down(sentence: str, scope: dict[str, Any], codes: Any) -> dict[str, Any] | None:
        return None

    summary = run_reword_sweep(store, apply=True, propose_fn=down)

    assert summary["counts"] == {"llm-failed": 1}
    assert _hub_state(store, hub) == before


# ── limit ────────────────────────────────────────────────────────────────


def test_limit_caps_the_sweep(store: Any) -> None:
    first = _mint(store, _FAILING)
    _mint(store, _FAILING_2)
    calls: list[str] = []

    def counting(
        sentence: str, scope: dict[str, Any], codes: Any
    ) -> dict[str, Any] | None:
        calls.append(sentence)
        return {"verdict": "reword", "sentence": _ADMISSIBLE, "reason": "t"}

    summary = run_reword_sweep(store, apply=True, limit=1, propose_fn=counting)

    assert summary["cohort"] == 1
    assert summary["processed"] == 1
    assert calls == [_FAILING]
    assert _hub_state(store, first)[0] == _ADMISSIBLE
    # The second hub was never touched.
    assert [c.sentence for c in select_reword_cohort(store)] == [_FAILING_2]


# ── prompt/default-seam sanity ───────────────────────────────────────────


def test_default_propose_fn_is_the_llm_hook() -> None:
    # The sweep's default seam is the module-level MEDIUM-tier hook (the
    # monkeypatch target); a rename would silently orphan CLI wiring.
    assert callable(propose_reword)


# ── `precis taproot reword-sweep` ────────────────────────────────────────


def _cli_args(**overrides: Any) -> Any:
    import argparse

    from tests.conftest import _active_dsn

    base: dict[str, Any] = {
        "taproot_cmd": "reword-sweep",
        "dry_run": False,
        "apply": False,
        "hub": None,
        "limit": None,
        "out": None,
        "database_url": _active_dsn(),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_help_registers_the_subcommand(capsys: Any) -> None:
    import pytest

    from precis.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["taproot", "reword-sweep", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--apply", "--hub", "--limit", "--out"):
        assert flag in out
    # --dry-run/--apply are mutually exclusive -- argparse refuses both.
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["taproot", "reword-sweep", "--dry-run", "--apply"])
    assert exc.value.code == 2


def test_cli_dry_run_is_the_default_and_writes_nothing(
    store: Any, monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    hub = _mint(store, _FAILING)
    before = _hub_state(store, hub)
    monkeypatch.setattr("precis.taproot.reword.propose_reword", _stub(_ADMISSIBLE))
    out = tmp_path / "proposal.jsonl"

    taproot_cli.run(_cli_args(out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["hub"] == hub
    assert rows[0]["old"] == _FAILING
    assert rows[0]["new"] == _ADMISSIBLE
    assert rows[0]["status"] == "reworded"
    assert rows[0]["applied"] is False
    err = capsys.readouterr().err
    assert "DRY-RUN" in err
    assert "reworded=1" in err
    # Nothing written -- the hub is untouched.
    assert _hub_state(store, hub) == before


def test_cli_apply_retitles_through_the_retitle_door(
    store: Any, monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    hub = _mint(store, _FAILING)
    monkeypatch.setattr("precis.taproot.reword.propose_reword", _stub(_ADMISSIBLE))
    out = tmp_path / "applied.jsonl"

    taproot_cli.run(_cli_args(apply=True, out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["applied"] is True
    title, body = _hub_state(store, hub)
    # refs.title AND the finding_body chunk changed together -- the write
    # went through refine_claim_sentence, not a bare UPDATE refs.
    assert title == _ADMISSIBLE
    assert body == _ADMISSIBLE
    err = capsys.readouterr().err
    assert "DRY-RUN" not in err
    assert "applied=1" in err


def test_cli_bad_hub_handle_exits_nonzero(store: Any) -> None:
    # A paper is not a claim hub -- resolve_hub_ref_id refuses it.
    import pytest

    from precis.cli import taproot as taproot_cli
    from precis.utils import handle_registry

    paper = seed_ref(store, title="not a hub", kind="paper")
    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_args(hub=handle_registry.format_handle("paper", paper)))
    assert exc.value.code == 1
