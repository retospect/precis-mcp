"""Scientific/state boundaries: exact snapshots, precedence and no legacy trust."""

from __future__ import annotations

import copy
from typing import Any, cast

import numpy as np
import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.completion import completion_envelope, se_completion_envelope
from precis.structure.completion_codec import loads_wire, to_wire
from precis.structure.completion_identity import (
    InvalidGeometry,
    geometry_snapshot,
    stored_graph_token,
)
from precis.structure.completion_types import CompletionSnapshot, SeCompletionSnapshot
from precis.structure.scene import Atom, Bond, Scene
from tests.test_structure_completion_contract_fixtures import (
    blank,
    contract,
    geometry,
    proof,
    qualified,
    run_record,
    validate,
)


def read_snapshot(store: Store, ref_id: int, **kwargs: Any) -> Any:
    """Assert presence, keeping deliberate malformed-fixture mutations local."""
    captured = store.structure_completion_snapshot(ref_id, **kwargs)
    assert captured is not None
    return captured


def snapshot() -> dict[str, Any]:
    s = blank(contract.CompletionSnapshot)
    s.update(
        ref_id=geometry()["ref_id"],
        requested_display={"kind": "current"},
        snapshot_consistency="single-statement",
    )
    g = blank(contract.GeometrySnapshot)
    g.update(
        identity=geometry(),
        source="live-statement",
        lattice=[[10, 0, 0], [0, 10, 0], [0, 0, 10]],
        pbc=[False] * 3,
    )
    atom = blank(contract.AtomSnapshot)
    atom.update(row_id=1, label="C1", element="C", fractional=[0, 0, 0], fixed_mask=0)
    g["atoms"] = [atom]
    s["geometry"] = g
    s["display_resolution"] = {
        "status": "exact",
        "geometry": geometry(),
        "artifact": None,
        "role": None,
        "reason": None,
    }
    s["selected_run"].update(status="not-requested")
    for key in ("run_window", "request_window"):
        s[key].update(limit=64, returned=0, coverage="complete")
    return s


def output_snapshot() -> dict[str, Any]:
    s = snapshot()
    evidence = qualified()
    s["runs"] = [run_record(evidence)]
    s["run_window"]["returned"] = 1
    s["requested_display"] = {
        "kind": "persisted",
        "role": "adopted-output",
        "geometry": geometry(),
        "run_id": 12,
    }
    s["display_resolution"]["role"] = "adopted-output"
    s["selected_run"] = {
        "requested_run_id": 12,
        "status": "found",
        "record": s["runs"][0],
    }
    return s


