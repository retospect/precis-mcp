"""The ``se``/``nm`` web reader routes (gr335242 round 1, items 1-3):
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

import precis_nm
import precis_se
from precis_nm.handler import NmHandler
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"
_NM_MIGRATIONS = Path(precis_nm.__file__).parent / "migrations"


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


def test_nm_list_empty(client: TestClient) -> None:
    r = client.get("/nm")
    assert r.status_code == 200
    assert "No nm designs yet" in r.text


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
    _apply_migrations(store, _NM_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_se(runtime_with_store, slug: str = "unicycle_web") -> None:
    SeHandler(hub=runtime_with_store.hub).put(
        id=slug, text=json.dumps({"ops": _UNICYCLE_OPS})
    )


def _seed_nm(runtime_with_store, slug: str = "rotaxane_web") -> None:
    ops = [
        {"op": "add_block", "name": "axle", "pose": [0, 0, 0], "envelope": "cyl:r2h20"},
        {
            "op": "add_block",
            "name": "ring",
            "parent": "axle",
            "pose": [0, 0, 5],
            "envelope": "torus:R5r1",
        },
    ]
    NmHandler(hub=runtime_with_store.hub).put(id=slug, text=json.dumps({"ops": ops}))


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
    _seed_se(runtime_with_store)
    r = blocktree_client.get("/se/unicycle_web")
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


def test_se_detail_and_view_svg_url_encode_metacharacter_block_names(
    blocktree_client, runtime_with_store
) -> None:
    """A block name isn't URL-safe by construction (only ``#`` is reserved,
    ``add_block``'s ``_reject_hash``) — ``&``/space in an isolate name must
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

    r = blocktree_client.get(f"/se/{slug}?isolate=fork+%26+tip")
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


def test_nm_detail_and_view_svg(blocktree_client, runtime_with_store) -> None:
    _seed_nm(runtime_with_store)
    r = blocktree_client.get("/nm/rotaxane_web")
    assert r.status_code == 200
    assert "/nm/rotaxane_web/view.svg" in r.text

    r2 = blocktree_client.get("/nm/rotaxane_web/view.svg")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/svg+xml")
    assert "<title>axle</title>" in r2.text
    assert "<title>ring</title>" in r2.text
    # nm has no whole-structure stability verdict — only validate + fill.
    assert "stability:" not in r2.text
    assert "validate:" in r2.text
