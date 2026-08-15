"""``precis taproot-migrate reground`` — the CLI-level per-row re-grounding
glue (:mod:`precis.cli.taproot_migrate`'s ``_reground_row``/
``_row_regrounding_failed``), tested in isolation from argparse/DB/LLM.

Covers the error-sentinel contract (pre-ship review finding #1/#3): a
raising ``verify_atoms`` call, or a malformed row that can't even be
parsed, must write ``{"grounding": {"error": "..."}}`` on THAT row and
never abort the caller's loop over the rest of the file.
"""

from __future__ import annotations

from typing import Any

from precis.cli.taproot_migrate import _reground_row, _row_regrounding_failed
from precis.taproot.reground import (
    AtomGrounding,
    GroundedRecord,
    HubGroundingResult,
)

_FAKE_STORE: Any = None


def _split_row(hub: int, atoms: list[str]) -> dict[str, Any]:
    return {
        "hub": hub,
        "score": 0,
        "cohort": "likely-compound",
        "control": False,
        "sentence": "irrelevant original sentence",
        "verdict": "split",
        "gate_meta": {},
        "extraction": {
            "atoms": [{"sentence": s, "scope": {}} for s in atoms],
            "compound": None,
            "not_claims": [],
        },
        "error": None,
        "junk_candidate": False,
        "escalated_verdict": None,
        "escalated_gate_meta": {},
        "escalated_extraction": None,
        "escalation_error": None,
    }


def test_non_split_row_passes_through_unchanged() -> None:
    row = {"hub": 1, "verdict": "pass-through"}
    out = _reground_row(_FAKE_STORE, row, top_k=6)
    assert out is row
    assert _row_regrounding_failed(out) is None


def test_split_row_grounds_and_writes_grounding_key() -> None:
    row = _split_row(1, ["X shows A.", "X shows B."])

    def fake_verify_atoms(store: Any, hub_ref_id: int, atoms: Any, **kw: Any) -> Any:
        return HubGroundingResult(
            hub_ref_id=hub_ref_id,
            paper_ref_ids=(100,),
            atoms=tuple(
                AtomGrounding(
                    atom=a,
                    records=(
                        GroundedRecord(
                            paper_ref_id=100,
                            chunk_id=1,
                            chunk_ord=0,
                            quote="q",
                            bound=None,
                        ),
                    ),
                )
                for a in atoms
            ),
        )

    out = _reground_row(_FAKE_STORE, row, top_k=6, verify_atoms_fn=fake_verify_atoms)
    assert _row_regrounding_failed(out) is None
    assert out["grounding"]["paper_ref_ids"] == [100]
    assert out["grounding"]["summary"]["grounded"] == 2
    # The original row is untouched -- _reground_row returns a NEW dict.
    assert "grounding" not in row


def test_raising_verify_atoms_writes_error_sentinel_not_pass_through() -> None:
    row = _split_row(1, ["X shows A.", "X shows B."])

    def raising_verify_atoms(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("dispatch unavailable")

    out = _reground_row(_FAKE_STORE, row, top_k=6, verify_atoms_fn=raising_verify_atoms)
    assert _row_regrounding_failed(out) == "dispatch unavailable"
    assert out["grounding"] == {"error": "dispatch unavailable"}
    # Never the same shape as "no grounding key at all" -- that reads as
    # "never regrounded, safe to place as before" to apply_migrate.
    assert "grounding" in out


def test_malformed_row_writes_error_sentinel_and_never_raises() -> None:
    """A row with an unparseable 'hub' -- e.g. a stray non-numeric string --
    must degrade to THIS row's own error sentinel, not propagate an
    exception out of _reground_row (which would abort the caller's loop
    over every other row in the file)."""
    row = {
        "hub": "not-an-int",
        "verdict": "split",
        "extraction": {"atoms": [], "compound": None, "not_claims": []},
    }
    out = _reground_row(
        _FAKE_STORE, row, top_k=6, verify_atoms_fn=lambda *a, **kw: None
    )
    error = _row_regrounding_failed(out)
    assert error is not None
    assert "not-an-int" in error or "invalid literal" in error


def test_multiple_rows_one_malformed_others_preserved() -> None:
    """The caller's loop (mirrored here directly over _reground_row, since
    _run_reground itself needs a live Store.connect) must keep every
    OTHER row's real result even when one row in the middle fails."""
    good_row_1 = _split_row(1, ["X shows A.", "X shows B."])
    bad_row = {"hub": None, "verdict": "split", "extraction": None}
    good_row_2 = _split_row(2, ["Y shows C.", "Y shows D."])

    def fake_verify_atoms(store: Any, hub_ref_id: int, atoms: Any, **kw: Any) -> Any:
        return HubGroundingResult(hub_ref_id=hub_ref_id, paper_ref_ids=(), atoms=())

    results = [
        _reground_row(_FAKE_STORE, r, top_k=6, verify_atoms_fn=fake_verify_atoms)
        for r in (good_row_1, bad_row, good_row_2)
    ]

    assert _row_regrounding_failed(results[0]) is None
    assert results[0]["hub"] == 1
    assert _row_regrounding_failed(results[1]) is not None
    assert _row_regrounding_failed(results[2]) is None
    assert results[2]["hub"] == 2
