"""td568787: the closed-form fork window — margin(U) per branch point,
U_sel, the positive-margin U interval and the reversible-intermediate
flag — over a stored reaction graph (pure, no DB)."""

from __future__ import annotations

import pytest

from precis.utils import reaction_graph as rg


def _pcet_fork_graph() -> dict:
    """A→B→T route; at A the on-route +H supply step forks against the
    chemical A→C. margin(U) = (−0.20 − 0.0) − (0.10 + U) = −0.30 − U,
    so the window edge is −0.300 V."""
    return {
        "nodes": [
            {"id": "A", "rel_energy": 0.0, "n_H": 0},
            {"id": "B", "rel_energy": 0.10, "n_H": 1},
            {"id": "C", "rel_energy": -0.20, "n_H": 0},
            {"id": "T", "rel_energy": -0.05, "n_H": 1},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "supply"},
            {"source": "A", "target": "C", "kind": "reaction", "barrier": 0.5},
            {"source": "B", "target": "T", "kind": "reaction", "barrier": 0.4},
        ],
    }


def _flat_loss_graph() -> dict:
    """R→P→T route; the only fork is chemical vs chemical — the on-route
    step costs +0.20 eV, the side step −0.10 eV, so the margin is −0.30
    at every U."""
    return {
        "nodes": [
            {"id": "R", "rel_energy": 0.0, "n_H": 0},
            {"id": "P", "rel_energy": 0.20, "n_H": 0},
            {"id": "S", "rel_energy": -0.10, "n_H": 0},
            {"id": "T", "rel_energy": -0.40, "n_H": 0},
        ],
        "links": [
            {"source": "R", "target": "P", "kind": "reaction", "barrier": 0.6},
            {"source": "R", "target": "S", "kind": "reaction", "barrier": 0.3},
            {"source": "P", "target": "T", "kind": "reaction", "barrier": 0.2},
        ],
    }


def test_fork_margin_hand_computed():
    fs = rg.forks(_pcet_fork_graph(), "A", "T")
    assert len(fs) == 1
    fork = fs[0]
    assert fork["node"] == "A" and fork["step"] == "A→B"
    assert [s["step"] for s in fork["sides"]] == ["A→C"]
    assert fork["sides"][0]["m0"] == pytest.approx(-0.30)
    assert fork["sides"][0]["slope"] == pytest.approx(-1.0)
    assert rg.fork_margin(fork, 0.0) == pytest.approx(-0.30)
    assert rg.fork_margin(fork, -1.0) == pytest.approx(0.70)


def test_window_scalars_on_bounded_domain():
    out = rg.fork_window(_pcet_fork_graph(), "A", "T", bounds=(-1.0, 0.5), u_l=-0.10)
    assert out is not None
    assert out["U_sel"] == pytest.approx(-1.0)
    assert out["sel_margin_at_Usel"] == pytest.approx(0.70)
    assert out["u_sel_on_bound"] is True
    # the todo's tolerance: the window edge within 1 mV
    assert out["window_upper_edge"] == pytest.approx(-0.300, abs=1e-3)
    assert out["edge_fork"] == "A→B vs A→C"
    assert out["worst_fixed_margin"] is None
    assert out["U_sel_window"]["hi"] == pytest.approx(-0.300, abs=1e-3)


def test_u_l_sets_the_upper_edge():
    out = rg.fork_window(_pcet_fork_graph(), "A", "T", bounds=(-1.0, 0.5), u_l=-0.50)
    assert out is not None
    assert out["window_upper_edge"] == pytest.approx(-0.50)
    assert out["edge_fork"] == "U_L"


def test_no_bounds_means_no_u_sel_but_the_edge_still_reports():
    out = rg.fork_window(_pcet_fork_graph(), "A", "T")
    assert out is not None
    assert out["U_sel"] is None
    assert out["sel_margin_at_Usel"] is None
    assert out["u_sel_on_bound"] is None
    assert out["window_upper_edge"] == pytest.approx(-0.300, abs=1e-3)
    assert out["U_sel_window"]["hi"] == pytest.approx(-0.300, abs=1e-3)
    assert out["U_sel_window"]["lo"] is None


