"""H2: several radii along one tunnel -- sheet | (18,18) -> (12,12) -> (6,6) |
sheet through ``fuse{reducer}`` + ``opening(port=)``, read by the se path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hexfold.build import build
from hexfold.check import check
from hexfold.report import Severity
from precis_se.atomic.generators.hexfold_spec import build_hexfold

_EXAMPLE = (
    Path(__file__).resolve().parent.parent / "hexfold/examples/tunnel_two_steps.hx"
)


@pytest.fixture(scope="module")
def text() -> str:
    return _EXAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def findings(text: str) -> list[dict[str, Any]]:
    return build_hexfold({"spec": text}).topology["report"]["findings"]


def test_two_step_tunnel_builds_without_errors(findings: list[dict[str, Any]]) -> None:
    assert [f for f in findings if f["severity"] == "ERROR"] == []


def test_junction_strain_per_seam_and_mouth(findings: list[dict[str, Any]]) -> None:
    wheres = sorted(f["where"] for f in findings if f["code"] == "junction.strain")
    assert wheres == [
        "opening:a",
        "opening:b",
        "reducer:t1->t2:inner",
        "reducer:t1->t2:outer",
        "reducer:t2->t3:inner",
        "reducer:t2->t3:outer",
    ]


def test_reducer_rings_balanced_per_reducer(findings: list[dict[str, Any]]) -> None:
    rings = [f["data"] for f in findings if f["code"] == "reducer.rings"]
    assert len(rings) == 2
    for d in rings:
        assert d["pentagons"] == d["heptagons"] > 0


def test_one_component_and_hexfold_check_clean(text: str) -> None:
    net = build(text, strict=False)
    parent = list(range(len(net.atoms)))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in net.bonds:
        parent[root(i)] = root(j)
    assert len({root(x) for x in range(len(net.atoms))}) == 1
    assert [f for f in check(text).findings if f.severity == Severity.ERROR] == []


def test_origin_unset_with_sheet_first_builds_clean(text: str) -> None:
    assert "origin" not in "\n".join(
        ln for ln in text.splitlines() if not ln.startswith("#")
    )
    found = build_hexfold({"spec": text}).topology["report"]["findings"]
    assert [f for f in found if f["severity"] == "ERROR"] == []


def test_junction_records_are_consistent_with_the_net(text: str) -> None:
    net = build(text, strict=False)
    bonds = {(min(i, j), max(i, j)) for i, j, _ in net.bonds}
    rings = set(net.rings)
    assert len(net.junctions) == 6
    for jr in net.junctions:
        assert all(0 <= a < len(net.atoms) for a in jr.atoms)
        assert jr.fused
        for a, b in jr.fused:
            assert (min(a, b), max(a, b)) in bonds
        for f in jr.faces:
            assert f in rings or tuple(sorted(f)) in {tuple(sorted(r)) for r in rings}
