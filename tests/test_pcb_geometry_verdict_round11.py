"""Geometry losses must invalidate both archive and stored-run verdicts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from precis.pcb import epro_verify, epro_write, run_record


def _model(shape: str = "rect", *, slot: bool = False) -> dict[str, Any]:
    # Authored directly in board coordinates: the 2 x 1 land is centered
    # at (5, 5); a 1.2 x .6 slot has cutter endpoints .3 mm either side.
    pad: dict[str, Any] = {
        "refdes": "J1",
        "pin": "1",
        "net": "N",
        "layer": "F.Cu",
        "shape": shape,
        "x": 5.0,
        "y": 5.0,
        "w": 2.0,
        "h": 1.0,
    }
    if shape == "polygon":
        pad["poly"] = [[4.0, 4.5], [6.0, 4.5], [5.4, 5.5], [4.6, 5.5]]
    if slot:
        pad.update(drill=0.6, slot=[[4.7, 5.0], [5.3, 5.0]])
    pads = [pad]
    if slot:
        pads.append({**pad, "layer": "B.Cu"})
    return {
        "layers": ["F.Cu", "B.Cu"],
        "outline": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        "pads": pads,
        "copper": [],
        "drills": [],
        "instances": [
            {
                "refdes": "J1",
                "footprint": "TEST",
                "x": 5.0,
                "y": 5.0,
                "rot": 0.0,
                "layer": "F.Cu",
            }
        ],
        "silkscreen": {"top": [], "bottom": []},
    }


def _mutated_archive(
    exported: epro_write.EproExport, mutate: Callable[[dict[str, Any]], None]
) -> bytes:
    files = dict(exported.files)
    count = 0
    for name, content in files.items():
        if not name.endswith(".epru"):
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith('{"type":"PAD"'):
                continue
            header, payload = line.split("||", 1)
            body = json.loads(payload[:-1])
            mutate(body)
            lines[i] = f"{header}||{json.dumps(body, separators=(',', ':'))}|"
            count += 1
        files[name] = "\n".join(lines)
    assert count == 1
    return epro_write.zip_epro(files)


@pytest.mark.parametrize(
    "placer,old_version",
    [
        ("anneal", 1),
        ("anneal", 2),
        ("anneal", 4),
        ("topo", 2),
        ("topo", 3),
        ("topo", 5),
    ],
)
def test_verdicts_before_board_frame_geometry_are_stale(
    placer: str, old_version: int
) -> None:
    record = run_record.refused_record(
        op="place",
        placer=placer,
        params={},
        input_digest="input",
        footprint_digest="footprint",
        verdict="old oblique-pad overlap",
        failure_class="geometry",
    )
    assert run_record._validate(1, record, "place", "input", "footprint") is record
    record["placer_code_version"] = old_version
    assert run_record._validate(1, record, "place", "input", "footprint") is None


@pytest.mark.parametrize(
    "shape,slot", [("rect", False), ("obround", True), ("polygon", False)]
)
def test_clean_pad_geometry_verifies(shape: str, slot: bool) -> None:
    model = _model(shape, slot=slot)
    exported = epro_write.epro_files(model, slug="geometry-verdict")
    verdict = epro_verify.verify_export(
        model, epro_write.zip_epro(exported.files), stats=exported.stats
    )
    assert not verdict.fails, verdict.render()


def test_verifier_rejects_lost_slot_archive() -> None:
    model = _model("obround", slot=True)
    exported = epro_write.epro_files(model, slug="geometry-verdict")

    def remove_slot(body: dict[str, Any]) -> None:
        # Same .6 mm cutter, but the .6 mm travel has disappeared.
        body["hole"] = {
            "holeType": "ROUND",
            "width": epro_write.to_mils(0.6),
            "height": epro_write.to_mils(0.6),
        }

    verdict = epro_verify.verify_export(
        model, _mutated_archive(exported, remove_slot), stats=exported.stats
    )
    assert any("pad shape" in fail for fail in verdict.fails), verdict.render()


def test_verifier_rejects_changed_slot_endpoints() -> None:
    model = _model("obround", slot=True)
    exported = epro_write.epro_files(model, slug="geometry-verdict")

    def shorten_slot(body: dict[str, Any]) -> None:
        # The same .6 mm cutter now travels only .4 mm, rather than .6.
        body["hole"]["width"] = epro_write.to_mils(1.0)

    verdict = epro_verify.verify_export(
        model, _mutated_archive(exported, shorten_slot), stats=exported.stats
    )
    assert any("pad shape" in fail for fail in verdict.fails), verdict.render()


def test_verifier_compares_hole_center_in_the_board_frame() -> None:
    want = {**_model()["pads"][0], "drill": 0.6, "drill_x": 5.2, "drill_y": 4.9}
    got = {**want, "x": 3.0, "y": 2.0, "drill_x": 3.2, "drill_y": 1.9}

    def at(x: Any, y: Any) -> tuple[float, float]:
        return float(x) + 2.0, float(y) + 3.0

    assert epro_verify._pad_geometry_matches(want, got, at)
    got["drill_x"] = got["x"]
    assert not epro_verify._pad_geometry_matches(want, got, at)


@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_verifier_rejects_extra_37_degree_pad_rotation(shape: str) -> None:
    model = _model(shape)
    exported = epro_write.epro_files(model, slug="geometry-verdict")

    def rotate(body: dict[str, Any]) -> None:
        body["padAngle"] += 37.0

    verdict = epro_verify.verify_export(
        model, _mutated_archive(exported, rotate), stats=exported.stats
    )
    assert any("pad shape" in fail for fail in verdict.fails), verdict.render()


def test_verifier_rejects_polygon_change_with_same_center_and_bounds() -> None:
    model = _model("polygon")
    exported = epro_write.epro_files(model, slug="geometry-verdict")

    def change_polygon(body: dict[str, Any]) -> None:
        # Move a top trapezoid corner by .1 mm, preserving the bbox and
        # explicit pad center, so dimensions alone cannot see the change.
        path = body["defaultPad"]["path"]
        path[5] -= epro_write.to_mils(0.1)

    verdict = epro_verify.verify_export(
        model, _mutated_archive(exported, change_polygon), stats=exported.stats
    )
    assert any("pad shape" in fail for fail in verdict.fails), verdict.render()
