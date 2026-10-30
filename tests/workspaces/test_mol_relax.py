"""Molecular workspaces slice 3 — ``mol.relax``, the cached collapse check
(docs/backlog/molecular-workspace-code.md §Collapse check, Slices row 3).

Acceptance clauses pinned here:

- a capped finite prism checks ``ok`` under ``uff`` in < 10 s;
- a fixture with a 0.9 Å C-C contact checks ``collapsed`` before step 30 naming
  both maker frames under ``emt`` (its atoms bond); under ``uff`` — which
  repels a non-bonded contact apart and so cannot "collapse" it — and under
  every other rung the verdict is ``collapsed`` from the INPUT: a non-bonded
  contact inside the bond-inference window is a build error, named by both
  atoms' frames (slice 5 follow-up);
- ``uff`` on a ``pbc`` scene refuses; ``auto`` picks ``xtb`` where tblite
  imports, ``emt`` only for a scene with an EMT metal, and refuses an organic
  periodic scene naming ``rung='xtb'`` and the service relax (EMT breaks
  intact C-H bonds);
- ``geo`` refuses ``fmax``;
- an identical second call is a step-cache hit;
- the imported block's ``view='validate'`` shows ``relaxed_by uff ok``.

Plus the early exit, the checkpoint resume, ``raise_if_collapsed`` through
``out.run``'s failure record, the ``relaxed_by`` manifest row and its
importer vet, and the ``uff`` rung itself.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("rdkit")

import precis_geom.assembly as pga
import precis_geom.mol as mol
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.relax import RelaxUnsupported, relax
from precis.structure.scene import Atom, Bond, Scene
from precis_geom import steps, trace
from precis_geom.mol import collapse
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore:steps:UserWarning")


@pytest.fixture(autouse=True)
def cache(tmp_path: Path):
    """Every test gets its own host step cache."""
    steps.configure(tmp_path / "cache")
    try:
        yield tmp_path / "cache"
    finally:
        steps.configure()


@pytest.fixture(scope="module")
def prism() -> Any:
    """The capped (4, 4, 2, '++++++') prism: 600 atoms, 756 bonds."""
    net, _tube = prism_tube(4, 4, 2, "++++++")
    return mol.cap(pga.geom(net, coords=stick(net)))


def _chain(x0: float) -> Scene:
    """Two bonded carbons (1.54 Å) at ``x0`` along x, in a 40 Å box."""
    atoms = {
        f"aC{k + 1}": Atom(
            f"aC{k + 1}", "C", np.array([x0 + 1.54 * k + 10.0, 20.0, 20.0]) / 40.0
        )
        for k in range(2)
    }
    return Scene(
        cell=Cell(np.eye(3) * 40.0, (False, False, False)),
        atoms=atoms,
        bonds=[Bond("aC1", "aC2", 1.0)],
    )


#: Two C2 sticks 0.9 Å apart end to end — each made by its own ``mol.frag``
#: call, so the atoms cite two different script lines. ``# A`` / ``# B``
#: mark them; ``# RELAX`` is the collapse check, ``# BLOCK`` the emit.
_CONTACT = """\
import copy

import numpy as np

import precis_geom.assembly as pga
import precis_geom.mol as mol
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene
from precis_geom import out


def chain(x0):
    atoms = {
        f"aC{k + 1}": Atom(f"aC{k + 1}", "C", np.array([x0 + 1.54 * k + 10.0, 20, 20]) / 40.0)
        for k in range(2)
    }
    cell = Cell(np.eye(3) * 40.0, (False, False, False))
    return Scene(cell=cell, atoms=atoms, bonds=[Bond("aC1", "aC2", 1.0)])


def merged(a, b):
    sc = copy.deepcopy(a.scene)
    for la, at in b.scene.atoms.items():
        sc.atoms["b" + la] = Atom("b" + la, at.element, at.frac.copy(), maker=at.maker)
    sc.bonds.extend(Bond("b" + x.i, "b" + x.j, x.order) for x in b.scene.bonds)
    return pga.geom(sc)


def build(gap):
    a = mol.frag(scene=chain(0.0), ports={})  # A
    b = mol.frag(scene=chain(1.54 + gap), ports={})  # B
    return merged(a, b)


