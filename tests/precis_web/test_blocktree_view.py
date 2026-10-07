"""The ``se`` web reader routes (gr335242 round 1, items 1-3):
envelope-union SVG projection, part isolation, and the stepped
abstraction ladder. Two layers, matching ``test_cad.py``'s own split:
fast FakeStore-backed degradation checks, and a real-store integration
that seeds a small design and exercises the level/isolate/colour params
and the axial-member force overlay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"


def _apply_migrations(store: Any, directory: Path) -> None:
    with store.pool.connection() as c:
        for sql in sorted(directory.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


# ── FakeStore degradation paths ──────────────────────────────────────────


def test_se_list_empty(client: TestClient) -> None:
    r = client.get("/se")
    assert r.status_code == 200
    assert "No se designs yet" in r.text


def test_se_detail_404(client: TestClient) -> None:
    r = client.get("/se/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_se_view_svg_404(client: TestClient) -> None:
    r = client.get("/se/nope/view.svg")
    assert r.status_code == 404


# ── real-store integration ───────────────────────────────────────────────

#: hub—rim tie (an axial connect), plus a 3-level fork/arm/tip subtree —
#: enough structure to exercise the abstraction ladder (fork has kids,
#: fork_arm has kids, fork_tip is a genuine leaf) and part isolation.
_UNICYCLE_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "hub", "pose": [0, 0, 0], "envelope": "cyl:r0.02h0.05"},
    {"op": "add_port", "block": "hub", "name": "pin"},
    {
        "op": "add_block",
        "name": "rim",
        "pose": [0, 0, 0.3],
        "envelope": "torus:R0.3r0.01",
    },
    {"op": "add_port", "block": "rim", "name": "pin"},
    {
        "op": "connect",
        "a": "hub.pin",
        "b": "rim.pin",
        "joint": {
            "class": "axial",
            "params": {"tension_capacity": 100.0, "compression_capacity": 0.0},
        },
    },
    {
        "op": "add_block",
        "name": "fork",
        "pose": [0, 0, -0.1],
        "envelope": "box:w0.04d0.02h0.08",
    },
    {
        "op": "add_block",
        "name": "fork_arm",
        "parent": "fork",
        "pose": [0, 0, -0.15],
        "envelope": "box:w0.01d0.01h0.05",
    },
    {
        "op": "add_block",
        "name": "fork_tip",
        "parent": "fork_arm",
        "pose": [0, 0, -0.18],
        "envelope": "sphere:r0.005",
    },
]


@pytest.fixture
def blocktree_client(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_se(runtime_with_store, slug: str = "unicycle_web") -> None:
    SeHandler(hub=runtime_with_store.hub).put(
        id=slug, text=json.dumps({"ops": _UNICYCLE_OPS})
    )


# ── stability verdict -> header tier ─────────────────────────────────────


def test_stability_tier_confirmed_mechanism_is_error() -> None:
    from precis_web.routes.blocktree_view import _stability_tier

    assert (
        _stability_tier(
            "first-order mobile (1 mechanism(s)) — NOT stabilized: no "
            "self-stress state exists"
        )
        == "error"
    )
    assert (
        _stability_tier(
            "first-order mobile (1 mechanism(s)) — NOT stabilized by the "
            "available self-stress state(s)"
        )
        == "error"
    )
    assert (
        _stability_tier(
            "first-order mobile (1 mechanism(s)) — the self-stress state "
            "is infeasible for the declared members: x.pin—y.pin (tie "
            "asked to carry compression)"
        )
        == "error"
    )


def test_stability_tier_tripwire_is_warn_not_error() -> None:
    from precis_se import stability as se_stability
    from precis_web.routes.blocktree_view import _stability_tier

    assert _stability_tier(se_stability.TRIPWIRE_LINE) == "warn"


def test_stability_tier_rigid_and_prestress_and_no_members_are_ok() -> None:
    from precis_web.routes.blocktree_view import _stability_tier

    assert _stability_tier("rigid (statically determinate)") == "ok"
    assert (
        _stability_tier("rigid (statically indeterminate — 2 self-stress state(s))")
        == "ok"
    )
    assert (
        _stability_tier(
            "prestress-stabilized (1 mechanism(s) stiffened by the "
            "reported self-stress state)"
        )
        == "ok"
    )
    assert (
        _stability_tier("no axial members — stability analysis does not apply") == "ok"
    )


#: An open four-bar (test_se_stability.py's own canonical unstabilizable
#: mechanism) — no self-stress state exists, so classify() returns a
#: CONFIRMED "NOT stabilized" verdict, not the tripwire line.
_OPEN_FOUR_BAR_OPS: list[dict[str, Any]] = [
    *[
        {"op": "add_block", "name": n, "pose": p}
        for n, p in {
            "g0": [0.0, 0.0, 0.0],
            "g1": [1.0, 0.0, 0.0],
            "n2": [1.0, 1.0, 0.0],
            "n3": [0.0, 1.0, 0.0],
        }.items()
    ],
    *[{"op": "add_port", "block": n, "name": "pin"} for n in ("g0", "g1", "n2", "n3")],
    *[
        {
            "op": "connect",
            "a": f"{a}.pin",
            "b": f"{b}.pin",
            "joint": {
                "class": "axial",
                "params": {"tension_capacity": 100.0, "compression_capacity": 100.0},
            },
        }
        for a, b in [("g1", "n2"), ("n2", "n3"), ("n3", "g0")]
    ],
    *[
        {"op": "set_load", "block": n, "fixed": spec}
        for n, spec in {
            "g0": True,
            "g1": True,
            "n2": ["z"],
            "n3": ["z"],
        }.items()
    ],
]


def test_se_view_svg_confirmed_mechanism_is_error_tier_with_full_text(
    blocktree_client, runtime_with_store
) -> None:
    slug = "four_bar_web"
    SeHandler(hub=runtime_with_store.hub).put(
        id=slug, text=json.dumps({"ops": _OPEN_FOUR_BAR_OPS})
    )
    r = blocktree_client.get(f"/se/{slug}/view.svg")
    assert r.status_code == 200
    # the header renders the verdict's FULL text, never a shortened label.
    assert "NOT stabilized: no self-stress state exists" in r.text
    # error tier -> the header banner's red background.
    assert 'fill="#fef2f2"' in r.text


def test_se_list_shows_seeded_design(blocktree_client, runtime_with_store) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se")
    assert r.status_code == 200
    assert "unicycle_web" in r.text


def test_se_detail_renders_selectors_and_image(
    blocktree_client, runtime_with_store
) -> None:
    # gr337745: the 2D SVG reader moved off the bare slug URL to '/2d'.
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/2d")
    assert r.status_code == 200
    assert "/se/unicycle_web/view.svg" in r.text
    # the isolate dropdown lists every block name
    assert "fork_arm" in r.text and "fork_tip" in r.text and "hub" in r.text


def test_se_view_svg_default_renders_all_blocks(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg") or "<svg" in r.text[:40]
    for name in ("hub", "rim", "fork", "fork_arm", "fork_tip"):
        assert f"<title>{name}</title>" in r.text
    # the axial tie between hub and rim renders as a coloured line, tagged
    # with its connect subject — tension role = green.
    assert "hub.pin—rim.pin" in r.text
    assert "#16a34a" in r.text  # tie colour (force_colour's tension green)
    # verdict + honesty header lines, on top
    assert "stability:" in r.text
    assert "validate:" in r.text
    assert "block(s) have envelopes" in r.text


def test_se_view_svg_envelope_level_collapses_subtree(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?level=envelope")
    assert r.status_code == 200
    # fork has children -> collapses to its own box; the descendants never
    # get their own <title> at this level.
    assert "<title>fork</title>" in r.text
    assert "<title>fork_arm</title>" not in r.text
    assert "<title>fork_tip</title>" not in r.text
    # true leaves (no children) always render their own shape regardless
    # of level.
    assert "<title>hub</title>" in r.text
    assert "<title>rim</title>" in r.text


def test_se_view_svg_interfaces_level_reveals_one_more_layer(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?level=interfaces")
    assert r.status_code == 200
    assert "<title>fork</title>" in r.text
    assert "<title>fork_arm</title>" in r.text
    # fork_arm itself has a child (fork_tip) and is now at the cutoff depth
    # -> collapses in turn, one layer further down than 'envelope'.
    assert "<title>fork_tip</title>" not in r.text


def test_se_view_svg_refined_level_shows_every_leaf(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?level=refined")
    assert r.status_code == 200
    for name in ("fork", "fork_arm", "fork_tip"):
        assert f"<title>{name}</title>" in r.text


def test_se_view_svg_isolate_narrows_to_one_subtree(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?level=refined&isolate=fork")
    assert r.status_code == 200
    assert "<title>fork</title>" in r.text
    assert "<title>fork_arm</title>" in r.text
    assert "<title>hub</title>" not in r.text
    assert "<title>rim</title>" not in r.text


def test_se_view_svg_draws_a_cross_design_instance(
    blocktree_client, runtime_with_store
) -> None:
    """A block instanced from ANOTHER design (``'slug#block'``, docs/
    backlog/blocktree-library-build-plan.md slice 1) resolves its envelope
    through the loaded tree's own cross-design resolver — the web reader
    loads a tree straight from ``persist.load_tree``, never through the
    handler, so without that wiring the borrowed block silently draws
    nothing (no envelope → ``build_block_draws`` skips it)."""
    h = SeHandler(hub=runtime_with_store.hub)
    h.put(
        id="web_library",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "part",
                        "envelope": "box:w0.02d0.02h0.02",
                    }
                ]
            }
        ),
    )
    h.put(
        id="web_consumer",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "instance_block",
                        "name": "borrowed",
                        "template": "web_library#part",
                    }
                ]
            }
        ),
    )
    r = blocktree_client.get("/se/web_consumer/view.svg")
    assert r.status_code == 200
    assert "<title>borrowed</title>" in r.text


def test_se_detail_and_view_svg_url_encode_metacharacter_block_names(
    blocktree_client, runtime_with_store
) -> None:
    """A block name isn't URL-safe by construction (only ``#`` and a
    leading ``uid:`` are reserved, ``add_block``'s
    ``_reject_reserved_name``) — ``&``/space in an isolate name must
    not corrupt or truncate the query string the detail page builds, and
    the view.svg route must accept the same encoded name back."""
    slug = "unicycle_meta"
    ops = [
        {
            "op": "add_block",
            "name": "fork & tip",
            "pose": [0, 0, 0],
            "envelope": "box:w0.01d0.01h0.01",
        }
    ]
    SeHandler(hub=runtime_with_store.hub).put(id=slug, text=json.dumps({"ops": ops}))

    # gr337745: the 2D SVG reader moved off the bare slug URL to '/2d'.
    r = blocktree_client.get(f"/se/{slug}/2d?isolate=fork+%26+tip")
    assert r.status_code == 200
    # the <img> src carries a single, correctly percent-encoded isolate
    # param -- never a bare '&' that would split into a second query key.
    assert "isolate=fork%20%26%20tip" in r.text
    assert "isolate=fork & tip" not in r.text

    r2 = blocktree_client.get(f"/se/{slug}/view.svg?isolate=fork%20%26%20tip")
    assert r2.status_code == 200
    # the SVG's own escaping turns the literal '&' into an entity.
    assert "<title>fork &amp; tip</title>" in r2.text


def test_se_view_svg_unknown_isolate_is_400(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?isolate=nope")
    assert r.status_code == 400


@pytest.mark.parametrize("param", ["axis=w", "level=nope", "colour=nope"])
def test_se_view_svg_bad_param_is_400(
    blocktree_client, runtime_with_store, param: str
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get(f"/se/unicycle_web/view.svg?{param}")
    assert r.status_code == 400


def test_se_view_svg_part_colour_groups_by_render_root(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/view.svg?colour=part")
    assert r.status_code == 200
    # three independent root-level groups (hub, rim, fork) -> at least two
    # distinct fill colours among the polygons.
    import re

    fills = set(re.findall(r'fill="(#[0-9a-f]{6})"', r.text))
    assert len(fills) >= 2


# ── round 2a: per-subtree level override (SVG route) ─────────────────────


def test_se_view_svg_overrides_reveals_one_subtree_at_a_different_level(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get(
        "/se/unicycle_web/view.svg?level=envelope&overrides=fork:refined"
    )
    assert r.status_code == 200
    assert "<title>fork</title>" in r.text
    assert "<title>fork_arm</title>" in r.text
    assert "<title>fork_tip</title>" in r.text
    # hub isn't a subtree with children -> unaffected either way.
    assert "<title>hub</title>" in r.text


def test_se_view_svg_overrides_unknown_level_is_400(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get(
        "/se/unicycle_web/view.svg?level=refined&overrides=fork:nope"
    )
    assert r.status_code == 400


def test_se_detail_page_shows_view3d_link_and_overrides_field(
    blocktree_client, runtime_with_store
) -> None:
    # gr337745: the 3D view is now the default landing page (no
    # '/view3d' suffix) — the 2D page's own "3D view" link points there.
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/2d?overrides=fork%3Arefined")
    assert r.status_code == 200
    assert 'href="/se/unicycle_web?' in r.text
    assert "/se/unicycle_web/view3d" not in r.text
    assert 'value="fork:refined"' in r.text


# ── round 2a: three-cad-viewer 3D route (gr337745: now the default) ──────


def test_se_view3d_404(client) -> None:
    # the 2D reader's own 404 path (gr337745 moved it to '/2d').
    r = client.get("/se/nope/2d")
    assert r.status_code == 404


def test_se_scene3d_404(client) -> None:
    r = client.get("/se/nope/scene3d.json")
    assert r.status_code == 404


def test_se_view3d_page_renders(blocktree_client, runtime_with_store) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    assert "/se/unicycle_web/scene3d.json" in r.text
    assert "three-cad-viewer" in r.text


def test_se_view3d_page_carries_svg_fallback_url(
    blocktree_client, runtime_with_store
) -> None:
    # gr462702: the no-WebGL fallback shows the 2D SVG inline.
    import html
    import re

    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    m = re.search(r'data-svg-url="([^"]+)"', r.text)
    assert m, "3D page lacks data-svg-url"
    url = html.unescape(m.group(1))
    assert url.startswith("/se/unicycle_web/view.svg")
    svg = blocktree_client.get(url)
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert 'data-2d-url="/se/unicycle_web/2d' in r.text


def test_se_view3d_scene_controls_are_not_a_submitting_form(
    blocktree_client, runtime_with_store
) -> None:
    """Live-scene slice: ``level``/``isolate``/``overrides`` changed the
    scene by submitting a GET form, which reloaded the page and reset the
    camera. They are now JS-driven, addressed by id. Asserts the ids the
    page hands to ``blocktreeViewer3D`` exist and that no submit button
    is left behind to reload the page out from under the live scene —
    the revision scrubber's own form is a separate control and stays."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    for control_id in ("bt3d-level", "bt3d-isolate", "bt3d-overrides"):
        assert f'id="{control_id}"' in r.text, control_id
        assert f'document.getElementById("{control_id}")' in r.text, control_id
    assert 'type="submit"' not in r.text


