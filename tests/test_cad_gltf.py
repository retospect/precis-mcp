"""IR → glTF writer (ADR 0041 web bundle). Pure — no store, no heavy kernel."""

from __future__ import annotations

import json
import struct

import pytest

from precis.cad.gltf import component_colors, solid_available, to_glb
from precis.cad.scene import parse_source

_ASM = """
component shaft
rod   add  cyl:r5h40   @0,0,-20
component hub
plate add  cyl:r20h10
bore  cut  cyl:r5.1h12 @0,0,-1
"""


def _parse_glb(glb: bytes) -> dict:
    magic, ver, total = struct.unpack("<III", glb[:12])
    assert magic == 0x46546C67 and ver == 2 and total == len(glb)
    jlen, jtype = struct.unpack("<II", glb[12:20])
    assert jtype == 0x4E4F534A  # 'JSON'
    return json.loads(glb[20 : 20 + jlen])


def test_features_glb_is_valid_and_named_per_feature() -> None:
    spec = parse_source(_ASM)
    gltf = _parse_glb(to_glb(spec, mode="features"))
    names = [n["name"] for n in gltf["nodes"]]
    assert names == ["rod", "plate", "bore"]
    # POSITION accessors carry min/max (glTF spec requires it)
    pos = gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    assert "min" in gltf["accessors"][pos] and "max" in gltf["accessors"][pos]


def test_parts_get_distinct_colours() -> None:
    spec = parse_source(_ASM)
    colors = component_colors(spec.components)
    assert colors["shaft"] != colors["hub"]
    gltf = _parse_glb(to_glb(spec, mode="features"))
    factors = {
        tuple(m["pbrMetallicRoughness"]["baseColorFactor"][:3])
        for m in gltf["materials"]
    }
    assert len(factors) >= 2  # at least the two parts differ


def test_cut_features_are_translucent() -> None:
    spec = parse_source(_ASM)
    gltf = _parse_glb(to_glb(spec, mode="features"))
    modes = [m.get("alphaMode") for m in gltf["materials"]]
    alphas = [
        m["pbrMetallicRoughness"]["baseColorFactor"][3] for m in gltf["materials"]
    ]
    assert "BLEND" in modes  # the 'bore' cut is translucent
    assert any(a < 1.0 for a in alphas)


def test_solid_mode_needs_extra() -> None:
    spec = parse_source(_ASM)
    if solid_available():
        gltf = _parse_glb(to_glb(spec, mode="solid"))
        # folded: one welded mesh per component
        assert {n["name"] for n in gltf["nodes"]} == {"shaft", "hub"}
    else:
        from precis.cad.export import ExportError

        with pytest.raises(ExportError):
            to_glb(spec, mode="solid")
