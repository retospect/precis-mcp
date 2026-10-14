"""Quest-minted surface-coverage anchors (surface-Pourbaix wave B):

dispatch gating (the flag, the neb-or-front rule, idempotency) and the
harvest stamp — the precis-side glue around ``surface_coverage_scan``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import surface_anchors as anchors
from precis.store import Store
from precis.store.types import Tag

#: Minimal structure spec for the DB-backed harvest test.
SPEC: dict[str, Any] = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.5]}],
}

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
    by_id = {c.id: c for c in candidates}
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {
            i: (quest if i == 9 else by_id[i]) for i in ids if i == 9 or i in by_id
        },
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
    candidate off the front is skipped; on the front it is scanned. Front
    membership presupposes measures — the probe carries a harvest marker —
    and an unmeasured candidate is ``unevaluated``, never on the front, so
    it costs no frontier eval at all."""
    screening = _candidate(
        id=50,
        meta={"geom_hash_c": "g", "tier": "screening", "quest_harvested_upto": 7},
    )
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

    # The unmeasured candidate never reaches the frontier eval: force a
    # frontier that would mint it and show it is still skipped.
    store, handler, _e, _l = _harness(monkeypatch, FLAGGED, [fresh], frontier={51})
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []
    assert handler.puts == []


def test_dispatch_mints_anchor_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    """With window objectives declared, dispatch mints each top-ranked
    anchor-placement proposal: bracketing coverage counts folded into the
    scan's ``coverages``, the proposal carried as provenance, a distinct
    idem key (so a re-tick mints nothing)."""
    quest_meta = {
        **FLAGGED,
        "rubric_objectives": [{"key": "window_area_VpH", "sense": "max"}],
    }
    candidate = _candidate(meta={"geom_hash_c": "g", "tier": "neb"})
    store, handler, existing, _links = _harness(monkeypatch, quest_meta, [candidate])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    proposal = {
        "structure_ref": 42,
        "anchor_key": "k" * 64,
        "job_type": "surface_coverage_scan",
        "params": {
            "around_U_RHE": -0.1,
            "adsorbates": ["H", "O", "OH"],
            "coverage_grid_extension": {
                "below": {"adsorbate": "H", "n": 2},
                "above": {"adsorbate": "O", "n": 4},
                "n": [2, 4],
            },
        },
        "band_propagated_V": 0.11,
        "reason": "widest propagated band",
    }
    monkeypatch.setattr(
        "precis.quest.window_objectives.propose_anchor_scans",
        lambda *_a, **_k: [proposal],
    )

    notes = anchors.dispatch_surface_anchors(store, 9, hub=hub)
    # One baseline scan + one proposal scan.
    assert len(handler.puts) == 2
    baseline, prop = handler.puts
    assert "coverages" not in (baseline["params"]["config"].get("coverage") or {})
    pcov = prop["params"]["config"]["coverage"]
    # n_top=9; brackets of {2,4} add counts {1,3,5} onto {2,4,7,9}.
    assert pcov["coverages"] == [pytest.approx(n / 9) for n in (1, 2, 3, 4, 5, 7, 9)]
    assert pcov["adsorbates"] == ["H", "O", "OH"]
    assert prop["params"]["point_U_RHE"] == -0.1
    assert prop["params"]["proposal"]["band_propagated_V"] == 0.11
    assert prop["idem_key"] != baseline["idem_key"]
    assert any(n.startswith("anchor proposal") for n in notes)

    for put in handler.puts:
        existing.add(_put_idem_key(put))
    assert anchors.dispatch_surface_anchors(store, 9, hub=hub) == []
    assert len(handler.puts) == 2