def test_se_view3d_ships_a_busy_mark_for_the_refetching_controls(
    blocktree_client, runtime_with_store
) -> None:
    """``level``/``overrides`` refetch and re-render, which on a real
    design is seconds — gr458329 measured ~3.5 s on prod's 20-block
    `unicycle-c1`, and that silence is what made a working swap read as a
    dead control, in a browser probe written to look for exactly this.
    So the page must carry the mark and hand it to the module: hidden at
    rest, shown for the duration, with the two triggering controls
    disabled rather than live over a guard that drops the second change.
    Only the markup contract is checkable here; that the module toggles it
    is browser-level (scripts/viewer_check.py probe)."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    assert 'id="bt3d-busy"' in r.text
    assert 'document.getElementById("bt3d-busy")' in r.text
    # Hidden at rest, in the markup rather than only via script: a mark
    # that ships visible is worse than none.
    busy_tag = r.text.split('id="bt3d-busy"', 1)[1].split(">", 1)[0]
    assert "hidden" in busy_tag, busy_tag


def test_se_view3d_page_carries_the_load_progress_bar(
    blocktree_client, runtime_with_store
) -> None:
    """gr462703: the bar sits above the viewer shell (a sibling, so the
    fallback's ``replaceChildren`` cannot take it), starts on the ``scene``
    phase, announces its label politely, and is handed to the module.
    That the phases advance is browser-level (``viewer_check.py``
    ``progress_phases_in_order``)."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    assert 'id="bt3d-progress"' in r.text
    bar_tag = r.text.split('id="bt3d-progress"', 1)[1].split(">", 1)[0]
    assert 'data-phase="scene"' in bar_tag, bar_tag
    label_tag = r.text.split('id="bt3d-progress-label"', 1)[1].split(">", 1)[0]
    assert 'aria-live="polite"' in label_tag, label_tag
    assert 'document.getElementById("bt3d-progress")' in r.text
    assert r.text.index('id="bt3d-progress"') < r.text.index('id="bt3d-viewer"')


def test_view3d_url_permanently_redirects_to_the_new_default(client) -> None:
    """gr337745 moved the 3D view off ``/se/{slug}/view3d`` onto the
    bare slug URL — the old URL must still resolve via a permanent
    redirect (never a bare 404), query string forwarded unchanged."""
    r = client.get("/se/unicycle_web/view3d?level=envelope", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/se/unicycle_web?level=envelope"


def test_se_scene3d_json_shapes_tree_and_connections(
    blocktree_client, runtime_with_store, store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json")
    assert r.status_code == 200
    body = r.json()

    # viewer-toggles fix (precis_web/blocktree_3d.py's module docstring):
    # the root id is now the design's own kind LABEL (``adapter.label``),
    # not the slug — it must equal the vendored treeview's own root path
    # exactly, byte for byte, or nothing under it resolves in
    # ``nestedGroup.groups``. This supersedes gr338445's old
    # ``/se-<slug>`` scheme (a nicer id than the opaque numeric ref id,
    # but no longer an option once id had to mirror the name chain).
    assert body["shapes"]["id"] == "/Structural envelopes"

    # Every SOLID leaf's own ``id`` is exactly its PARENT's path plus its
    # own ``name`` — no lookup table needed on the client, and (unlike a
    # uid-derived path) it changes on a rename, which is exactly why
    # stable identity moved to the leaf's own explicit ``uid`` field
    # instead. ``fork`` has its own envelope AND visible children
    # (``fork_arm``), so its doubled self-leaf carries the "
    # (envelope)" suffix on BOTH its label and its path's last segment
    # (viewer fix, gr337746 neighbour) — stripped back off here since
    # this check is about the id/name correspondence, not that suffix
    # (covered separately below).
    leaves: dict[str, dict[str, Any]] = {}
    _CONTAINER_SUFFIX = " (envelope)"

    def _walk(node: Any, parent_path: str | None) -> None:
        if "parts" in node:
            for p in node["parts"]:
                _walk(p, node["id"])
        elif node.get("type") == "shapes":
            raw_name = node["name"]  # carries " (envelope)" for a container self-leaf
            stripped = (
                raw_name[: -len(_CONTAINER_SUFFIX)]
                if raw_name.endswith(_CONTAINER_SUFFIX)
                else raw_name
            )
            leaves[stripped] = {
                "id": node["id"],
                "uid": node["uid"],
                # id-mirrors-name invariant: the id's own last segment is
                # the RAW (unstripped) name, parent path plus "/" plus it.
                "expected_id": f"{parent_path}/{raw_name}",
            }

    _walk(body["shapes"], None)
    assert leaves  # at least one leaf rendered
    for name, info in leaves.items():
        assert info["id"] == info["expected_id"], name
    ref = store.get_ref(kind="se", id="unicycle_web")
    assert ref is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, uid FROM se_blocks WHERE ref_id = %s AND retired_at IS NULL",
            (ref.id,),
        ).fetchall()
    uid_by_name = {str(row[0]): int(row[1]) for row in rows}
    # stable identity now lives in the explicit ``uid`` field, not the path.
    assert {name: info["uid"] for name, info in leaves.items()} == {
        name: uid_by_name[name] for name in leaves
    }
    # the hub—rim axial tie is drawn as a connection, labelled by its
    # kinematic class/mechanism (round 2a spec §5.8 comment 5(c)).
    assert len(body["connections"]) == 1
    conn = body["connections"][0]
    assert {conn["a_name"], conn["b_name"]} == {"hub", "rim"}
    assert "B" in body["mermaid"] and "graph LR" in body["mermaid"]
    assert isinstance(body["explode"], dict) and body["explode"]
    # gr340030 — the scale-bar overlay's own conversion factor; this
    # fixture's metre-scale design is already inside the working range, so
    # scene_scale is a near-noop (never a flat 1.0-only assertion — a
    # regression that hardcoded 1.0 would slip past that).
    assert isinstance(body["scale"], (int, float)) and body["scale"] > 0


def test_se_scene3d_json_container_paths_flags_doubled_uid_leaf(
    blocktree_client, runtime_with_store, store
) -> None:
    """Viewer fix (user report against se:unicycle-c1): a block with both
    its own envelope AND visible children — ``fork``, here, which has
    ``fork_arm`` as a visible child — doubles its last path segment
    (``.../<name>/<name> (envelope)``, post viewer-toggles fix — the
    ``precis_web.blocktree_3d`` module docstring's "container leaf"
    convention). That path must be reported in
    ``container_paths`` so the client can default it to translucent, and
    the leaf's own display name must differ from the group's (``"fork
    (envelope)"`` vs ``"fork"``) — otherwise the vendored assembly tree
    shows two indistinguishable rows both labelled "fork", with no way to
    tell which eyeball hides the enclosing box. ``fork_arm`` (nested one
    level deeper) is ALSO a container by the same condition — its own
    envelope plus a visible child, ``fork_tip`` — and a nested container
    occludes its own subtree for exactly the same reason the outermost
    one does, so it must be flagged too. Both self-leaves carry the SAME
    stable ``uid`` as their enclosing group — that's what the revision
    scrubber's ``tint_blocks`` now keys on, not the path."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json")
    assert r.status_code == 200
    body = r.json()

    ref = store.get_ref(kind="se", id="unicycle_web")
    assert ref is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, uid FROM se_blocks WHERE ref_id = %s"
            " AND name IN ('fork', 'fork_arm') AND retired_at IS NULL",
            (ref.id,),
        ).fetchall()
    uids = {str(r[0]): int(r[1]) for r in rows}
    fork_uid = uids["fork"]
    fork_arm_uid = uids["fork_arm"]
    root = "/Structural envelopes"
    fork_group_path = f"{root}/fork"
    fork_self_path = f"{fork_group_path}/fork (envelope)"
    fork_arm_group_path = f"{fork_group_path}/fork_arm"
    fork_arm_self_path = f"{fork_arm_group_path}/fork_arm (envelope)"
    # Order is pre-order (a container's own path is appended before its
    # visible children are walked) — deterministic, so an exact list
    # compare is safe here.
    assert body["container_paths"] == [fork_self_path, fork_arm_self_path]

    def _find(node: Any, node_id: str) -> Any:
        if node.get("id") == node_id:
            return node
        for p in node.get("parts", []):
            found = _find(p, node_id)
            if found is not None:
                return found
        return None

    group = _find(body["shapes"], fork_group_path)
    self_leaf = _find(body["shapes"], fork_self_path)
    assert group is not None and self_leaf is not None
    assert group["name"] == "fork"
    assert self_leaf["name"] == "fork (envelope)"
    assert group["uid"] == fork_uid
    assert self_leaf["uid"] == fork_uid

    fork_arm_self_leaf = _find(body["shapes"], fork_arm_self_path)
    assert fork_arm_self_leaf is not None
    assert fork_arm_self_leaf["uid"] == fork_arm_uid


def test_se_scene3d_json_container_paths_empty_for_a_flat_design(
    blocktree_client, runtime_with_store
) -> None:
    """No block in a flat design (no parent has both its own envelope AND
    a visible child) doubles its path — ``container_paths`` must come
    back empty rather than flagging something that isn't there."""
    SeHandler(hub=runtime_with_store.hub).put(
        id="flat_web", text=json.dumps({"ops": _SOCKET_OPS})
    )
    r = blocktree_client.get("/se/flat_web/scene3d.json")
    assert r.status_code == 200
    assert r.json()["container_paths"] == []


def test_se_scene3d_json_isolate_is_accepted_and_ignored(
    blocktree_client, runtime_with_store
) -> None:
    """Live-scene slice: isolating is a client-side filter over the scene
    the page already holds, so this endpoint always returns the WHOLE
    design. An unknown name used to 400 here; it must now render, since
    the client is what resolves the name and an old bookmark carrying a
    stale one should still show the design."""
    _seed_se(runtime_with_store)
    whole = blocktree_client.get("/se/unicycle_web/scene3d.json")
    assert whole.status_code == 200
    for isolate in ("fork", "nope"):
        r = blocktree_client.get(f"/se/unicycle_web/scene3d.json?isolate={isolate}")
        assert r.status_code == 200, isolate
        assert r.json()["shapes"] == whole.json()["shapes"], isolate


def test_se_scene3d_json_bad_level_is_400(blocktree_client, runtime_with_store) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json?level=nope")
    assert r.status_code == 400


def test_se_scene3d_json_bad_override_level_is_400_clean(
    blocktree_client, runtime_with_store
) -> None:
    """A ``overrides=`` entry naming a real block but an unrecognised
    LEVEL still 400s cleanly through ``_resolve_level_overrides`` — a
    RecursionError/500 here would mean the cycle guard or the plan/error
    plumbing broke, not just this one param."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json?overrides=hub:nope")
    assert r.status_code == 400
    assert "error" in r.json()


def test_se_view3d_hostile_overrides_excluded_from_scene_url(
    blocktree_client, runtime_with_store
) -> None:
    """Reflected-XSS regression: an ``overrides`` pair whose LEVEL half
    is attacker text must never survive into the page's embedded
    ``scene_url`` — if it did, the 3D page's own JS would fetch it,
    ``scene3d.json`` would echo the unrecognised level back verbatim in
    its 400 body, and (pre-fix) the client rendered that text via
    ``innerHTML``. ``_valid_overrides_qs`` drops the whole pair (the
    LEVEL half fails the known-values check) before ``scene_url`` is
    ever built, so neither the raw nor the encoded payload should appear
    anywhere the browser would execute it."""
    from urllib.parse import quote

    _seed_se(runtime_with_store)
    hostile = "<script>alert(1)</script>"
    r = blocktree_client.get(f"/se/unicycle_web?overrides={quote('hub:' + hostile)}")
    assert r.status_code == 200
    scene_url_line = next(line for line in r.text.splitlines() if "sceneUrl" in line)
    assert hostile not in scene_url_line
    assert "alert" not in scene_url_line
    assert "overrides=" not in scene_url_line  # the whole invalid pair was dropped
    assert "scene3d.json" in scene_url_line  # sanity: the right line


# ── topology cloud payload (slice 1 of
#    docs/backlog/se-topology-cloud-and-surface-notes.md) ────────────────


def test_scene3d_carries_topology_nodes(blocktree_client, runtime_with_store) -> None:
    """The cloud replaces the mermaid panel, so ``scene3d.json`` must
    carry the nodes as DATA (ids, parents, detail), not only as a
    ``graph LR`` string."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json")
    assert r.status_code == 200
    body = r.json()
    nodes = {n["name"]: n for n in body["nodes"]}
    assert {"hub", "rim", "fork", "fork_arm", "fork_tip"} <= set(nodes)
    # ids use the SAME B<uid> scheme the mermaid source uses, so the
    # client's id<->3D-path correspondence is unchanged.
    for n in nodes.values():
        assert n["id"].startswith("B")
        assert f"{n['id']}[" in body["mermaid"] or f'{n["id"]}["' in body["mermaid"]
    # containment is carried, so the cloud can draw an opened parent's hull
    assert nodes["fork_arm"]["parent"] == nodes["fork"]["id"]
    assert nodes["fork"]["parent"] is None
    # declared detail rides along; nothing is synthesized for a bare block
    assert "envelope: cyl:r0.02h0.05" in nodes["hub"]["detail"]


def test_scene3d_nodes_carry_level_rungs_and_override(
    blocktree_client, runtime_with_store
) -> None:
    """The per-block level chip's datum: ``level_rungs`` per node, the
    block's own override (or None), and the ambient level."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json?overrides=fork:interfaces")
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "refined"
    nodes = {n["name"]: n for n in body["nodes"]}
    assert nodes["hub"]["level_rungs"] == [True, False, False, False]
    assert nodes["fork_arm"]["level_rungs"] == [True, True, False, False]
    assert nodes["fork"]["level_rungs"] == [True, True, True, False]
    assert nodes["fork"]["level_override"] == "interfaces"
    assert nodes["hub"]["level_override"] is None
    # fork is a shape with a collapsed child under its own interfaces
    # override; fork_arm is a box (envelope relative to itself).
    assert nodes["fork"]["level_active"] == "interfaces"
    assert nodes["fork_arm"]["level_active"] == "envelope"
    assert nodes["hub"]["level_active"] == "refined"


def test_scene3d_level_active_is_relative_to_the_plan(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json?level=interfaces")
    nodes = {n["name"]: n for n in r.json()["nodes"]}
    assert nodes["fork"]["level_active"] == "interfaces"
    assert nodes["fork_arm"]["level_active"] == "envelope"
    r = blocktree_client.get(
        "/se/unicycle_web/scene3d.json?level=interfaces&overrides=fork_arm:refined"
    )
    nodes = {n["name"]: n for n in r.json()["nodes"]}
    assert nodes["fork_arm"]["level_active"] == "refined"
    assert nodes["fork_arm"]["level_override"] == "refined"


def test_se_view3d_keeps_hidden_overrides_input_as_chip_state_carrier(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert 'id="bt3d-overrides"' in r.text
    assert "<div hidden>" in r.text


def test_scene3d_nodes_follow_the_abstraction_ladder(
    blocktree_client, runtime_with_store
) -> None:
    """A collapsed parent is one ``box`` node standing for its subtree —
    the same plan the 3D view renders, not a second visibility rule."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json?level=envelope")
    assert r.status_code == 200
    nodes = {n["name"]: n for n in r.json()["nodes"]}
    assert "fork_arm" not in nodes
    assert nodes["fork"]["kind"] == "box"


