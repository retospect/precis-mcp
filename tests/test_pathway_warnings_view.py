"""precis_pathway warnings view: ``get(kind='pathway', id='<slug>',
view='warnings')`` — severity- and route-aware sections over catpath's
structured ``trust`` records + ``trust_summary`` (spec: docs/backlog/
pathway-conditions-effects-report.md "Warnings" fix 1), not the raw flat
``meta['warnings']`` prose list. Three sections: ``blocking`` (real fatal
on-route trust-record fails), ``counts`` (everything else collapsed to one
row per check/verdict), ``messages`` (the flat prose collapsed by
numeric-literal template).

Store-seeded fake ``meta['results']`` (no calculator, no NEB) — same
rationale as ``test_pathway_trust.py``/``test_pathway_kinetics.py``: split
out to stay cheap to target on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autocatpath")

import precis_pathway
from precis.dispatch import Hub
from precis.store import Store
from precis_pathway.handler import PathwayHandler
from precis_pathway.toon_views import _warnings_summary, analysis_text

_MIGRATIONS_DIR = Path(precis_pathway.__file__).parent / "migrations"

_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "R"}, {"id": "M"}, {"id": "P"}, {"id": "S"}],
    "links": [
        {
            "source": "R",
            "target": "M",
            "barrier": 0.5,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        },
        {
            "source": "M",
            "target": "P",
            "barrier": 0.3,
            "delta_e": -0.2,
            "low_confidence": False,
            "kind": "reaction",
        },
    ],
}

# Two on-route fatal fails (blocking), one off-route fatal fail (not
# blocking — folds into counts), a marginal check split on/off route, a
# warn-severity ("fail(warn)") check, and a pass (must never surface as
# noise anywhere).
_TRUST_RECORDS: list[dict[str, Any]] = [
    {
        "step": "R->M",
        "seed": 0,
        "check": "detachment",
        "verdict": "fail",
        "severity": "fatal",
        "evidence": {"image": 3},
        "id": "R->M#s0#detachment",
    },
    {
        "step": "M->P",
        "seed": 0,
        "check": "saddle_verified",
        "verdict": "fail",
        "severity": "fatal",
        "evidence": {"ts_image": 5},
        "id": "M->P#s0#saddle_verified",
    },
    {
        "step": "M->S",  # off the reported route
        "seed": 0,
        "check": "detachment",
        "verdict": "fail",
        "severity": "fatal",
        "evidence": {"image": 4},
        "id": "M->S#s0#detachment",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 0,
        "check": "wrong_binder",
        "verdict": "marginal",
        "severity": "warn",
        "evidence": {"dz": 0.12},
        "id": "M@R->M#s0#wrong_binder#0",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 1,
        "check": "wrong_binder",
        "verdict": "marginal",
        "severity": "warn",
        "evidence": {"dz": 0.08},
        "id": "M@R->M#s1#wrong_binder",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 2,
        "check": "wrong_binder",
        "verdict": "marginal",
        "severity": "warn",
        "evidence": {"dz": 0.15},
        "id": "M@R->M#s2#wrong_binder",
    },
    {
        "state": "S",
        "step": "M->S",  # off-route marginal
        "seed": 0,
        "check": "wrong_binder",
        "verdict": "marginal",
        "severity": "warn",
        "evidence": {"dz": 0.20},
        "id": "S@M->S#s0#wrong_binder",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 0,
        "check": "reconstruction",
        "verdict": "fail",
        "severity": "warn",
        "evidence": {"delta": 0.6},
        "id": "M@R->M#s0#reconstruction#0",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 1,
        "check": "reconstruction",
        "verdict": "fail",
        "severity": "warn",
        "evidence": {"delta": 0.5},
        "id": "M@R->M#s1#reconstruction",
    },
    {
        "step": "R->M",
        "seed": 0,
        "check": "neb_convergence",
        "verdict": "pass",
        "severity": "fatal",
        "evidence": {"ts_image": 3},
        "id": "R->M#s0#neb_convergence",
    },
]

# Repeated flat-string templates differing only by numeric literals — the
# "182 orientation notes" / "~140 thermo gap" noise from the prod audit.
_WARNINGS: list[str] = [
    "orientation, not a re-seat; NEB allowed (dz=0.12 A)",
    "orientation, not a re-seat; NEB allowed (dz=0.08 A)",
    "orientation, not a re-seat; NEB allowed (dz=0.15 A)",
    "thermo: no table data for NH3 (T=298.15K)",
    "thermo: no table data for NH3 (T=310.00K)",
    "barrier untrustworthy, off a non-surface path (edge 3)",
]

_RESULTS_V2: dict[str, Any] = {
    "pathway": ["R", "M", "P"],
    "target": "P",
    "trust_schema": 2,
    "trust": _TRUST_RECORDS,
    "route_steps": ["R->M", "M->P"],
    "trust_summary": {
        "barrier": {
            "available": False,
            "blocked_by": ["M->P#s0#saddle_verified", "R->M#s0#detachment"],
        },
        "selectivity": {
            "available": False,
            "blocked_by": [
                {
                    "fork": "M",
                    "competitor": "S",
                    "on_route": False,
                    "reasons": ["M->S#s0#detachment"],
                },
            ],
        },
    },
}

_RESULTS_LEGACY: dict[str, Any] = {"pathway": ["R", "M", "P"], "target": "P"}


@pytest.fixture
def pathway_store(store: Store) -> Store:
    """Mirrors ``test_pathway_trust.py``'s fixture of the same name
    (duplicated on purpose, keeps this file independently collectable)."""
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return store


def _handler(store: Store) -> PathwayHandler:
    hub = Hub(store=store)
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    return h


def _seed(store: Store, slug: str, meta: dict[str, Any]) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway", slug=slug, title=f"{slug} pathway", meta=meta, conn=conn
        )
    return ref.id


def test_warnings_view_blocking_section_is_fatal_on_route_only(
    pathway_store: Store,
) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "warn-rows",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": _RESULTS_V2,
            "warnings": _WARNINGS,
        },
    )

    r = h.get(id="warn-rows", view="warnings")
    body = r.body
    assert "## blocking" in body
    assert "## counts" in body
    assert "## messages" in body

    # blocked_by priority puts saddle_verified before the plain detachment.
    assert body.index("M->P#s0#saddle_verified") < body.index("R->M#s0#detachment")
    # the off-route fatal fail is not a blocker.
    assert "M->S#s0#detachment" not in body


def test_warnings_view_counts_collapse_marginal_offroute_and_warn_fails(
    pathway_store: Store,
) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "warn-counts",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": _RESULTS_V2,
            "warnings": _WARNINGS,
        },
    )

    r = h.get(id="warn-counts", view="warnings")
    body = r.body

    # 4 wrong_binder marginals total, 1 of them off-route (M->S).
    assert "wrong_binder" in body
    assert "marginal" in body
    assert "4 (off-route 1)" in body

    # the off-route fatal detachment (not blocking) surfaces as a count,
    # not silently dropped.
    assert "1 (off-route 1)" in body

    # warn-severity reconstruction fails render distinctly from a blocker.
    assert "reconstruction" in body
    assert "fail(warn)" in body
    assert "2" in body

    # pass records are never noise.
    assert "neb_convergence" not in body

    # barrier/selectivity blocked_by totals.
    assert "barrier blocked_by: 2" in body
    assert "selectivity blocked_by: 1" in body


def test_warnings_view_messages_collapse_numeric_templates(
    pathway_store: Store,
) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "warn-messages",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": _RESULTS_V2,
            "warnings": _WARNINGS,
        },
    )

    r = h.get(id="warn-messages", view="warnings")
    body = r.body

    assert "3 × orientation, not a re-seat; NEB allowed (dz=N A)" in body
    assert "2 × thermo: no table data for NH3 (T=NK)" in body
    # a once-seen template keeps its literal numbers
    assert "1 × barrier untrustworthy, off a non-surface path (edge 3)" in body
    # only 3 distinct templates from 6 raw strings — no "+K more" needed.
    assert "more templates" not in body


def test_warnings_view_no_trust_records_keeps_note_but_still_collapses_messages(
    pathway_store: Store,
) -> None:
    """A pre-trust-schema artifact (no structured records) gets the old
    empty-blocking-section guidance, no counts section, but still gets the
    collapsed messages section."""
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "warn-legacy",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": _RESULTS_LEGACY,
            "warnings": _WARNINGS,
        },
    )

    r = h.get(id="warn-legacy", view="warnings")
    body = r.body
    assert "## blocking" in body
    assert "no trust records: pre-trust-schema artifact" in body
    assert "## counts" not in body
    assert "## messages" in body
    assert "3 × orientation, not a re-seat; NEB allowed (dz=N A)" in body


def test_warnings_view_no_warnings_no_records_is_quiet() -> None:
    meta = {"results": _RESULTS_LEGACY, "warnings": []}
    from precis_pathway.toon_views import warnings_toon

    body = warnings_toon(meta)
    assert "no trust records: pre-trust-schema artifact" in body
    assert "no messages" in body


def test_warnings_summary_line_is_compact() -> None:
    meta = {"results": _RESULTS_V2, "warnings": _WARNINGS}
    summary = _warnings_summary(meta)
    # 2 blocking (on-route fatal); 10 records total, minus 2 blocking minus
    # 1 pass = 7 other (1 off-route fatal + 4 marginal + 2 warn-fail).
    assert summary == "blocking=2 · other-records=7 · messages=3 templates/6"


def test_analysis_view_shows_compact_warnings_summary_not_raw_count() -> None:
    meta = {
        "graph": _GRAPH,
        "results": _RESULTS_V2,
        "warnings": _WARNINGS,
    }
    body = analysis_text(meta)
    assert "warnings: blocking=2 · other-records=7 · messages=3 templates/6" in body
    # never the bare "6 warnings" a reader would misread as 6 problems.
    assert "6 warning" not in body
