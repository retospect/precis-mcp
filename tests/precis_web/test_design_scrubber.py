"""The revision scrubber — design-workbench build, slice 2, acceptance
**S2 scrubber** (page half; the store half is ``tests/test_design_revisions.py``).

* ``GET /structure/{slug}?rev=N`` renders the scene as of save N (that
  version's atom count), a diff panel against N−1, and exactly revision
  N's recorded ops; a past revision is read-only (no relax / instruct /
  apply forms); ``rev`` outside ``1..current`` is a 404; selecting a
  version never writes.
* ``GET /se/{slug}?rev=N`` renders the ``rev-N`` snapshot; ``scene3d.json
  ?rev=N`` carries ``changed_uids`` = the blocks added/changed at N, and
  tints exactly those leaves.
* A design with no revision rows still renders, as one position —
  "current, no record".

Real Postgres (the ``store`` fixture): the handlers write the versioned
rows the page reads.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis.design import history
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.blocktree_3d import CHANGED_COLOUR
from precis_web.config import WebConfig

from .conftest import FakeRuntime

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"

_PD_OPS: list[dict[str, Any]] = [
    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
    {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
    {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
]
_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": _PD_OPS,
    }
)

#: The edits after the put, in order — v2 adds an atom, v3 moves one (a
#: 0.5 Å cartesian shove, well past the 0.05 Å "moved" floor), v4 removes
#: an atom (and with it the Pd—Pd bond). ``ops[N]`` is revision N's record.
_STRUCTURE_EDITS: dict[int, list[dict[str, Any]]] = {
    2: [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    3: [{"op": "displace", "atom": "aPd1", "vector": [0.5, 0.0, 0.0]}],
    4: [{"op": "vacancy", "atom": "aPd2"}],
}
_ATOM_COUNTS = {1: 2, 2: 3, 3: 3, 4: 2}


def _ref_id(store: Any, kind: str, slug: str) -> int:
    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None
    return int(ref.id)


def _revision_rows(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM design_revisions").fetchone()
    return int(row[0])


def _drop_record(store: Any, ref_id: int) -> None:
    """Make a design look like it was saved before the record existed."""
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM design_revisions WHERE ref_id = %s", (ref_id,))
        conn.commit()


def _panel_ops(page: str, pre_id: str) -> list[dict[str, Any]]:
    """The ops the page's revision panel lists — one JSON object per line
    of the ``<pre id=…>`` block."""
    m = re.search(rf'<pre id="{pre_id}"[^>]*>(.*?)</pre>', page, re.S)
    if m is None:
        return []
    # Jinja autoescapes the quotes inside the <pre>; undo that first.
    text = html.unescape(m.group(1))
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── structure ────────────────────────────────────────────────────────────


def _seed_structure(store: Any, slug: str = "pd_scrub") -> int:
    handler = StructureHandler(hub=Hub(store=store))
    handler.put(id=slug, text=_PD)
    for ops in _STRUCTURE_EDITS.values():
        handler.edit(id=slug, ops=ops)
    ref_id = _ref_id(store, "structure", slug)
    assert store.structure_version(ref_id) == 4
    return ref_id


def _structure_client(store: Any) -> TestClient:
    app = create_app(runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def test_structure_scrubber_renders_each_version_with_its_ops(store) -> None:
    ref_id = _seed_structure(store)
    client = _structure_client(store)
    rows_before = _revision_rows(store)

    for n, n_atoms in _ATOM_COUNTS.items():
        r = client.get(f"/structure/pd_scrub?rev={n}")
        assert r.status_code == 200, n
        # The header's atom count is the scene AT N, not the live one.
        assert f"{n_atoms} atom" in r.text, n
        # The panel lists exactly revision N's recorded ops.
        expected = _PD_OPS if n == 1 else _STRUCTURE_EDITS[n]
        assert _panel_ops(r.text, "sv-revision-ops") == expected, n
        assert history.revision(store, ref_id, n) is not None
        # The slider spans 1..current and sits at N.
        assert 'min="1" max="4"' in r.text
        assert f'value="{n}"' in r.text

    # Selecting a version never writes.
    assert _revision_rows(store) == rows_before


def test_structure_scrubber_diff_panel_says_what_changed(store) -> None:
    _seed_structure(store)
    client = _structure_client(store)

    r1 = client.get("/structure/pd_scrub?rev=1").text
    assert "+2 atoms" in r1 and "+1 bond" in r1  # everything is new at v1

    r2 = client.get("/structure/pd_scrub?rev=2").text
    assert "+1 atom" in r2 and "aO1" in r2
    assert "moved" not in r2.split('id="sv-revision"', 1)[1].split("Energy on", 1)[0]

    r3 = client.get("/structure/pd_scrub?rev=3").text
    panel = r3.split('id="sv-revision"', 1)[1].split("Energy on", 1)[0]
    assert "1 moved" in panel and "aPd1" in panel and "0.500 Å" in panel

    r4 = client.get("/structure/pd_scrub").text  # current = v4, same panel
    panel = r4.split('id="sv-revision"', 1)[1].split("Energy on", 1)[0]
    assert "−1 atom" in panel and "aPd2" in panel and "−1 bond" in panel
    assert "aPd1—aPd2" in panel


def test_structure_scrubber_energy_comes_from_the_run_on_that_version(store) -> None:
    ref_id = _seed_structure(store)
    store.structure_record_run(
        ref_id,
        fidelity="emt",
        on_version=2,
        converged=True,
        n_steps=1,
        energy=-3.25,
        max_force=0.01,
        max_disp=0.0,
        status="succeeded",
    )
    client = _structure_client(store)
    assert "-3.2500" in client.get("/structure/pd_scrub?rev=2").text
    r3 = client.get("/structure/pd_scrub?rev=3").text
    assert "no succeeded run on this version" in r3


def test_structure_past_revision_is_read_only(store) -> None:
    _seed_structure(store)
    client = _structure_client(store)

    past = client.get("/structure/pd_scrub?rev=2").text
    assert 'id="sv-readonly"' in past
    assert "revision 2 of 4" in past
    assert 'action="/structure/pd_scrub/relax"' not in past
    assert 'action="/structure/pd_scrub/instruct"' not in past
    assert 'id="sv-proposal"' not in past  # its poll would render an Apply form

    current = client.get("/structure/pd_scrub?rev=4").text  # explicit current
    assert 'id="sv-readonly"' not in current
    assert 'action="/structure/pd_scrub/relax"' in current
    assert 'action="/structure/pd_scrub/instruct"' in current
    assert 'id="sv-proposal"' in current


def test_structure_rev_outside_range_is_404(store) -> None:
    _seed_structure(store)
    client = _structure_client(store)
    assert client.get("/structure/pd_scrub?rev=5").status_code == 404
    assert client.get("/structure/pd_scrub?rev=0").status_code == 404
    # Junk degrades to the default view (the existing ``run=`` convention).
    assert client.get("/structure/pd_scrub?rev=abc").status_code == 200


def test_structure_without_record_shows_one_position(store) -> None:
    ref_id = _seed_structure(store)
    _drop_record(store, ref_id)
    client = _structure_client(store)
    r = client.get("/structure/pd_scrub")
    assert r.status_code == 200
    assert "current, no record" in r.text
    assert "no op record" in r.text
    assert 'min="4" max="4"' in r.text  # one position


# ── se ───────────────────────────────────────────────────────────────────

_CASTER_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "fork", "envelope": "box:w0.04d0.02h0.08"},
    {
        "op": "add_block",
        "name": "hub",
        "parent": "fork",
        "pose": [0, 0, -0.05],
        "envelope": "cyl:r0.008h0.03",
    },
]
_CAP_OPS: list[dict[str, Any]] = [
    {
        "op": "add_block",
        "name": "cap",
        "parent": "hub",
        "pose": [0, 0, -0.07],
        "envelope": "sphere:r0.005",
    }
]


@pytest.fixture
def se_client(store, runtime_with_store, tmp_path) -> TestClient:
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_se(runtime_with_store, store, slug: str = "caster_scrub") -> int:
    handler = SeHandler(hub=runtime_with_store.hub)
    handler.put(id=slug, text=json.dumps({"ops": _CASTER_OPS}))
    handler.edit(id=slug, ops=_CAP_OPS)
    ref_id = _ref_id(store, "se", slug)
    assert [r.rev for r in history.list_revisions(store, ref_id)] == [1, 2]
    return ref_id


def _uid_by_name(store: Any, ref_id: int) -> dict[str, int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, uid FROM se_blocks WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _leaves(shapes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{block name: leaf}`` for every solid leaf in a Shapes tree."""
    out: dict[str, dict[str, Any]] = {}

    def _walk(node: dict[str, Any]) -> None:
        if "parts" in node:
            for p in node["parts"]:
                _walk(p)
        elif node.get("type") == "shapes":
            out[node["name"]] = node

    _walk(shapes)
    return out