def test_empty_window_names_the_losing_fork():
    out = rg.fork_window(_flat_loss_graph(), "R", "T", bounds=(-1.0, 0.5))
    assert out is not None
    assert out["U_sel_window"] is None
    assert out["losing_fork"] == "R→P vs R→S"
    assert out["edge_fork"] == "R→P vs R→S"
    assert out["worst_fixed_margin"] == pytest.approx(-0.30)
    assert out["window_upper_edge"] is None


def test_supply_side_is_not_a_competitor():
    base = rg.fork_window(_pcet_fork_graph(), "A", "T", bounds=(-1.0, 0.5), u_l=-0.10)
    g = _pcet_fork_graph()
    g["nodes"].append({"id": "D", "rel_energy": 0.90, "n_H": 1})
    g["links"].append({"source": "A", "target": "D", "kind": "supply"})
    assert rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5), u_l=-0.10) == base
    assert rg.forks(g, "A", "T") == rg.forks(_pcet_fork_graph(), "A", "T")


def test_no_potential_lever_is_unavailable_not_zero():
    g = _pcet_fork_graph()
    for n in g["nodes"]:
        del n["n_H"]
    assert rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5)) is None
    fs = rg.forks(g, "A", "T")
    assert fs[0]["sides"][0]["m0"] == pytest.approx(-0.30)
    assert fs[0]["sides"][0]["slope"] is None
    assert rg.fork_margin(fs[0], -0.5) is None


def test_no_path_and_no_forks_are_unavailable_not_empty():
    assert rg.fork_window(_pcet_fork_graph(), "A", "ZZZ") is None
    chain = {
        "nodes": [
            {"id": "X", "rel_energy": 0.0, "n_H": 0},
            {"id": "Y", "rel_energy": -0.1, "n_H": 0},
        ],
        "links": [{"source": "X", "target": "Y", "kind": "reaction"}],
    }
    assert rg.fork_window(chain, "X", "Y") is None


def test_reversible_flag_set_and_unset():
    # the root has no incoming step → not flagged
    assert rg.forks(_pcet_fork_graph(), "A", "T")[0]["reversible"] is False

    # fed by a +H supply bridge → the intermediate is reservoir-equilibrated
    g = _pcet_fork_graph()
    g["nodes"].insert(0, {"id": "R", "rel_energy": 0.30, "n_H": 0})
    g["links"].insert(0, {"source": "R", "target": "A", "kind": "supply"})
    assert rg.forks(g, "R", "T")[0]["reversible"] is True

    # a committed exergonic reaction in (reverse barrier 0.4 − (−0.9) = 1.3)
    g = _pcet_fork_graph()
    g["nodes"].insert(0, {"id": "R", "rel_energy": 0.90, "n_H": 0})
    g["links"].insert(
        0, {"source": "R", "target": "A", "kind": "reaction", "barrier": 0.4}
    )
    assert rg.forks(g, "R", "T")[0]["reversible"] is False

    # a near-thermoneutral step in (reverse barrier 0.35 − 0.10 = 0.25)
    g = _pcet_fork_graph()
    g["nodes"].insert(0, {"id": "R", "rel_energy": -0.10, "n_H": 0})
    g["links"].insert(
        0, {"source": "R", "target": "A", "kind": "reaction", "barrier": 0.35}
    )
    assert rg.forks(g, "R", "T")[0]["reversible"] is True


def test_fork_with_missing_side_energy_is_listed_margin_none():
    g = _pcet_fork_graph()
    del g["nodes"][2]["rel_energy"]  # C unmeasured
    fork = rg.forks(g, "A", "T")[0]
    assert fork["sides"][0]["m0"] is None
    assert rg.fork_margin(fork, -0.5) is None
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5))
    assert out is not None
    assert out["unknown_forks"] == ["A→B vs A→C"]
    assert out["U_sel"] is None and out["U_sel_window"] is None
    assert out["forks"][0]["sides"][0]["m0"] is None


def test_singleton_window_at_the_u_l_cut_is_valid():
    # review td659953: bounds (-1, 0.5) with U_L = -1 leave exactly the
    # point U = -1, where the margin is +0.70 — a valid one-point window,
    # not an empty one.
    out = rg.fork_window(_pcet_fork_graph(), "A", "T", bounds=(-1.0, 0.5), u_l=-1.0)
    assert out is not None
    assert out["U_sel_window"] is not None
    assert out["U_sel_window"]["lo"] == pytest.approx(-1.0)
    assert out["U_sel_window"]["hi"] == pytest.approx(-1.0)
    assert out["window_upper_edge"] == pytest.approx(-1.0)
    assert out["edge_fork"] == "U_L"
    assert out["losing_fork"] is None
    assert out["sel_margin_at_Usel"] == pytest.approx(0.70)