def derive(s: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    before = copy.deepcopy(s)
    result = completion_envelope(cast(CompletionSnapshot, s), **kwargs)
    assert s == before, "derivation mutated its snapshot"
    validate(result, contract.CompletionEnvelope)
    assert result["next"]["can_submit"] is False
    return cast(dict[str, Any], result)


def test_coincident_input_output_requires_explicit_role_and_actual_final_step() -> None:
    s = output_snapshot()
    out = derive(s)
    assert out["state"] == "converged" and not out["finished"]
    s["requested_display"] = {"kind": "current"}
    s["display_resolution"]["role"] = None
    assert derive(s)["selected_evidence"] is None
    s["requested_display"] = {
        "kind": "persisted",
        "role": "input-request",
        "geometry": geometry(),
        "run_id": 12,
    }
    s["display_resolution"]["role"] = "input-request"
    assert derive(s)["state"] == "unknown"  # output force cannot qualify input


@pytest.mark.parametrize(
    "mutation",
    [
        "cap-true",
        "nonfinite",
        "false-converged",
        "wrong-units",
        "projected-free",
        "missing-force",
        "wrong-job",
        "wrong-owner",
        "proof-digest",
        "unknown-constraints",
        "raw-force-disagrees",
        "raw-steps-disagree",
        "wrong-mapped-count",
    ],
)
def test_contradictory_scientific_receipts_never_qualify(mutation: str) -> None:
    s = output_snapshot()
    run = s["runs"][0]
    verified = run["receipt_assessment"]["verified"]
    receipt = verified["receipt"]
    term = receipt["termination"]
    if mutation == "cap-true":
        term["reason"] = "step-cap"
    elif mutation == "nonfinite":
        term["forces"]["max_raw"] = float("nan")
    elif mutation == "false-converged":
        term["backend_converged"] = False
    elif mutation == "wrong-units":
        term["forces"]["units"] = "hartree/bohr"
    elif mutation == "projected-free":
        term["criterion"]["scope"] = term["forces"]["criterion_scope"] = (
            "constraint-projected"
        )
    elif mutation == "missing-force":
        term["forces"]["criterion_value"] = None
    elif mutation == "wrong-job":
        receipt["job_id"] = 999
    elif mutation == "wrong-owner":
        run["ref_id"] = 999
    elif mutation == "proof-digest":
        verified["producer_proof"]["bound_receipt_digest"] = "wrong"
    elif mutation == "unknown-constraints":
        receipt["constraints"]["atoms"] = "unknown"
    elif mutation == "raw-force-disagrees":
        run["raw_max_force"] = 10.0
    elif mutation == "raw-steps-disagree":
        run["raw_n_steps"] = 301
    elif mutation == "wrong-mapped-count":
        receipt["output"]["mapped_atom_count"] = 99
    assert derive(s)["selected_evidence"] is None


def test_verified_cap_is_not_final_step_convergence() -> None:
    s = output_snapshot()
    run = s["runs"][0]
    term = run["receipt_assessment"]["verified"]["receipt"]["termination"]
    term.update(reason="step-cap", backend_converged=False)
    run.update(raw_converged=False)
    out = derive(s)
    assert (
        out["state"] == "stopped-unconverged"
        and out["level"]["qualification"] == "attempted"
    )


def test_stale_conflicting_missing_and_unavailable_selection_never_falls_back() -> None:
    s = output_snapshot()
    out = derive(s, shown_geometry_token="old")
    assert (
        out["binding"] == "stale"
        and out["shown_geometry"] is None
        and out["selected_evidence"] is None
    )
    assert out["latest_geometry"] == geometry()
    assert (
        derive(s, requested_display={"kind": "current"})["display_resolution"]["status"]
        == "conflict"
    )
    assert derive(s, selected_run_id=999)["selected_evidence"] is None
    for status in ("missing", "wrong-owner"):
        s["selected_run"].update(status=status, record=None)
        assert derive(s)["selected_evidence"] is None


def policy_and_pass(s: dict[str, Any]) -> dict[str, Any]:
    receipt = s["runs"][0]["receipt_assessment"]["verified"]["receipt"]
    p = blank(contract.EffectivePolicy)
    requirement = {
        "check_id": "clashes",
        "implementation_version": "v1",
        "criterion_digest": "synthetic:criterion",
        "criteria": {"maximum": 0},
    }
    p.update(
        policy_id="synthetic:policy",
        policy_digest="synthetic:policy-digest",
        version="v1",
        state="applicable",
        authority_proof={**proof(), "reason": None},
        method=copy.deepcopy(receipt["method"]),
        constraints=copy.deepcopy(receipt["constraints"]),
        criterion=copy.deepcopy(receipt["termination"]["criterion"]),
        required_post_checks=[requirement],
    )
    checks = blank(contract.PostChecks)
    checks.update(
        policy_id=p["policy_id"],
        policy_digest=p["policy_digest"],
        geometry=geometry(),
        graph_digest=stored_graph_token(s["geometry"]),
        suite_version="v1",
        required=[requirement],
        results=[
            {
                "requirement": requirement,
                "outcome": "pass",
                "evidence_id": "synthetic:check",
                "reason": None,
            }
        ],
        state="complete",
        outcome="pass",
    )
    receipt["post_checks"] = checks
    return p


def test_policy_postchecks_and_holds_are_separate_from_force_evidence() -> None:
    s = output_snapshot()
    p = policy_and_pass(s)
    assert derive(s, policy=p)["finished"] is True
    p["holds"] = [
        {
            "hold_id": "synthetic",
            "reason": "submission held",
            "blocks_submission": True,
            "blocks_completion": False,
            "authority_record_id": "synthetic",
        }
    ]
    held = derive(s, policy=p)
    assert held["state"] == "held" and held["finished"] is True
    p["holds"][0]["blocks_completion"] = True
    blocked = derive(s, policy=p)
    assert blocked["finished"] is False and blocked["selected_evidence"] is not None
    p["holds"] = []
    s["runs"][0]["receipt_assessment"]["verified"]["receipt"]["post_checks"][
        "results"
    ] = []
    assert not derive(s, policy=p)["finished"]


def test_new_failed_request_does_not_erase_previous_qualified_evidence() -> None:
    s = output_snapshot()
    s["selected_run"] = {
        "requested_run_id": None,
        "status": "not-requested",
        "record": None,
    }
    s["requested_display"]["run_id"] = None
    request = blank(contract.RequestRecord)
    request.update(
        request_id=22,
        job_id=22,
        owner_ref_id=s["ref_id"],
        input={
            "persisted": geometry(),
            "artifact": None,
            "physical_input_digest": None,
        },
        created_at="2026-10-08T00:00:00Z",
        state="failed",
        failure_reason="synthetic",
    )
    s["requests"] = [request]
    out = derive(s)
    assert out["state"] == "failed" and out["selected_evidence"] is None
    assert out["earlier_qualified_evidence"][0]["run_id"] == 12
    token = out["status_token"]
    request["state"] = "running"
    assert derive(s)["status_token"] != token
    assert derive(s)["shown_geometry"] == out["shown_geometry"]


def raw_geometry() -> dict[str, Any]:
    return {
        "ref_id": 1,
        "version": 2,
        "lattice": [[10, 0, 0], [0, 10, 0], [0, 0, 10]],
        "pbc": [False] * 3,
        "atoms": [
            {
                "id": 1,
                "ref_id": 1,
                "label": "C1",
                "element": "C",
                "fa": 0.1,
                "fb": 0,
                "fc": 0,
                "fixed": 0,
                "magmom": None,
                "oxidation": None,
                "charge": None,
                "hybridization": None,
            }
        ],
        "bonds": [],
    }


@pytest.mark.parametrize(
    "field", ["charge", "magmom", "oxidation", "fixed", "hybridization", "fa"]
)
def test_geometry_tokens_include_all_scientific_intent(field: str) -> None:
    raw = raw_geometry()
    before = geometry_snapshot(raw)["identity"]
    raw["atoms"][0][field] = "sp2" if field == "hybridization" else 1
    assert geometry_snapshot(raw)["identity"] != before


@pytest.mark.parametrize(
    "bad", ["cell", "pbc", "duplicate", "nan", "dangling", "bool-id"]
)
def test_invalid_geometry_cannot_receive_identity(bad: str) -> None:
    raw = raw_geometry()
    if bad == "cell":
        raw["lattice"] = None
    elif bad == "pbc":
        raw["pbc"] = [1, 0, 0]
    elif bad == "duplicate":
        raw["atoms"].append(copy.deepcopy(raw["atoms"][0]))
    elif bad == "nan":
        raw["atoms"][0]["fa"] = float("nan")
    elif bad == "bool-id":
        raw["atoms"][0]["id"] = True
    else:
        raw["bonds"] = [
            {
                "id": 1,
                "ref_id": 1,
                "i": 1,
                "j": 999,
                "image": [0, 0, 0],
                "bond_order": 1,
                "kind": "pairwise",
                "provenance": "declared",
            }
        ]
    with pytest.raises((InvalidGeometry, ValueError)):
        geometry_snapshot(raw)


def save_pair(store: Store) -> Any:
    scene = Scene(cell=Cell(np.eye(3) * 10, pbc=(False, False, False)))
    scene.atoms["C1"] = Atom(label="C1", element="C", frac=np.array([0.1, 0, 0]))
    scene.atoms["C2"] = Atom(label="C2", element="C", frac=np.array([0.242, 0, 0]))
    scene.bonds.append(Bond(i="C1", j="C2"))
    return store.structure_save(
        slug="completion-read",
        title="Completion fixture",
        scene=scene,
        version=1,
        card_text="fixture",
    )[0]


def test_reader_bounded_history_selected_lookup_and_no_raw_proof_trust(
    store: Store,
) -> None:
    ref = save_pair(store)
    with store.pool.connection() as conn:
        ids = []
        for _ in range(3):
            row = conn.execute(
                "INSERT INTO struct_runs(ref_id,fidelity,on_version,converged,n_steps,params) VALUES (%s,'ml',1,true,300,%s) RETURNING id",
                (
                    ref.id,
                    Jsonb(
                        {
                            "completion": {
                                "format": 1,
                                "receipt_assessment": {"status": "verified"},
                            }
                        }
                    ),
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
        foreign = store.insert_ref(
            kind="structure", title="Foreign", slug="completion-foreign", conn=conn
        )
        row = conn.execute(
            "INSERT INTO struct_runs(ref_id,fidelity,on_version) VALUES (%s,'ml',1) RETURNING id",
            (foreign.id,),
        ).fetchone()
        assert row is not None
        foreign_run = row[0]
    snap = read_snapshot(store, ref.id, selected_run_id=ids[0], history_limit=1)
    assert snap["run_window"]["truncated"] and snap["run_window"]["returned"] == 1
    assert snap["selected_run"]["record"]["run_id"] == ids[0]
    assert completion_envelope(snap)["selected_evidence"] is None
    assert snap["runs"][0]["receipt_assessment"]["status"] == "unavailable"
    assert (
        read_snapshot(store, ref.id, selected_run_id=foreign_run)["selected_run"][
            "status"
        ]
        == "wrong-owner"
    )
    with pytest.raises(ValueError):
        read_snapshot(store, ref.id, history_limit=65)
    with pytest.raises(ValueError):
        read_snapshot(store, ref.id, selected_run_id=True)


def test_status_changes_without_geometry_change_and_same_version_rewrite(
    store: Store,
) -> None:
    ref = save_pair(store)
    initial = read_snapshot(store, ref.id)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Fixture",
        parent_id=ref.id,
        meta={"job_type": "struct_relax", "params": {"structure_ref_id": ref.id}},
    )
    after = read_snapshot(store, ref.id)
    assert initial["geometry"] == after["geometry"]
    assert (
        completion_envelope(initial)["status_token"]
        != completion_envelope(after)["status_token"]
    )
    with store.pool.connection() as conn:
        conn.execute("UPDATE struct_atoms SET fa=fa+0.01 WHERE ref_id=%s", (ref.id,))
        conn.execute(
            "UPDATE refs SET meta=meta || %s WHERE ref_id=%s",
            (Jsonb({"failure_reason": "fixture failure"}), job.id),
        )
    edited = read_snapshot(store, ref.id)
    assert (
        edited["geometry"]["identity"]["version"]
        == initial["geometry"]["identity"]["version"]
    )
    assert (
        edited["geometry"]["identity"]["token"]
        != initial["geometry"]["identity"]["token"]
    )


def test_handler_completion_never_hydrates_computes_or_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = save_pair(store)
    handler = StructureHandler(hub=Hub(store=store))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("write/hydration on read")

    for method in (
        "structure_load",
        "structure_save",
        "structure_record_run",
        "insert_ref",
    ):
        monkeypatch.setattr(store, method, forbidden)
    result = loads_wire(handler.get(id=ref.id, view="completion").body)
    assert result["state"] == "unknown" and result["next"]["can_submit"] is False
    assert (
        "Submission enabled: false"
        in handler.get(id=ref.id, view="completion_digest").body
    )
    with pytest.raises(BadInput):
        handler.get(view="completion")
    with pytest.raises(BadInput):
        handler.get(id=ref.id, view="completion", args={"policy": {}})
    with pytest.raises(BadInput):
        handler.get(id=ref.id, view="completion", args={"selected_run_id": 12})
    selected = loads_wire(
        handler.get(
            id=ref.id, view="completion", args={"selected_run_id": to_wire(123456)}
        ).body
    )
    assert selected["selected_run"]["status"] == "missing"
    with pytest.raises(BadInput):
        handler.get(id=ref.id, view="completion", source="external")


def test_se_hidden_dangling_empty_and_stale_do_not_vacuously_finish() -> None:
    nested = derive(output_snapshot())
    nested["finished"] = True
    s = blank(contract.SeCompletionSnapshot)
    design = {
        "ref_id": 3,
        "revision": 1,
        "token": "synthetic:design",
        "source": "live-statement",
    }
    s.update(
        design=design,
        latest_design=design,
        complete_inventory=True,
        snapshot_consistency="single-statement",
    )
    block = blank(contract.SeBlockCompletionInput)
    block.update(
        block_uid=1,
        name="hidden",
        applicability="atomistic",
        binding_status="exact",
        binding_token="bound",
        geometry=geometry(),
        resolved_structure_id=geometry()["ref_id"],
        completion=nested,
        hidden=True,
    )
    s["blocks"] = [block]
    out = se_completion_envelope(cast(SeCompletionSnapshot, s))
    assert out["all_atomistic_blocks_finished"] and not out["finished"]
    other = copy.deepcopy(block)
    other.update(
        block_uid=2,
        name="dangling",
        binding_status="dangling",
        geometry=None,
        completion=None,
    )
    s["blocks"].append(other)
    assert not se_completion_envelope(cast(SeCompletionSnapshot, s))[
        "all_atomistic_blocks_finished"
    ]
    assert (
        se_completion_envelope(cast(SeCompletionSnapshot, s), shown_design_token="old")[
            "binding"
        ]
        == "stale"
    )
    s["blocks"] = []
    assert not se_completion_envelope(cast(SeCompletionSnapshot, s))[
        "all_atomistic_blocks_finished"
    ]


def test_se_database_snapshot_and_handler_include_dangling_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis_se import persist
    from precis_se.completion import se_completion_snapshot
    from precis_se.handler import SeHandler

    ref = save_pair(store)
    design = store.insert_ref(kind="se", slug="completion-se", title="SE completion")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO se_blocks(ref_id,uid,name,bound_kind,bound_design) VALUES (%s,1,'bound','structure','completion-read'),(%s,2,'missing','structure','absent')",
            (design.id, design.id),
        )
    captured = se_completion_snapshot(store, design.id)
    assert captured is not None and captured["complete_inventory"]
    assert len(captured["blocks"]) == 2
    assert captured["blocks"][0]["resolved_structure_id"] == ref.id
    assert captured["blocks"][1]["binding_status"] == "dangling"
    result = se_completion_envelope(captured)
    validate(result, contract.SeCompletionEnvelope)
    assert result["counts"]["all_blocks"] == 2
    assert not result["all_atomistic_blocks_finished"] and not result["finished"]

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("completion view must not hydrate a live blocktree")

    monkeypatch.setattr(persist, "load_tree", forbidden)
    handler = SeHandler(hub=Hub(store=store))
    assert (
        loads_wire(handler.get(id=design.id, view="completion").body)["counts"][
            "all_blocks"
        ]
        == 2
    )
    assert (
        "Submission enabled: false"
        in handler.get(id=design.id, view="completion_digest").body
    )
    with pytest.raises(BadInput):
        handler.get(view="completion")
    with pytest.raises(BadInput):
        handler.get(id=design.id, view="completion", args={"state": {}})


def test_coherent_completion_read_survives_geometry_and_run_race(store: Store) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from precis.store._structure_completion import read_completion_snapshot

    ref = save_pair(store)
    initial = read_snapshot(store, ref.id)
    statements = []
    with store.pool.connection() as conn:

        class Cursor:
            def __init__(self, cur: Any) -> None:
                self.cur = cur

            def execute(self, sql: str, params: Any) -> None:
                self.cur.execute(sql, params)
                statements.append(sql)
                with store.pool.connection() as writer:
                    writer.execute(
                        "UPDATE struct_atoms SET fa=fa+0.01 WHERE ref_id=%s", (ref.id,)
                    )
                    writer.execute(
                        "INSERT INTO struct_runs(ref_id,fidelity,on_version) VALUES (%s,'ml',1)",
                        (ref.id,),
                    )

            def fetchone(self) -> Any:
                return self.cur.fetchone()

        class Wrapped:
            @contextmanager
            def cursor(self, **kwargs: Any) -> Iterator[Cursor]:
                with conn.cursor(**kwargs) as cur:
                    yield Cursor(cur)

        captured = read_completion_snapshot(store, ref.id, conn=cast(Any, Wrapped()))
        assert not conn.closed and len(statements) == 1
    latest = read_snapshot(store, ref.id)
    assert captured is not None
    assert captured["geometry"] == initial["geometry"]
    assert captured["runs"] == initial["runs"] == []
    assert latest["geometry"] != captured["geometry"] and len(latest["runs"]) == 1


def test_large_geometry_token_has_independent_bounded_budget() -> None:
    from precis.structure.completion_identity import exact_token

    raw = raw_geometry()
    template = raw["atoms"][0]
    raw["atoms"] = [{**template, "id": i, "label": f"C{i}"} for i in range(1, 5791)]
    captured = geometry_snapshot(raw)
    assert len(captured["atoms"]) == 5790
    assert exact_token("test", {"value": 1}) != exact_token(
        "test", {"value": {"$int": "1"}}
    )


def test_persisted_role_cannot_be_inferred_from_legacy_current_identity(
    store: Store,
) -> None:
    ref = save_pair(store)
    current = read_snapshot(store, ref.id)
    selector = {
        "kind": "persisted",
        "role": "adopted-output",
        "geometry": current["geometry"]["identity"],
        "run_id": None,
    }
    selected = read_snapshot(store, ref.id, requested_display=selector)
    assert (
        selected["geometry"] == current["geometry"]
    )  # independently drawable current rows remain
    assert selected["display_resolution"]["status"] == "unsupported"
    assert selected["display_resolution"]["reason"] == "role-provenance-unavailable"
    assert completion_envelope(selected)["shown_geometry"] is None


def test_policy_request_outside_window_cannot_select_a_different_success() -> None:
    s = output_snapshot()
    p = policy_and_pass(s)
    p["request_id"] = 999
    s["requested_display"]["run_id"] = None
    s["selected_run"] = {
        "requested_run_id": None,
        "status": "not-requested",
        "record": None,
    }
    s["request_window"].update(truncated=True, coverage="bounded")
    out = derive(s, policy=p)
    assert out["state"] == "unknown" and not out["finished"]
    assert out["selected_evidence"] is None and out["earlier_qualified_evidence"]
    assert "policy-request-not-captured" in out["reason_codes"]
    assert not out["earlier_evidence_complete"]


@pytest.mark.parametrize(
    ("coverage", "truncated"),
    [("bounded", True), ("unknown", False), ("complete", True)],
)
def test_automatic_evidence_fallback_requires_complete_request_coverage(
    coverage: str, truncated: bool
) -> None:
    s = output_snapshot()
    p = policy_and_pass(s)
    s["requested_display"]["run_id"] = None
    s["selected_run"] = {
        "requested_run_id": None,
        "status": "not-requested",
        "record": None,
    }
    # An unrelated captured request cannot rule out a newer relevant failure
    # outside this bounded/unknown window.
    unrelated = blank(contract.RequestRecord)
    unrelated.update(request_id=99, owner_ref_id=s["ref_id"] + 1, state="failed")
    s["requests"] = [unrelated]
    s["request_window"].update(returned=1, coverage=coverage, truncated=truncated)
    out = derive(s, policy=p)
    assert out["state"] == "unknown" and not out["finished"]
    assert out["selected_evidence"] is None
    assert [q["run_id"] for q in out["earlier_qualified_evidence"]] == [12]
    assert "request-selection-incomplete" in out["reason_codes"]

    # Exact explicit selection remains useful despite bounded request history.
    s["requested_display"]["run_id"] = 12
    s["selected_run"] = {
        "requested_run_id": 12,
        "status": "found",
        "record": s["runs"][0],
    }
    assert derive(s, policy=p)["finished"]

    # Complete coverage permits the original no-request fallback.
    s["requested_display"]["run_id"] = None
    s["selected_run"].update(requested_run_id=None, status="not-requested", record=None)
    s["request_window"].update(coverage="complete", truncated=False)
    assert derive(s, policy=p)["finished"]


@pytest.mark.parametrize("capture", ["exact", "incomplete", "incoherent", "stale"])
def test_empty_se_applicability_requires_exact_complete_inventory(capture: str) -> None:
    s = blank(contract.SeCompletionSnapshot)
    design = {"ref_id": 3, "revision": 1, "token": "design", "source": "live-statement"}
    s.update(
        design=design,
        latest_design=design,
        complete_inventory=capture != "incomplete",
        snapshot_consistency="unavailable"
        if capture == "incoherent"
        else "single-statement",
    )
    out = se_completion_envelope(
        cast(SeCompletionSnapshot, s),
        shown_design_token="old" if capture == "stale" else None,
    )
    assert out["state"] == ("not-applicable" if capture == "exact" else "unknown")
    assert not out["finished"] and not out["all_atomistic_blocks_finished"]


@pytest.mark.parametrize(
    ("kind", "design", "mode", "expected"),
    [
        (None, None, None, "unknown"),
        (None, None, "future-mode", "unknown"),
        ("cad", None, None, "unknown"),
        ("cad", "declared-cad", None, "non-atomistic"),
        ("component", "declared-component", None, "non-atomistic"),
        ("part", "declared-part", None, "non-atomistic"),
        ("structure", "missing-structure", None, "atomistic"),
        ("cad", "declared-cad", "atomic", "atomistic"),
    ],
)
def test_se_reader_requires_positive_non_atomistic_kind(
    store: Store, kind: str | None, design: str | None, mode: str | None, expected: str
) -> None:
    from precis_se.completion import se_completion_snapshot

    owner = store.insert_ref(kind="se", slug="completion-kind-se", title="Kinds")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO se_blocks(ref_id,uid,name,bound_kind,bound_design,mode) "
            "VALUES (%s,1,'block',%s,%s,%s)",
            (owner.id, kind, design, mode),
        )
    captured = se_completion_snapshot(store, owner.id)
    assert captured is not None
    assert captured["blocks"][0]["applicability"] == expected
    out = se_completion_envelope(captured)
    assert out["state"] == (
        "not-applicable" if expected == "non-atomistic" else "unknown"
    )
    assert not out["all_atomistic_blocks_finished"] and not out["finished"]


