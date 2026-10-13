"""Quest-minted surface-coverage anchors (surface-Pourbaix wave B):

dispatch gating (the flag, the neb-or-front rule, idempotency) and the
harvest stamp — the precis-side glue around ``surface_coverage_scan``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.quest import surface_anchors as anchors

REACTION: dict[str, Any] = {
    "name": "no_to_nh3_pd",
    "slab": {"element": "Pd", "size": [3, 3, 4], "vacuum": 10, "fix_layers": 2},
    "mlip": {"backend": "mace", "model": "medium"},
    "substrate": "NO",
    "target": "NH3",
    "network": "ammonia",
}

FLAGGED: dict[str, Any] = {
    "surface_anchors": True,
    "reaction_config": REACTION,
    "operating_conditions": {"U_RHE": -0.3, "pH": 7},
}


def _candidate(id: int = 42, meta: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(id=id, kind="structure", title="cand", meta=meta or {})


# ── _request ──────────────────────────────────────────────────────────────


def test_request_key_tracks_geometry_conditions_and_flag() -> None:
    candidate = _candidate(meta={"geom_hash_c": "geom-a"})
    built = anchors._request(FLAGGED, candidate)
    assert built is not None
    params, key = built
    assert params["config"] is REACTION
    assert params["point_U_RHE"] == -0.3
    assert params["pH"] == 7.0
    assert "window_U_RHE" not in params

    changed_u = anchors._request(
        {**FLAGGED, "operating_conditions": {"U_RHE": -0.2, "pH": 7}}, candidate
    )
    changed_geom = anchors._request(FLAGGED, _candidate(meta={"geom_hash_c": "g2"}))
    assert changed_u is not None and changed_u[1] != key
    assert changed_geom is not None and changed_geom[1] != key

    flagged_window = {
        **FLAGGED,
        "operating_conditions": {
            "U_RHE": -0.3,
            "pH": 7,
            "window": {"U_RHE": [-0.6, 0.0], "pH": [7, 10]},
        },
    }
    built_w = anchors._request(flagged_window, candidate)
    assert built_w is not None and built_w[0]["window_U_RHE"] == [-0.6, 0.0]


def test_request_fails_closed_without_flag_config_or_geometry() -> None:
    candidate = _candidate(meta={"geom_hash_c": "g"})
    assert anchors._request({}, candidate) is None
    assert anchors._request({"reaction_config": REACTION}, candidate) is None
    assert (
        anchors._request(
            {"surface_anchors": True, "reaction_config": REACTION},
            _candidate(meta={}),
        )
        is None
    )
    assert anchors._request({"surface_anchors": True}, candidate) is None, (
        "no reaction_config → nothing to scan"
    )


# ── dispatch ──────────────────────────────────────────────────────────────


class _JobHandler:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put(self, **kwargs: Any) -> Any:
        self.puts.append(kwargs)
        return SimpleNamespace(
            body=f"created job id={700 + len(self.puts)} (STATUS:queued)",
            ref_id=700 + len(self.puts),
        )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    quest_meta: dict[str, Any],
    candidates: list[Any],
    *,
    frontier: set[int] | None = None,
) -> tuple[Any, _JobHandler, set[str], list[dict[str, Any]]]:
    quest = SimpleNamespace(id=9, meta=quest_meta)
    links: list[dict[str, Any]] = []
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {9: quest} if 9 in ids else {},
        structure_load=lambda sid: (f"scene-{sid}", None),
        add_link=lambda **kw: links.append(kw),
    )
    handler = _JobHandler()
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    existing: set[str] = set()

    def find_job(_store: Any, key: str) -> tuple[int, str] | None:
        return (71, "queued") if key in existing else None

    monkeypatch.setattr("precis.quest.gaps._live_servers", lambda *_: candidates)
    monkeypatch.setattr("precis.quest.compute._find_job_by_idem_key", find_job)
    monkeypatch.setattr(
        "precis.structure.export.to_extxyz",
        lambda scene, constraints=True: f"extxyz({scene})",
    )
    monkeypatch.setattr(anchors, "_frontier_ids", lambda *_: frontier or set())
    return store, handler, existing, links


def _put_idem_key(put: dict[str, Any]) -> str:
    return str(put["idem_key"])


def test_dispatch_mints_one_scan_per_neb_candidate_and_skips_retick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flagged quest mints one ``surface_coverage_scan`` (parented on the
    candidate, part-of the quest, ssh_node, the exported slab injected);
    a re-tick mints nothing."""
    candidate = _candidate(meta={"geom_hash_c": "g", "tier": "neb"})
    store, handler, existing, links = _harness(monkeypatch, FLAGGED, [candidate])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)

    notes = anchors.dispatch_surface_anchors(store, 9, hub=hub)
    assert len(handler.puts) == 1
    put = handler.puts[0]
    assert put["parent_id"] == 42
    assert put["job_type"] == "surface_coverage_scan"
    assert put["executor"] == "ssh_node"
    assert put["params"]["slab_extxyz"] == "extxyz(scene-42)"
    assert put["params"]["point_U_RHE"] == -0.3
    assert notes == ["created job id=701 (STATUS:queued)"]
    assert links == [{"src_ref_id": 701, "dst_ref_id": 9, "relation": "part-of"}]

    existing.add(_put_idem_key(put))
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []
    assert len(handler.puts) == 1


