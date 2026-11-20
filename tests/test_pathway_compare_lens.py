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


def _cand_ea(slug: str, ea: float) -> dict[str, Any]:
    """A candidate whose rate-limiting barrier is exactly ``ea`` (both route
    steps) — distinct raw RATEs that may round to the same 2-dp display."""
    meta = _meta(_results(_records([0]), [0]))
    graph = meta["graph"]
    for link in graph["links"]:
        link["barrier"] = ea
    root, target = analysis.roots(graph, meta["results"])
    return {
        "slug": slug,
        "ref_id": 1000 + abs(hash(slug)) % 1000,
        "lever": "Pd",
        "graph": graph,
        "root": root,
        "target": target,
    }


def test_compare_numeric_where_uses_raw_measurements() -> None:
    """0.121 and 0.124 both display as 0.12, but where={'RATE': {'max':
    0.122}} must keep 0.121 and drop 0.124 (gr592637 review)."""
    out = compare_toon(
        [
            _cand_ea("run-lo", 0.121),
            _cand_ea("run-hi", 0.124),
            _cand_ea("run-far", 0.5),
        ],
        lens_args={"where": {"RATE": {"max": 0.122}}},
        lens_call=_call,
    )
    assert "(1–1 of 1 pathways matching where (3 total))" in out
    assert "run-lo" in out
    assert "run-hi" not in out
    assert "run-far" not in out


def test_compare_numeric_sort_uses_raw_measurements() -> None:
    """``sort='-RATE'`` orders by the raw barrier — 0.124 ahead of 0.121 —
    even though both render as 0.12."""
    out = compare_toon(
        [
            _cand_ea("run-far", 0.5),
            _cand_ea("run-lo", 0.121),
            _cand_ea("run-hi", 0.124),
        ],
        lens_args={"sort": "-RATE", "columns": ["cand", "RATE"]},
        lens_call=_call,
    )
    body = out.split("left out")[0]
    assert body.index("run-far") < body.index("run-hi") < body.index("run-lo")


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


def test_compare_at_potential_marks_unshifted_rows() -> None:
    """The paged response must not name every pre-CHE slug in the fixed
    header — the header sits outside the byte guard, so a long list can
    exceed the response cap and deny even a one-row page (gr592637
    review). The header carries the count; the shown row is marked."""
    out = compare_toon(_cands(2), U=0.2, lens_args={"limit": 1}, lens_call=_call)
    assert "2 candidate(s) have no n_H" in out
    assert "shown unshifted: run-00, run-01" not in out
    assert "[no n_H — unshifted]" in out  # the one shown row is marked


def test_compare_at_potential_legacy_text_names_unshifted_candidates() -> None:
    """The plain (non-lens) dump keeps the full unshifted list in its
    header — unit-level readers see every slug in one place."""
    out = compare_toon(_cands(2), U=0.2)
    assert "shown unshifted: run-00, run-01" in out