def test_unbound_block_prevents_finished_mixed_se_inventory(store: Store) -> None:
    from precis_se.completion import se_completion_snapshot

    save_pair(store)
    owner = store.insert_ref(kind="se", slug="completion-mixed-se", title="Mixed")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO se_blocks(ref_id,uid,name,bound_kind,bound_design) VALUES "
            "(%s,1,'atoms','structure','completion-read'),"
            "(%s,2,'solid','cad','declared-cad'),(%s,3,'unspecified',NULL,NULL)",
            (owner.id, owner.id, owner.id),
        )
    captured = se_completion_snapshot(store, owner.id)
    assert captured is not None
    # Isolate applicability aggregation from the zero-trusted-producer reader:
    # a qualified atomistic result is supplied by the test, never by legacy DB JSON.
    nested = captured["blocks"][0]["completion"]
    assert nested is not None
    nested["state"] = "converged"
    nested["finished"] = True
    out = se_completion_envelope(captured)
    assert out["counts"]["atomistic_blocks_finished"] == 1
    assert out["counts"]["non_atomistic_blocks"] == 1
    assert out["counts"]["unknown_applicability_blocks"] == 1
    assert not out["all_atomistic_blocks_finished"] and not out["finished"]
    captured["blocks"].pop()
    assert se_completion_envelope(captured)["all_atomistic_blocks_finished"]