def test_se_scrubber_rev1_renders_the_snapshot_tree(
    se_client, runtime_with_store, store
) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    rows_before = _revision_rows(store)

    page = se_client.get("/se/caster_scrub?rev=1")
    assert page.status_code == 200
    assert 'id="bt3d-readonly"' in page.text
    assert "revision 1 of 2" in page.text
    assert "2 blocks" in page.text  # the snapshot's count, not the live 3
    assert '<option value="cap"' not in page.text  # cap did not exist at 1
    assert 'id="bt3d-note-panel"' not in page.text  # read-only: no note form
    # The scene URL is tojson-embedded, so its "&" is the \u0026 escape.
    assert "/se/caster_scrub/scene3d.json?level=refined\\u0026rev=1" in page.text
    assert _panel_ops(page.text, "bt3d-revision-ops") == _CASTER_OPS

    scene = se_client.get("/se/caster_scrub/scene3d.json?rev=1")
    assert scene.status_code == 200
    body = scene.json()
    leaves = _leaves(body["shapes"])
    assert set(leaves) == {"fork", "hub"}
    # At the first save everything is new — both blocks are "changed".
    uids = _uid_by_name(store, ref_id)
    assert sorted(body["changed_uids"]) == sorted([uids["fork"], uids["hub"]])
    assert _revision_rows(store) == rows_before


