"""Molecular workspaces slice 4 — the DB-free chemistry check
(``precis.structure.check``): ``Run.finish()`` runs it on every atomic block
(``check=False`` opts out), the importer runs the same function on arrival,
and the two read the same finding text, each naming the nearest user frame
(``main.py:<line>``)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_geom.assembly as pga
import precis_geom.mol as mol
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis.store import Store
from precis.structure import check as chem
from precis_se.atomic.generators import hexfold_spec
from precis_se.workspace_import import import_workspace_run
from tests.workspaces._script import load_script

#: A methyl radical-by-omission: C with three H, bonds declared. The
#: ``# BLOCK`` line is where ``main.py`` hands the scene to ``out.block``.
_SCRIPT = """\
import numpy as np

from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene
from precis_geom import out

_TET = [(1, 1, 1), (-1, -1, 1), (-1, 1, -1), (1, -1, -1)]


def methyl(n_h, magmom=None):
    atoms = {"aC1": Atom("aC1", "C", np.full(3, 0.5), magmom=magmom)}
    bonds = []
    for k in range(n_h):
        d = np.array(_TET[k], dtype=float) * (1.09 / 3**0.5) / 20.0
        lab = f"aH{k + 1}"
        atoms[lab] = Atom(lab, "H", np.full(3, 0.5) + d)
        bonds.append(Bond("aC1", lab, 1.0))
    cell = Cell(np.eye(3) * 20.0, (False, False, False))
    return Scene(cell=cell, atoms=atoms, bonds=bonds)


def main(out_dir, n_h=3, **kw):
    with out.run(out_dir, **kw.pop("run", {})) as o:
        o.block("ch3", methyl(n_h, **kw))  # BLOCK
"""

_BLOCK_LINE = next(
    i for i, ln in enumerate(_SCRIPT.splitlines(), 1) if ln.endswith("# BLOCK")
)


def _run(tmp_path: Path, **kw: Any) -> Path:
    """Run ``main.py`` under ``tmp_path``; returns the out dir."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "r"
    mod.main(out_dir, **kw)
    return out_dir


def _texts(out_dir: Path) -> list[str]:
    doc = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
    return [f["text"] for f in doc["blocks"].get("ch3", [])]


def test_under_valent_carbon_finding_names_script_line(tmp_path: Path) -> None:
    out_dir = _run(tmp_path)
    open_valence = [t for t in _texts(out_dir) if "open_valence" in t]
    assert len(open_valence) == 1, _texts(out_dir)
    assert open_valence[0].endswith(f"at main.py:{_BLOCK_LINE}")
    assert "aC1 (C, 1 open)" in open_valence[0]
    doc = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
    assert doc["summary"]["errors"] >= 1


def test_saturated_port_and_radical_are_clean(tmp_path: Path) -> None:
    clean = _run(tmp_path / "a", n_h=4)
    assert not [t for t in _texts(clean) if "open_valence" in t]
    radical = _run(tmp_path / "b", magmom=1.0)
    assert not [t for t in _texts(radical) if "open_valence" in t]


def test_port_atom_exempts_open_valence(tmp_path: Path) -> None:
    scene = load_script(tmp_path, _SCRIPT).methyl(3)
    assert [f.rule for f in chem.check(scene) if f.rule == "open_valence"]
    ported = chem.check(scene, ports=[{"name": "p", "atom": "aC1"}])
    assert not [f for f in ported if f.rule == "open_valence"]


@pytest.fixture(scope="module")
def prism() -> tuple[Any, Any, Any]:
    net, _tube = prism_tube(4, 4, 2, "++++++")
    xyz = stick(net)
    return net, xyz, pga.geom(net, coords=xyz)


def _open_labels(geom: Any) -> list[str]:
    """Labels ``open_valence`` names — read off ``_open_valence`` (the
    finding text names six), after the same normalising ``check`` does."""
    scene = chem._normalised(geom.flatten())
    return [lab for lab, _el, _d in chem._open_valence(scene, set())]


def test_uncapped_prism_is_open_only_on_its_edge_atoms(prism: tuple) -> None:
    """A stick-relaxed prism is a closed sp2 net plus open edges: the
    interior sp2 carbons (bonds 3 x 4/3) are not under-valent; the open
    atoms are exactly the hosts ``terminate_open_edges`` caps — 96 two-bond
    atoms (one H) and 24 one-bond corners (two H): 120 atoms, 144 H."""
    net, xyz, g = prism
    hosts, _pos = hexfold_spec.terminate_open_edges(net, xyz, "H")
    labels = list(g.scene.atoms)
    assert (len(hosts), len(set(hosts))) == (144, 120)
    got = _open_labels(g)
    assert sorted(got) == sorted({labels[h] for h in hosts})
    # and the deficit is the number of H a host takes (1 or 2)
    scene = chem._normalised(g.flatten())
    deficit = {lab: round(d) for lab, _el, d in chem._open_valence(scene, set())}
    assert Counter(deficit.values()) == {1: 96, 2: 24}
    texts = [f.text() for f in chem.check(g) if f.rule == "open_valence"]
    assert len(texts) == 1 and "120 atom(s) below" in texts[0]


