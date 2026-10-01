"""A ring-less net must survive the stick-geometry pass.

``stick_relax_pinned`` documents ``springs`` as ``(K,3)``, but
``stick_info`` built it with ``np.array(_angle_springs(net))`` — shape
``(0,)`` when the net has no rings, so the ``springs[:, 0]`` unpack raised
``IndexError: too many indices for array``. A one-period armchair tube is
precisely that net: every atom sits on a rim and no ring closes.

The topology half of ``len=1`` is fine and is gr454650's fix, covered by
``test_len1_rims.py`` — the two rims are disjoint tens and a cap fuse
succeeds. Only the derived-coordinate step fell over, and it reached the
caller as "hexfold internal error while compiling the spec", which reads
as a library bug on a legal spec. It was one.
"""

from __future__ import annotations

from hexfold.check import check

_HEAD = "hexfold 0.2\nlattice: element=C sigma=1.42\n\n"


def test_a_ringless_tube_with_geometry_on_reports_instead_of_raising() -> None:
    rep = check(_HEAD + "origin t\nt: tube(5,5,len=1)\n", geometry=True)
    # It must come back as a report at all — that was the whole defect.
    assert rep.ok
    # And the geometry pass must have actually run, not been skipped: the
    # summary finding is what proves coordinates were derived.
    assert "geom.summary" in {f.code for f in rep.findings}


def test_a_tube_with_rings_is_the_control() -> None:
    """len=2 has interior angles, so it always had a non-empty springs
    array — it is the case that was working and must keep working."""
    rep = check(_HEAD + "origin t\nt: tube(5,5,len=2)\n", geometry=True)
    assert rep.ok
    assert "geom.summary" in {f.code for f in rep.findings}


def test_the_ringless_cap_fuse_also_reaches_geometry() -> None:
    """The spec from gr454650's report, with geometry on: the fuse adds
    rings, but the tube's own patch is still ring-less on the way in."""
    rep = check(
        _HEAD
        + "origin t\n"
        + "t: tube(5,5,len=1)\n"
        + "c: cap(5,5)\n"
        + "t.out --fuse k=0--> c.in\n",
        geometry=True,
    )
    assert "geom.summary" in {f.code for f in rep.findings}