def test_se_scrubber_rev2_changed_uids_are_the_added_block(
    se_client, runtime_with_store, store
) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    uids = _uid_by_name(store, ref_id)

    body = se_client.get("/se/caster_scrub/scene3d.json?rev=2").json()
    assert body["changed_uids"] == [uids["cap"]]
    leaves = _leaves(body["shapes"])
    assert set(leaves) == {"fork", "hub", "cap"}
    # Exactly the changed block is tinted.
    assert leaves["cap"]["color"] == CHANGED_COLOUR
    assert leaves["fork"]["color"] != CHANGED_COLOUR
    assert leaves["hub"]["color"] != CHANGED_COLOUR

    # The bare scene (no rev) is the live tree, untinted, as before.
    bare = se_client.get("/se/caster_scrub/scene3d.json").json()
    assert bare["changed_uids"] == []
    assert all(
        leaf["color"] != CHANGED_COLOUR for leaf in _leaves(bare["shapes"]).values()
    )

    page = se_client.get("/se/caster_scrub?rev=2").text
    assert 'id="bt3d-readonly"' not in page  # rev 2 IS current: editable
    assert 'id="bt3d-note-panel"' in page
    assert "+1 block" in page and "cap" in page
    assert _panel_ops(page, "bt3d-revision-ops") == _CAP_OPS


def test_se_scrubber_flags_a_moved_block(se_client, runtime_with_store, store) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    SeHandler(hub=runtime_with_store.hub).edit(
        id="caster_scrub",
        ops=[{"op": "set_pose", "block": "cap", "pose": [0, 0, -0.09]}],
    )
    uids = _uid_by_name(store, ref_id)
    body = se_client.get("/se/caster_scrub/scene3d.json?rev=3").json()
    assert body["changed_uids"] == [uids["cap"]]
    page = se_client.get("/se/caster_scrub?rev=3").text
    assert "1 changed" in page and "(pose)" in page


def test_se_rev_outside_range_is_404(se_client, runtime_with_store, store) -> None:
    _seed_se(runtime_with_store, store)
    assert se_client.get("/se/caster_scrub?rev=3").status_code == 404
    assert se_client.get("/se/caster_scrub?rev=0").status_code == 404
    assert se_client.get("/se/caster_scrub/scene3d.json?rev=3").status_code == 404


def test_se_without_record_still_renders(se_client, runtime_with_store, store) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    _drop_record(store, ref_id)
    page = se_client.get("/se/caster_scrub")
    assert page.status_code == 200
    assert "current, no record" in page.text
    assert "no op record" in page.text
    assert 'min="1" max="1"' in page.text  # one position
    # And the scene at that one position is the live tree.
    body = se_client.get("/se/caster_scrub/scene3d.json?rev=1").json()
    assert set(_leaves(body["shapes"])) == {"fork", "hub", "cap"}
    assert body["changed_uids"] == []