def main(out_dir, rung="emt", gap=0.9, **kw):
    with out.run(out_dir) as o:
        g = build(gap)
        g, cc = mol.relax(g, rung, **kw)  # RELAX
        cc.raise_if_collapsed()
        o.block("pair", g)  # BLOCK
"""


def _line(tag: str) -> int:
    return next(
        i for i, ln in enumerate(_CONTACT.splitlines(), 1) if ln.endswith(f"# {tag}")
    )


# ── the acceptance ──────────────────────────────────────────────────────


def test_capped_prism_checks_ok_under_uff_in_under_ten_seconds(prism: Any) -> None:
    t0 = time.perf_counter()
    relaxed, cc = mol.relax(prism)  # rung='auto': a finite scene -> uff
    wall = time.perf_counter() - t0
    assert wall < 10.0, f"{wall:.1f} s"
    assert (cc.rung, cc.verdict) == ("uff", "ok"), cc.text()
    assert (len(cc.broken), len(cc.formed), cc.rings_lost) == (0, 0, 0)
    assert cc.fragments == (1, 1)
    assert 0.0 < cc.max_disp_A < collapse.MAX_DISP_A
    assert cc.energy_drop is not None and cc.energy_drop > 0
    assert cc.steps == 200 and cc.early_exit is None
    # relax moves atoms, it never re-makes them
    before = prism.flatten()
    assert [a.maker for a in relaxed.scene.atoms.values()] == [
        a.maker for a in before.atoms.values()
    ]
    assert list(relaxed.scene.atoms) == list(before.atoms)
    assert relaxed.scene.bonds == before.bonds  # the declared graph is untouched
    assert relaxed.meta["relaxed_by"]["rung"] == "uff"


def test_contact_collapses_under_emt_before_step_30_naming_both_frames(
    tmp_path: Path,
) -> None:
    mod = load_script(tmp_path, _CONTACT)
    out_dir = tmp_path / "out" / "r"
    with pytest.raises(collapse.RelaxCollapsed) as ei:
        mod.main(out_dir, "emt")
    cc = ei.value.check
    assert cc is not None and cc.verdict == "collapsed" and cc.rung == "emt"
    assert cc.steps < 30
    assert [(c.a, c.b) for c in cc.formed] == [("aC2", "baC1")]
    assert (cc.formed[0].d_before, cc.formed[0].at_start) == (0.9, True)
    assert cc.formed[0].d_after < 1.8  # inside the bond cutoff: a bond formed
    assert (cc.fragments, cc.rings_lost) == ((2, 1), 0)
    a, b = _line("A"), _line("B")
    text = str(ei.value)
    assert f"main.py:{a}" in text and f"main.py:{b}" in text, text
    assert "formed aC2–baC1 (0.90 →" in text
    # out.run's except path recorded the failed run, message naming the frames
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["status"] == "failed"
    assert man["error"]["type"] == "RelaxCollapsed"
    assert man["error"]["code"] == "relax.collapsed"
    assert f"main.py:{a}" in man["error"]["message"]


def test_input_contact_is_a_build_error_under_every_rung(tmp_path: Path) -> None:
    """A non-bonded 0.9 Å contact in the INPUT collapses the verdict whatever
    the rung does with it: UFF repels it apart (so the end graph "holds"),
    ``geo`` cannot touch the graph — the contact is still the build error,
    named by both atoms' frames."""
    mod = load_script(tmp_path, _CONTACT)
    a, b = _line("A"), _line("B")
    with trace.run(work_root=tmp_path):
        g = mod.build(0.9)
        for rung in ("uff", "geo", "emt"):
            _r, cc = mol.relax(g, rung)
            assert cc.verdict == "collapsed", (rung, cc.text())
            assert cc.rung == rung
            [c] = cc.input_contacts
            assert (c.kind, c.a, c.b, c.d_before) == ("contact", "aC2", "baC1", 0.9)
            assert f"main.py:{a}" in c.frames and f"main.py:{b}" in c.frames, c.frames
            assert "non-bonded contact(s) in the input geometry" in cc.reasons[0]
            assert f"main.py:{a}" in cc.text() and "build error" in cc.summary()
            with pytest.raises(collapse.RelaxCollapsed, match="input geometry"):
                cc.raise_if_collapsed()
    # uff really did push the pair apart (it is the input that is wrong)
    _r, cc = mol.relax(g, "uff")
    assert not cc.formed and not cc.broken and cc.input_mismatch == 1
    assert 0.5 < cc.max_disp_A < collapse.MAX_DISP_A
    # the same pair 3.2 Å apart (outside the bond window) checks ok
    _r, fine = mol.relax(mod.build(3.2), "uff")
    assert fine.verdict == "ok" and fine.input_contacts == ()


