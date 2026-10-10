"""Validation selection uses the loaded snapshot and actual local port anchors."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from fastapi.testclient import TestClient

from precis.errors import NotFound
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se.handler import SeHandler
from precis_se.ops import PortSpec, SeBlock, SeTree, compose_world_pose
from precis_web.app import create_app
from precis_web.blocktree_3d import scene_scale
from precis_web.config import WebConfig
from precis_web.routes import blocktree_view as web
from tests.precis_web.test_blocktree_view import _SE_MIGRATIONS, _apply_migrations
from tests.test_web_se_atomic3d import _seed_atomic_se, _seed_c60_structure


@pytest.fixture
def blocktree_client(store, runtime_with_store, tmp_path):
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def port_tree() -> SeTree:
    tree = SeTree()
    tree.blocks["parent"] = SeBlock(
        name="parent",
        uid=10,
        pose=[6, 2, 0],
        rot=[0, 0, math.pi / 2],
        envelope="sphere:r0.1",
    )
    for i, name in enumerate(["ball12", "ball12r11", "cyl12open"]):
        tree.blocks[name] = SeBlock(
            name=name,
            uid=20 + i,
            parent="parent",
            pose=[2 + i * 3, 0, 0],
            envelope="sphere:r0.5",
            ports={
                "s_rim": PortSpec(name="s_rim", pose=[1, 0, 0], pose_source="declared")
            },
        )
    tree.blocks["cyl12open"].ports["q_out"] = PortSpec(
        name="q_out", pose=[0, 0, 2], pose_source="declared"
    )
    compose_world_pose(tree)
    return tree


@pytest.mark.parametrize(
    "subject,world",
    [
        ("ball12.s_rim", [6, 5, 0]),
        ("ball12r11.s_rim", [6, 8, 0]),
        ("cyl12open.s_rim", [6, 11, 0]),
        ("cyl12open.q_out", [6, 10, 2]),
    ],
)
def test_r5_port_targets_include_parent_rotation(subject, world):
    tree = port_tree()
    original = json.dumps(web.se_persist.tree_to_json(tree), sort_keys=True)
    result = web._validation_targets(tree, subject)
    target = result["targets"][0]
    scale = scene_scale(cast(Any, tree), web._ADAPTERS["se"].effective_envelope)
    assert target["point"] == pytest.approx([v * scale for v in world])
    assert target["uid"] == tree.blocks[target["block"]].uid
    assert target["port"] == subject.rsplit(".", 1)[1]
    assert original == json.dumps(web.se_persist.tree_to_json(tree), sort_keys=True)


def test_exact_subjects_pairs_dotted_names_and_absence():
    tree = port_tree()
    node = tree.blocks.pop("ball12")
    node.name = "ball.12"
    tree.blocks[node.name] = node
    assert web._validation_targets(tree, "ball.12.s_rim")["targets"][0]["uid"] == 20
    pair = web._validation_targets(tree, "ball.12.s_rim—cyl12open.q_out")
    assert [t["uid"] for t in pair["targets"]] == [20, 22]
    assert web._validation_targets(tree, "ball.12")["targets"][0]["port"] is None
    for subject in ["4 pair(s)", "absent.s_rim", "ball.12.missing"]:
        result = web._validation_targets(tree, subject)
        assert result["targets"] == []
        assert result["unavailable"]
    tree.blocks["ball.12"].ports["s_rim"].pose = None
    assert web._validation_targets(tree, "ball.12.s_rim")["targets"] == []
    tree.blocks["cyl12open"].ports["q_out"].pose = [math.nan, 0, 0]
    assert web._validation_targets(tree, "cyl12open.q_out")["targets"] == []
    tree.blocks["cyl12open"].uid = None
    assert web._validation_targets(tree, "cyl12open.s_rim")["targets"] == []
    tree.blocks["ball.12.s_rim"] = SeBlock(
        name="ball.12.s_rim", uid=30, envelope="sphere:r0.1"
    )
    ambiguous = web._validation_targets(tree, "ball.12.s_rim")
    assert ambiguous["targets"] == []
    assert "ambiguous" in ambiguous["unavailable"]


def test_identity_changes_with_version_geometry_port_and_uid():
    tree = port_tree()
    identity = web._validation_identity(tree, 42, 1)
    assert identity == web._validation_identity(tree, 42, 1)
    assert identity != web._validation_identity(tree, 42, 2)
    assert identity != web._validation_identity(tree, 43, 1)
    tree.blocks["ball12"].ports["s_rim"].pose = [0, 1, 0]
    port_changed = web._validation_identity(tree, 42, 1)
    assert port_changed != identity
    tree.blocks["ball12"].uid = 99
    assert web._validation_identity(tree, 42, 1) != port_changed


def test_legacy_bound_port_uses_pinned_atom_and_display_transform(monkeypatch):
    tree = port_tree()
    node = tree.blocks["ball12"]
    node.bound_kind = "structure"
    node.bound = "fragment"
    port = node.ports["s_rim"]
    port.pose = None
    port.bound_design = "fragment"
    port.bound_atom = "aC1"
    ref = SimpleNamespace(id=77, slug="fragment", meta={"version": 3}, updated_at=None)
    cell = Cell.from_lengths_angles(30, 30, 30, pbc=(False, False, False))
    scene = Scene(cell=cell)
    scene.atoms["aC1"] = Atom(
        label="aC1", element="C", frac=cell.cart_to_frac(np.array([10, 20, 30]))
    )
    loads = []

    class BoundStore:
        def structure_load(self, ref_id, *, version):
            loads.append((ref_id, version))
            return scene, {}

    store = cast(Any, BoundStore())
    monkeypatch.setattr(web, "_atomic_struct_ref", lambda _store, _node: ref)
    identity = web._validation_identity(tree, 42, 1, store)
    target = web._validation_targets(tree, "ball12.s_rim", store)["targets"][0]
    scale = scene_scale(cast(Any, tree), web._ADAPTERS["se"].effective_envelope)
    assert target["point"] == pytest.approx(
        np.array([6 - 2e-9, 4 + 1e-9, 3e-9]) * scale, abs=1e-10
    )
    assert loads == [(77, 3)]
    assert target["binding"] == {"ref_id": 77, "version": 3, "updated": None}
    # A stored measured pose must not replace the actual shown atom position.
    port.pose, port.pose_source = [9, 9, 9], "bound"
    assert web._validation_targets(tree, "ball12.s_rim", store)["targets"][0] == target
    monkeypatch.setattr(web, "_atomic_struct_ref", lambda _store, _node: None)
    assert not web._validation_targets(tree, "ball12.s_rim", store)["targets"]
    monkeypatch.setattr(web, "_atomic_struct_ref", lambda _store, _node: ref)
    port.pose = None
    ref.meta["version"] = 4
    assert web._validation_identity(tree, 42, 1, store) != identity
    assert (
        web._validation_targets(tree, "ball12.s_rim", store)["targets"][0]["binding"][
            "version"
        ]
        == 4
    )
    port.bound_atom = "missing"
    assert not web._validation_targets(tree, "ball12.s_rim", store)["targets"]
    port.bound_atom = "aC1"
    port.bound_design = "other"
    assert not web._validation_targets(tree, "ball12.s_rim", store)["targets"]
    port.bound_design = "fragment"
    ref.meta = {}
    assert not web._validation_targets(tree, "ball12.s_rim", store)["targets"]
    ref.meta = {"version": 3}

    def changed_during_load(ref_id, *, version):
        assert ref_id == 77 and version == 3
        ref.meta["version"] = 4
        return scene, {}

    monkeypatch.setattr(store, "structure_load", changed_during_load)
    monkeypatch.setattr(store, "get_ref", lambda **_kwargs: ref, raising=False)
    assert (
        web._build_atomic_block_payload(
            store, node, ref, block_uid=20, name="ball12", scale=scale
        )
        is None
    )
    ref.meta = {"version": 3}

    def absent(*_args, **_kwargs):
        raise NotFound("missing")

    monkeypatch.setattr(store, "structure_load", absent)
    assert not web._validation_targets(tree, "ball12.s_rim", store)["targets"]


def seed_ports(runtime, slug="validation_ports"):
    ops = []
    for i, name in enumerate(["ball12", "ball12r11", "cyl12open"]):
        ops.append(
            {
                "op": "add_block",
                "name": name,
                "pose": [i * 3, 0, 0],
                "envelope": "sphere:r0.5",
            }
        )
        ops.append(
            {"op": "add_port", "block": name, "name": "s_rim", "pose": [0.5, 0, 0]}
        )
    ops.append(
        {"op": "add_port", "block": "cyl12open", "name": "q_out", "pose": [0, 0, 0.5]}
    )
    return SeHandler(hub=runtime.hub).put(id=slug, text=json.dumps({"ops": ops}))


def test_legacy_target_matches_the_rendered_pinned_atomic_payload(
    blocktree_client, runtime_with_store, store, tmp_path
):
    _seed_c60_structure(runtime_with_store, "selection_fragment")
    _seed_atomic_se(
        runtime_with_store, slug="selection_atomic", structure_slug="selection_fragment"
    )
    SeHandler(hub=runtime_with_store.hub).edit(
        id="selection_atomic",
        ops=[
            {"op": "add_port", "block": "hub", "name": "rim"},
            {
                "op": "set_pose",
                "block": "hub",
                "pose": [6e-9, 2e-9, 0],
                "rot": [0, 0, math.pi / 2],
            },
        ],
    )
    ref = web._require_ref(store, "se", "selection_atomic")
    tree = web._ADAPTERS["se"].load_tree(store, ref.id)
    port = tree.blocks["hub"].ports["rim"]
    port.pose = None
    port.bound_design, port.bound_atom = "selection_fragment", "aC1"
    web.se_persist.save_tree(
        store, ref_id=ref.id, tree=tree, card_text="Legacy test fixture"
    )
    page = blocktree_client.get("/se/selection_atomic")
    frag, page_html = _inline_validate_panel(
        blocktree_client, "selection_atomic", page.text
    )
    assert 'data-validation-subject="hub.rim"' in frag.text
    scene = blocktree_client.get("/se/selection_atomic/scene3d.json").json()
    atomic_payload = blocktree_client.get("/se/selection_atomic/atomic3d.json").json()
    atomic = atomic_payload["blocks"][0]
    response = blocktree_client.get(
        "/se/selection_atomic/validation-targets",
        params={"identity": scene["validation_identity"], "subject": "hub.rim"},
    )
    assert response.status_code == 200
    target = response.json()["targets"][0]
    assert target["binding"] == atomic["binding"]
    # The existing render payload rounds scene coordinates; the target
    # retains the actual atom position rather than introducing that loss.
    assert target["point"] == pytest.approx(atomic["coords"][0], abs=1e-4)
    assert target["uid"] == atomic["uid"]
    (tmp_path / "atomic-validation-fixture.html").write_text(
        page_html, encoding="utf-8"
    )
    (tmp_path / "atomic-validation-fixture.json").write_text(
        json.dumps(
            {
                "scene": scene,
                "atomic": atomic_payload,
                "targets": {"hub.rim": response.json()},
            }
        ),
        encoding="utf-8",
    )


def _inline_validate_panel(client, slug, page_html):
    """The page now fetches its validate panel after first paint; the
    standalone browser fixture has no server, so inline the fragment."""
    frag = client.get(f"/se/{slug}/validate-panel")
    assert frag.status_code == 200
    identity = frag.headers["X-Validation-Identity"]
    marker = (
        '<summary class="cursor-pointer text-slate-500">validate: checking…</summary>'
    )
    assert marker in page_html
    html = page_html.replace(marker, frag.text, 1).replace(
        'id="bt3d-validate"', f'id="bt3d-validate" data-identity="{identity}"', 1
    )
    return frag, html


def test_page_and_click_recheck_the_same_snapshot(
    blocktree_client, runtime_with_store, tmp_path, store
):
    seed_ports(runtime_with_store)
    page = blocktree_client.get("/se/validation_ports")
    assert page.status_code == 200
    frag, page_html = _inline_validate_panel(
        blocktree_client, "validation_ports", page.text
    )
    assert frag.text.count('data-validation-subject="') == 4
    assert "0 error(s), 4 warning(s)" in frag.text
    scene = blocktree_client.get("/se/validation_ports/scene3d.json").json()
    identity = scene["validation_identity"]
    assert identity == frag.headers["X-Validation-Identity"]
    url = "/se/validation_ports/validation-targets"
    params = {"identity": identity, "subject": "ball12.s_rim"}
    target = blocktree_client.get(url, params=params)
    assert target.status_code == 200
    assert target.json()["identity"] == identity
    assert target.json()["targets"][0]["port"] == "s_rim"
    assert target.headers["cache-control"] == "no-store"
    fixture_targets = {}
    for subject in [
        "ball12.s_rim",
        "ball12r11.s_rim",
        "cyl12open.q_out",
        "cyl12open.s_rim",
    ]:
        fixture_targets[subject] = blocktree_client.get(
            url, params={**params, "subject": subject}
        ).json()
    # Actual rendered page and endpoint payloads for the standalone browser
    # proof. --basetemp in the slice preserves these fixture-only artifacts.
    (tmp_path / "validation-fixture.html").write_text(page_html, encoding="utf-8")
    (tmp_path / "validation-fixture.json").write_text(
        json.dumps({"scene": scene, "targets": fixture_targets}), encoding="utf-8"
    )
    ref = web._require_ref(store, "se", "validation_ports")
    before = web.se_persist.tree_to_json(web._ADAPTERS["se"].load_tree(store, ref.id))
    with store.pool.connection() as conn:
        jobs_before = conn.execute(
            "SELECT count(*) FROM refs WHERE kind='job'"
        ).fetchone()[0]
    absent = blocktree_client.get(url, params={**params, "subject": "missing.s_rim"})
    assert absent.status_code == 200 and not absent.json()["targets"]
    after = web.se_persist.tree_to_json(web._ADAPTERS["se"].load_tree(store, ref.id))
    assert before == after
    with store.pool.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM refs WHERE kind='job'").fetchone()[0]
            == jobs_before
        )
    SeHandler(hub=runtime_with_store.hub).edit(
        id="validation_ports",
        ops=[{"op": "set_pose", "block": "ball12", "pose": [0, 3, 0]}],
    )
    stale = blocktree_client.get(url, params=params)
    assert stale.status_code == 409
    assert "refresh" in stale.json()["error"]
    historic = blocktree_client.get(url, params={**params, "rev": 1})
    assert historic.status_code == 200
    assert historic.json() == target.json()
    missing_rev = blocktree_client.get(url, params={**params, "rev": 999})
    assert missing_rev.status_code == 404
    assert (
        blocktree_client.get(
            "/se/missing/validation-targets", params=params
        ).status_code
        == 404
    )


def test_click_read_race_refuses_mixed_snapshot(
    monkeypatch, blocktree_client, runtime_with_store
):
    seed_ports(runtime_with_store)
    scene = blocktree_client.get("/se/validation_ports/scene3d.json").json()
    real_axis = web._revision_axis
    calls = 0

    def racing_axis(store, ref_id, rev):
        nonlocal calls
        calls += 1
        axis = real_axis(store, ref_id, rev)
        return replace(axis, current=axis.current + 1) if calls == 2 else axis

    monkeypatch.setattr(web, "_revision_axis", racing_axis)
    response = blocktree_client.get(
        "/se/validation_ports/validation-targets",
        params={
            "identity": scene["validation_identity"],
            "subject": "ball12.s_rim",
        },
    )
    assert response.status_code == 409


def test_panel_preserves_diagnostic_counts_and_details(monkeypatch):
    from precis_se.validate import ValidationIssue

    findings = [
        ValidationIssue(
            rule="unconnected_port",
            subject="ball12.s_rim",
            detail="unreferenced",
            severity="warn",
        ),
        ValidationIssue(
            rule="aggregate", subject="4 pair(s)", detail="unchecked", severity="error"
        ),
    ]
    monkeypatch.setattr(web, "se_validate_findings", lambda *_: findings)
    panel = web._se_validate_panel(cast(Any, None), port_tree(), 42)
    assert "1 error(s), 1 warning(s)" in panel["digest"]
    assert panel["rows"][0]["detail"] == "unreferenced"
    assert panel["rows"][0]["targets"][0]["port"] == "s_rim"
    assert panel["rows"][1]["detail"] == "unchecked"
    assert not panel["rows"][1]["targets"]


def test_page_and_scene_races_disable_identity(
    monkeypatch, blocktree_client, runtime_with_store
):
    seed_ports(runtime_with_store)
    original = web._revision_axis
    calls = 0

    def racing_axis(store, ref_id, rev):
        nonlocal calls
        calls += 1
        axis = original(store, ref_id, rev)
        return replace(axis, current=axis.current + 1) if calls == 2 else axis

    monkeypatch.setattr(web, "_revision_axis", racing_axis)
    response = blocktree_client.get("/se/validation_ports/scene3d.json")
    assert response.status_code == 200
    assert response.json()["validation_identity"] is None
    calls = 0
    frag = blocktree_client.get("/se/validation_ports/validate-panel")
    assert frag.status_code == 200
    assert frag.headers["X-Validation-Identity"] == ""
