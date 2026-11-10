"""gr460408: a raw NaN barrier (graph straight off the engine, pre-persist) is
handled like the persisted JSON null by every reaction_graph consumer."""

from __future__ import annotations

import itertools
import math

import pytest

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
    assert span is None
    ranked = rg.barriers_ranked(g)
    assert all(r["ea"] is None or math.isfinite(r["ea"]) for r in ranked)
    assert ranked[-1]["ea"] is None
    only = rg.rate_limiting(_graph([NAN]), "s0", "s1")
    assert only is not None and only["ea"] is None


@pytest.mark.parametrize("barrier", [None, NAN, float("inf"), float("-inf")])
def test_span_is_unavailable_with_any_unknown_route_barrier(barrier):
    graph = _graph([1.2, barrier, 0.3])

    assert rg.energetic_span(graph, "s0", "s3") is None
    assert rg.summarize(graph, "s0", "s3")["span"] is None


def test_span_is_unavailable_when_route_barrier_key_is_missing():
    graph = _graph([0.4])
    graph["links"][0].pop("barrier")

    assert rg.energetic_span(graph, "s0", "s1") is None


def test_span_preserves_measured_zero_and_supply_links():
    graph = _graph([None, 0.0, 0.7])
    graph["links"][0]["kind"] = "supply"
    graph["nodes"][1]["rel_energy"] = -0.5
    graph["nodes"][2]["rel_energy"] = -0.5

    assert rg.energetic_span(graph, "s0", "s2") == 0.0
    assert rg.energetic_span(graph, "s0", "s3") == pytest.approx(0.7)


def test_span_ignores_unknown_barriers_off_the_route():
    graph = _graph([0.4, 0.7])
    graph["links"].append({"source": "s0", "target": "side", "barrier": None})

    assert rg.energetic_span(graph, "s0", "s2") == pytest.approx(0.7)


def test_unknown_span_is_rendered_as_na():
    from precis_pathway.toon_views import analysis_text, compare_toon

    graph = _graph([None, 0.7])
    meta = {"graph": graph, "results": {"pathway": ["s0", "s1", "s2"]}}

    assert "energetic span (whole-path apparent barrier): n/a" in analysis_text(meta)
    comparison = compare_toon(
        [{"slug": "unknown", "graph": graph, "root": "s0", "target": "s2"}]
    )
    row = next(ln for ln in comparison.splitlines() if ln.startswith("unknown\t"))
    assert row.split("\t")[-2] == "n/a"  # SPAN


def test_missing_barrier_has_no_measured_spread_or_verdict():
    # gr549592: a null barrier carried std 0.0 and conf "ok" downstream —
    # both read as a clean measurement. std is None, conf is "n/a".
    rl = rg.rate_limiting(_graph([None]), "s0", "s1")
    assert rl is not None and rl["ea"] is None and rl["std"] is None

    ranked = rg.barriers_ranked(_graph([None]))
    assert ranked[0]["std"] is None and ranked[0]["conf"] == "n/a"

    flagged = _graph([None])
    flagged["links"][0]["low_confidence"] = True
    assert rg.barriers_ranked(flagged)[0]["conf"] == "low"


def test_analysis_view_shows_na_for_missing_barriers():
    from precis_pathway.toon_views import analysis_text, step_view, steps_toon

    meta = {
        "graph": _graph([None, None]),
        "results": {"pathway": ["s0", "s1", "s2"]},
    }

    ana = analysis_text(meta)
    assert "rate-limiting: n/a" in ana
    assert "energetic span (whole-path apparent barrier): n/a" in ana
    # no fake measured zero and no "ok" trust verdict on empty barriers
    assert "0.00" not in ana
    assert "\tok" not in ana
    assert "s0→s1\tn/a\tn/a\tn/a" in ana

    steps = steps_toon(meta)
    assert "s1→s2\tn/a\tn/a" in steps
    assert "\tok" not in steps

    edge = meta["graph"]["links"][0]
    assert "n/a" in step_view(meta, "pw1", edge)
    assert "0.00" not in step_view(meta, "pw1", edge)


def test_analysis_view_shows_real_values_when_barriers_exist():
    from precis_pathway.toon_views import analysis_text, compare_toon, steps_toon

    meta = {
        "graph": _graph([0.4, 0.7]),
        "results": {"pathway": ["s0", "s1", "s2"]},
    }

    ana = analysis_text(meta)
    assert "rate-limiting: s1→s2   Ea = 0.70 ± 0.00 eV" in ana
    assert "s0→s1\t0.40\t0.00\tok" in ana
    assert "n/a" not in ana

    assert "s1→s2\t0.70" in steps_toon(meta)

    comparison = compare_toon(
        [{"slug": "measured", "graph": meta["graph"], "root": "s0", "target": "s2"}]
    )
    row = next(ln for ln in comparison.splitlines() if ln.startswith("measured\t"))
    last = row.split("\t")
    assert last[-2] == "0.70" and last[-1] == "ok"  # SPAN, conf
    assert last[2] == "n/a"  # no gate supplied → n/a, never a blank or fake pass


def test_compare_view_shows_na_for_missing_barriers():
    from precis_pathway.toon_views import compare_toon

    graph = _graph([None])
    comparison = compare_toon(
        [{"slug": "unknown", "graph": graph, "root": "s0", "target": "s1"}]
    )
    row = next(ln for ln in comparison.splitlines() if ln.startswith("unknown\t"))
    cells = row.split("\t")
    # gate, ‡ barrier column, RATE, SPAN and conf are all n/a — none reads "ok"
    assert cells.count("n/a") == 5
    assert "ok" not in row