def test_capped_prism_has_no_open_valence(prism: tuple) -> None:
    _net, _xyz, g = prism
    capped = mol.cap(g)
    assert _open_labels(capped) == []
    assert [f for f in chem.check(capped) if f.rule == "open_valence"] == []


def test_decorated_then_capped_prism_has_no_skips_or_open_valence(
    prism: tuple,
) -> None:
    _net, _xyz, g = prism
    decorated = mol.decorate(g, "open & face:out & every:3", group="COOH")
    capped = mol.cap(decorated)
    assert [f for f in capped.findings if "site_skipped" in f] == []
    assert _open_labels(capped) == []


def test_benzene_has_no_open_valence() -> None:
    benzene = mol.frag(smiles="c1ccccc1")
    assert _open_labels(benzene) == []
    assert _open_labels(mol.cap(benzene)) == []


def test_a_bare_sp2_carbon_in_a_net_still_counts_as_open() -> None:
    """The sigma rule only fills a delocalized carbon to three neighbours:
    two 4/3 bonds and no H leave one open."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Bond, Scene

    atoms = {
        la: Atom(la, "C", np.full(3, 0.5) + np.array([k * 0.05, 0.0, 0.0]))
        for k, la in enumerate(("aC1", "aC2", "aC3"))
    }
    bonds = [
        Bond("aC1", "aC2", 4.0 / 3.0, kind="aromatic"),
        Bond("aC2", "aC3", 4.0 / 3.0, kind="aromatic"),
    ]
    scene = Scene(
        cell=Cell(np.eye(3) * 20.0, (False, False, False)), atoms=atoms, bonds=bonds
    )
    deficits = {lab: round(d) for lab, _e, d in chem._open_valence(scene, set())}
    assert deficits == {"aC1": 2, "aC2": 1, "aC3": 2}


def test_finding_site_is_the_script_file_when_run_as_main(tmp_path: Path) -> None:
    """``python path/main.py`` outside the run's work root: the trace frame
    is module ``__main__``, and the site still reads ``main.py:<line>``."""
    src = tmp_path / "src"
    src.mkdir()
    script = src / "main.py"
    script.write_text(
        _SCRIPT
        + "\n\nif __name__ == '__main__':\n    import sys\n    main(sys.argv[1])\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "work" / "out" / "r"  # work root = tmp_path/work
    run = subprocess.run(
        [sys.executable, str(script), str(out_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        check=False,
    )
    assert run.returncode == 0, run.stderr
    open_valence = [t for t in _texts(out_dir) if "open_valence" in t]
    assert len(open_valence) == 1, _texts(out_dir)
    assert open_valence[0].endswith(f"at main.py:{_BLOCK_LINE}"), open_valence[0]
    assert "__main__" not in open_valence[0]


def test_strict_raises_and_check_json_still_written(tmp_path: Path) -> None:
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "r"
    with pytest.raises(chem.ChemCheckError):
        mod.main(out_dir, run={"check": {"strict": True}})
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "check.json").is_file()


def test_check_false_opts_out(tmp_path: Path) -> None:
    out_dir = _run(tmp_path, run={"check": False})
    assert (out_dir / "manifest.json").is_file()
    assert not (out_dir / "check.json").exists()
    assert not (out_dir / "fit.json").exists()


def test_import_yields_the_same_finding_text(store: Store, tmp_path: Path) -> None:
    """The importer runs the same function on the same bytes: its block
    findings are exactly ``check.json``'s texts."""
    out_dir = _run(tmp_path)
    finish_texts = _texts(out_dir)
    assert finish_texts
    store.insert_ref(kind="se", slug="mol-check", title="mol check", meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    res = import_workspace_run(
        store,
        workspace="mol-check-ws",
        design="mol-check",
        out_dir=out_dir,
        run_job=int(job.id),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        params={},
        requested_by="agent:test",
    )
    prefix = "block 'ch3': "
    imported = [f[len(prefix) :] for f in res.findings if f.startswith(prefix)]
    chem_texts = [t for t in imported if t.startswith("mol.check ")]
    assert chem_texts == finish_texts
    assert any(
        t.startswith("mol.check open_valence") and f"main.py:{_BLOCK_LINE}" in t
        for t in chem_texts
    )
