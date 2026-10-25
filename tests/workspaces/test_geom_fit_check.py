"""``precis_geom.fit`` — the offline fit check (se-fit-check-and-probe
slice 1): manifest loading, ``Run.check``/``finish()`` wiring, the
unicycle fixture end-to-end, determinism and the cold-run budget."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from precis.utils.units import parse_quantity
from precis_geom import fit as geom_fit
from precis_geom import out as geom_out

_FIXTURE = Path(__file__).parent / "data" / "unicycle" / "out"


@pytest.fixture()
def unicycle(tmp_path: Path) -> Path:
    """Copy the checked-in unicycle run dir to tmp — the check writes
    ``fit.json`` and ``.fitcache/`` beside it."""
    dst = tmp_path / "unicycle"
    shutil.copytree(_FIXTURE, dst)
    return dst


def test_unicycle_findings(unicycle: Path) -> None:
    """The dogfood bugs: bolts where no hole exists, the +0.24 mm
    seatpost press gap, cranks/forks into the wheel."""
    parse_quantity("1 mm", "length")  # warm pint — not part of the check
    t0 = time.monotonic()
    rep = geom_fit.check(unicycle)
    cold_ms = (time.monotonic() - t0) * 1000
    text = rep.text()
    assert "error" in text
    pairs = {tuple(f["pair"]) for f in rep.findings}
    assert ("flange_bolt_left", "fork_left") in pairs
    assert ("crown", "seatpost") in pairs
    assert ("fork_left", "wheel") in pairs
    assert any("gap +0.2" in r.measure for r in rep.errors), text
    # made-by labels resolve through trace.json to script lines
    assert "make_fixture.py:" in text
    # Wall-clock: loaded CI runners measured 335-345 ms (r100), so the bound
    # catches an order-of-magnitude regression, not load jitter.
    assert cold_ms < 1500.0


def test_determinism_byte_identical(unicycle: Path) -> None:
    t1 = geom_fit.check(unicycle).text()
    t2 = geom_fit.check(unicycle).text()
    assert t1 == t2
    j1 = json.loads((unicycle / "fit.json").read_text(encoding="utf-8"))
    assert j1["findings"] == [f for f in geom_fit.check(unicycle).to_json()["findings"]]


def test_fit_json_and_prev_rotation(unicycle: Path) -> None:
    geom_fit.check(unicycle)
    assert (unicycle / "fit.json").is_file()
    geom_fit.check(unicycle)
    assert (unicycle / "fit.prev.json").is_file()


def test_strict_raises_and_manifest_still_written(tmp_path: Path) -> None:
    """Decision 13: a failing run still writes manifest.json; only
    strict=True raises."""
    with geom_out.run(tmp_path / "r") as o:
        o.block("a", "base add box:w10mmd10mmh10mm", scale="macro")
        o.block("b", "base add box:w10mmd10mmh10mm", scale="macro")
        o.instance("ia", template="a")
        o.instance("ib", template="b", pose="[5 0 0] mm")
    root = tmp_path / "r"
    assert (root / "manifest.json").is_file()  # written despite findings
    assert (root / "fit.json").is_file()
    with pytest.raises(geom_fit.FitCheckError):
        geom_fit.check(root, strict=True)


def test_check_false_opts_out(tmp_path: Path) -> None:
    with geom_out.run(tmp_path / "r", check=False) as o:
        o.block("a", "base add box:w10mmd10mmh10mm", scale="macro")
        o.instance("ia", template="a")
    assert not (tmp_path / "r" / "fit.json").exists()


def test_clean_run_passes_check(tmp_path: Path) -> None:
    with geom_out.run(tmp_path / "r") as o:
        o.block("a", "base add box:w10mmd10mmh10mm", scale="macro")
        o.block("b", "base add box:w10mmd10mmh10mm", scale="macro")
        o.instance("ia", template="a")
        o.instance("ib", template="b", pose="[30 0 0] mm")
    rep = geom_fit.check(tmp_path / "r")
    assert rep.errors == []


def test_parent_chain_and_array(tmp_path: Path) -> None:
    """A parented instance composes its parent's pose; a ``linear``
    array instance expands to members ``name#k`` — each measured."""
    with geom_out.run(tmp_path / "r") as o:
        o.block("plate", "base add box:w100mmd100mmh4mm", scale="macro")
        o.block("pin", "base add cyl:r1mmh10mm", scale="macro")
        o.block("frame", "base add box:w20mmd20mmh20mm", scale="macro")
        o.instance("frame", template="frame", pose="[0 0 30] mm")
        o.instance(
            "pins",
            template="pin",
            parent="frame",
            pose="[0 0 0] mm",
            linear={"kind": "linear", "count": 3, "pitch": 0.02, "axis": [1, 0, 0]},
        )
        o.instance("plate", template="plate", pose="[0 0 32] mm")
    rep = geom_fit.check(tmp_path / "r")
    names = {r.subject for r in rep.errors}
    # pins (world z 30..40 mm, x = 0/20/40 mm) punch through the plate at
    # z 32..36 mm → one overlap error per array member.
    assert {"pins#1 / plate", "pins#2 / plate", "pins#3 / plate"} <= names
    # the pins/frame pair is parent/child and must be skipped even though
    # pin#1 sits inside the frame's box.
    assert not any("frame" in s and "pins" in s for s in names)


def test_run_check_records_min_clearance(tmp_path: Path) -> None:
    """``Run.check(min_clearance=...)`` lands as manifest ``fit_rules``
    and binds later checks on the dir."""
    with geom_out.run(tmp_path / "r") as o:
        o.block("a", "base add box:w10mmd10mmh10mm", scale="macro")
        o.block("b", "base add box:w10mmd10mmh10mm", scale="macro")
        o.instance("ia", template="a")
        o.instance("ib", template="b", pose="[11 0 0] mm")
        o.check(min_clearance={("ia", "ib"): "2 mm"})
    manifest = json.loads(
        (tmp_path / "r" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["fit_rules"]["min_clearance"] == [
        {"pair": ["ia", "ib"], "value": "2 mm"}
    ]
    rep = geom_fit.check(tmp_path / "r")
    assert ("ia", "ib") in {tuple(f["pair"]) for f in rep.findings}