def test_malformed_cap_receipt_is_not_returned_as_verified() -> None:
    s = output_snapshot()
    run = s["runs"][0]
    term = run["receipt_assessment"]["verified"]["receipt"]["termination"]
    term.update(reason="step-cap", backend_converged=False)
    run["raw_converged"] = False
    term["forces"]["units"] = "wrong-unit"
    out = derive(s)
    assert out["selected_evidence"] is None
    assert out["selected_run"]["record"]["receipt_assessment"]["verified"] is None


def test_unknown_se_applicability_does_not_increment_finished_atomistic_count() -> None:
    nested = derive(output_snapshot())
    nested["finished"] = True
    s = blank(contract.SeCompletionSnapshot)
    design = {"ref_id": 3, "revision": 1, "token": "design", "source": "live-statement"}
    s.update(
        design=design,
        latest_design=design,
        complete_inventory=True,
        snapshot_consistency="single-statement",
    )
    block = blank(contract.SeBlockCompletionInput)
    block.update(
        block_uid=1,
        name="unknown",
        applicability="unknown",
        binding_status="exact",
        binding_token="bound",
        geometry=geometry(),
        resolved_structure_id=geometry()["ref_id"],
        completion=nested,
    )
    s["blocks"] = [block]
    out = se_completion_envelope(cast(SeCompletionSnapshot, s))
    assert (
        out["counts"]["atomistic_blocks_finished"] == 0
        and not out["all_atomistic_blocks_finished"]
    )