def test_singleton_on_a_zero_crossing_is_tied_not_won():
    # the one domain point (lo = U_L = -0.30) sits exactly on the fork's
    # zero crossing: margin 0 is tied, not won, so the window is empty
    # and the bound — not the fork — emptied it.
    out = rg.fork_window(_pcet_fork_graph(), "A", "T", bounds=(-0.30, 0.5), u_l=-0.30)
    assert out is not None
    assert out["U_sel_window"] is None
    assert out["losing_fork"] == "bounds"
    assert out["edge_fork"] == "bounds"


def test_contradictory_margins_name_a_fork_not_the_cut():
    # one side's margin is -0.50 - U (wins only below -0.50), the other's
    # 0.30 + U (wins only above -0.30): no U wins both even before any
    # cut, so a U_L that cuts further still names the fork, not "U_L".
    g = {
        "nodes": [
            {"id": "A", "rel_energy": 0.0, "n_H": 0},
            {"id": "B", "rel_energy": 0.0, "n_H": 1},
            {"id": "C", "rel_energy": -0.50, "n_H": 0},
            {"id": "D", "rel_energy": 0.30, "n_H": 2},
            {"id": "T", "rel_energy": -0.10, "n_H": 1},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "supply"},
            {"source": "A", "target": "C", "kind": "reaction", "barrier": 0.5},
            {"source": "A", "target": "D", "kind": "reaction", "barrier": 0.6},
            {"source": "B", "target": "T", "kind": "reaction", "barrier": 0.4},
        ],
    }
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5), u_l=-2.0)
    assert out is not None
    assert out["U_sel_window"] is None
    assert out["losing_fork"] == "A→B vs A→C | A→D"
    assert out["edge_fork"] == "A→B vs A→C | A→D"


def test_nan_side_fields_read_as_unknown_not_nan():
    # a raw NaN off the engine (pre-persist) is unavailable data, like a
    # persisted null — never a NaN margin that poisons the comparisons.
    g = _pcet_fork_graph()
    g["nodes"][2]["rel_energy"] = float("nan")  # C unmeasured
    fork = rg.forks(g, "A", "T")[0]
    assert fork["sides"][0]["m0"] is None
    assert rg.fork_margin(fork, -0.5) is None
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5))
    assert out is not None
    assert out["unknown_forks"] == ["A→B vs A→C"]
    assert out["U_sel"] is None and out["U_sel_window"] is None

    g = _pcet_fork_graph()
    g["nodes"][2]["n_H"] = float("nan")  # C's stoichiometry unmeasured
    fork = rg.forks(g, "A", "T")[0]
    assert fork["sides"][0]["slope"] is None
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5))
    assert out is not None
    assert out["unknown_forks"] == ["A→B vs A→C"]


def test_window_empty_only_by_the_u_l_cut_names_the_limit():
    # side step adds a hydrogen chemically (n_H change +1 vs on-route 0):
    # margin = 0.40 + U > 0 only for U > −0.40, so U_L = −0.50 empties it.
    g = {
        "nodes": [
            {"id": "A", "rel_energy": 0.0, "n_H": 0},
            {"id": "B", "rel_energy": 0.0, "n_H": 0},
            {"id": "C", "rel_energy": 0.40, "n_H": 1},
            {"id": "T", "rel_energy": -0.20, "n_H": 0},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "reaction", "barrier": 0.4},
            {"source": "A", "target": "C", "kind": "reaction", "barrier": 0.4},
            {"source": "B", "target": "T", "kind": "reaction", "barrier": 0.2},
        ],
    }
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5), u_l=-0.50)
    assert out is not None
    assert out["U_sel_window"] is None
    assert out["losing_fork"] == "U_L"
    assert out["edge_fork"] == "U_L"

    # without the cut the same window is bounded by the domain edge
    out = rg.fork_window(g, "A", "T", bounds=(-1.0, 0.5))
    assert out is not None
    assert out["U_sel_window"] == {"lo": pytest.approx(-0.40), "hi": 0.5}
    assert out["edge_fork"] == "bounds"
