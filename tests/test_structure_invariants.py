"""Invariant fingerprint + comparison (structure round-trip eval comparator).

ASE-free: scenes are built by hand (add_atom at chosen Cartesian sites) so the
tests pin the *invariance* properties directly, without the [dft] slab op.
"""

from __future__ import annotations

import numpy as np

from precis.structure.cell import Cell
from precis.structure.invariants import compare, fingerprint
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene


def _cubic(a: float = 10.0) -> Cell:
    return Cell(np.eye(3) * a, (True, True, True))


def _scene(atoms: list[tuple[str, list[float]]]) -> Scene:
    """A scene from ``(element, [x, y, z] Å)`` pairs, in the given order."""
    scene = Scene(cell=_cubic())
    apply_ops(
        scene, [{"op": "add_atom", "element": el, "cart": xyz} for el, xyz in atoms]
    )
    return scene


def test_fingerprint_is_order_and_label_invariant() -> None:
    # THE property that makes round-trip comparison tractable: the same physical
    # structure reached in a different insertion order (→ different labels) must
    # fingerprint identically and score a perfect match.
    a = _scene(
        [("Pd", [0, 0, 1]), ("Pd", [3, 0, 1]), ("Cu", [0, 0, 3]), ("Pd", [3, 0, 3])]
    )
    b = _scene(
        [("Pd", [3, 0, 3]), ("Cu", [0, 0, 3]), ("Pd", [3, 0, 1]), ("Pd", [0, 0, 1])]
    )
    assert fingerprint(a) == fingerprint(b)
    assert compare(fingerprint(a), fingerprint(b))["score"] == 1.0


def test_dopant_layer_is_discriminated() -> None:
    # Same composition (3 Pd + 1 Cu), Cu in the TOP layer vs the BOTTOM layer:
    # per-layer composition must catch it even though the multiset is identical.
    top = _scene(
        [("Pd", [0, 0, 1]), ("Pd", [3, 0, 1]), ("Cu", [0, 0, 3]), ("Pd", [3, 0, 3])]
    )
    bot = _scene(
        [("Cu", [0, 0, 1]), ("Pd", [3, 0, 1]), ("Pd", [0, 0, 3]), ("Pd", [3, 0, 3])]
    )
    fa, fb = fingerprint(top), fingerprint(bot)
    assert fa.composition == fb.composition
    res = compare(fa, fb)
    assert res["parts"]["layers"] < 1.0
    assert res["score"] < 1.0


def test_atomic_overlap_caps_the_score() -> None:
    # A rebuilt structure with overlapping atoms is physically invalid — capped
    # at 0.5 no matter how well the invariants otherwise line up.
    good = _scene([("Pd", [0, 0, 1]), ("Pd", [3, 0, 1])])
    bad = _scene([("Pd", [0, 0, 1]), ("Pd", [0, 0, 1])])
    res = compare(fingerprint(good), fingerprint(bad))
    assert res["valid"] is False
    assert res["score"] <= 0.5


def test_adsorbate_above_surface_is_typed() -> None:
    # A dense metal layer + one atom above it: the above-surface atom is detected
    # as an adsorbate and classified by how many surface atoms it caps.
    s = _scene(
        [
            ("Pd", [0, 0, 1]),
            ("Pd", [2, 0, 1]),
            ("Pd", [1, 2, 1]),
            ("Pd", [3, 2, 1]),
            ("O", [1, 0, 2.5]),
        ]
    )
    fp = fingerprint(s)
    assert len(fp.adsorbate_sites) == 1
    assert fp.adsorbate_sites[0] in {"top", "bridge", "hollow", "detached"}


def test_identical_scene_scores_perfect() -> None:
    s = _scene([("Pd", [0, 0, 1]), ("Ni", [3, 0, 1]), ("Pd", [0, 0, 3])])
    res = compare(fingerprint(s), fingerprint(s))
    assert res["score"] == 1.0 and res["valid"] is True
