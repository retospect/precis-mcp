"""Molecular workspaces slice 5 — the costly ``mol`` calls as cached steps.

``mol.frag`` (the SMILES embed), ``mol.lib``, ``mol.decorate``, ``mol.cap``
(and ``mol.relax``, slice 3) plus the new ``mol.prism`` (the hex tiler) and
``mol.generate`` (the ``GENERATORS`` registry) are ``@steps.step``:

- a second identical call is a hit and equals the miss byte for byte;
- the trace frame keeps its public name (``frag``, ``lib``, ``decorate``,
  ``cap``) and carries the step key and hit flag, with the call site of the
  script line (not a library line);
- a Scene / Net argument or a non-keyable parameter takes the documented
  path (uncached wrap / ``BadInput`` naming the way out);
- the generators come back as Geoms with their ports, hybridization and bond
  kinds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("rdkit")

import precis_geom.assembly as pga
import precis_geom.mol as mol
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis.errors import BadInput
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_geom import steps, trace
from precis_geom.mol.generate import GENERATOR_NAMES

pytestmark = pytest.mark.filterwarnings("ignore:steps:UserWarning")


@pytest.fixture(autouse=True)
def cache(tmp_path: Path):
    steps.configure(tmp_path / "cache")
    try:
        yield tmp_path / "cache"
    finally:
        steps.configure()


def _text(g: Any) -> tuple[Any, ...]:
    """Everything a build decides, bit for bit — except the makers, which are
    the live run's frame ids (a hit grafts fresh ones): labels, elements,
    exact fractional coordinates, bonds, cell."""
    sc = g.scene
    return (
        list(sc.atoms),
        [a.element for a in sc.atoms.values()],
        np.array([a.frac for a in sc.atoms.values()]).tobytes(),
        [(b.i, b.j, b.order, b.kind, b.provenance, b.image) for b in sc.bonds],
        np.asarray(sc.cell.lattice).tobytes(),
        [a.hybridization for a in sc.atoms.values()],
    )


def _log(tr: trace.Tracer) -> list[tuple[str, bool]]:
    return [(s["name"], s["hit"]) for s in tr.steps_log]


# ── frag / lib ───────────────────────────────────────────────────────────


def test_frag_embed_is_a_step_and_a_hit_equals_the_miss(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        a = mol.frag("[*:1]C(=O)O", name="acid")
        b = mol.frag("[*:1]C(=O)O", name="acid")
        assert [n for n, _h in _log(tr)] == ["frag", "frag"]
        assert _log(tr)[0][1] is False and _log(tr)[1][1] is True
        assert [f.qual for f in tr.root.children] == ["frag", "frag"]
        assert [(f.step or {}).get("hit") for f in tr.root.children] == [False, True]
    assert _text(a) == _text(b)
    assert a.ports == b.ports and a.meta["step"] == b.meta["step"]
    # another seed, name or SMILES is another key
    mol.frag("[*:1]C(=O)O", name="acid", conf_seed=1)
    mol.frag("[*:1]CC")
    assert len(steps.ls("frag")) == 3


def test_frag_scene_and_refusals_stay_uncached(tmp_path: Path) -> None:
    sc = Scene(
        cell=Cell(np.eye(3) * 10.0, (False, False, False)),
        atoms={"aC1": Atom("aC1", "C", np.array([0.5, 0.5, 0.5]))},
        bonds=[],
    )
    with trace.run(work_root=tmp_path) as tr:
        g = mol.frag(scene=sc, ports={"a1": {"atom": "aC1"}})
        assert "a1" in g.ports and _log(tr) == []
        assert [f.qual for f in tr.root.children] == ["frag"]
        assert tr.root.children[0].step is None
    with pytest.raises(BadInput, match="exactly one of"):
        mol.frag()
    with pytest.raises(BadInput, match="derives its ports"):
        mol.frag("[*:1]C", ports={"a1": {"atom": "x"}})
    with pytest.raises(BadInput, match="read MCP"):
        mol.frag(ref="structure:x")
    assert steps.ls("frag") == []


def test_lib_is_cached_across_call_sites_and_names_its_frame(tmp_path: Path) -> None:
    def site_a() -> Any:
        return mol.lib("COOH")

    with trace.run(work_root=tmp_path) as tr:
        a = site_a()
        b = site_a()
        c = mol.lib("PEG", n=2)
        d = mol.lib("PEG", n=2)
        quals = [f.qual for f in tr.root.children]
        assert quals == ["lib"] * 4
        log = _log(tr)
    assert _text(a) == _text(b) and _text(c) == _text(d)
    assert a.meta["step"] == b.meta["step"]
    assert [x for x in _log(tr) if x[0] == "lib"] == [
        ("lib", False),
        ("lib", True),
        ("lib", False),
        ("lib", True),
    ]
    with pytest.raises(BadInput, match="unknown entry"):
        mol.lib("nope")
    with pytest.raises(BadInput, match="needs n="):
        mol.lib("PEG")
    with pytest.raises(BadInput, match="takes no"):
        mol.lib("OH", bogus=1)


def test_lib_c60_is_a_step_with_its_makers() -> None:
    a = mol.lib("c60")
    b = mol.lib("c60")
    assert len(a.scene.atoms) == 60 and _text(a) == _text(b)
    assert {at.hybridization for at in a.scene.atoms.values()} == {"sp2"}
    assert len(steps.ls("lib")) == 1


# ── decorate / cap ───────────────────────────────────────────────────────


def _benzene() -> Any:
    return mol.frag("c1ccccc1", name="benzene")


def test_decorate_and_cap_are_steps_and_a_hit_equals_the_miss(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        host = _benzene()
        d1 = mol.decorate(host, "element:C & every:2", "OH")
        d2 = mol.decorate(host, "element:C & every:2", "OH")
        c1 = mol.cap(d1)
        c2 = mol.cap(d1)
        log = _log(tr)
        assert ("decorate", False) in log and ("decorate", True) in log
        assert ("cap", False) in log and ("cap", True) in log
        [first, second] = [f for f in tr.root.children if f.qual == "decorate"]
        assert (first.step or {}).get("hit") is False
        assert (second.step or {}).get("hit") is True
        # the counts ride the frame on a hit too (grafted)
        assert second.made == first.made and first.made["groups"] == 3
    assert _text(d1) == _text(d2) and _text(c1) == _text(c2)
    assert d1.findings == d2.findings
    # a different group or site list is a different key
    mol.decorate(host, "element:C & every:2", "NH2")
    mol.decorate(host, "element:C & every:3", "OH")
    assert len(steps.ls("decorate")) == 3


def test_decorate_accepts_a_site_list_and_a_geom_group(tmp_path: Path) -> None:
    host = _benzene()
    rows = mol.sites(host, "element:C & every:3")
    grp = mol.lib("CH3")
    a = mol.decorate(host, rows, grp)
    b = mol.decorate(host, rows, grp)
    assert _text(a) == _text(b)
    assert [r["hits"] for r in steps.ls("decorate")] == [1]


def test_cap_on_a_net_built_geom_keeps_the_net_path_across_a_hit() -> None:
    net, _t = prism_tube(4, 4, 2, "++++++")
    g = pga.geom(net, coords=stick(net))  # content-keyed: built outside a step
    a = mol.cap(g)
    b = mol.cap(g)
    assert len(a.scene.atoms) == 600 and _text(a) == _text(b)
    assert a.meta["capped"] == b.meta["capped"] == 144
    assert b.meta["step"] == a.meta["step"]
    assert [r["hits"] for r in steps.ls("cap")] == [1]


def test_a_bare_scene_still_decorates_and_caps_uncached() -> None:
    """``pga.geom`` wraps a Scene / Net inside the (uncached) call — the old
    behaviour; only a Geom is a step parameter."""
    sc = _benzene().scene
    out_g = mol.cap(sc)
    assert len(out_g.scene.atoms) == 12  # benzene is saturated: nothing to add
    assert steps.ls("cap") == []
    out2 = mol.decorate(sc, "element:C & every:3", "OH")
    assert len(out2.scene.atoms) == 12 + 2  # two sites: each H swapped for O + H
    assert steps.ls("decorate") == []


def test_unkeyable_step_parameter_refuses_naming_the_way_out() -> None:
    host = _benzene()
    with pytest.raises(BadInput, match="cannot key a object parameter") as ei:
        mol.decorate(host, "element:C", object())
    assert ei.value.next is not None and "recipe values" in ei.value.next


# ── prism / generate ─────────────────────────────────────────────────────


def test_prism_is_prism_tube_stick_and_geom_as_one_step(tmp_path: Path) -> None:
    net, _t = prism_tube(4, 4, 2, "++++++")
    want = pga.geom(net, coords=stick(net))
    with trace.run(work_root=tmp_path) as tr:
        a = mol.prism(4, 4, 2, "++++++")
        b = mol.prism(4, 4, 2, "++++++")
        assert _log(tr) == [("prism", False), ("prism", True)]
        assert [f.qual for f in tr.root.children] == ["prism", "prism"]
    assert _text(a) == _text(b)
    assert len(a.scene.atoms) == len(want.scene.atoms) == 456
    assert np.allclose(
        [a.scene.cell.frac_to_cart(x.frac) for x in a.scene.atoms.values()],
        [want.scene.cell.frac_to_cart(x.frac) for x in want.scene.atoms.values()],
        atol=1e-9,
    )
    assert a.meta["census"] == want.meta["census"] and "_net" in b.meta
    # the Net rides the cache entry: cap takes the Net path off a hit
    assert mol.cap(b).meta["capped"] == 144


def test_prism_refuses_what_prism_tube_refuses() -> None:
    with pytest.raises(BadInput, match="odd_rows") as ei:
        mol.prism(4, 5, 2, "++++++")
    assert ei.value.next is not None
    with pytest.raises(BadInput, match="mol.prism"):
        mol.prism(4, 4, 2, "+++")  # #plus - #minus != 6: closure fails


def test_generate_cnt_carries_ports_kinds_and_hybridization(tmp_path: Path) -> None:
    with trace.run(work_root=tmp_path) as tr:
        a = mol.generate("cnt", n=6, m=6, length_A=15.0)
        b = mol.generate("cnt", n=6, m=6, length_A=15.0)
        assert _log(tr) == [("generate", False), ("generate", True)]
        assert [f.qual for f in tr.root.children] == ["generate", "generate"]
    assert _text(a) == _text(b) and a.ports == b.ports
    sc = a.scene
    assert len(sc.atoms) == 168 and len(sc.bonds) == 240
    assert {bd.kind for bd in sc.bonds} == {"aromatic"}
    assert {at.hybridization for at in sc.atoms.values()} == {"sp2"}
    assert len(a.ports) == 24
    p = a.ports["rim_b1"]
    assert p.atom in sc.atoms and p.direction == (0.0, 0.0, -1.0)
    assert "sp2-rim" in p.roles
    assert a.meta["generator"] == "cnt" and a.meta["topology"]["chiral_index"] == [6, 6]
    # another parameter set is another key
    mol.generate("cnt", n=6, m=6, length_A=20.0)
    assert len(steps.ls("generate")) == 2


def test_generate_fullerene_cyclodextrin_and_scene() -> None:
    c60 = mol.generate("fullerene", atoms=60)
    assert len(c60.scene.atoms) == 60 and c60.ports == {}
    cd = mol.generate("cyclodextrin", variant="alpha")
    assert len(cd.scene.atoms) == 126 and len(cd.ports) == 12
    assert {a.hybridization for a in cd.scene.atoms.values()} == {"sp3"}
    assert cd.ports["prim_rim1"].atom in cd.scene.atoms
    feat = {"name": "t", "at": [12, 9], "n": 6, "radius": 3.0, "tube_len": 1}
    sc = mol.generate(
        "hexfold_scene", sheet=[24, 18], features=[{**feat, "top": "ball"}]
    )
    assert len(sc.scene.atoms) > 900
    port = next(iter(sc.ports.values()))
    assert port.annotations["lattice"] == "sp2-hex" and port.annotations["atoms"]
    for g in (c60, cd, sc):  # JSON-safe, so the entry was stored: a second call hits
        assert g.meta["step"]
    assert len(steps.ls("generate")) == 3


def test_generate_refusals_name_the_way_out() -> None:
    with pytest.raises(BadInput, match="unknown generator") as ei:
        mol.generate("cone", n=5)
    assert ei.value.options == list(GENERATOR_NAMES)
    assert ei.value.next is not None and "generate" in ei.value.next
    with pytest.raises(BadInput, match="mol.generate\\('cnt'\\)"):
        mol.generate("cnt", n=6)  # missing m / length_A: the generator's own message
    assert steps.ls("generate") == []


def test_generated_scene_decorates_and_checks() -> None:
    """A generated Geom is a first-class input to the other steps."""
    tube = mol.generate("cnt", n=6, m=6, length_A=12.0)
    d = mol.decorate(tube, "open & every:2", "OH")
    c = mol.cap(d)
    assert len(c.scene.atoms) > len(tube.scene.atoms)
    # parent keys chain: the cap step's key depends on the decorate step's
    assert c.meta["step"] != d.meta["step"] != tube.meta["step"]


# ── the stale-hit guard: library code is key material ─────────────────────


def test_code_sha_follows_the_sources_it_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import sys

    pkg = tmp_path / "libpkg_for_code_sha"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "mod.py").write_text("Y = 2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    steps._module_sha.cache_clear()
    first = steps.code_sha("libpkg_for_code_sha")
    assert first == steps.code_sha("libpkg_for_code_sha")  # cached, stable
    (pkg / "mod.py").write_text("Y = 3\n", encoding="utf-8")
    steps._module_sha.cache_clear()
    assert steps.code_sha("libpkg_for_code_sha") != first
    # an unresolvable name digests as itself, never raises
    assert steps.code_sha("no_such_pkg_anywhere") != steps.code_sha("another_missing")
    sys.modules.pop("libpkg_for_code_sha", None)
    steps._module_sha.cache_clear()


def test_a_salt_is_part_of_the_key() -> None:
    calls: list[str] = []
    version = ["a"]

    @steps.step(salt=lambda: version[0])
    def build(n: int) -> Any:
        calls.append(version[0])
        return mol.frag("[*:1]C" if n == 0 else "[*:1]CC")

    build(0)
    build(0)
    assert calls == ["a"]
    version[0] = "b"  # the library it builds with changed: a miss, not a stale hit
    build(0)
    assert calls == ["a", "b"]
    version[0] = ""  # an empty salt is absent from the key: the unsalted key
    build(0)
    assert calls == ["a", "b", ""]
    assert mol.fragment.build_salt().startswith("rdkit ")
