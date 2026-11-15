"""The conditions-repair sweep (`src/precis/taproot/conditions_repair.py`).

DB-backed (the `store` fixture), never networked: every test injects
`propose_fn`, so the MEDIUM call is a local stub while the code
post-validation (quote in paper, insertion-only, progress, lint) runs for
real.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.taproot.canon import CanonicalClaim
from precis.taproot.conditions import check_conditions
from precis.taproot.conditions_repair import (
    ConditionsCandidate,
    run_conditions_sweep,
    select_conditions_cohort,
)
from precis.taproot.hub import HubFrozenError, mint_hub
from tests.workers._helpers import seed_chunk, seed_ref

OLD = (
    "Electrolysis measurements show a Faradaic efficiency of 90% for NH3 "
    "production on Cu."
)
NEW = (
    "Electrolysis measurements show a Faradaic efficiency of 90% for NH3 "
    "production on Cu at -0.5 V vs RHE in 0.5 M K2SO4."
)
METHODS = "Electrolysis was run at −0.5 V vs RHE in 0.5 M K₂SO₄ for 2 h in an H-cell."
QUOTE = "−0.5 V vs RHE in 0.5 M K₂SO₄"
GROUNDING = "The Faradaic efficiency of NH3 on Cu reached 90%."


def _hub(store: Any, sentence: str = OLD, *, methods: str | None = METHODS) -> int:
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    paper = seed_ref(store, title="source", kind="paper")
    seed_chunk(store, ref_id=paper, text=GROUNDING, ord=0)
    if methods is not None:
        cid = seed_chunk(store, ref_id=paper, text=methods, ord=3)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET section_path = %s WHERE chunk_id = %s",
                (["Experimental"], cid),
            )
            conn.commit()
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        src_pos=0,
        meta={},
    )
    return hub


def _title(store: Any, hub: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _add(sentence: str = NEW, quote: str = QUOTE, text: str = "-0.5 V vs RHE") -> Any:
    def fn(cand: Any, paper_text: str) -> dict[str, Any]:
        return {
            "verdict": "add",
            "sentence": sentence,
            "added": [
                {"condition": "potential", "text": text, "quote": quote},
                {"condition": "electrolyte", "text": "0.5 M K2SO4", "quote": QUOTE},
            ],
            "reason": "stated in Methods",
        }

    return fn


def _rows(path: Any) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_precondition_old_has_gaps_new_does_not() -> None:
    (gap,) = check_conditions(OLD)
    assert gap.family == "ec-measurement"
    assert set(gap.missing) == {"potential", "electrolyte"}
    assert check_conditions(NEW) == []


def test_cohort_selects_gap_hubs_with_evidence_and_family_filter(store: Any) -> None:
    hub = _hub(store)
    mint_hub(
        store,
        CanonicalClaim(
            sentence="Graphene has a tensile strength of 130 GPa.", scope={}
        ),
    )
    (cand,) = select_conditions_cohort(store)
    assert cand.hub_ref_id == hub
    assert cand.gap.family == "ec-measurement"
    assert len(cand.evidence) == 1
    assert select_conditions_cohort(store, family="photo-rate") == []


def test_add_verdict_applies_through_the_retitle_door(
    store: Any, tmp_path: Any
) -> None:
    hub = _hub(store)
    out = tmp_path / "o.jsonl"
    summary = run_conditions_sweep(store, apply=True, out=out, propose_fn=_add())
    assert summary["counts"] == {"added": 1}
    assert summary["applied"] == 1
    assert summary["llm_calls"] == 1
    assert _title(store, hub) == NEW
    assert check_conditions(_title(store, hub)) == []
    (row,) = _rows(out)
    assert row["old"] == OLD and row["new"] == NEW
    assert row["applied"] is True and row["verdict"] == "add"
    assert row["added"][0]["quote"] == QUOTE
    assert "needs_review" in row


def test_dry_run_writes_nothing(store: Any, tmp_path: Any) -> None:
    hub = _hub(store)
    out = tmp_path / "o.jsonl"
    summary = run_conditions_sweep(store, out=out, propose_fn=_add())
    assert summary["apply"] is False and summary["applied"] == 0
    assert _title(store, hub) == OLD
    (row,) = _rows(out)
    assert row["status"] == "added" and row["applied"] is False, row
    assert row["new"] == NEW


def test_quote_not_in_paper_is_rejected(store: Any, tmp_path: Any) -> None:
    hub = _hub(store)
    out = tmp_path / "o.jsonl"
    fn = _add(quote="-0.5 V vs RHE in 0.5 M K2SO4 under argon")
    summary = run_conditions_sweep(store, apply=True, out=out, propose_fn=fn)
    assert summary["counts"] == {"rejected": 1}
    assert _title(store, hub) == OLD
    (row,) = _rows(out)
    assert "quote-not-in-paper" in row["checks_failed"]


def test_quote_must_carry_the_numbers_of_the_added_text(store: Any) -> None:
    hub = _hub(store)
    bad = NEW.replace("-0.5 V", "-0.7 V")
    summary = run_conditions_sweep(
        store, apply=True, propose_fn=_add(sentence=bad, text="-0.7 V vs RHE")
    )
    assert summary["counts"] == {"rejected": 1}
    assert _title(store, hub) == OLD


def test_rewriting_other_words_is_rejected(store: Any, tmp_path: Any) -> None:
    hub = _hub(store)
    out = tmp_path / "o.jsonl"
    rewritten = (
        "Electrolysis experiments demonstrate a Faradaic efficiency of 90% for "
        "NH3 production on Cu at -0.5 V vs RHE in 0.5 M K2SO4."
    )
    summary = run_conditions_sweep(
        store, apply=True, out=out, propose_fn=_add(sentence=rewritten)
    )
    assert summary["counts"] == {"rejected": 1}
    assert _title(store, hub) == OLD
    (row,) = _rows(out)
    assert "rewrites-old" in row["checks_failed"]


def test_unexplained_insertion_is_rejected(store: Any) -> None:
    hub = _hub(store)
    padded = NEW[:-1] + " and the best reported catalyst."
    summary = run_conditions_sweep(store, apply=True, propose_fn=_add(sentence=padded))
    assert summary["counts"] == {"rejected": 1}
    assert _title(store, hub) == OLD


def test_not_reported_adds_the_marker_in_code(store: Any, tmp_path: Any) -> None:
    # Methods text is held (so absence is evidence) but names no condition.
    hub = _hub(
        store, methods="The working electrode was Cu foil in a two-compartment cell."
    )
    out = tmp_path / "o.jsonl"

    def fn(cand: Any, paper_text: str) -> dict[str, Any]:
        # the sentence, if any, is ignored: the marker is built in code
        return {"verdict": "not_reported", "sentence": "ignored", "reason": "silent"}

    summary = run_conditions_sweep(store, apply=True, out=out, propose_fn=fn)
    assert summary["counts"] == {"not-reported": 1}
    title = _title(store, hub)
    assert title == OLD[:-1] + " (potential and electrolyte not reported)."
    assert check_conditions(title) == []
    (row,) = _rows(out)
    assert row["status"] == "not-reported" and row["added"] == []


def test_not_reported_without_methods_text_is_ambiguous(store: Any) -> None:
    """Absence proves nothing when the paper's methods are not held
    (fi348800, 2026-11-15)."""
    hub = _hub(store, methods=None)

    def fn(cand: Any, paper_text: str) -> dict[str, Any]:
        return {"verdict": "not_reported", "reason": "methods not in excerpts"}

    summary = run_conditions_sweep(store, apply=True, propose_fn=fn)
    assert summary["counts"] == {"ambiguous": 1}
    assert _title(store, hub) == OLD


def test_ambiguous_writes_nothing(store: Any, tmp_path: Any) -> None:
    hub = _hub(store)
    out = tmp_path / "o.jsonl"

    def fn(cand: Any, paper_text: str) -> dict[str, Any]:
        return {"verdict": "ambiguous", "reason": "three potentials"}

    summary = run_conditions_sweep(store, apply=True, out=out, propose_fn=fn)
    assert summary["counts"] == {"ambiguous": 1}
    assert _title(store, hub) == OLD
    assert _rows(out)[0]["reason"] == "three potentials"


def test_hub_without_evidence_is_skipped_without_an_llm_call(store: Any) -> None:
    mint_hub(store, CanonicalClaim(sentence=OLD, scope={}))

    def never(cand: Any, paper_text: str) -> None:
        raise AssertionError("no LLM call without evidence")

    summary = run_conditions_sweep(store, apply=True, propose_fn=never)
    assert summary["counts"] == {"no-evidence": 1}
    assert summary["llm_calls"] == 0


def test_frozen_hub_and_pub_id_collision_are_named_statuses(
    store: Any, monkeypatch: Any
) -> None:
    hub = _hub(store)

    def frozen(*a: Any, **k: Any) -> None:
        raise HubFrozenError("signed")

    monkeypatch.setattr(
        "precis.taproot.conditions_repair.refine_claim_sentence", frozen
    )
    summary = run_conditions_sweep(store, apply=True, propose_fn=_add())
    assert summary["counts"] == {"frozen": 1}

    def dup(*a: Any, **k: Any) -> None:
        raise ValueError("pub_id belongs to ref_id=9 - dedup/merge candidate")

    monkeypatch.setattr("precis.taproot.conditions_repair.refine_claim_sentence", dup)
    summary = run_conditions_sweep(store, apply=True, propose_fn=_add())
    assert summary["counts"] == {"possible-duplicate": 1}
    assert _title(store, hub) == OLD


def test_llm_failure_is_a_status(store: Any) -> None:
    _hub(store)
    summary = run_conditions_sweep(store, apply=True, propose_fn=lambda c, t: None)
    assert summary["counts"] == {"llm-failed": 1}


def test_cli_dry_run_default(
    store: Any, monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    import argparse

    from precis.cli import taproot as taproot_cli
    from tests.conftest import _active_dsn

    hub = _hub(store)
    monkeypatch.setattr("precis.taproot.conditions_repair.propose_conditions", _add())
    out = tmp_path / "o.jsonl"
    taproot_cli.run(
        argparse.Namespace(
            taproot_cmd="conditions-sweep",
            dry_run=False,
            apply=False,
            hub=None,
            draft=None,
            family=None,
            limit=None,
            after_ref_id=None,
            out=str(out),
            database_url=_active_dsn(),
        )
    )
    assert _title(store, hub) == OLD
    assert _rows(out)[0]["status"] == "added"
    err = capsys.readouterr().err
    assert "DRY-RUN" in err and "added=1" in err and "llm_calls=1" in err


def test_cli_help_registers_the_subcommand() -> None:
    from precis.cli import _build_parser

    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["taproot", "conditions-sweep", "--help"])
    assert exc.value.code == 0


def test_context_picks_keyword_chunks_when_section_path_is_only_the_title(
    store: Any,
) -> None:
    from precis.taproot.conditions_repair import build_context

    hub = _hub(store, methods=None)
    (cand,) = select_conditions_cohort(store, hub=hub)
    paper = cand.evidence[0][0]
    seed_chunk(store, ref_id=paper, text="Unrelated discussion of kinetics.", ord=2)
    seed_chunk(store, ref_id=paper, text=METHODS, ord=3)
    ctx = build_context(store, cand)
    assert ctx.startswith("[paper 1]\n" + GROUNDING)
    assert "K₂SO₄" in ctx
    assert "Unrelated" not in ctx
    assert len(ctx) <= 6100


def test_not_reported_with_a_keyword_hit_elsewhere_is_ambiguous(store: Any) -> None:
    hub = _hub(store, methods=None)
    (cand,) = select_conditions_cohort(store, hub=hub)
    # outside any Methods heading and outside the grounding passage
    seed_chunk(store, ref_id=cand.evidence[0][0], text="Cells held 0.1 M KOH.", ord=9)

    def fn(c: Any, t: str) -> dict[str, Any]:
        return {"verdict": "not_reported", "reason": "silent"}

    out = run_conditions_sweep(store, apply=True, propose_fn=fn)
    assert out["counts"] == {"ambiguous": 1}
    assert _title(store, hub) == OLD


def test_partial_add_carries_added_condition_plus_marker(store: Any) -> None:
    hub = _hub(store, methods="Electrolysis was run at −0.5 V vs RHE for 2 h.")
    sentence = OLD[:-1] + " at -0.5 V vs RHE."

    def fn(c: Any, t: str) -> dict[str, Any]:
        return {
            "verdict": "add",
            "sentence": sentence,
            "added": [
                {
                    "condition": "potential",
                    "text": "-0.5 V vs RHE",
                    "quote": "−0.5 V vs RHE",
                }
            ],
            "not_reported": ["electrolyte"],
            "reason": "partial",
        }

    summary = run_conditions_sweep(store, apply=True, propose_fn=fn)
    assert summary["counts"] == {"added": 1}
    title = _title(store, hub)
    assert title == OLD[:-1] + " at -0.5 V vs RHE (electrolyte not reported)."
    assert check_conditions(title) == []


def test_quoted_only_holds_marker_sentences(store: Any, tmp_path: Any) -> None:
    hub = _hub(store, methods="Electrolysis was run at −0.5 V vs RHE for 2 h.")
    out = tmp_path / "o.jsonl"

    def fn(c: Any, t: str) -> dict[str, Any]:
        return {
            "verdict": "add",
            "sentence": OLD[:-1] + " at -0.5 V vs RHE.",
            "added": [
                {
                    "condition": "potential",
                    "text": "-0.5 V vs RHE",
                    "quote": "−0.5 V vs RHE",
                }
            ],
            "not_reported": ["electrolyte"],
            "reason": "partial",
        }

    summary = run_conditions_sweep(
        store, apply=True, out=out, propose_fn=fn, quoted_only=True
    )
    assert summary["counts"] == {"marker-held": 1}
    assert summary["applied"] == 0
    assert _title(store, hub) == OLD
    (row,) = _rows(out)
    assert row["new"].endswith("(electrolyte not reported).")


def test_quoted_only_still_writes_fully_quoted_adds(store: Any) -> None:
    hub = _hub(store)
    summary = run_conditions_sweep(
        store, apply=True, propose_fn=_add(), quoted_only=True
    )
    assert summary["counts"] == {"added": 1} and summary["applied"] == 1
    assert _title(store, hub) == NEW


# ── validator holes (reviewer round) ─────────────────────────────────────

PAPER = "Runs were done at −0.5 V vs RHE in 0.5 M K₂SO₄ electrolyte under argon."
_POT = ("potential", "-0.5 V vs RHE", "−0.5 V vs RHE")
_ELY = ("electrolyte", "0.5 M K2SO4", "0.5 M K₂SO₄")


def _validate(
    old: str, new: str, items: list[tuple[str, str, str]], **kw: Any
) -> list[str]:
    from precis.taproot.conditions_repair import validate_add

    added = [{"condition": c, "text": t, "quote": q} for c, t, q in items]
    checks, _ = validate_add(
        old,
        new,
        added,
        kw.pop("paper", PAPER),
        missing=("potential", "electrolyte"),
        **kw,
    )
    return checks


def test_validator_accepts_the_clean_insertion() -> None:
    assert _validate(OLD, NEW, [_POT, _ELY]) == []


def test_reordering_old_words_is_rejected() -> None:
    old = "The catalyst reaches a Faradaic efficiency of 85% for ammonia."
    new = (
        "The ammonia reaches a Faradaic efficiency of 85% for catalyst at "
        "-0.5 V vs RHE in 0.5 M K2SO4."
    )
    assert "rewrites-old" in _validate(old, new, [_POT, _ELY])


def test_added_words_must_be_in_the_quote() -> None:
    new = OLD[:-1] + " at -0.5 V vs RHE in acidic KOH electrolyte."
    checks = _validate(
        OLD,
        new,
        [_POT, ("electrolyte", "in acidic KOH electrolyte", "electrolyte under")],
    )
    assert "quote-mismatch" in checks


def test_model_written_marker_is_rejected() -> None:
    new = OLD[:-1] + " at 0.5 V vs RHE (the catalyst is toxic not reported)."
    checks = _validate(
        OLD, new, [("potential", "0.5 V vs RHE", "0.5 V vs RHE")], paper="0.5 V vs RHE"
    )
    assert "bad-marker" in checks
    # well-formed, but the code did not put it there
    looks_ok = OLD[:-1] + " at -0.5 V vs RHE (electrolyte not reported)."
    assert "bad-marker" in _validate(OLD, looks_ok, [_POT])
    assert "bad-marker" not in _validate(
        OLD, looks_ok, [_POT], unstated=["electrolyte"]
    )


def test_sign_must_match_the_quote() -> None:
    new = OLD[:-1] + " at -0.5 V vs RHE."
    checks = _validate(
        OLD,
        new,
        [("potential", "-0.5 V vs RHE", "0.5 V vs RHE")],
        paper="Held at 0.5 V vs RHE.",
    )
    assert "quote-mismatch" in checks


def test_unit_needs_a_right_boundary_and_ignores_trailing_punctuation() -> None:
    paper = "Potential was 0.5 mV vs RHE and later −0.5 V."
    new = OLD[:-1] + " at 0.5 m."
    assert "quote-mismatch" in _validate(
        OLD, new, [("potential", "0.5 m", "0.5 mV vs RHE")], paper=paper
    )
    new2 = OLD[:-1] + " at -0.5 V."
    assert "quote-mismatch" not in _validate(
        OLD, new2, [("potential", "-0.5 V.", "−0.5 V")], paper=paper
    )


def test_conditions_report_draft_token_is_resolved_not_digit_scraped(
    store: Any,
) -> None:
    import argparse

    from precis.cli import taproot as taproot_cli
    from tests.conftest import _active_dsn

    with pytest.raises(SystemExit) as exc:
        taproot_cli._run_conditions_report(
            argparse.Namespace(
                draft="my-draft-2",
                all=False,
                json=False,
                limit=None,
                database_url=_active_dsn(),
            )
        )
    assert exc.value.code == 2


def test_title_round_trip_error_is_apply_failed(store: Any, monkeypatch: Any) -> None:
    from precis.taproot.hub import TitleRoundTripError

    hub = _hub(store)

    def boom(*a: Any, **k: Any) -> None:
        raise TitleRoundTripError("mismatch")

    monkeypatch.setattr("precis.taproot.conditions_repair.refine_claim_sentence", boom)
    summary = run_conditions_sweep(store, apply=True, propose_fn=_add())
    assert summary["counts"] == {"apply-failed": 1}
    assert _title(store, hub) == OLD


def test_proposal_parses_json_from_prose_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fallback rung that answers in a fenced block (no ``data``) still
    yields the proposal; prose with no object yields None (2026-11-15 prod
    dry run: 4 of 5 replies came back unparsed)."""
    from types import SimpleNamespace

    from precis.taproot import conditions_repair

    cand = ConditionsCandidate(
        hub_ref_id=1, sentence=OLD, scope={}, gap=check_conditions(OLD)[0]
    )
    reply = SimpleNamespace(
        error=None,
        data=None,
        text='Here you go:\n```json\n{"verdict": "ambiguous", "reason": "x"}\n```',
        stop_reason="end_turn",
    )
    monkeypatch.setattr(conditions_repair, "route", lambda req: reply)
    got = conditions_repair.propose_conditions(cand, "paper")
    assert got == {"verdict": "ambiguous", "reason": "x"}
    reply.text = "I cannot tell."
    assert conditions_repair.propose_conditions(cand, "paper") is None


def test_quote_with_markdown_and_tex_markup_validates() -> None:
    """The chunker keeps TeX/markdown markup ("$U = 0$ V *versus* RHE");
    folding must treat it as the same text (fi349041, 2026-11-15)."""
    from precis.taproot.conditions_repair import validate_add

    old = "DFT on Ru-Cu gives a H2-formation ΔG of 0.33 eV on the Ru site."
    new = (
        "DFT on Ru-Cu gives a H2-formation ΔG of 0.33 eV at U = 0 V vs RHE "
        "on the Ru site."
    )
    paper = "Free energies were computed at  $U = 0$  V *versus* RHE with CHE."
    added = [
        {
            "condition": "potential",
            "text": "at U = 0 V vs RHE",
            "quote": "at  $U = 0$  V *versus* RHE",
        }
    ]
    failed, details = validate_add(old, new, added, paper, missing=("potential",))
    assert "quote-mismatch" not in failed, details
