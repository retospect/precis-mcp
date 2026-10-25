"""Structure-workspaces slice 2e acceptance — the spatial fisheye.

- ``~p#aC12`` at ``fisheye``: hop-1 bonds with order and Å, hop-2/3
  groups, hop 4–6 counts, the ``Up:`` ladder, ``rings 6,6,6`` and one
  π system whose ``pi:p#0`` selector resolves to its atom count;
- a seam atom two bonds from a unit atom renders ``relax mix:`` + WARN;
- ``~frame:<key>`` at ``fisheye`` shows children and the boundary shell,
  never an atom list;
- benzene: one aromatic 6-ring, RDKit cross-check agrees (no finding);
- pyrrole: verdict stated either way;
- ``pi_key`` changes on a bond-order change while ``structure_sha`` does
  not;
- every rung's token count ≤ budget; linkify covers every ``~`` handle;
- ``+recall`` refuses with ``next=``; the structure eye takes
  ``st<id>~<label>`` and the ``st<id>#<label>`` alias.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.store import Store
from precis.structure.cache import pi_key, structure_sha
from precis.structure.cell import Cell
from precis.structure.scene import Bond, Scene
from precis.utils import handle_registry
from precis.utils.llm.admit import estimate_tokens
from precis_se import datums as se_datums
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "a2_eye" / "out"
SLUG = "hcol-a2"
JUNCTION = "903ec1076846da85"  # schema-2 sha8 recipe key


@pytest.fixture
def se(store: Store) -> SeHandler:
    store.insert_ref(kind="se", slug=SLUG, title="a2 eye", meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    res = import_workspace_run(
        store, workspace="hcol", design=SLUG, out_dir=FIXTURE, run_job=int(job.id)
    )
    assert res.trace is not None
    return SeHandler(hub=Hub(store=store))


def test_atom_fisheye(se: SeHandler) -> None:
    body = se.eye(id=f"{SLUG}~p#aC12", extent="fisheye").body
    # hop 1: 3 bonds, order + Å on every line
    hop1 = [
        l for l in body.splitlines() if l.startswith("  ") and "~" in l and "Å" in l
    ]
    assert len(hop1) == 3
    assert all(re.search(r"1\.5.*\d\.\d\d Å", l) for l in hop1)
    assert "hop 2:" in body and "hop 3:" in body
    assert "hop 4–6:" in body
    up = next(l for l in body.splitlines() if l.startswith("Up:"))
    assert "junction" in up and "frame:" in up
    assert re.search(r"main\.py:\d+", up)
    assert f"block p · design {SLUG}" in up
    assert "rings 6,6,6" in body
    assert f"{SLUG}~pi:p#0" in body


def test_pi_selector_resolves(store: Store, se: SeHandler) -> None:
    ref = store.get_ref(kind="se", id=SLUG)
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    rdat = se_datums.resolve(tree, tree.blocks["p"], "pi:p#0")
    assert rdat.error is None
    assert tree.structure_pi is not None
    data = tree.structure_pi("hcol-a2-p")
    assert data is not None
    sys0 = next(s for s in data["systems"] if s["id"] == 0)
    assert len(rdat.atoms) == len(sys0["atoms"]) == 30


def test_seam_atom_relax_mix_warn(se: SeHandler) -> None:
    body = se.eye(id=f"{SLUG}~p#aC30", extent="fisheye").body
    assert "relax mix:" in body
    assert "gfnff" in body
    assert "WARN mixed methods within 2 bonds" in body
    # per-atom level/frozen stay empty until 4a — the (4a) pending marker
    # is the honest render, not an inferred value
    assert "level — · frozen — (4a)" in body


def test_frame_focus_no_atom_list(se: SeHandler) -> None:
    body = se.eye(id=f"{SLUG}~frame:{JUNCTION}", extent="fisheye").body
    assert "Down:" in body
    assert "shell:" in body
    # the boundary shell carries refocusable handles…
    assert re.search(rf"{SLUG}~[\w:#.()/=-]*aC\d+", body)
    # …never a bare atom list: aC appears only inside ~ handles
    for m in re.finditer(r"aC\d+", body):
        assert "~" in body[: m.start()].rsplit(" ", 1)[-1]


def _smiles_scene(smiles: str) -> Scene:
    from precis.structure import ops as structure_ops

    scene = Scene(Cell.from_lengths_angles(30, 30, 30, pbc=(False, False, False)))
    return structure_ops.apply_ops(scene, [{"op": "from_smiles", "smiles": smiles}])


def _pi_row(store: Store, ref_id: int) -> dict:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT data FROM structure_pi WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def test_benzene_one_aromatic_ring(store: Store) -> None:
    scene = _smiles_scene("c1ccccc1")
    ref, _ = store.structure_save(
        slug="benzene-e2", title="benzene", scene=scene, version=1, card_text="bz"
    )
    data = _pi_row(store, int(ref.id))
    arom = [r for r in data["rings"] if r["aromatic"]]
    assert len(arom) == 1 and len(arom[0]["atoms"]) == 6
    assert data["check"]["rdkit"] == "agrees"
    assert "finding" not in data["check"]


def test_pyrrole_check_recorded(store: Store) -> None:
    pytest.importorskip("rdkit")  # a core dep, but be explicit anyway
    scene = _smiles_scene("c1cc[nH]c1")
    ref, _ = store.structure_save(
        slug="pyrrole-e2", title="pyrrole", scene=scene, version=1, card_text="py"
    )
    check = _pi_row(store, int(ref.id)).get("check") or {}
    assert check["rdkit"] in {"agrees", "differs"}
    if check["rdkit"] == "differs":
        assert check["finding"] == "pi.perception_differs"


def test_pi_key_bond_order_change() -> None:
    scene = Scene(Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)))
    from precis.structure.scene import Atom

    scene.atoms["aC1"] = Atom("aC1", "C", np.array([0.1, 0.1, 0.1]))
    scene.atoms["aC2"] = Atom("aC2", "C", np.array([0.2, 0.1, 0.1]))
    scene.bonds = [Bond(i="aC1", j="aC2", order=1.0)]
    k1, s1 = pi_key(scene), structure_sha(scene)
    scene.bonds = [Bond(i="aC1", j="aC2", order=2.0)]
    assert pi_key(scene) != k1
    assert structure_sha(scene) == s1


@pytest.mark.parametrize(
    "extent,budget",
    [
        # Headroom for CI, which renders 4-5 tokens over local (longer DB ids
        # in a fresh CI db); raised per Reto 2026-10-24 ("raise token budget
        # as needed for this"): summary measured 64 and verbatim ~155 in CI.
        ("kwd", 35),
        ("summary", 70),
        ("verbatim", 175),
        ("fisheye", 420),
        ("fisheye+1hop", 630),
        ("fisheye+2hop", 680),
    ],
)
def test_token_budgets(se: SeHandler, extent: str, budget: int) -> None:
    body = se.eye(id=f"{SLUG}~p#aC12", extent=extent).body
    assert estimate_tokens(len(body)) <= budget, (extent, estimate_tokens(len(body)))


def test_linkify_covers_neighbour_handles(se: SeHandler) -> None:
    from precis_web.routes.eye import linkify_handles

    body = se.eye(id=f"{SLUG}~p#aC12", extent="fisheye").body
    html = linkify_handles(body)
    handles = set(re.findall(rf"{SLUG}~\S+?aC\d+", body))
    assert handles
    for h in handles:
        assert f"/eye/{h}" in html or f"/eye/{h.replace('#', '%23')}" in html


def test_linkify_focus_handles_one_link_no_nested() -> None:
    from precis_web.routes.eye import linkify_handles

    # the base handle inside a focus handle must not become a nested link
    out = linkify_handles("see st7~aC12 next")
    assert out.count("<a") == 1
    assert "/eye/st7~aC12" in out and 'href="/eye/st7"' not in out
    # trailing punctuation is not part of the href
    out = linkify_handles("focus hcol-a2~p#aC12, and more")
    assert "/eye/hcol-a2~p%23aC12?" in out and "aC12%2C" not in out
    # non-focus '#' words and non-handles stay text
    out = linkify_handles("issue#5 c#1 sp2 sp3")
    assert "<a" not in out


def test_route_focus_handle_reaches_dispatch(
    store: Store, se: SeHandler, runtime_with_store: Any
) -> None:
    """``/eye/<slug>~p%23aC12`` resolves through ``_handle_target``'s
    store path and renders the spatial eye (not a query 404)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    client = TestClient(
        create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    )
    page = client.get(f"/eye/{SLUG}~p%23aC12", params={"extent": "fisheye"})
    assert page.status_code == 200, page.text[:400]
    assert "hop 2:" in page.text