def test_dispatch_failed_scan_is_not_reminted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part A convention: a job carrying the idem key in ANY status —
    including failed — blocks re-minting."""
    candidate = _candidate(meta={"geom_hash_c": "g", "tier": "verify"})
    store, handler, _existing, _links = _harness(monkeypatch, FLAGGED, [candidate])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    monkeypatch.setattr(
        "precis.quest.compute._find_job_by_idem_key",
        lambda *_: (66, "failed"),
    )
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []
    assert handler.puts == []


def test_dispatch_front_membership_anchors_ahead_of_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mint rule: neb+ rung OR the merged front. A screening-tier
    candidate off the front is skipped; on the front it is scanned."""
    screening = _candidate(id=50, meta={"geom_hash_c": "g", "tier": "screening"})
    fresh = _candidate(id=51, meta={"geom_hash_c": "h"})

    store, handler, _e, _l = _harness(
        monkeypatch, FLAGGED, [screening, fresh], frontier=set()
    )
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []
    assert handler.puts == []

    store, handler, _e, _l = _harness(monkeypatch, FLAGGED, [screening], frontier={50})
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    assert len(anchors.dispatch_surface_anchors(store, 9, hub=hub)) == 1
    assert len(handler.puts) == 1


def test_dispatch_flag_off_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    quest = SimpleNamespace(id=9, meta={"reaction_config": REACTION})
    store = SimpleNamespace(fetch_refs_by_ids=lambda ids: {9: quest})
    hub = SimpleNamespace(
        handler_for=lambda _kind: (_ for _ in ()).throw(
            AssertionError("dispatch called")
        )
    )
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []


def test_dispatch_skips_missing_geometry_and_refused_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_geom = _candidate(id=60, meta={"tier": "neb"})
    store, handler, _e, _l = _harness(monkeypatch, FLAGGED, [no_geom])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    notes = anchors.dispatch_surface_anchors(store, 9, hub=hub)
    assert notes == ["surface anchors skipped: structure 60 has no geometry hash"]
    assert handler.puts == []

    flagged_facets = {
        **FLAGGED,
        "reaction_config": {
            **REACTION,
            "coverage": {"facets": [[1, 0, 0]]},
        },
    }
    ok = _candidate(id=61, meta={"geom_hash_c": "g", "tier": "neb"})
    store, handler, _e, _l = _harness(monkeypatch, flagged_facets, [ok])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    notes = anchors.dispatch_surface_anchors(store, 9, hub=hub)
    assert len(notes) == 1 and "(111) facet only" in notes[0]
    assert handler.puts == []


# ── harvest ───────────────────────────────────────────────────────────────

