"""gr522032 — the hex probe resolver (shared-core-layers S2):
``hexfold.probe.probe(obj, selector, label=...)`` resolves a
``sites:``/``atoms:``/``frame:`` selector against a hexfold object at
script time — atoms, cells or bands — prints one ``probe <label>:
resolves to N <kind>`` line and records ``{label, count, kind,
digest}`` on the calling trace frame so ``view='trace'`` lists it
there.  An unresolvable selector raises ``precis_diag.ProbeError``
carrying the refusal Diag with a ``next``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import precis_diag
from precis_geom import out as geom_out
from precis_geom import trace, trace_schema
from tests.workspaces._script import load_script

pytestmark = pytest.mark.filterwarnings("ignore")

_WORK_ROOT = Path(__file__).resolve().parent.parent

_SPEC = "hexfold 0.2\norigin t\nt: tube(5,0, len=3)\n"

#: The seam spec from test_hexfold_trace — four tubes plus a k>=3 seam
#: named ``foot``, so the net carries seam/region band tables.
_SEAM_SPEC = (
    "hexfold 0.2\norigin t\n"
    "t: tube(5,0, len=3)\n"
    "u: tube(5,0, len=3)\n"
    "v: tube(5,0, len=3)\n"
    "w: tube(5,0, len=3)\n"
    "t.out --fuse k=0--> u.in\n"
    "seam foot: t.in == v.in == w.in\n"
    "terminate: u.out = H\n"
)

#: ``build(o)`` builds a net and probes two selectors — the records must
#: land on the ``main.build`` user frame.
_SCRIPT = (
    "from hexfold.build import build\n"
    "from hexfold.probe import probe\n"
    "\n"
    "\n"
    "def build_net(o):\n"
    "    net = build('hexfold 0.2\\norigin t\\nt: tube(5,0, len=3)\\n')\n"
    "    probe(net, 'atoms:t[0,3,5-9]', label='seven')\n"
    "    probe(net, 'atoms:t[0..59]' if False else 'atoms:t[0-2]')\n"
)


def test_probe_atoms_gives_the_right_count() -> None:
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SPEC)
    result = probe(net, "atoms:t[0,3,5-9]", label="seven")
    assert result["count"] == 7
    assert result["digest"]["kind"] == "atoms"
    assert result["digest"]["count"] == 7
    # ids are the hexfold path spellings (t/(u,v,s)); count is on ordinals
    assert len(result["digest"]["ids"]) == 7
    assert all(str(i).startswith("t/") for i in result["digest"]["ids"])


def test_probe_whole_object_atoms() -> None:
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SPEC)
    result = probe(net, "atoms:_[0-59]")
    assert result["count"] == len(net.atoms) == 60
    # a whole-object atom set carries an extent (the net's seed3 bbox)
    assert len(result["digest"]["extent"]) == 6


def test_probe_sites_band_and_bands_listing() -> None:
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SEAM_SPEC)
    foot = next(s for s in net.seams if s.name == "foot")
    result = probe(net, "sites:_/foot/s0..s2", label="seam-head")
    assert result["count"] == 3
    assert result["digest"]["kind"] == "atoms"
    assert result["digest"]["ids"] == [str(net.atoms[o].path) for o in foot.atoms[:3]]

    bands = probe(net, "sites:_")
    assert bands["digest"]["kind"] == "bands"
    assert "foot" in bands["digest"]["ids"]
    # the instance regions are bands; surviving ports (e.g. v.out) too
    assert "t" in bands["digest"]["ids"]
    assert any(k.endswith(".out") for k in bands["digest"]["ids"])


def test_probe_strip_cells_and_rim_band() -> None:
    from hexfold.probe import probe
    from hexfold.sheetnet import strip

    st = strip(rows=4, periods=3)
    cells = probe(st, "sites:_/cells")
    assert cells["digest"]["kind"] == "cells"
    assert cells["count"] > 0
    assert cells["digest"]["ids"][0] == "(-1,0)"

    one = probe(st, "sites:_/(1,2)")
    assert one["count"] == 1
    assert one["digest"]["kind"] == "cells"

    rim = probe(st, "sites:_/rim/s0..s1")
    assert rim["count"] == 2
    assert rim["digest"]["kind"] == "atoms"


def test_probe_records_under_the_calling_frame(tmp_path: Path) -> None:
    mod = load_script(tmp_path, _SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with geom_out.run(out_dir, work_root=tmp_path):
        mod.build_net(None)
    doc = trace_schema.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    frame = next(f for f in doc["frames"] if f["qual"] == "main.build_net")
    probes = frame["probes"]
    assert [p["label"] for p in probes] == ["seven", "atoms:t[0-2]"]
    assert probes[0]["count"] == 7
    assert probes[0]["kind"] == "atoms"
    assert probes[1]["count"] == 3


def test_probe_frame_selector_resolves_maker_atoms() -> None:
    from hexfold.build import build
    from hexfold.probe import probe

    with trace.run(work_root=_WORK_ROOT):
        net = build(_SPEC)
        result = probe(net, "frame:qual=build.tube", label="made")
    assert result["count"] == len(net.atoms) == 60
    assert result["digest"]["kind"] == "atoms"


def test_probe_bad_selector_gives_a_diag_with_next() -> None:
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SPEC)
    with pytest.raises(precis_diag.ProbeError) as exc_info:
        probe(net, "sites:_/nope/s0..s1")
    assert exc_info.value.diag.code == "core.probe-unresolved"
    assert exc_info.value.diag.next
    assert "foot" in exc_info.value.diag.next or "band" in exc_info.value.diag.next

    with pytest.raises(precis_diag.ProbeError):
        probe(net, "bananas")
    with pytest.raises(precis_diag.ProbeError):
        probe(net, "atoms:t[500]")


def test_probe_atoms_indexes_parts_not_bands() -> None:
    """``atoms:<part>[i]`` is the se reading — an ordinal within the
    named part's atoms.  A band (``foot``, ``v.out``) is not a part:
    band indexing is ``sites:<part>/<band>/s<i>..s<j>``."""
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SEAM_SPEC)
    t_ords = dict(net.regions)["t"]
    result = probe(net, "atoms:t[0-2]")
    assert result["count"] == 3
    assert result["digest"]["ids"] == [str(net.atoms[o].path) for o in t_ords[:3]]

    with pytest.raises(precis_diag.ProbeError) as exc_info:
        probe(net, "atoms:foot[0-2]")
    assert "sites:" in exc_info.value.diag.next


def test_probe_sites_validates_the_part_on_every_form() -> None:
    """``sites:<part>/…`` names a real part on all three forms —
    ``sites:bogus/cells`` and ``sites:bogus/r0`` used to answer instead
    of refusing."""
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SPEC)
    for selector in ("sites:bogus", "sites:bogus/cells", "sites:bogus/r0"):
        with pytest.raises(precis_diag.ProbeError) as exc_info:
            probe(net, selector)
        assert exc_info.value.diag.code == "core.probe-unresolved"
        assert "bogus" in exc_info.value.diag.message
        assert exc_info.value.diag.next


def test_probe_sites_cells_scope_to_the_part() -> None:
    """``sites:t/cells`` lists only t's ring cells — the ``r<i>`` rings
    whose atoms sit inside the part — not every ring on the net."""
    from hexfold.build import build
    from hexfold.probe import probe

    net = build(_SEAM_SPEC)
    t_atoms = set(dict(net.regions)["t"])
    u_atoms = set(dict(net.regions)["u"])
    cells = probe(net, "sites:t/cells")
    rings = net.rings
    want = [f"r{i}" for i, ring in enumerate(rings) if set(ring) <= t_atoms]
    assert cells["digest"]["kind"] == "cells"
    assert cells["digest"]["ids"] == want
    assert 0 < len(want) < len(rings)
    # a single cell outside the part refuses as a band, not the cell
    u_only = next(i for i, ring in enumerate(rings) if set(ring) <= u_atoms)
    with pytest.raises(precis_diag.ProbeError):
        probe(net, f"sites:t/r{u_only}")
    one = probe(net, f"sites:u/r{u_only}")
    assert one["digest"]["kind"] == "cells"
    assert one["digest"]["ids"] == [f"r{u_only}"]


def test_probe_cells_on_a_cellless_object_refuses() -> None:
    """``sites:<part>/cells`` on an object with no ring cells refuses —
    the object has no cells."""
    import types

    from hexfold.probe import probe

    obj = types.SimpleNamespace(atoms=[types.SimpleNamespace(ord=i) for i in range(4)])
    with pytest.raises(precis_diag.ProbeError) as exc_info:
        probe(obj, "sites:_/cells")
    assert "has no cells" in exc_info.value.diag.message


def test_probe_frame_touched_unions_the_subtree() -> None:
    """``frame:<key>/touched`` unions the whole subtree's touched
    ranges, the se ``touched_ordinals`` reading — not only the named
    frame's own."""
    from hexfold.build import build
    from hexfold.probe import probe

    @trace.traced(name="hxprobe.child")
    def _child() -> None:
        trace.record(touched={"atoms": [[2, 4]]})

    @trace.traced(name="hxprobe.parent")
    def _parent() -> None:
        _child()

    with trace.run(work_root=_WORK_ROOT):
        net = build(_SPEC)
        _parent()
        result = probe(net, "frame:qual=hxprobe.parent/touched")
    assert result["count"] == 3
    assert result["digest"]["ids"] == [str(net.atoms[o].path) for o in (2, 3, 4)]


def test_probe_frame_touched_count_only_refuses() -> None:
    """A frame that recorded a bare ``touched`` count (``pore``'s
    ``len(drops)``, not ranges) names no ids — ``/touched`` refuses."""
    from hexfold.probe import probe
    from hexfold.sheetnet import pore, strip

    with trace.run(work_root=_WORK_ROOT):
        st = pore(strip(rows=6, periods=4), [(2, 2)])
        with pytest.raises(precis_diag.ProbeError) as exc_info:
            probe(st, "frame:qual=sheetnet.pore/touched")
    assert "a count, not ids" in exc_info.value.diag.next
