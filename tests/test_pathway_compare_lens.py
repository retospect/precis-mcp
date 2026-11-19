"""``view='compare'`` through the table lens (gr592637) — pure over rows."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput
from precis.utils import reaction_graph as analysis
from precis_pathway.toon_views import compare_toon
from tests.test_trust_gate import _meta, _records, _results


def _cands(n: int) -> list[dict[str, Any]]:
    meta = _meta(_results(_records([0]), [0]))
    graph = meta["graph"]
    root, target = analysis.roots(graph, meta["results"])
    return [
        {
            "slug": f"run-{i:02d}",
            "ref_id": 1000 + i,
            "lever": "Pd" if i % 2 else "Pt",
            "graph": graph,
            "root": root,
            "target": target,
            "gate": "pass" if i % 3 else "blocked (twin)",
        }
        for i in range(n)
    ]


def _call(a: dict[str, Any]) -> str:
    return f"get(kind='pathway', id='pw1', view='compare', args={a!r})"


def test_compare_limit_and_cursor() -> None:
    cands = _cands(12)
    first = compare_toon(cands, lens_args={"limit": 5}, lens_call=_call)
    assert "(1–5 of 12 pathways)" in first
    assert "left out (7 pathways)" in first and "gate: " in first
    m = re.search(r"'cursor': '([^']+)'", first)
    assert m is not None
    rest = compare_toon(
        cands, lens_args={"limit": 5, "cursor": m.group(1)}, lens_call=_call
    )
    assert "(6–10 of 12 pathways)" in rest
    shown = re.findall(r"run-\d\d", first + rest)
    assert len(shown) == len(set(shown)) == 10


def test_compare_where_columns() -> None:
    out = compare_toon(
        _cands(12),
        lens_args={"where": {"gate": "pass"}, "columns": ["cand", "gate"]},
        lens_call=_call,
    )
    assert "(1–8 of 8 pathways matching where (12 total))" in out
    assert "blocked (twin)" not in out.split("(1–8")[0]
    with pytest.raises(BadInput):
        compare_toon(_cands(3), lens_args={"where": {"nope": 1}}, lens_call=_call)


def test_compare_without_lens_is_plain_dump() -> None:
    assert "pathways)" not in compare_toon(_cands(3))


def test_compare_at_potential_names_unshifted_candidates() -> None:
    out = compare_toon(_cands(2), U=0.2, lens_args={"limit": 1}, lens_call=_call)
    assert "shown unshifted: run-00, run-01" in out
