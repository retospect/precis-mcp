"""The slab skill's bridging-O Cu(111) CH4 TS recipe (gr524426) — run the
ops exactly as the skill prints them and validate every endpoint.

Review fix round 1: the recipe's TS/FS geometry pushed CH4's down-H
straight into its anchor Cu (atom_overlap aCu31/aH1) and left a
stretched, still-declared C–H (bond_too_long), while the prose claimed
the validator's 1.3× bond ceiling allowed it. This test parses the
recipe back out of ``src/precis/data/skills/precis-structure-slab-help.md``
and runs it through ``apply_ops`` + ``validate`` — IS, TS and FS must
all come out clean, and the FS must be CH3* + OH* by bond bookkeeping.
"""

from __future__ import annotations

import copy
import json
import re

import numpy as np
import pytest

from precis.structure import Scene, apply_ops, validate
from precis.structure.cell import Cell

#: The O–H bond-cutoff (1.2 × the 0.97 Å covalent sum): inside this the
#: validator infers an O–H bond and H's valence budget (1) trips
#: over_valence — the TS hint keeps aH1 outside it.
OH_INFERENCE_CUTOFF = 1.2 * (0.66 + 0.31)


def _recipe_section() -> str:
    from tests._skill_roots import skill_path

    text = skill_path("precis-structure-slab-help").read_text(encoding="utf-8")
    return text.split("## Worked example — bridging-O Cu(111) CH4 TS", 1)[1]


def _is_ops() -> list[dict]:
    """The IS payload, exactly as the skill's ``text='{…}'`` prints it."""
    section = _recipe_section()
    payloads = re.findall(r"text='(\{.*?\})'", section, re.DOTALL)
    assert len(payloads) == 1, "the recipe must print exactly one IS payload"
    return json.loads(payloads[0])["ops"]


def _derive_ops(slug: str) -> list[dict]:
    """The ``ops`` array of the recipe's ``put(..., id=<slug>, ...)``,
    JSON-decoded exactly as printed."""
    chunk = _recipe_section().split(f'id="{slug}"', 1)[1]
    start = chunk.index("[", chunk.index('"ops":'))
    ops, _end = json.JSONDecoder().raw_decode(chunk[start:])
    return ops


def _is_scene() -> Scene:
    pytest.importorskip("ase")  # slab op + default site heights
    scene = Scene(cell=Cell(np.eye(3)))
    apply_ops(scene, _is_ops())
    return scene


def _errors(scene: Scene) -> list:
    return [f for f in validate(scene) if f.severity == "error"]


def _cart(scene: Scene, label: str) -> np.ndarray:
    return scene.cell.frac_to_cart(scene.atoms[label].frac)


def test_recipe_is_ts_fs_all_validate_clean() -> None:
    scene_is = _is_scene()
    assert _errors(scene_is) == []

    scene_ts = copy.deepcopy(scene_is)
    apply_ops(scene_ts, _derive_ops("cu_ch4o_ts"))
    assert _errors(scene_ts) == []

    scene_fs = copy.deepcopy(scene_is)
    apply_ops(scene_fs, _derive_ops("cu_ch4o_fs"))
    assert _errors(scene_fs) == []

    # The TS moves aH1 TOWARD the O: the H···O distance shrinks IS→TS→FS
    # and the TS H sits outside the O–H inference cutoff (no over_valence)
    # and outside every Cu's overlap floor (came clean above, pin the
    # distances so a geometry regression reads plainly).
    d_is = float(np.linalg.norm(_cart(scene_is, "aH1") - _cart(scene_is, "aO1")))
    d_ts = float(np.linalg.norm(_cart(scene_ts, "aH1") - _cart(scene_ts, "aO1")))
    d_fs = float(np.linalg.norm(_cart(scene_fs, "aH1") - _cart(scene_fs, "aO1")))
    assert d_is > d_ts > d_fs
    assert d_ts > OH_INFERENCE_CUTOFF
    c_h_ts = float(np.linalg.norm(_cart(scene_ts, "aH1") - _cart(scene_ts, "aC1")))
    assert c_h_ts < 1.3 * (0.76 + 0.31)  # declared C–H under the 1.3× ceiling

    # The FS is CH3* + OH*: the O–H bond is minted, the C–H removed, and
    # the C keeps exactly its three remaining H's.
    pairs = {frozenset((b.i, b.j)) for b in scene_fs.bonds}
    assert frozenset(("aO1", "aH1")) in pairs
    assert frozenset(("aC1", "aH1")) not in pairs
    assert sum("aC1" in p for p in pairs) == 3

    # Same atom set across all three — the NEB interpolates by label.
    assert set(scene_ts.atoms) == set(scene_is.atoms) == set(scene_fs.atoms)
