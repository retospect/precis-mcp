"""``join`` on the geo rung (slice 2, `docs/backlog/hexfold-integration.md`
step 5, `precis_se.atomic.join`'s `geo_relax_pinned`/`_select_relaxer`):
the same "reproduce a whole-spec fuse" contract `tests/test_se_join.py`
pins for the stick rung, now with both parts relaxed on ``geo``
(`precis.structure.georelax.relax_graph`) before the join, checked
against the SAME localisation thresholds `tests/test_hexfold_seam_decay.py`
measures (0.002 A / 0.15 deg) -- not the stick-rung numbers, which are
measured on genuinely different physics (a pinned guard band vs. a fully
free relax; see `hexfold.join.LEAK_THRESH_GEO`'s own docstring).

``relax_graph``'s O(N^2) python loop (module docstring's own "Risks") is
the whole reason this file is `pytest.mark.slow`: the tube has to be long
enough (`len=4`, 128 atoms/side) to leave atoms beyond the table radius's
guard band (shell > 10) for the localisation check to mean anything --
shorter tubes were measured to top out at shell 10 exactly, leaving
nothing "beyond" to compare (`tests/hexfold/test_join.py`'s own len=10
comment makes the same point for the stick rung's sub-graph-size test).
The mixed-rung/forced-rung tests below need none of that physics, so they
use a `len=1` tube (32 atoms, sub-second relax) instead.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from hexfold.build import build
from hexfold.join import SEAM_RADIUS, _adjacency, _angles_at, _shells_from
from hexfold.stick import stick
from precis.errors import BadInput
from precis.store import Store
from precis.structure.georelax import relax_graph
from precis.structure.relax import relax
from precis_se.atomic.generate import finish_generate, prepare_generate
from precis_se.atomic.join import finish_join, prepare_join
from precis_se.ops import SeTree

pytestmark = pytest.mark.slow

#: zigzag N=8, len=4 -- the shortest (8,0) tube with atoms beyond the
#: table radius's guard band (shell > SEAM_RADIUS["z"] + 2 == 10); measured
#: directly (see the module docstring): len=3 tops out at shell 10 exactly.
_TUBE = "hexfold 0.2\na: tube(8,0, len=4)\n"
#: a tiny tube for the rung-gate tests, which only exercise
#: `_select_relaxer`'s branching, not the seam-decay physics -- len=1
#: relaxes in well under a second.
_TUBE_SMALL = "hexfold 0.2\na: tube(8,0, len=1)\n"

_GEO_RELAX_STEPS = 1500
_GEO_RELAX_TOL = 1e-4


def _generate(
    store: Store, tree: SeTree, name: str, spec: str, design_slug: str
) -> None:
    _echo, pending = prepare_generate(
        store,
        tree,
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": spec},
            "name": name,
        },
        design_slug,
    )
    assert pending is not None
    finish_generate(store, tree, pending)


def _relax_to_geo(store: Store, struct_slug: str) -> None:
    """Relax ``struct_slug``'s own structure design to the ``geo`` rung
    and re-save -- there is no SE op for a bare pre-join relax (`join`'s
    module docstring: the op covers the join itself, not either side's
    own prep), so this calls :func:`precis.structure.relax.relax` directly
    on the loaded scene and re-saves through ``structure_save``'s own
    ``relax_summary=`` -- exactly what ``edit(kind='structure', id=...,
    ops=[{'op':'relax','fidelity':'geo'}])`` does at the handler level
    (`precis.handlers.structure`'s ``_relax_summary``, a private
    staticmethod on that handler class -- reproduced inline here rather
    than reached into)."""
    ref = store.get_ref(kind="structure", id=struct_slug)
    assert ref is not None
    scene, _handles = store.structure_load(ref.id)
    res = relax(scene, fidelity="geo", steps=_GEO_RELAX_STEPS, tol=_GEO_RELAX_TOL)
    assert res.rung == "geo"
    store.structure_save(
        slug=struct_slug,
        title=ref.title,
        scene=scene,
        version=int((ref.meta or {}).get("version", 1)) + 1,
        card_text=f"{struct_slug} (relaxed to the geo rung for a join test).",
        relax_summary={
            "rung": res.rung,
            "converged": res.converged,
            "n_steps": res.n_steps,
            "max_disp": res.max_disp,
        },
    )


def _bound(tree: SeTree, name: str) -> str:
    bound = tree.blocks[name].bound
    assert bound is not None
    return bound


def _join(
    store: Store, tree: SeTree, design_slug: str, **op: object
) -> tuple[str, SeTree]:
    op.setdefault("op", "join")
    echo, pending = prepare_join(store, tree, op, design_slug)
    assert pending is not None
    finish_join(store, tree, pending)
    return echo, tree


def test_join_geo_rung_matches_whole_spec_fuse_beyond_the_guard_band(
    store: Store,
) -> None:
    design_slug = "hx-join-geo"
    tree = SeTree()
    _generate(store, tree, "tube_a", _TUBE, design_slug)
    _generate(store, tree, "tube_b", _TUBE, design_slug)
    _relax_to_geo(store, _bound(tree, "tube_a"))
    _relax_to_geo(store, _bound(tree, "tube_b"))

    _echo, tree = _join(
        store, tree, design_slug, name="composite", a="tube_a.out", b="tube_b.in"
    )
    node = tree.blocks["composite"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    assert rec["relaxer"] == "geo"
    codes = {f["code"] for f in rec["report"]["findings"]}
    assert "seam.leak" not in codes, codes
    assert "join.rung" not in codes, codes
    # seam.strain (INFO, always emitted by `compose` regardless of rung) is
    # carried through unchanged on the geo rung too -- not a stick-only fact.
    strain = [f for f in rec["report"]["findings"] if f["code"] == "seam.strain"]
    assert len(strain) == 1, rec["report"]["findings"]
    assert strain[0]["data"]["bonds"] > 0
    assert strain[0]["data"]["vertices"] > 0

    scene, _handles = store.structure_load(ref.id)
    labels = list(scene.atoms)
    composite_coords = np.array(
        [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels],
        dtype=np.float64,
    )

    # the same "one .hx text, both instances, one build+stick" whole-spec
    # fuse the stick-rung test compares against (`tests/hexfold/test_join.py`
    # module docstring) -- but relaxed on geo instead of stick.
    whole_text = (
        "hexfold 0.2\na: tube(8,0, len=4)\nb: tube(8,0, len=4)\n"
        "a.out --fuse k=0--> b.in\n"
    )
    whole_net = build(whole_text, strict=False)
    whole_coords = stick(whole_net).astype(np.float64)
    whole_elements = [a.element for a in whole_net.atoms]
    whole_bonds = [(i, j) for i, j, _k in whole_net.bonds]
    trace = relax_graph(
        whole_elements,
        whole_coords,
        whole_bonds,
        set(),
        hybridizations="sp2",
        iters=4000,
        tol=_GEO_RELAX_TOL,
    )
    assert trace.converged, ("whole-spec relax did not converge", trace.n_steps)

    net_a = build(_TUBE, strict=False)
    net_b = build(_TUBE, strict=False)
    n_a = len(net_a.atoms)
    # instance a's own path-sorted ordinals are the same whether built
    # alone or as part of the two-instance whole spec (checked, not
    # assumed -- the same fact `tests/hexfold/test_join.py` and
    # `tests/test_hexfold_seam_decay.py` both rely on); instance b's
    # ordinals then follow at offset n_a, the SAME offset the se composite
    # itself uses for its own b-side ordinals (module docstring's
    # "Composite" contract). ``net_b`` is built from `_TUBE` standalone,
    # so its own instance is spelled "a" too (the spec text's own instance
    # name, independent of the se block name "tube_b") -- the `instance`
    # field is stripped before comparing so this is a check on `site`/
    # `defect`/`h`/`label` only, not on the instance letter itself.
    def _bare(path):
        return replace(path, instance="")

    free_a_ord = {_bare(a.path): a.ord for a in net_a.atoms}
    free_b_ord = {_bare(a.path): a.ord for a in net_b.atoms}
    whole_a_ord = {
        _bare(a.path): a.ord for a in whole_net.atoms if a.instance == "a"
    }
    whole_b_ord = {
        _bare(a.path): a.ord for a in whole_net.atoms if a.instance == "b"
    }
    assert whole_a_ord == free_a_ord
    assert {p: o - n_a for p, o in whole_b_ord.items()} == free_b_ord

    r = SEAM_RADIUS["z"]
    lo = r + 2  # the pinned guard band's outer shell; "beyond" == strictly >

    def _beyond_guard_deltas(net, offset: int) -> tuple[float, float]:
        adj = _adjacency(net.bonds)
        port = dict(net.ports)["out" if net is net_a else "in"]
        dist = _shells_from(set(port.atoms), adj)
        max_dl = 0.0
        for i, j, _k in net.bonds:
            s = min(dist.get(i, 1 << 30), dist.get(j, 1 << 30))
            if s <= lo:
                continue
            ci, cj = offset + i, offset + j
            dl = abs(
                float(np.linalg.norm(composite_coords[ci] - composite_coords[cj]))
                - float(np.linalg.norm(whole_coords[ci] - whole_coords[cj]))
            )
            max_dl = max(max_dl, dl)
        max_dtheta = 0.0
        for v, d in dist.items():
            if d <= lo:
                continue
            nbrs = adj.get(v, [])
            cv = offset + v
            before = _angles_at(composite_coords, cv, [offset + n for n in nbrs])
            after = _angles_at(whole_coords, cv, [offset + n for n in nbrs])
            for x, y in zip(before, after, strict=True):
                max_dtheta = max(max_dtheta, abs(x - y))
        return max_dl, max_dtheta

    dl_a, dtheta_a = _beyond_guard_deltas(net_a, 0)
    dl_b, dtheta_b = _beyond_guard_deltas(net_b, n_a)

    assert dl_a < 0.002, dl_a
    assert dtheta_a < 0.15, dtheta_a
    assert dl_b < 0.002, dl_b
    assert dtheta_b < 0.15, dtheta_b


def test_join_mixed_rung_is_an_error_and_mints_no_orphan(store: Store) -> None:
    design_slug = "hx-join-rung-mixed"
    tree = SeTree()
    _generate(store, tree, "tube_a", _TUBE_SMALL, design_slug)
    _generate(store, tree, "tube_b", _TUBE_SMALL, design_slug)
    _relax_to_geo(store, _bound(tree, "tube_a"))
    # tube_b stays on the stick rung (never relaxed) -- a genuine mismatch.

    with pytest.raises(BadInput, match="join.rung"):
        prepare_join(
            store,
            tree,
            {"op": "join", "name": "bad", "a": "tube_a.out", "b": "tube_b.in"},
            design_slug,
        )
    assert "bad" not in tree.blocks
    assert store.get_ref(kind="structure", id=f"{design_slug}-bad") is None


def test_join_forced_geo_on_stick_rung_blocks_warns(store: Store) -> None:
    design_slug = "hx-join-rung-forced"
    tree = SeTree()
    _generate(store, tree, "tube_a", _TUBE_SMALL, design_slug)
    _generate(store, tree, "tube_b", _TUBE_SMALL, design_slug)
    # neither side relaxed -- both default to "stick".

    _echo, tree = _join(
        store,
        tree,
        design_slug,
        name="composite",
        a="tube_a.out",
        b="tube_b.in",
        rung="geo",
    )
    node = tree.blocks["composite"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    rec = (ref.meta or {})["generated"]
    assert rec["relaxer"] == "geo"
    findings = rec["report"]["findings"]
    rung_findings = [f for f in findings if f["code"] == "join.rung"]
    assert len(rung_findings) == 1, findings
    assert rung_findings[0]["severity"] == "WARN"