def test_scene3d_connections_carry_subject_and_gap(
    blocktree_client, runtime_with_store
) -> None:
    """``subject`` is the join key into ``forces`` — without it the client
    would have to re-parse the human label."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json")
    conn = r.json()["connections"][0]
    assert conn["subject"] == "hub.pin—rim.pin"
    assert conn["subject"] in conn["label"]
    assert isinstance(conn["witness_gap"], (int, float))


def test_scene3d_forces_report_role_without_inventing_numbers(
    blocktree_client, runtime_with_store
) -> None:
    """The seeded design declares no preload, so there is no solved force
    to show: the entry carries the member's role and NO newton figure.
    Honesty rule from the spec — an absent number is reported as absent,
    never as zero."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web/scene3d.json")
    forces = r.json()["forces"]
    entry = forces["hub.pin—rim.pin"]
    assert entry["role"] == "tie"
    assert "declared_n" not in entry
    assert "implied_n" not in entry


def test_scene3d_forces_carry_declared_preload_when_solved(
    blocktree_client, runtime_with_store
) -> None:
    """With a declared preload the prestress solve has real newtons to
    report, and they reach the hover payload."""
    ops = [
        {"op": "add_block", "name": "a", "pose": [0, 0, 0]},
        {"op": "add_block", "name": "b", "pose": [1, 0, 0]},
        {"op": "add_port", "block": "a", "name": "pin"},
        {"op": "add_port", "block": "b", "name": "pin"},
        {
            "op": "connect",
            "a": "a.pin",
            "b": "b.pin",
            "joint": {
                "class": "axial",
                "params": {
                    "tension_capacity": 500.0,
                    "compression_capacity": 0.0,
                    "preload": 120.0,
                },
            },
        },
    ]
    SeHandler(hub=runtime_with_store.hub).put(
        id="preloaded_web", text=json.dumps({"ops": ops})
    )
    r = blocktree_client.get("/se/preloaded_web/scene3d.json")
    assert r.status_code == 200
    entry = r.json()["forces"]["a.pin—b.pin"]
    assert entry["declared_n"] == pytest.approx(120.0)