def test_dispatch_proposal_adding_nothing_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proposal whose bracketing counts are all already measured mints
    nothing — a same-anchors job would only re-key a duplicate footing."""
    quest_meta = {
        **FLAGGED,
        "rubric_objectives": [{"key": "window_area_VpH", "sense": "max"}],
    }
    candidate = _candidate(meta={"geom_hash_c": "g", "tier": "neb"})
    reaction = {**REACTION, "coverage": {"coverages": "all"}}
    meta = {**quest_meta, "reaction_config": reaction}
    store, handler, _existing, _links = _harness(monkeypatch, meta, [candidate])
    hub = SimpleNamespace(handler_for=lambda _kind: handler)
    monkeypatch.setattr(
        "precis.quest.window_objectives.propose_anchor_scans",
        lambda *_a, **_k: [
            {
                "structure_ref": 42,
                "params": {
                    "adsorbates": [],
                    "coverage_grid_extension": {"n": [2, 4]},
                },
            }
        ],
    )
    anchors.dispatch_surface_anchors(store, 9, hub=hub)
    assert len(handler.puts) == 1  # baseline only


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
    assert values["theta0_note"] == "scan job carries no coverage anchors"
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


# ── theta0 check ──────────────────────────────────────────────────────────


def test_fragments_of_mirrors_the_engine_split() -> None:
    assert anchors._fragments_of("OH") == ["OH"]
    assert anchors._fragments_of("NO@N") == ["NO"]
    assert anchors._fragments_of("NO@par") == ["NO"]
    assert anchors._fragments_of("N+H") == ["N", "H"]
    # The clean-slab root and any '~' desorption channel are not cells
    # whose bookkeeping this check can trust.
    assert anchors._fragments_of("*") == []
    assert anchors._fragments_of("*~n2o") == []
    assert anchors._fragments_of("O~n2") == []


def test_theta0_reference_direct_and_proxy_estimates() -> None:
    """A5's recipe: bare parked fragments and single-fragment nodes are
    direct cells; X+S states give a differential proxy for species with
    no bare cell (here H*); '~' keys and unpriced species are skipped."""
    partial: dict[str, Any] = {
        "refs": {"slab": -100.0, "bare": {"O": -105.0, "N": -107.0}},
        "states": {
            "*": -100.0,
            "OH": -107.0,
            "O+H": -106.0,
            "NO@N": -110.0,
            "NO@par": -111.0,  # lower site wins the single-frag cell
            "NO+H": -112.5,
            "O~n2": -130.0,  # desorption channel — skipped
        },
    }
    mu = {"H": -3.28, "O": -7.52, "OH": -10.8}
    c_ads = {"H": 0.18, "O": 0.07, "OH": 0.36}
    ref, prov, used = anchors._theta0_reference([partial], set(), mu, c_ads)
    assert used == 1
    # O: bare cell (−105 −(−100) = −5) − μ_O → 2.52, +c_ads 0.07 → 2.59.
    assert ref["O"] == pytest.approx(2.59)
    # OH: node cell (−107+100 = −7) − μ_OH → 3.8, +0.36 → 4.16.
    assert ref["OH"] == pytest.approx(4.16)
    # H: two proxies — O+H − bare(O) − μ_H = 2.28 and NO+H − NO@par − μ_H
    # = 1.78; mean 2.03, +0.18 → 2.21. (cell(NO) is the @par site.)
    assert ref["H"] == pytest.approx(2.21)
    assert prov["H"]["n_estimates"] == 2
    # N has no reservoir mu on the scan's gauge — unpriced, skipped.
    assert "N" not in ref


def test_theta0_reference_skips_low_confidence_and_missing_slab() -> None:
    partial: dict[str, Any] = {
        "states": {"OH": -107.0, "N+H": -108.0},  # no refs.slab and no '*'
    }
    ref, prov, used = anchors._theta0_reference([partial], set(), {"H": -3.0}, {})
    assert used == 0 and ref == {}
    # A flagged node contributes no estimate.
    partial = {
        "refs": {"slab": -100.0, "bare": {"N": -107.0}},
        "states": {"N+H": -108.0},
    }
    ref, prov, used = anchors._theta0_reference(
        [partial], {"N+H"}, {"H": -3.0, "N": -6.0}, {}
    )
    assert used == 1
    assert "H" not in ref  # the only H source was low-confidence
    assert ref["N"] == pytest.approx(-107.0 - (-100.0) - (-6.0))


def _cov_payload() -> dict[str, Any]:
    """A minimal ``coverage.json`` payload: H/O/OH single-point anchors."""
    ads: dict[str, dict[str, Any]] = {}
    for frag, g in (("H", 2.25), ("O", 2.55), ("OH", 4.1)):
        ads[frag] = {
            "mu": {"H": -3.28, "O": -7.52, "OH": -10.8}[frag],
            "c_ads": {"H": 0.18, "O": 0.07, "OH": 0.36}[frag],
            "site": "fcc",
            "points": [
                {
                    "n": 1,
                    "theta": 0.25,
                    "energy": -100.0 + g * 10.0,
                    "gamma": g,
                    "converged": True,
                    "detached": False,
                }
            ],
            "best": {"theta": 0.25, "n": 1, "gamma": g},
        }
    return {
        "model": "mace:medium",
        "mu": {"H": -3.28, "O": -7.52, "OH": -10.8},
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": 10.0,
                "n_top": 4,
                "adsorbates": ads,
            }
        ],
    }


@pytest.mark.db
def test_harvest_stamps_theta0_check(store: Store, hub: Hub) -> None:
    """End to end: a succeeded scan + a pathway with a seed partial stamps
    a populated theta0_check (rows per shared species on the scan's mu /
    c_ads gauge), not the deferred null."""
    resp = QuestHandler(hub=hub).put(text="theta0 quest")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    qid = int(m.group(1))
    store.stamp_ref_meta(qid, FLAGGED)
    sid = compute_mod.ensure_candidate(store, qid, {"name": "c", "structure": SPEC})
    assert sid is not None
    store.stamp_ref_meta(sid, {"geom_hash_c": "g"})
    built = anchors._request(
        FLAGGED, SimpleNamespace(id=sid, meta={"geom_hash_c": "g"})
    )
    assert built is not None
    _params, idem = built
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="scan",
        parent_id=sid,
        meta={
            "job_type": "surface_coverage_scan",
            "idem_key": idem,
            "anchor_key": "a" * 64,
            "coverage": _cov_payload(),
            "che_sweep": {"point": None, "boundaries": [], "needs_anchor": []},
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    pw = store.insert_ref(
        kind="pathway",
        slug=f"pw{sid}",
        title="pw",
        meta={"candidate_ref": str(sid), "results": {"nodes": {}}},
    )
    seed = store.insert_ref(
        kind="job",
        slug=None,
        title="seed",
        meta={
            "job_type": "autocatpath_seed",
            "pathway_ref": str(pw.id),
            "partial": {
                "refs": {"slab": -100.0, "bare": {"O": -105.0}},
                "states": {"*": -100.0, "OH": -107.0, "O+H": -106.0},
            },
        },
    )
    store.add_tag(
        seed.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )

    candidate = store.fetch_refs_by_ids({sid})[sid]
    note = anchors.harvest_surface_anchors(store, qid, candidate)
    assert note is not None
    meta = store.fetch_refs_by_ids({sid})[sid].meta or {}
    check = meta["theta0_check"]
    assert check["n_partials"] == 1
    rows = {r["adsorbate"]: r for r in check["rows"]}
    assert set(rows) == {"H", "O", "OH"}
    # references: O 2.52+0.07, OH 3.8+0.36, H 2.28+0.18 (single proxy)
    assert rows["O"]["reference"] == pytest.approx(2.59)
    assert rows["OH"]["reference"] == pytest.approx(4.16)
    assert rows["H"]["reference"] == pytest.approx(2.46)
    assert rows["O"]["delta"] == pytest.approx(rows["O"]["g_ads_anchor"] - 2.59)
    assert "pathway single-adsorbate" in meta["theta0_note"]


@pytest.mark.db
def test_unflagged_scanned_candidate_meta_unchanged(store: Store, hub: Hub) -> None:
    """Snapshot: on an unflagged quest a candidate that already HAS a
    succeeded scan keeps its meta bit-identical through dispatch +
    harvest — nothing mints, nothing stamps."""
    resp = QuestHandler(hub=hub).put(text="unflagged quest")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    qid = int(m.group(1))
    sid = compute_mod.ensure_candidate(store, qid, {"name": "c", "structure": SPEC})
    assert sid is not None
    store.stamp_ref_meta(sid, {"geom_hash_c": "g"})
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="scan",
        parent_id=sid,
        meta={
            "job_type": "surface_coverage_scan",
            "anchor_key": "a" * 64,
            "coverage": _cov_payload(),
            "che_sweep": {"point": None, "boundaries": [], "needs_anchor": []},
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    with store.pool.connection() as conn:
        jobs_before = (
            conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
                (sid,),
            ).fetchone()
            or (0,)
        )[0]
    before = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})

    from precis.quest import window_objectives as wo

    ref = store.fetch_refs_by_ids({sid})[sid]
    assert anchors.dispatch_surface_anchors(store, qid, hub=hub) == []
    assert anchors.harvest_surface_anchors(store, qid, ref) is None
    assert wo.stamp_window_measures(store, qid, ref) is None

    after = dict(store.fetch_refs_by_ids({sid})[sid].meta or {})
    assert after == before
    with store.pool.connection() as conn:
        jobs_after = (
            conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
                (sid,),
            ).fetchone()
            or (0,)
        )[0]
    assert jobs_after == jobs_before