def test_postchecks_cannot_borrow_an_unrelated_graph() -> None:
    s = output_snapshot()
    p = policy_and_pass(s)
    assert derive(s, policy=p)["finished"]
    s["runs"][0]["receipt_assessment"]["verified"]["receipt"]["post_checks"][
        "graph_digest"
    ] = "unrelated"
    assert not derive(s, policy=p)["finished"]


@pytest.mark.parametrize(
    "missing", ["source", "mapping_proof", "reuse_mapping_digest", "mapped_atom_count"]
)
def test_cache_reuse_requires_complete_origin(missing: str) -> None:
    s = output_snapshot()
    receipt = s["runs"][0]["receipt_assessment"]["verified"]["receipt"]
    origin = blank(contract.CacheSourceEvidence)
    origin.update(
        ref_id=geometry()["ref_id"],
        run_id=11,
        input=copy.deepcopy(receipt["input"]),
        output=copy.deepcopy(receipt["output"]),
        receipt_digest="synthetic:cache-source",
        producer_proof={**proof(), "bound_receipt_digest": "synthetic:cache-source"},
        output_proof={**proof(), "bound_receipt_digest": "synthetic:cache-source"},
        method=copy.deepcopy(receipt["method"]),
        constraints=copy.deepcopy(receipt["constraints"]),
        termination=copy.deepcopy(receipt["termination"]),
    )
    cache = receipt["cache"]
    cache.update(
        kind="cache-reuse",
        source=origin,
        source_lookup="self-contained",
        mapping_proof={**proof(), "reason": None},
        reuse_mapping_digest="synthetic:mapping",
        mapped_atom_count=receipt["output"]["mapped_atom_count"],
    )
    assert derive(s)["state"] == "converged"
    cache[missing] = None
    assert derive(s)["selected_evidence"] is None