# ── validator findings, bucketed by block (defect 3, topology cloud
#    legibility pass) ──────────────────────────────────────────────────

#: crown—fork_left, echoing the live unicycle-c1 gap that motivated this:
#: two unconnected ports on ``crown`` (warn), plus an undeclared overlap
#: with ``fork_left`` (also warn) since they share a pose with no connect
#: between them.
_SOCKET_OPS: list[dict[str, Any]] = [
    {
        "op": "add_block",
        "name": "crown",
        "pose": [0, 0, 0],
        "envelope": "box:w0.02d0.02h0.02",
    },
    {"op": "add_port", "block": "crown", "name": "left_socket"},
    {"op": "add_port", "block": "crown", "name": "right_socket"},
    {
        "op": "add_block",
        "name": "fork_left",
        "pose": [0, 0, 0],
        "envelope": "box:w0.02d0.02h0.02",
    },
]


def test_scene3d_findings_bucket_by_block(blocktree_client, runtime_with_store) -> None:
    """The endpoint must carry validator findings, bucketed by the block
    each concerns, or the topology panel has nothing to badge — the exact
    gap ``crown.left_socket``/``crown.right_socket`` exposed live: real
    ``unconnected_port`` warnings the reader showed nothing for."""
    SeHandler(hub=runtime_with_store.hub).put(
        id="socket_web", text=json.dumps({"ops": _SOCKET_OPS})
    )
    r = blocktree_client.get("/se/socket_web/scene3d.json")
    assert r.status_code == 200
    findings = r.json()["findings"]
    crown_unconnected = [
        f for f in findings["crown"] if f["rule"] == "unconnected_port"
    ]
    assert len(crown_unconnected) == 2  # left_socket AND right_socket
    assert all(f["severity"] == "warn" for f in crown_unconnected)
    assert all(set(f) == {"severity", "rule", "detail"} for f in findings["crown"])
    # a pairwise finding (undeclared_interpenetration) is filed under BOTH
    # named blocks, not just the one whose name sorts first.
    assert "undeclared_interpenetration" in {f["rule"] for f in findings["crown"]}
    assert "undeclared_interpenetration" in {f["rule"] for f in findings["fork_left"]}