def test_input_contact_fails_the_run_naming_both_frames_under_uff(
    tmp_path: Path,
) -> None:
    mod = load_script(tmp_path, _CONTACT)
    out_dir = tmp_path / "out" / "r"
    with pytest.raises(collapse.RelaxCollapsed) as ei:
        mod.main(out_dir, "uff")
    a, b = _line("A"), _line("B")
    assert f"main.py:{a}" in str(ei.value) and f"main.py:{b}" in str(ei.value)
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["status"] == "failed" and man["error"]["code"] == "relax.collapsed"


def test_displacement_bound_is_a_verdict_of_its_own(prism: Any) -> None:
    _r, tight = mol.relax(prism, "uff", max_disp=0.5)
    assert tight.verdict == "collapsed" and not tight.broken
    assert not tight.input_contacts
    assert "max displacement" in tight.summary()
    with pytest.raises(collapse.RelaxCollapsed, match="max displacement"):
        tight.raise_if_collapsed()


def test_ring_census_does_not_depend_on_bond_order(prism: Any) -> None:
    """``probe.rings`` breaks ties between equal-length rings by set
    iteration order (PYTHONHASHSEED-dependent), which once made the prism's
    census differ between the declared and the perceived graph and called
    an intact prism ``2 ring(s) lost``. The check's own census is
    order-free."""
    import dataclasses
    import random

    sc = prism.flatten()
    rings = collapse._rings(sc)
    assert len(rings) > 100 and max(len(r) for r in rings) == 8
    bonds = list(sc.bonds)
    random.Random(7).shuffle(bonds)
    assert collapse._rings(dataclasses.replace(sc, bonds=bonds)) == rings
    assert collapse._rings(dataclasses.replace(sc, bonds=bonds[::-1])) == rings


def test_emt_collapse_on_the_prism_exits_early_and_names_the_bonds(prism: Any) -> None:
    t0 = time.perf_counter()
    _r, cc = mol.relax(prism, "emt")
    wall = time.perf_counter() - t0
    assert cc.verdict == "collapsed"
    assert cc.early_exit == cc.steps == 20  # first probe after step 10 saw nothing new
    assert cc.steps < 200 and wall < 20.0
    # measured: 40 bonds broken (all C-H: EMT lets the cap H go), 12 C...C
    # contacts formed across the pockets, the C skeleton's rings all intact
    assert len(cc.broken) > 0 and len(cc.formed) > 0
    assert all("aH" in c.a + c.b for c in cc.broken)
    assert cc.rings_lost == 0
    assert cc.fragments[1] > cc.fragments[0]
    assert "declared bond(s) broke" in cc.summary()
    assert cc.text().count("\n") >= 4  # the first few changes, "+N more broken"


def _periodic(*elements: str) -> Scene:
    """A short periodic chain of ``elements`` along x, 2.5 Å apart in a
    20 Å box, each bonded to the next (and the last to the first, next cell)."""
    cell = Cell(np.diag([2.5 * len(elements), 20.0, 20.0]), (True, False, False))
    n = len(elements)
    atoms = {
        f"a{el}{k + 1}": Atom(
            f"a{el}{k + 1}", el, np.array([k / n, 0.5, 0.5 + 0.002 * k])
        )
        for k, el in enumerate(elements)
    }
    labels = list(atoms)
    bonds = [Bond(labels[k], labels[k + 1], 1.0) for k in range(n - 1)]
    bonds.append(Bond(labels[-1], labels[0], 1.0, image=(1, 0, 0)))
    return Scene(cell=cell, atoms=atoms, bonds=bonds)


def test_uff_refuses_a_pbc_scene_and_auto_picks_xtb_where_tblite_imports(
    monkeypatch: Any,
) -> None:
    g = pga.geom(_periodic("C", "C"))
    with pytest.raises(RelaxUnsupported) as ei:
        mol.relax(g, "uff")
    assert ei.value.next is not None
    assert "emt" in ei.value.next and "xtb" in ei.value.next
    # tblite importable: auto picks xtb (the rung is the answer; none is run)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    assert collapse._auto_rung(g.scene) == "xtb"


