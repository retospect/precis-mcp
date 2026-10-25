"""scripts/_neb_seed_trust_drylist.py — the gate replay on fixture rows
(gr517804): same-key pooling, the --ref sibling refetch, ready-only
pooling, key mismatches, the non-array results.trust skip, and the
flip-only-when-source count."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_neb_seed_trust_drylist.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_neb_seed_trust_drylist", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _j(value: Any) -> str:
    return "" if value is None else json.dumps(value)


def _ident(state: str, seed: int, e_ev: float) -> dict[str, Any]:
    """One ``endpoint_identity`` trust record — the material the union
    agreement replay (round-2 R1) compares across pooled seeds."""
    return {
        "check": "endpoint_identity",
        "verdict": "pass",
        "severity": "warn",
        "seed": seed,
        "state": state,
        "evidence": {"e_eV": e_ev, "fingerprint": "fp"},
        "id": f"{state}#s{seed}#endpoint_identity",
    }


def _row(
    ref_id: int,
    *,
    cand: str = "500",
    engine: str | None = "0.39.0",
    declared: list[int] | None = None,
    live: list[int] | None = None,
    all_rec: list[int] | None = None,
    trust_type: str = "array",
    tier: str = "neb",
    status: str = "ready",
    reaction_key: str | None = "rk1",
    slab: str | None = "sha-one",
    trusted: bool = False,
    fidelity: str = "neb",
    route_steps: list[str] | None = None,
    identity: list[dict[str, Any]] | None = None,
    tol: float | None = None,
) -> list[str]:
    """One CSV row in the script's 21-column ``_SELECT`` shape: ref_id,
    candidate_ref, engine, declared seeds, live (non-stale) record seeds,
    all record seeds, trust typeof, tier, screening, template,
    neb_schedule flag, status, reaction_key, config, slab sha,
    structure_refs, barrier_trusted, barrier_fidelity, route_steps,
    non-stale endpoint_identity records, stamped endpoint tol_eV."""
    return [
        str(ref_id),
        cand,
        engine or "",
        _j(declared),
        _j(live),
        _j(all_rec),
        trust_type,
        tier,
        "",
        "",
        "f",
        status,
        reaction_key or "",
        "",
        slab or "",
        "",
        "true" if trusted else "",
        fidelity,
        _j(route_steps),
        _j(identity),
        "" if tol is None else str(tol),
    ]


def _csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def test_same_key_siblings_pool_into_the_union() -> None:
    mod = _load()
    # Disjoint seeds across two same-key ready runs union to 2 AND the
    # merged endpoint_identity evidence agrees on both route states
    # (round-2 R1) → no fire.
    rows = [
        _row(
            100,
            live=[0],
            all_rec=[0],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 0, -10.0), _ident("A", 0, -10.0)],
        ),
        _row(
            101,
            live=[1],
            all_rec=[1],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 1, -10.0), _ident("A", 1, -10.0)],
        ),
    ]
    lines, fired, _flips, scanned, _skip = mod._evaluate(rows)
    assert (fired, scanned) == (0, 2) and lines == []
    # The pw512178/pw513347 shape: identical seed lists still union to 2 —
    # each run stands on ≥2 own seeds, so the engine's own agreement
    # governs and no endpoint_identity fixtures are needed.
    rows = [
        _row(100, live=[0, 1], all_rec=[0, 1], trusted=True),
        _row(101, live=[0, 1], all_rec=[0, 1], trusted=True),
    ]
    _lines, fired, *_ = mod._evaluate(rows)
    assert fired == 0


def test_a_lone_one_seed_run_fires_and_flips() -> None:
    mod = _load()
    rows = [_row(100, live=[0], all_rec=[0], trusted=True)]
    lines, fired, flips, *_ = mod._evaluate(rows)
    assert fired == 1 and flips == 1
    assert "seeds:1-independent" in lines[0]
    assert "trusted->untrusted" in lines[0]


def test_pooled_union_replays_endpoint_agreement() -> None:
    """Round-2 R1: pooled seeds must agree at the endpoints — two 1-seed
    runs with no endpoint_identity records fire union-not-run; a spread
    beyond tol fires union-disagree."""
    mod = _load()
    # no endpoint_identity anywhere → the union's agreement is unprovable
    rows = [
        _row(100, live=[0], all_rec=[0], trusted=True),
        _row(101, live=[1], all_rec=[1], trusted=True),
    ]
    lines, fired, _flips, _scanned, _skip = mod._evaluate(rows)
    assert fired == 2
    assert all("endpoint:union-not-run" in ln for ln in lines)
    # seed 1's state-A endpoint lands in a different basin → disagree
    rows = [
        _row(
            100,
            live=[0],
            all_rec=[0],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 0, -10.0), _ident("A", 0, -10.0)],
        ),
        _row(
            101,
            live=[1],
            all_rec=[1],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 1, -10.0), _ident("A", 1, -9.1)],
        ),
    ]
    lines, fired, *_ = mod._evaluate(rows)
    assert fired == 2
    assert all("endpoint:union-disagree" in ln for ln in lines)


def test_named_filter_pools_siblings_and_suppresses_unnamed_fires() -> None:
    mod = _load()
    rows = [
        _row(  # named
            100,
            live=[0],
            all_rec=[0],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 0, -10.0), _ident("A", 0, -10.0)],
        ),
        _row(  # same-key sibling context
            101,
            live=[1],
            all_rec=[1],
            route_steps=["R->A"],
            identity=[_ident("R", 1, -10.0), _ident("A", 1, -10.0)],
        ),
        _row(200, live=[0], all_rec=[0], cand="600", trusted=True),  # fires
    ]
    lines, fired, *_ = mod._evaluate(rows, named={100})
    assert fired == 1  # pw200's lone seed; pw100 pooled to {0,1}
    assert lines == []  # but pw200 isn't named → suppressed


def test_ref_mode_refetches_the_named_rows_siblings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    named_rows = [
        _row(
            101,
            live=[0],
            all_rec=[0],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 0, -10.0), _ident("A", 0, -10.0)],
        )
    ]
    seen: list[str] = []

    def fake_psql(sql: str) -> str:
        seen.append(sql)
        if "r.ref_id = ANY" in sql:
            return _csv(named_rows)
        # The candidate_ref refetch returns the sibling set, named included.
        return _csv(
            named_rows
            + [
                _row(
                    100,
                    live=[1],
                    all_rec=[1],
                    route_steps=["R->A"],
                    identity=[_ident("R", 1, -10.0), _ident("A", 1, -10.0)],
                )
            ]
        )

    monkeypatch.setattr(mod, "_psql", fake_psql)
    monkeypatch.setattr(sys, "argv", ["drylist", "--ref", "101"])
    mod.main()
    out = capsys.readouterr().out
    assert len(seen) == 2 and "candidate_ref" in seen[1]
    assert "pw101" not in out  # pooled union {0,1} → no fire
    assert "sibling context" in out and "gate fires on 0" in out


def test_a_non_ready_sibling_pools_nothing() -> None:
    mod = _load()
    rows = [
        _row(
            100,
            live=[0],
            all_rec=[0],
            trusted=True,
            route_steps=["R->A"],
            identity=[_ident("R", 0, -10.0), _ident("A", 0, -10.0)],
        ),
        _row(
            101,
            live=[1],
            all_rec=[1],
            status="running",
            route_steps=["R->A"],
            identity=[_ident("R", 1, -10.0), _ident("A", 1, -10.0)],
        ),
    ]
    lines, fired, *_ = mod._evaluate(rows)
    # 101 contributes nothing (not ready), so 100 fires on its lone {0};
    # 101 itself reads the pool and sees {0,1} → does not fire.
    assert fired == 1
    assert len(lines) == 1 and "pw100" in lines[0]


def test_a_different_reaction_key_or_geometry_pools_nothing() -> None:
    mod = _load()
    rows = [
        _row(100, live=[0], all_rec=[0], trusted=True),
        _row(101, live=[1], all_rec=[1], reaction_key="rk2"),
        _row(102, live=[1], all_rec=[1], slab="sha-two"),
    ]
    lines, fired, *_ = mod._evaluate(rows)
    assert fired == 3  # all three stand alone at one seed
    assert len(lines) == 1 and "pw100" in lines[0]


def test_trust_stored_as_an_object_is_skipped_not_aborted() -> None:
    mod = _load()
    # Object trust + no declared seeds → unknowable → seeds:unknown fires.
    rows = [_row(100, trust_type="object", trusted=True)]
    lines, fired, _f, _s, skipped = mod._evaluate(rows)
    assert skipped == 1 and fired == 1
    assert "seeds:unknown" in lines[0]
    # …but a non-array trust counts as no records, so declared seeds count.
    rows = [_row(100, trust_type="object", declared=[0, 1], trusted=True)]
    lines, fired, _f, _s, skipped = mod._evaluate(rows)
    assert skipped == 1 and fired == 0 and lines == []


def test_a_flip_counts_only_when_the_firing_pathway_is_the_source() -> None:
    mod = _load()
    # pw100 fires but the candidate's barrier came from the later pw101
    # (a different geometry → a different key → no seed pooling).
    rows = [
        _row(100, live=[0], all_rec=[0], trusted=True),
        _row(101, live=[0, 1], all_rec=[0, 1], trusted=True, slab="sha-two"),
    ]
    lines, fired, flips, *_ = mod._evaluate(rows)
    assert (fired, flips) == (1, 0)
    assert "pw100" in lines[0] and "trusted->untrusted" not in lines[0]
    # A barrier carried at a different tier is likewise not this row's flip.
    rows = [_row(100, live=[0], all_rec=[0], trusted=True, fidelity="verify")]
    lines, fired, flips, *_ = mod._evaluate(rows)
    assert (fired, flips) == (1, 0)
    assert "pw100" in lines[0] and "trusted->untrusted" not in lines[0]