def test_se_block_findings_and_subject_parsing() -> None:
    """Unit-level coverage of the bucketing helper and its subject parser
    (:mod:`precis_se.validate`'s own subject conventions), off a bare
    :class:`~precis_se.ops.SeTree` — no store, no HTTP."""
    from precis_se.ops import SeTree, apply_ops
    from precis_web.routes.blocktree_view import _se_block_findings, _se_finding_blocks

    names = {"crown", "fork_left"}
    assert _se_finding_blocks("crown.left_socket", names) == ["crown"]
    assert _se_finding_blocks("crown—fork_left", names) == ["crown", "fork_left"]
    assert _se_finding_blocks("crown.a—fork_left.b", names) == ["crown", "fork_left"]
    # the cross_scale_unverifiable aggregate's "N pair(s)" names no real
    # block — dropped rather than guessed at.
    assert _se_finding_blocks("3 pair(s)", names) == []

    tree = apply_ops(SeTree(), _SOCKET_OPS)
    buckets = _se_block_findings(tree)
    assert {"unconnected_port", "undeclared_interpenetration"} <= {
        f["rule"] for f in buckets["crown"]
    }
    assert "undeclared_interpenetration" in {f["rule"] for f in buckets["fork_left"]}


def test_view3d_page_mounts_the_cloud_with_mermaid_as_fallback(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    assert 'id="bt3d-topology"' in r.text
    assert 'topologyEl: document.getElementById("bt3d-topology")' in r.text
    # the mermaid pre survives one release as the fallback, but starts hidden
    wrap = next(
        line for line in r.text.splitlines() if 'id="bt3d-mermaid-wrap"' in line
    )
    assert "hidden" in wrap


# ── comment-on-selection → interview note (slice 2 of
#    docs/backlog/se-topology-cloud-and-surface-notes.md) ────────────────


def _load_notes(store: Any, slug: str = "unicycle_web") -> list[Any]:
    from precis_se import persist as se_persist

    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return se_persist.load_tree(store, ref.id).notes


def test_se_note_save_appends_interview_note(
    blocktree_client, runtime_with_store, store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.post(
        "/se/unicycle_web/note",
        json={
            "text": "Should the hub bore be 17 mm?",
            "kind": "question",
            "block": "hub",
            "verbatim": "hub bore 17mm??",
        },
    )
    assert r.status_code == 200
    name = r.json()["name"]
    assert name.startswith("q-")
    note = next(n for n in _load_notes(store) if n.name == name)
    assert note.kind == "question"
    assert note.origin == "user"
    assert note.about == ["hub"]
    assert note.body.startswith("Should the hub bore be 17 mm?")
    assert "(verbatim: hub bore 17mm??)" in note.body


def test_se_note_save_equal_verbatim_gets_no_trailer(
    blocktree_client, runtime_with_store, store
) -> None:
    """The trailer exists to preserve words the rewrite changed — when
    the user saved their own text untouched there is nothing to
    preserve, and a ``(verbatim: …)`` echo would just be noise."""
    _seed_se(runtime_with_store)
    text = "Use a 17 mm bore for the hub."
    r = blocktree_client.post(
        "/se/unicycle_web/note",
        json={"text": text, "kind": "decision", "block": "hub", "verbatim": text},
    )
    assert r.status_code == 200
    name = r.json()["name"]
    assert name.startswith("d-")
    note = next(n for n in _load_notes(store) if n.name == name)
    assert note.body == text
    assert "(verbatim" not in note.body


def test_se_note_save_dedupes_names(
    blocktree_client, runtime_with_store, store
) -> None:
    _seed_se(runtime_with_store)
    payload = {"text": "Should the rim be wider?", "kind": "question", "block": "rim"}
    first = blocktree_client.post("/se/unicycle_web/note", json=payload)
    second = blocktree_client.post("/se/unicycle_web/note", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    names = {first.json()["name"], second.json()["name"]}
    assert len(names) == 2
    saved = {n.name for n in _load_notes(store)}
    assert names <= saved


def test_se_note_save_rejects_bad_kind(blocktree_client, runtime_with_store) -> None:
    """``answer`` needs a ``re`` target picked from the ledger — the
    viewer comment box only mints question|decision."""
    _seed_se(runtime_with_store)
    r = blocktree_client.post(
        "/se/unicycle_web/note",
        json={"text": "the bore is 17 mm", "kind": "answer", "block": "hub"},
    )
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


def test_se_note_save_rejects_empty_text(blocktree_client, runtime_with_store) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.post("/se/unicycle_web/note", json={"text": "  "})
    assert r.status_code == 400


def test_se_note_routes_404_on_unknown_slug(client: TestClient) -> None:
    assert client.post("/se/nope/note", json={"text": "x"}).status_code == 404
    assert (
        client.post("/se/nope/note/rewrite", json={"comment": "x"}).status_code == 404
    )


def test_se_note_rewrite_returns_proposal(
    blocktree_client, runtime_with_store, monkeypatch
) -> None:
    from precis_web.routes import blocktree_view as btv

    _seed_se(runtime_with_store)
    monkeypatch.setattr(
        btv,
        "_rewrite_note_comment",
        lambda comment, block: {
            "text": f"Should {block}'s bore be 17 mm?",
            "kind": "question",
        },
    )
    r = blocktree_client.post(
        "/se/unicycle_web/note/rewrite",
        json={"comment": "hub bore 17?", "block": "hub"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "text": "Should hub's bore be 17 mm?",
        "kind": "question",
        "degraded": False,
    }


def test_se_note_rewrite_degrades_to_raw_text_on_llm_failure(
    blocktree_client, runtime_with_store, monkeypatch
) -> None:
    """Capture must never block on the model: a failed rewrite comes
    back 200 with the raw words, flagged ``degraded`` so the UI says so
    honestly."""
    from precis_web.routes import blocktree_view as btv

    _seed_se(runtime_with_store)

    def _boom(comment: str, block: str | None) -> dict[str, Any]:
        raise RuntimeError("llm down")

    monkeypatch.setattr(btv, "_rewrite_note_comment", _boom)
    r = blocktree_client.post(
        "/se/unicycle_web/note/rewrite",
        json={"comment": "hub bore 17?", "block": "hub"},
    )
    assert r.status_code == 200
    assert r.json() == {"text": "hub bore 17?", "kind": "question", "degraded": True}


def test_se_note_rewrite_rejects_empty_comment(
    blocktree_client, runtime_with_store
) -> None:
    _seed_se(runtime_with_store)
    r = blocktree_client.post("/se/unicycle_web/note/rewrite", json={"comment": ""})
    assert r.status_code == 400


def test_note_name_slugs_and_dedupes() -> None:
    from precis_web.routes.blocktree_view import _note_name

    assert _note_name("question", "Should the hub bore be 17mm?", set()) == (
        "q-should-the-hub-bore"
    )
    assert _note_name("decision", "Use 17 mm.", set()) == "d-use-17-mm"
    taken = {"q-should-the-hub-bore", "q-should-the-hub-bore-2"}
    assert _note_name("question", "Should the hub bore be 17mm?", taken) == (
        "q-should-the-hub-bore-3"
    )
    assert _note_name("question", "???", set()) == "q-note"


def test_payload_cache_charges_a_payload_at_its_heap_size() -> None:
    """A cached payload dict costs several times its JSON length in memory,
    so the byte cap charges it at that multiple; a body is charged as is."""
    from precis_web.routes import blocktree_view as bv

    payload = {"coords": [[0.1, 0.2, 0.3]] * 10}
    json_len = len(json.dumps(payload, separators=(",", ":")))
    # Room for one payload at its heap charge, many at its JSON length.
    cache = bv._PayloadCache(2 * bv._PAYLOAD_HEAP_PER_JSON_BYTE * json_len - 1)
    cache.put(("a",), payload)
    assert cache.get(("a",)) is payload
    cache.put(("b",), payload)
    assert cache.get(("a",)) is None
    assert cache.get(("b",)) is payload

    cache.put(("body", "e"), (b"x", b"y"), size=2)
    assert cache.get(("body", "e")) is not None


def test_se_view3d_page_shows_the_validate_digest(
    blocktree_client, runtime_with_store
) -> None:
    """gr470909 — the page carries the same one-line validate digest as the
    put/edit reply, with the findings table behind it."""
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
    assert r.status_code == 200
    assert 'id="bt3d-validate"' in r.text
    assert "validate: " in r.text