def test_digit_focus_is_the_chunk_selector(se: SeHandler, store: Store) -> None:
    """``~<digits>`` stays the chunk selector — a digit focus must take
    the base eye's chunk path, not the atom eye (round-1 review).
    Neither kind has a chunk code, so the honest observable is the
    chunk lookup itself: NotFound naming the missing chunk."""
    from precis.errors import NotFound
    from precis.handlers.structure import StructureHandler

    sh = StructureHandler(hub=Hub(store=store))
    sref = store.get_ref(kind="structure", id="hcol-a2-p")
    assert sref is not None
    st = handle_registry.format_handle("structure", sref.id)
    with pytest.raises(NotFound, match=r"chunk ~3"):
        sh.eye(id=f"{st}~3", extent="kwd")
    with pytest.raises(NotFound, match=r"chunk ~3"):
        se.eye(id=f"{SLUG}~3", extent="kwd")


def test_save_proceeds_without_pi_table(store: Store) -> None:
    """The pre-0238 guard: with ``structure_pi`` renamed away, a save
    still commits — the savepoint confines the UndefinedTable to the
    INSERT (round-1 review: without it the whole save aborted)."""
    with store.pool.connection() as conn:
        conn.execute("ALTER TABLE structure_pi RENAME TO structure_pi_0238x")
    try:
        scene = _smiles_scene("c1ccccc1")
        ref, created = store.structure_save(
            slug="nopi-e2", title="nopi", scene=scene, version=1, card_text="np"
        )
        assert created and store.get_ref(kind="structure", id=ref.id) is not None
    finally:
        with store.pool.connection() as conn:
            conn.execute("ALTER TABLE structure_pi_0238x RENAME TO structure_pi")


def test_recall_refuses_with_next(se: SeHandler) -> None:
    with pytest.raises(Unsupported) as exc:
        se.eye(id=f"{SLUG}~p#aC12", extent="fisheye+recall")
    assert exc.value.next is not None


def test_structure_eye(store: Store, se: SeHandler) -> None:
    from precis.handlers.structure import StructureHandler

    sh = StructureHandler(hub=Hub(store=store))
    sref = store.get_ref(kind="structure", id="hcol-a2-p")
    assert sref is not None
    st = handle_registry.format_handle("structure", sref.id)
    body = sh.eye(id=f"{st}~aC12", extent="kwd").body
    assert "aC12" in body and "bonds" in body
    alias = sh.eye(id=f"{st}#aC12", extent="kwd").body
    assert "aC12" in alias
    bare = sh.eye(id=st, extent="kwd").body
    assert "[structure]" in bare or st in bare


def test_atom_eye_rungs_contain(se: SeHandler) -> None:
    kwd = se.eye(id=f"{SLUG}~p#aC12", extent="kwd").body
    summary = se.eye(id=f"{SLUG}~p#aC12", extent="summary").body
    assert "bonds:" in summary and kwd.splitlines()[0].split(" ·")[0] in summary
