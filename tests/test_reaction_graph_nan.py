"""gr460408: a raw NaN barrier (graph straight off the engine, pre-persist) is
handled like the persisted JSON null by every reaction_graph consumer."""

from __future__ import annotations

import itertools
import math

from precis.utils import reaction_graph as rg

NAN = float("nan")


def _graph(barriers: list[float | None]) -> dict:
    n = len(barriers)
    return {
        "nodes": [{"id": f"s{i}", "rel_energy": 0.0} for i in range(n + 1)],
        "links": [
            {"source": f"s{i}", "target": f"s{i + 1}", "barrier": b}
            for i, b in enumerate(barriers)
        ],
    }


def test_rate_limiting_is_order_independent_with_nan():
    for order in itertools.permutations([NAN, 0.4, 1.2]):
        out = rg.rate_limiting(_graph(list(order)), "s0", "s3")
        assert out is not None
        assert out["ea"] == 1.2


def test_nan_matches_null():
    a, b = _graph([NAN, 0.5, 0.9]), _graph([None, 0.5, 0.9])
    assert rg.rate_limiting(a, "s0", "s3") == rg.rate_limiting(b, "s0", "s3")
    assert rg.energetic_span(a, "s0", "s3") == rg.energetic_span(b, "s0", "s3")
    assert rg.barriers_ranked(a) == rg.barriers_ranked(b)
    assert rg.selectivity(a, "s0", "s3") == rg.selectivity(b, "s0", "s3")


def test_no_raw_nan_escapes():
    g = _graph([NAN, NAN, 0.3])
    top = rg.rate_limiting(g, "s0", "s3")
    assert top is not None and top["ea"] == 0.3
    span = rg.energetic_span(g, "s0", "s3")
    assert span is not None and math.isfinite(span)
    ranked = rg.barriers_ranked(g)
    assert all(r["ea"] is None or math.isfinite(r["ea"]) for r in ranked)
    assert ranked[-1]["ea"] is None
    only = rg.rate_limiting(_graph([NAN]), "s0", "s1")
    assert only is not None and only["ea"] is None