def test_constrained_force_does_not_become_free_convergence() -> None:
    s = output_snapshot()
    receipt = s["runs"][0]["receipt_assessment"]["verified"]["receipt"]
    constraints = receipt["constraints"]
    constraints.update(atoms="constrained", fixed_atom_count=1, fixed_axis_count=1)
    s["geometry"]["atoms"][0]["fixed_mask"] = 1
    term = receipt["termination"]
    term["criterion"]["scope"] = "constraint-projected"
    term["forces"].update(
        criterion_scope="constraint-projected",
        max_projected=term["forces"]["criterion_value"],
        max_raw=12,
    )
    out = derive(s)
    assert out["state"] == "converged" and out["constraints"]["atoms"] == "constrained"
    assert not out["finished"]
    term["criterion"]["comparator"] = "lt"
    assert derive(s)["selected_evidence"] is None  # equality only meets <=


def test_se_binding_and_geometry_race_uses_one_captured_statement(store: Store) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from precis_se.completion import se_completion_snapshot

    ref = save_pair(store)
    design = store.insert_ref(kind="se", slug="completion-race-se", title="SE race")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO se_blocks(ref_id,uid,name,bound_kind,bound_design) VALUES (%s,1,'bound','structure','completion-read')",
            (design.id,),
        )
    initial = se_completion_snapshot(store, design.id)
    statements = []
    with store.pool.connection() as conn:

        class Cursor:
            def __init__(self, cur: Any) -> None:
                self.cur = cur

            def execute(self, sql: str, params: Any) -> None:
                self.cur.execute(sql, params)
                statements.append(sql)
                with store.pool.connection() as writer:
                    writer.execute(
                        "UPDATE struct_atoms SET fa=fa+0.01 WHERE ref_id=%s", (ref.id,)
                    )
                    writer.execute(
                        "UPDATE se_blocks SET bound_design='now-missing' WHERE ref_id=%s",
                        (design.id,),
                    )

            def fetchone(self) -> Any:
                return self.cur.fetchone()

        class Wrapped:
            @contextmanager
            def cursor(self, **kwargs: Any) -> Iterator[Cursor]:
                with conn.cursor(**kwargs) as cur:
                    yield Cursor(cur)

        captured = se_completion_snapshot(store, design.id, conn=cast(Any, Wrapped()))
        assert len(statements) == 1 and not conn.closed
    latest = se_completion_snapshot(store, design.id)
    assert captured == initial
    assert captured is not None and captured["blocks"][0]["binding_status"] == "exact"
    assert latest is not None and latest["blocks"][0]["binding_status"] == "dangling"
    assert latest["design"] != captured["design"]


