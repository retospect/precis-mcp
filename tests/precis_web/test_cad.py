"""CAD web editor routes (ADR 0041 web bundle).

Two layers: fast FakeStore-backed degradation checks (empty list, 404, bad
export format), and a real-store integration that seeds a design and exercises
the detail render + glTF model endpoint + apply-derives-new-design flow.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.cad import CadHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_FLANGE = """
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
component ring
rim       add  cyl:r30h4    @0,0,8
"""


# ── FakeStore degradation paths ──────────────────────────────────────────


def test_cad_list_empty(client: TestClient) -> None:
    r = client.get("/cad")
    assert r.status_code == 200
    assert "No cad designs yet" in r.text


def test_cad_detail_404(client: TestClient) -> None:
    r = client.get("/cad/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_cad_export_unknown_format(client: TestClient) -> None:
    r = client.get("/cad/whatever/export.gcode", follow_redirects=False)
    assert r.status_code == 400
    assert "unknown export format" in r.text


def test_drive_default_root_redirects_to_drive(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/drive"


# ── real-store integration ───────────────────────────────────────────────


@pytest.fixture
def cad_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str = "web_flange") -> None:
    CadHandler(hub=runtime_with_store.hub).put(id=slug, text=_FLANGE)


def test_cad_detail_renders_viewer_and_parts(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store)
    r = cad_client.get("/cad/web_flange")
    assert r.status_code == 200
    # the glTF model endpoint the viewer loads
    assert "/cad/web_flange/model.gltf" in r.text
    # per-feature node list + per-part legend (two parts → two colours)
    assert "plate" in r.text and "hub_bore" in r.text and "rim" in r.text
    assert "flange" in r.text and "ring" in r.text
    # download affordances
    assert "export.scad" in r.text


def test_cad_detail_renders_drag_affordances(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_drag")
    r = cad_client.get("/cad/web_drag")
    assert r.status_code == 200
    # legend chips + feature rows are draggable reference tokens
    assert 'data-drag="flange"' in r.text  # a part chip
    assert 'data-drag="plate"' in r.text  # a feature row
    assert 'draggable="true"' in r.text
    # the drop-into-prompt wiring + body pointer-drag are present
    assert "insertToken" in r.text
    assert 'input[name="instruction"]' in r.text


def test_cad_model_gltf_returns_glb(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_glb")
    r = cad_client.get("/cad/web_glb/model.gltf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.content[:4] == b"glTF"  # binary glTF magic


def test_cad_scene_json_serves_recipe(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_scene")
    r = cad_client.get("/cad/web_scene/scene.json")
    assert r.status_code == 200
    body = r.json()
    assert body["components"] == ["flange", "ring"]
    names = {n["name"]: n for n in body["nodes"]}
    assert names["plate"]["shape"]["alias"] == "cyl"
    assert names["plate"]["shape"]["params"]["r"] == 25
    assert names["hub_bore"]["op"] == "cut"
    # every node carries a colour + pose so the browser can build + place it
    assert all("color" in n and "loc" in n and "rot" in n for n in body["nodes"])


def test_cad_detail_viewer_uses_scene_json(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_viewer")
    r = cad_client.get("/cad/web_viewer")
    assert r.status_code == 200
    # the viewer now builds client-side from the recipe + the shared tessellator
    assert "/static/cad-tessellate.js" in r.text
    assert "scene.json" in r.text


def test_cad_analysis_returns_bbox_and_volume(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_analysis")
    r = cad_client.get("/cad/web_analysis/analysis")
    assert r.status_code == 200
    body = r.json()
    assert len(body["bbox"]) == 3
    assert body["volume"] > 0
    assert "warnings" in body


def test_cad_export_scad_downloads(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_scad")
    r = cad_client.get("/cad/web_scad/export.scad")
    assert r.status_code == 200
    assert "cylinder(" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_cad_apply_derives_new_design(
    cad_client, runtime_with_store, monkeypatch
) -> None:
    _seed(runtime_with_store, slug="web_apply")
    store = runtime_with_store.store
    ref = resolve_live_slug_ref(store, kind="cad", id="web_apply")
    # Stand in a finished cad_propose job with a valid rewrite as its job_result.
    import precis_web.routes.cad as cad_routes

    monkeypatch.setattr(
        cad_routes,
        "_proposal_by_job",
        lambda _store, _ref_id, _job_id: {
            "job_id": 1,
            "status": "succeeded",
            "created": "now",
            "proposal": {
                "source": "component flange\nplate add cyl:r40h8",
                "valid": True,
                "rationale": "widen",
            },
        },
    )
    r = cad_client.post(
        "/cad/web_apply/apply",
        data={"to": "web_apply_v2", "job_id": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/cad/web_apply_v2"
    # the derived design exists and is linked derived-from the parent
    child = store.get_ref(kind="cad", id="web_apply_v2")
    assert child is not None
    links = store.links_for(child.id, direction="out", relation="derived-from")
    assert any(lnk.dst_ref_id == ref.id for lnk in links)


def test_cad_apply_soft_deletes_parent_when_checked(
    cad_client, runtime_with_store, monkeypatch
) -> None:
    _seed(runtime_with_store, slug="web_drop")
    store = runtime_with_store.store
    import precis_web.routes.cad as cad_routes

    monkeypatch.setattr(
        cad_routes,
        "_proposal_by_job",
        lambda _s, _r, _j: {
            "job_id": 1,
            "status": "succeeded",
            "created": "now",
            "proposal": {"source": "p add cyl:r5h5", "valid": True, "rationale": "x"},
        },
    )
    r = cad_client.post(
        "/cad/web_drop/apply",
        data={"to": "web_drop_v2", "job_id": "1", "delete_original": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # parent is soft-deleted (no longer resolvable), child lives
    from precis.errors import NotFound

    with pytest.raises(NotFound):
        resolve_live_slug_ref(store, kind="cad", id="web_drop")
    assert store.get_ref(kind="cad", id="web_drop_v2") is not None


def test_cad_apply_in_place_mutates_same_slug(
    cad_client, runtime_with_store, monkeypatch
) -> None:
    """Apply-in-place edits the working part (same slug), mints no new ref."""
    _seed(runtime_with_store, slug="web_live")
    store = runtime_with_store.store
    before = resolve_live_slug_ref(store, kind="cad", id="web_live")
    import precis_web.routes.cad as cad_routes

    monkeypatch.setattr(
        cad_routes,
        "_proposal_by_job",
        lambda _s, _r, _j: {
            "job_id": 7,
            "status": "succeeded",
            "created": "now",
            "proposal": {
                "source": "component flange\nplate add cyl:r99h9",
                "valid": True,
            },
        },
    )
    r = cad_client.post(
        "/cad/web_live/apply_in_place",
        data={"job_id": "7"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/cad/web_live"
    # same slug, same ref id — no new version forked
    after = resolve_live_slug_ref(store, kind="cad", id="web_live")
    assert after.id == before.id
    # no derived child got created
    assert store.get_ref(kind="cad", id="web_live-v2") is None
    # the new geometry is live (the r99 cylinder from the proposal)
    handler = runtime_with_store.hub.handler_for("cad")
    assert "r99" in handler.get(id="web_live").body


def _seed_discuss_job(
    store, cad_ref_id: int, slug: str, instruction: str, answer: str
) -> int:
    """Insert a succeeded cad_discuss job (a discussion turn) for ``cad_ref_id``."""
    from precis.store.types import Tag

    with store.tx() as conn:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="cad_discuss",
            meta={
                "job_type": "cad_discuss",
                "executor": "claude_inproc",
                "params": {
                    "cad_ref_id": cad_ref_id,
                    "slug": slug,
                    "instruction": instruction,
                },
            },
            conn=conn,
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s,'agent',0,'job_result',%s,'{}')",
            (job.id, json.dumps({"answer": answer, "instruction": instruction})),
        )
    store.add_tag(
        job.id,
        Tag.parse_strict("STATUS:succeeded", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    return job.id


def test_cad_thread_returns_turns_oldest_first(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_chat")
    store = runtime_with_store.store
    ref = resolve_live_slug_ref(store, kind="cad", id="web_chat")
    j1 = _seed_discuss_job(store, ref.id, "web_chat", "what is this?", "a flange.")
    j2 = _seed_discuss_job(store, ref.id, "web_chat", "is it one solid?", "yes.")

    r = cad_client.get("/cad/web_chat/thread")
    assert r.status_code == 200
    turns = r.json()["turns"]
    assert [t["job_id"] for t in turns] == [j1, j2]  # oldest first
    assert turns[0]["answer"] == "a flange." and turns[1]["answer"] == "yes."


def test_cad_discuss_post_redirects(cad_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_ask")
    r = cad_client.post(
        "/cad/web_ask/discuss",
        data={"instruction": "why isn't this functional?"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/cad/web_ask#discuss"


def _seed_propose_job(
    store, cad_ref_id: int, slug: str, instruction: str, *, status: str
) -> int:
    """Insert a cad_propose job for ``cad_ref_id`` (a queued job carries no
    job_result chunk; a succeeded one does). Returns the job ref_id."""
    from precis.store.types import Tag

    with store.tx() as conn:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="cad_propose",
            meta={
                "job_type": "cad_propose",
                "executor": "claude_inproc",
                "params": {
                    "cad_ref_id": cad_ref_id,
                    "slug": slug,
                    "instruction": instruction,
                },
            },
            conn=conn,
        )
        if status == "succeeded":
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
                "VALUES (%s,'agent',0,'job_result',%s,'{}')",
                (job.id, json.dumps({"source": "p add cyl:r5h5", "valid": True})),
            )
    store.add_tag(
        job.id,
        Tag.parse_strict(f"STATUS:{status}", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    return job.id


def test_recent_proposals_surfaces_every_outstanding_request(
    cad_client, runtime_with_store
) -> None:
    """The reload bug: two in-flight requests must both survive a reload, not
    just the newest. ``_recent_proposals`` / the /proposals endpoint return all."""
    _seed(runtime_with_store, slug="web_multi")
    store = runtime_with_store.store
    ref = resolve_live_slug_ref(store, kind="cad", id="web_multi")

    j1 = _seed_propose_job(
        store, ref.id, "web_multi", "widen the plate", status="running"
    )
    j2 = _seed_propose_job(
        store, ref.id, "web_multi", "add a bolt circle", status="queued"
    )

    import precis_web.routes.cad as cad_routes

    got = cad_routes._recent_proposals(store, ref.id)
    ids = {p["job_id"] for p in got}
    assert {j1, j2} <= ids  # both outstanding jobs present, not just the newest
    by_id = {p["job_id"]: p for p in got}
    assert by_id[j1]["instruction"] == "widen the plate"
    assert by_id[j2]["instruction"] == "add a bolt circle"

    # the endpoint the box polls returns the same list
    r = cad_client.get("/cad/web_multi/proposals")
    assert r.status_code == 200
    payload_ids = {p["job_id"] for p in r.json()["proposals"]}
    assert {j1, j2} <= payload_ids


def test_proposal_by_job_resolves_the_clicked_card(
    cad_client, runtime_with_store
) -> None:
    """Apply must resolve the specific job the user clicked, not the newest."""
    _seed(runtime_with_store, slug="web_pick")
    store = runtime_with_store.store
    ref = resolve_live_slug_ref(store, kind="cad", id="web_pick")

    j_old = _seed_propose_job(
        store, ref.id, "web_pick", "first idea", status="succeeded"
    )
    _seed_propose_job(store, ref.id, "web_pick", "second idea", status="succeeded")

    import precis_web.routes.cad as cad_routes

    got = cad_routes._proposal_by_job(store, ref.id, j_old)
    assert got is not None
    assert got["job_id"] == j_old
    assert got["instruction"] == "first idea"
