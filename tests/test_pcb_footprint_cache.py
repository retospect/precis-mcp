"""gr520330 — the laptop footprint cache (``footprints.json`` /
``$PRECIS_FOOTPRINT_CACHE``) and the ``view='footprints',
args={'format':'json'}`` export door that fills it.

A board script run locally (``pcb.run``, outside the sandbox) has no
``precis_client``, so every catalog part used to fall to the synthesized
landpattern bound. The local kernel now reads an exported cache file
(:func:`precis.pcb.local._catalog_footprints`), and a miss warns with the
export call to run (:func:`precis.pcb.local._uncached_lcscs` + the
``_design`` warning).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import local as kernel
from precis.pcb import session as pcb_session

_ROW = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {
        "1": {"name": "A", "tags": []},
        "2": {"name": "B", "tags": []},
    },
    "courtyard": {"bbox": [-1.0, -1.0, 1.0, 1.0]},
    "centroid": {"x": 0.0, "y": 0.0},
}


def _batch() -> dict:
    """A two-catalog-part board: U1 → CREAL1, U2 → CGHOST1."""
    return {
        "components": [
            {
                "refdes": "U1",
                "label": "cached",
                "part": "CREAL1",
                "pins": [{"name": "A"}, {"name": "B"}],
            },
            {
                "refdes": "U2",
                "label": "miss",
                "part": "CGHOST1",
                "pins": [{"name": "A"}, {"name": "B"}],
            },
        ],
        "nets": [{"name": "N1"}],
        "connections": [
            {"net": "N1", "refdes": "U1", "pin": "A"},
            {"net": "N1", "refdes": "U2", "pin": "A"},
        ],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]},
            }
        ],
    }


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


@pytest.fixture(autouse=True)
def _no_ambient_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Keep the lookup deterministic: no env var, and ``sys.argv[0]``
    pointing at a script with no ``footprints.json`` beside it."""
    monkeypatch.delenv("PRECIS_FOOTPRINT_CACHE", raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "main.py")])


# ── cache-file discovery ──────────────────────────────────────────────


def test_env_var_names_a_json_file(tmp_path, monkeypatch):
    cache = tmp_path / "rows.json"
    cache.write_text(json.dumps({"footprints": {"CREAL1": _ROW}}), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(cache))

    assert kernel._footprint_cache_path() == cache
    assert kernel._load_footprint_cache(cache)["CREAL1"]["pads"] == _ROW["pads"]


def test_env_var_directory_form_holds_footprints_json(tmp_path, monkeypatch):
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"CREAL1": _ROW}), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(tmp_path))

    assert kernel._footprint_cache_path() == cache


def test_footprints_json_next_to_the_script_is_found(tmp_path, monkeypatch):
    script = tmp_path / "main.py"
    script.write_text("", encoding="utf-8")
    (tmp_path / "footprints.json").write_text(
        json.dumps({"footprints": {"CREAL1": _ROW}}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", [str(script)])

    assert kernel._footprint_cache_path() == tmp_path / "footprints.json"


def test_no_cache_anywhere_resolves_none(tmp_path):
    assert kernel._footprint_cache_path() is None


def test_a_bare_lcsc_keyed_map_is_accepted(tmp_path):
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"CREAL1": _ROW}), encoding="utf-8")
    assert kernel._load_footprint_cache(cache)["CREAL1"]["pads"] == _ROW["pads"]


def test_an_unreadable_cache_file_refuses(tmp_path):
    cache = tmp_path / "footprints.json"
    cache.write_text("not json", encoding="utf-8")
    with pytest.raises(kernel.LocalError, match="unreadable"):
        kernel._load_footprint_cache(cache)


def test_catalog_arg_wins_over_the_file(tmp_path, monkeypatch):
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps({"CREAL1": _ROW}), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(cache))
    override = {"pads": [{"number": "9"}], "pin_map": {}}

    out = kernel._catalog_footprints(_batch(), {"CREAL1": override})

    assert out["CREAL1"] is override


# ── export → cache round-trip ─────────────────────────────────────────


def test_export_document_round_trips_to_the_same_pads(pcb, tmp_path, monkeypatch):
    """The view's JSON document, saved verbatim as ``footprints.json``,
    feeds the local resolver the same row the store-side join hands
    ``pad_geometry`` — the sandbox path and the laptop path read the
    same bytes."""
    pcb.put(id="cache-rt", args=_batch())
    pcb.store.part_footprint_put("CREAL1", _ROW)
    doc = json.loads(
        pcb.get(id="cache-rt", view="footprints", args={"format": "json"}).body
    )
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(cache))

    design = kernel._design(_batch(), None)
    ref_id = pcb.store.get_ref(kind="pcb", id="cache-rt").id
    store_fp = pcb.store.pcb_footprints_for(ref_id)["CREAL1"]

    ir = kernel._build_ir(design)
    local_fp = pcb_session.footprints_by_refdes(
        ir, design.by_lcsc, local_footprints_by_name=design.local_by_name
    )["U1"]
    assert local_fp["pads"] == store_fp["pads"]
    assert local_fp["pin_map"] == store_fp["pin_map"]


# ── the miss/hit messages ─────────────────────────────────────────────


def test_cached_parts_report_no_synthesized_lines(pcb, tmp_path, monkeypatch, caplog):
    """A local run with the export on disk logs no ``no cached
    footprint``/``synthesized`` line for the cached parts."""
    pcb.put(id="cache-ok", args=_batch())
    pcb.store.part_footprint_put("CREAL1", _ROW)
    pcb.store.part_footprint_put("CGHOST1", _ROW)
    doc = json.loads(
        pcb.get(id="cache-ok", view="footprints", args={"format": "json"}).body
    )
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(cache))

    with caplog.at_level(logging.WARNING):
        design = kernel._design(_batch(), None)
        kernel._build_ir(design)

    assert "no cached footprint" not in caplog.text
    assert "synthesized" not in caplog.text


def test_a_miss_warns_with_the_export_call(pcb, caplog):
    """Nothing cached anywhere → the run still synthesizes, but the
    warning names the export call and where to save it."""
    pcb.put(id="cache-miss", args=_batch())

    with caplog.at_level(logging.WARNING):
        kernel._design(_batch(), None)

    assert "CREAL1" in caplog.text and "CGHOST1" in caplog.text
    assert "view='footprints'" in caplog.text
    assert "'format':'json'" in caplog.text
    assert "footprints.json" in caplog.text
    assert "PRECIS_FOOTPRINT_CACHE" in caplog.text


def test_a_partial_hit_warns_only_for_the_missing_parts(
    pcb, tmp_path, monkeypatch, caplog
):
    pcb.put(id="cache-partial", args=_batch())
    pcb.store.part_footprint_put("CREAL1", _ROW)
    doc = json.loads(
        pcb.get(id="cache-partial", view="footprints", args={"format": "json"}).body
    )
    cache = tmp_path / "footprints.json"
    cache.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("PRECIS_FOOTPRINT_CACHE", str(cache))

    with caplog.at_level(logging.WARNING):
        design = kernel._design(_batch(), None)
        kernel._build_ir(design)

    assert "CGHOST1" in caplog.text
    for record in caplog.records:
        assert "CREAL1" not in record.getMessage()