def test_auto_never_falls_back_to_emt_for_an_organic_periodic_scene(
    monkeypatch: Any,
) -> None:
    """Measured: EMT breaks 40 C-H bonds on the intact capped prism. Without
    tblite, ``auto`` on an organic periodic scene refuses naming
    ``rung='xtb'`` and the service relax; it does not pick ``emt``."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    for els in (("C", "C"), ("C", "H"), ("C", "O", "N", "H")):
        with pytest.raises(BadInput) as ei:
            mol.relax(pga.geom(_periodic(*els)))
        err = ei.value
        assert "tblite" in str(err) and "C-H" in str(err)
        assert err.next is not None
        assert "rung='xtb'" in err.next and "relax:" in err.next
        assert "service" in err.next


def test_auto_picks_emt_for_a_periodic_scene_with_an_emt_metal(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    g = pga.geom(_periodic("Cu", "Cu"))
    _r, cc = mol.relax(g)  # auto -> emt: Cu is what EMT is for
    assert cc.rung == "emt"
    assert cc.limits == () and not cc.limited  # a metal-only bond: no organic caveat
    # a metal plus an adsorbate-set element is still EMT's domain ...
    assert collapse._auto_rung(_periodic("Cu", "Cu", "O")) == "emt"
    # ... but a metal next to an element EMT lacks is not
    with pytest.raises(BadInput, match="tblite"):
        collapse._auto_rung(_periodic("Cu", "Si"))


def test_auto_refuses_a_periodic_scene_neither_rung_covers(monkeypatch: Any) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    cell = Cell(np.eye(3) * 10.0, (True, True, True))
    sc = Scene(
        cell=cell,
        atoms={
            "aSi1": Atom("aSi1", "Si", np.array([0.1, 0.1, 0.1])),
            "aSi2": Atom("aSi2", "Si", np.array([0.3, 0.1, 0.1])),
        },
        bonds=[Bond("aSi1", "aSi2", 1.0)],
    )
    with pytest.raises(BadInput) as ei:
        mol.relax(pga.geom(sc))
    assert "xtb" in str(ei.value)
    assert ei.value.next is not None and "rung='xtb'" in ei.value.next


def test_explicit_emt_on_organic_bonds_carries_the_limited_note(prism: Any) -> None:
    """Allowed, but the check says EMT is not an organic force field."""
    _r, cc = mol.relax(prism, "emt")
    assert cc.limited and len(cc.limits) == 1
    assert "metals potential" in cc.limits[0] and "C-H" in cc.limits[0]
    assert "limited: emt is a metals potential" in cc.text()
    # the note rides the cached document, so a hit says it too
    _r2, again = mol.relax(prism, "emt")
    assert again.limits == cc.limits
    # uff on the same scene has no such caveat
    _r3, ok = mol.relax(prism, "uff", steps=20)
    assert ok.limits == () and not ok.limited


def test_geo_refuses_fmax_and_judges_only_displacement_and_rings(prism: Any) -> None:
    with pytest.raises(BadInput, match="fmax"):
        mol.relax(prism, "geo", fmax=0.1)
    _r, cc = mol.relax(prism, "geo")
    assert cc.rung == "geo" and cc.limited and "cannot break a bond" in cc.limits[0]
    assert cc.verdict == "ok" and cc.energy_drop is None
    assert cc.broken == () and cc.formed == () and cc.early_exit is None
    assert cc.max_disp_A < collapse.MAX_DISP_A


def test_identical_second_call_is_a_cache_hit(prism: Any, tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as _:
        r1, c1 = mol.relax(prism, "uff", steps=40)
        tr = trace.current()
        assert tr is not None
        assert [(s["name"], s["hit"]) for s in tr.steps_log][-1] == ("relax", False)
        t0 = time.perf_counter()
        r2, c2 = mol.relax(prism, "uff", steps=40)
        hit_wall = time.perf_counter() - t0
        assert [(s["name"], s["hit"]) for s in tr.steps_log][-1] == ("relax", True)
    assert c2 == c1 and c1.key is not None
    assert hit_wall < 2.0
    assert [a.frac.tolist() for a in r2.scene.atoms.values()] == [
        a.frac.tolist() for a in r1.scene.atoms.values()
    ]
    [row] = steps.ls("relax")
    assert row["hits"] == 1
    # the key carries rung, steps and fmax: any of them misses
    mol.relax(prism, "uff", steps=41)
    mol.relax(prism, "uff", steps=40, fmax=0.05)
    assert len(steps.ls("relax")) == 3


def test_checkpoint_resumes_a_killed_check(
    prism: Any, cache: Path, monkeypatch
) -> None:
    real = collapse._relax.relax

    def dying(scene: Any, **kw: Any) -> Any:
        inner = kw["on_step"]

        def hook(n: int, pos: Any, curve: Any) -> Any:
            stop = inner(n, pos, curve)
            if n == 25:  # a kill between the step-20 checkpoint and step 30
                raise KeyboardInterrupt
            return stop

        return real(scene, **{**kw, "on_step": hook})

    monkeypatch.setattr(collapse._relax, "relax", dying)
    with pytest.raises(KeyboardInterrupt):
        mol.relax(prism, "uff", steps=60)
    [ckpt] = list(cache.glob("*/*/checkpoint.json"))
    assert json.loads(ckpt.read_text(encoding="utf-8"))["done"] == 20
    assert steps.ls("relax") == []  # nothing completed, nothing cached
    monkeypatch.setattr(collapse._relax, "relax", real)
    _r, cc = mol.relax(prism, "uff", steps=60)
    assert cc.resumed_from == 20 and cc.steps == 60
    assert cc.energy_drop is None  # the start energy died with the first run
    assert list(cache.glob("*/*/checkpoint.json")) == []  # completing drops it
    [row] = steps.ls("relax")
    assert row["name"] == "relax"


def test_cache_off_means_no_checkpoint_and_still_runs(prism: Any) -> None:
    steps.configure("off")
    _r, cc = mol.relax(prism, "uff", steps=12)
    assert cc.verdict == "ok" and cc.key is None and cc.resumed_from is None


# ── bad calls ───────────────────────────────────────────────────────────


def test_bad_calls_name_the_way_out(prism: Any) -> None:
    with pytest.raises(BadInput) as ei:
        mol.relax(prism, "mace")
    assert "options" in str(ei.value) and "uff" in str(ei.value)
    with pytest.raises(BadInput, match="steps"):
        mol.relax(prism, steps=0)
    with pytest.raises(BadInput, match="fmax"):
        mol.relax(prism, fmax=-1.0)
    bare = pga.geom(
        Scene(
            cell=Cell(np.eye(3) * 10, (False, False, False)),
            atoms={
                "aC1": Atom("aC1", "C", np.array([0.1, 0.1, 0.1])),
                "aC2": Atom("aC2", "C", np.array([0.2, 0.1, 0.1])),
            },
        )
    )
    with pytest.raises(BadInput, match="no bonds"):
        mol.relax(bare)


# ── the uff rung itself (precis.structure.relax) ────────────────────────


def test_uff_keeps_benzene_planar_and_its_ring_intact() -> None:
    benzene = mol.frag(smiles="c1ccccc1")  # order-1.5 aromatic bonds, sp2 C
    sc = benzene.flatten()
    res = relax(sc, fidelity="uff", steps=200, fmax=0.05)
    assert res.rung == "uff" and res.converged
    assert res.energy_start is not None and res.energy is not None
    # an embedded benzene is at its minimum already: FIRE's first step may
    # even rise a hair — the point is it does not move
    assert abs(res.energy - res.energy_start) < 0.05 and res.max_disp < 0.05
    cart = np.array([sc.cell.frac_to_cart(a.frac) for a in sc.atoms.values()])
    centred = cart - cart.mean(axis=0)
    assert np.linalg.svd(centred, compute_uv=False)[-1] < 0.05  # planar: 3rd sv ~ 0
    cc = [
        float(np.linalg.norm(cart[i] - cart[j]))
        for i, a in enumerate(sc.atoms)
        for j, b in enumerate(sc.atoms)
        if i < j and a.startswith("aC") and b.startswith("aC")
    ]
    ring = sorted(d for d in cc if d < 1.6)
    assert len(ring) == 6 and all(1.36 < d < 1.44 for d in ring), ring


def test_uff_bond_orders_map_hexfold_four_thirds_to_aromatic() -> None:
    from precis.structure.relax import _uff_bond_kind as kind

    assert kind(4.0 / 3.0, "aromatic") == "AROMATIC"
    assert kind(1.0, "aromatic") == "AROMATIC"  # kind wins
    assert [kind(o, "pairwise") for o in (1.0, 1.2, 1.5, 2.0, 3.0)] == [
        "SINGLE",
        "SINGLE",
        "AROMATIC",
        "DOUBLE",
        "TRIPLE",
    ]


def test_uff_on_step_can_stop_the_run_and_fmax_is_honoured() -> None:
    sc = mol.frag(smiles="CCO").flatten()
    seen: list[int] = []

    def hook(n: int, pos: np.ndarray, curve: list[float]) -> bool:
        seen.append(n)
        assert pos.shape == (len(sc.atoms), 3) and len(curve) == n + 1
        return n == 3

    res = relax(sc, fidelity="uff", steps=100, fmax=1e-6, on_step=hook)
    assert seen == [0, 1, 2, 3] and res.n_steps == 3 and not res.converged
    assert res.fmax == 1e-6
    with pytest.raises(RelaxUnsupported, match="fmax"):
        relax(sc, fidelity="geo", fmax=0.1)


# ── relaxed_by: manifest row, importer vet, view='validate' ─────────────

_EMIT = """\
import precis_geom.assembly as pga
import precis_geom.mol as mol
from precis_geom import out


