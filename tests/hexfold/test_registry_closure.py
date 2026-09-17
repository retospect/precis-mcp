"""registry.closure -- cycle-basis frame residual (SPEC 12.2, 0.2 slice C).

``registry.redundant`` (a tree: ``registry:`` lines are in register by
construction) and ``registry.closure`` (a cycle in the part graph: fuse/
bond edges compose to a residual number of symmetry steps) are mutually
exclusive per part graph -- a cyclic graph never emits redundant, an
acyclic one never emits closure.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.check import check
from hexfold.report import Profile

_ROOT = Path(__file__).resolve().parents[2] / "hexfold"
_EXAMPLE = (_ROOT / "examples" / "tube_ring_closure.hx").read_text(encoding="utf-8")


def _net(text: str, *, profile: Profile = Profile.DEFAULT) -> Net:
    return build(text, profile=profile, strict=False)


def _closures(net: Net) -> list:
    return [f for f in net.report.findings if f.code == "registry.closure"]


def _redundant(net: Net) -> list:
    return [f for f in net.report.findings if f.code == "registry.redundant"]


def test_example_closes_with_nonzero_residual() -> None:
    # a.out --fuse k=1--> b.in ; a.in --fuse k=0--> b.out : both (5,0)
    # tube ends, N=5.  Tree edge a->b (k=1) contributes +1 step; closing
    # the a.in/b.out edge (k=0) reversed contributes -0; residual = 1 mod 5.
    net = _net(_EXAMPLE)
    closures = _closures(net)
    assert len(closures) == 1
    f = closures[0]
    assert f.severity.name == "WARN"
    data = dict(f.data)
    assert data["residual"] == 1
    assert data["period"] == 5
    assert set(data["cycle"]) == {"a", "b"}


def test_matching_k_closes_with_zero_residual_info() -> None:
    text = (
        "hexfold 0.2\n"
        "a: tube(5,0, len=3)\n"
        "b: tube(5,0, len=3)\n"
        "a.out --fuse k=1--> b.in\n"
        "a.in --fuse k=1--> b.out\n"
    )
    net = _net(text)
    closures = _closures(net)
    assert len(closures) == 1
    f = closures[0]
    assert f.severity.name == "INFO"
    data = dict(f.data)
    assert data["residual"] == 0
    assert data["period"] == 5
    assert "residual 0" in f.message


@pytest.mark.parametrize("path", ["pillar.hx", "tube_fuse.hx"])
def test_tree_examples_emit_redundant_not_closure(path: str) -> None:
    text = (_ROOT / "examples" / path).read_text(encoding="utf-8")
    net = _net(text)
    assert not _closures(net)
    if "registry:" in text:
        assert _redundant(net)
        for f in _redundant(net):
            assert f.severity.name == "INFO"
    else:
        assert not _redundant(net)


def test_cyclic_registry_line_emits_no_redundant() -> None:
    # a registry: line on a part graph that also has a cycle: the cycle's
    # closure findings cover it; registry.redundant does not fire.
    text = (
        "hexfold 0.2\n"
        "a: tube(5,0, len=3)\n"
        "b: tube(5,0, len=3)\n"
        "a.out --fuse k=1--> b.in\n"
        "a.in --fuse k=0--> b.out\n"
        "registry: a.in == b.out\n"
    )
    net = _net(text)
    assert _closures(net)
    assert not _redundant(net)


def test_strict_promotes_closure_to_error() -> None:
    net = _net(_EXAMPLE, profile=Profile.STRICT)
    closures = _closures(net)
    assert len(closures) == 1
    assert closures[0].severity.name == "ERROR"
    assert not net.report.ok


def test_default_profile_stays_ok() -> None:
    net = _net(_EXAMPLE)
    assert net.report.ok


@pytest.mark.parametrize(
    "path", sorted(Path(p).name for p in glob.glob(str(_ROOT / "examples" / "*.hx")))
)
def test_no_existing_example_gains_a_closure_warning(path: str) -> None:
    """Regression guard: none of the pre-slice-C examples turn a WARN
    (or ERROR) registry.closure -- nanobud_22.hx's [2+2] cycloaddition
    authors two bonds between the same bud/host pair, which is a genuine
    (if harmless, residual-0) part-graph cycle and now legitimately
    surfaces an INFO registry.closure; that is a new INFO finding, not a
    regression, since it was never a tree to begin with."""
    if path == "tube_ring_closure.hx":
        pytest.skip("the new slice-C example is expected to warn")
    text = (_ROOT / "examples" / path).read_text(encoding="utf-8")
    rep = check(text)
    assert rep.ok
    bad = [
        f
        for f in rep.findings
        if f.code == "registry.closure" and f.severity.name != "INFO"
    ]
    assert not bad, [f.message for f in bad]