@pytest.mark.parametrize("field", ["geometry", "role", "run_id"])
def test_explicit_selector_and_resolved_display_must_agree(field: str) -> None:
    s = output_snapshot()
    if field == "geometry":
        s["requested_display"]["geometry"] = {**geometry(), "token": "other"}
    elif field == "role":
        s["display_resolution"]["role"] = "input-request"
    else:
        s["requested_display"]["run_id"] = 999
    out = derive(s)
    assert out["display_resolution"]["status"] == "conflict"
    assert out["selected_evidence"] is None and out["shown_geometry"] is None


def test_historic_evidence_uses_its_own_verified_cell_and_pbc() -> None:
    s = output_snapshot()
    historical = copy.deepcopy(s["geometry"])
    historical["source"] = "verified-historic-snapshot"
    s["selected_geometry"] = historical
    s["geometry"]["identity"] = {
        **geometry(),
        "version": 2,
        "token": "synthetic:current-v2",
    }
    s["geometry"]["pbc"] = [True, True, True]
    s["geometry"]["lattice"] = [[20, 0, 0], [0, 20, 0], [0, 0, 20]]
    assert derive(s)["state"] == "converged"
    s["selected_geometry"] = None
    out = derive(s)
    assert (
        out["binding"] == "historical-unavailable" and out["selected_evidence"] is None
    )