def main(out_dir, moved=False):
    with out.run(out_dir) as o:
        g = mol.frag(smiles="c1ccccc1")
        g, cc = mol.relax(g)
        cc.raise_if_collapsed()
        if moved:
            g = pga.translate(g, [1.0, 0.0, 0.0])
        o.block("ring", g)
        return cc
"""


def _manifest(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def test_block_row_carries_relaxed_by_only_while_the_coordinates_are_relaxed(
    tmp_path: Path,
) -> None:
    mod = load_script(tmp_path, _EMIT)
    cc = mod.main(tmp_path / "out" / "a")
    man = _manifest(tmp_path / "out" / "a")
    assert man["blocks"][0]["relaxed_by"] == {
        "rung": "uff",
        "key": cc.key,
        "verdict": "ok",
    }
    assert cc.key is not None and len(cc.key) == 64
    mod.main(tmp_path / "out" / "b", moved=True)
    man = _manifest(tmp_path / "out" / "b")
    assert "relaxed_by" not in man["blocks"][0]  # moved since: not claimed


def _import(store: Store, out_dir: Path) -> Any:
    if store.get_ref(kind="se", id="relax-ws") is None:
        store.insert_ref(kind="se", slug="relax-ws", title="relax ws", meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    return import_workspace_run(
        store,
        workspace="relax-ws-w",
        design="relax-ws",
        out_dir=out_dir,
        run_job=int(job.id),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        params={},
        requested_by="agent:test",
    )


def test_import_shows_relaxed_by_in_view_validate(store: Store, tmp_path: Path) -> None:
    mod = load_script(tmp_path, _EMIT)
    cc = mod.main(tmp_path / "out" / "r")
    res = _import(store, tmp_path / "out" / "r")
    assert res.imported == ["ring"]
    sref = store.get_ref(kind="structure", id="relax-ws-ring")
    assert sref is not None
    assert sref.meta["generated"]["relaxed_by"] == {
        "rung": "uff",
        "key": cc.key,
        "verdict": "ok",
    }
    body = SeHandler(hub=Hub(store=store)).get(id="relax-ws", view="validate").body
    line = next(ln for ln in body.splitlines() if "relaxed_by" in ln)
    assert line.startswith("ring: relaxed_by uff ok")
    assert cc.key[:12] in line
    assert "not the service relax" in line


def test_importer_vets_the_relaxed_by_row(store: Store, tmp_path: Path) -> None:
    mod = load_script(tmp_path, _EMIT)
    mod.main(tmp_path / "out" / "r")
    path = tmp_path / "out" / "r" / "manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for bad, why in (
        ("uff", "must be an object"),
        ({"rung": "uff"}, "needs 'verdict'"),
        ({"rung": "uff", "verdict": "ok", "extra": 1}, "extra"),
    ):
        doc["blocks"][0]["relaxed_by"] = bad
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(BadInput, match=why):
            _import(store, tmp_path / "out" / "r")