CHE_SWEEP: dict[str, Any] = {
    "point": {
        "U_RHE": -0.3,
        "U_SHE": -0.713,
        "winner": {"adsorbate": "H", "n": 9, "theta": 1.0},
        "gamma": -0.02,
    },
    "boundaries": [
        {
            "below": {"adsorbate": "H", "n": 9},
            "above": {"adsorbate": "H", "n": 7},
            "U_RHE": 0.44,
            "band_propagated": 0.05,
            "band_model_form": None,
            "n_models": 1,
        }
    ],
    "needs_anchor": [
        {
            "below": {"adsorbate": "H", "n": 9},
            "above": {"adsorbate": "H", "n": 7},
            "U_RHE": 0.44,
            "band_propagated": 0.05,
            "band_model_form": None,
            "n_models": 1,
        }
    ],
}


def _harvest_harness(
    monkeypatch: pytest.MonkeyPatch,
    candidate: Any,
    job_status: str = "succeeded",
    job_meta: dict[str, Any] | None = None,
) -> tuple[Any, list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    quest = SimpleNamespace(id=9, meta=FLAGGED)
    job = SimpleNamespace(meta=job_meta or {})
    rows = {9: quest, 71: job}
    stamps: list[tuple[int, dict[str, Any]]] = []
    entries: list[dict[str, Any]] = []
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {rid: rows[rid] for rid in ids if rid in rows},
        stamp_ref_meta=lambda rid, meta: stamps.append((rid, meta)),
    )
    monkeypatch.setattr(
        "precis.quest.compute._find_job_by_idem_key",
        lambda *_: (71, job_status),
    )
    monkeypatch.setattr(
        "precis.quest.logbook.append_entry", lambda *_a, **kw: entries.append(kw)
    )
    monkeypatch.setattr("precis.utils.handle_registry.try_format", lambda *_: "st42")
    return store, stamps, entries


def test_harvest_stamps_the_sweep_once(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate(meta={"geom_hash_c": "g"})
    meta = {
        "anchor_key": "a" * 64,
        "model": "mace:medium",
        "engine_version": "0.25.0",
        "corrections": {"gas": "x"},
        "che_sweep": CHE_SWEEP,
    }
    store, stamps, entries = _harvest_harness(monkeypatch, candidate, job_meta=meta)

    note = anchors.harvest_surface_anchors(store, 9, candidate)
    assert note == (
        "surface anchors for [st42]: resting termination H*(9) at -0.30 V_RHE"
    )
    rid, values = stamps[0]
    assert rid == 42
    assert values["anchors_job_id"] == 71
    assert values["anchor_key"] == "a" * 64
    assert values["anchors_footing"] == {
        "model": "mace:medium",
        "engine_version": "0.25.0",
        "corrections": {"gas": "x"},
    }
    assert values["resting_termination_at_point"]["winner"] == {
        "adsorbate": "H",
        "n": 9,
        "theta": 1.0,
    }
    assert values["needs_anchor_top"]["U_RHE"] == 0.44
    assert values["theta0_check"] is None
    assert "deferred" in values["theta0_note"]
    assert entries and entries[0]["entry_type"] == "result"

    # Idempotent: the same job is never re-stamped.
    candidate.meta["anchors_job_id"] = 71
    assert anchors.harvest_surface_anchors(store, 9, candidate) is None


def test_harvest_clean_surface_and_no_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(meta={"geom_hash_c": "g"})
    store, stamps, entries = _harvest_harness(
        monkeypatch,
        candidate,
        job_meta={
            "anchor_key": "b" * 64,
            "model": "emt",
            "che_sweep": {"boundaries": []},
        },
    )
    note = anchors.harvest_surface_anchors(store, 9, candidate)
    assert note == "surface anchors for [st42]: sweep: 0 boundary(ies)"
    assert stamps[0][1]["resting_termination_at_point"] is None
    assert stamps[0][1]["needs_anchor_top"] is None


def test_harvest_ignores_unsucceeded_and_flagless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(meta={"geom_hash_c": "g"})
    store, stamps, _e = _harvest_harness(monkeypatch, candidate, job_status="failed")
    assert anchors.harvest_surface_anchors(store, 9, candidate) is None
    assert stamps == []

    quest = SimpleNamespace(id=9, meta={"reaction_config": REACTION})
    store = SimpleNamespace(fetch_refs_by_ids=lambda ids: {9: quest})
    assert anchors.harvest_surface_anchors(store, 9, candidate) is None
