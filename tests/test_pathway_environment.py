"""Kinetics v2 slice 1 glue (docs/backlog/kinetics-engine-v2.md D1/D3): the
``environment:`` block through ``_prep``/``put``, the basis fold into
``compute._network_basis``/``frontier.same_network_basis``, the
``view='kinetics'`` environment lines, and the slice's acceptance replays on
the trimmed pw455722 fixture (``tests/fixtures/pw455722_results.json`` —
NO -> NH3 on Pd(111), nodes/edges/span plus the links, gas ledger and gas
references the replay needs; references reconstructed from the stored
closure dGs).

No calculator, no NEB: the replays call ``autocatpath.kinetics.solve`` on a
stub network built from the fixture's links, so the file is cheap to target
on its own. Needs the engine (>= 0.32) importable.
"""

from __future__ import annotations

import copy
import json
import math
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

pytest.importorskip("autocatpath")
pytest.importorskip("autocatpath.environment")  # engine >= 0.32

from autocatpath import kinetics
from autocatpath.config import Config
from autocatpath.ledger import compute_ledgers

from precis.errors import BadInput
from precis.quest.compute import _network_basis
from precis.quest.frontier import same_network_basis
from precis_pathway import runner
from precis_pathway._dispatch_common import _kinetics_scalars
from precis_pathway.handler import PathwayHandler
from precis_pathway.toon_views import kinetics_text

if TYPE_CHECKING:
    from precis.dispatch import Hub

FIXTURE = Path(__file__).parent / "fixtures" / "pw455722_results.json"

ENV: dict[str, Any] = {
    "T": 298.15,
    "gases": {
        "inputs": {"NO": 0.1, "H2": 1.0},
        "outputs": {"NH3": 1e-3, "H2O": 1e-3, "NH2OH": 1e-6},
    },
    "sticking": {"NO": {"s0": 0.5, "source": "pa498804"}},
}


@pytest.fixture(scope="module")
def fx() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


class _Net:
    """The slice of ``autocatpath.network.Network`` ``kinetics.solve`` and
    ``compute_ledgers`` read, rebuilt from the fixture's graph links."""

    def __init__(self, fx: dict[str, Any]) -> None:
        self.links = [(a, b) for a, b, kind in fx["links"] if kind != "reaction"]
        self.steps = [
            SimpleNamespace(
                name=f"{a}->{b}",
                reactant=SimpleNamespace(name=a),
                product=SimpleNamespace(name=b),
            )
            for a, b, kind in fx["links"]
            if kind == "reaction"
        ]
        self._states = sorted(fx["nodes"])

    def order(self) -> list[str]:
        return ["*", *(s for s in self._states if s != "*")]

    def states(self) -> list[str]:
        return list(self._states)

    def slab(self) -> None:
        return None


@pytest.fixture
def replay(fx: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """``replay(environment=None)`` -> the chain solve of pw455722 at that
    environment (``None`` = its stored legacy config). The stored nodes were
    priced at the legacy 1 bar table, so a block replay says so
    (``priced_pressures={}``) and the engine reprices nodes and refs."""
    monkeypatch.setattr(kinetics, "site_area_A2", lambda _slab: fx["site_area_A2"])
    net = _Net(fx)

    def run(environment: dict[str, Any] | None = None) -> dict[str, Any]:
        d = copy.deepcopy(fx["config"])
        d["kinetics"]["n_samples"] = 0
        kw: dict[str, Any] = {}
        if environment is not None:
            d["conditions"]["pressures"] = {}
            d["environment"] = environment
            kw["priced_pressures"] = {}
        return kinetics.solve(
            Config.from_dict(d),
            fx["nodes"],
            fx["edges"],
            net,
            fx["refs"],
            span=fx["score"]["activity"]["span_eV"],
            log=lambda *a, **k: None,
            **kw,
        )

    return run


# --- fixture sanity -----------------------------------------------------------


def test_fixture_is_trimmed_and_self_consistent(fx: dict[str, Any]) -> None:
    assert FIXTURE.stat().st_size < 200_000
    assert "structures_extxyz" not in fx
    ledgers, warns = compute_ledgers(_Net(fx))
    assert not warns
    for name, led in fx["gas_ledger"].items():
        assert ledgers[name].as_dict() == led, name


# --- acceptance (spec §Slices item 1) -----------------------------------------


def test_config_lacking_a_released_product_refuses_naming_the_gas(replay) -> None:
    env = {**ENV, "gases": {**ENV["gases"], "outputs": {"NH3": 1e-3, "H2O": 1e-3}}}
    with pytest.raises(
        ValueError, match=r"releases NH2OH .*environment\.gases\.outputs"
    ):
        replay(env)


def test_refusal_names_the_sweep_as_the_intended_path(replay) -> None:
    with pytest.raises(ValueError, match="sweep"):
        replay({"T": 298.15, "gases": {"inputs": {"NO": 0.1, "H2": 1.0}}})


def test_far_vs_one_bar_changes_selectivity_on_a_well_posed_chain() -> None:
    """The selectivity acceptance on a chain whose steady state is unique
    (one absorbing component): NO -> NH3 (+H2O) with an N2O side product.
    Products at the legacy 1 bar fallback vs the far preset: the far solve
    stops re-adsorbing them, and the target share moves."""
    from autocatpath.environment import Environment
    from autocatpath.ledger import Ledger

    t = 298.15
    refs = {
        "gas": {"NO": -3.0, "H2": -6.7, "NH3": -6.8, "H2O": -13.0, "N2O": -6.05},
        "mu": {"H": -3.35},
    }
    ledgers = {
        "*": Ledger(),
        "NO@N": Ledger(consumed=Counter({"NO": 1})),
        "NOH": Ledger(consumed=Counter({"NO": 1})),
        "NH3": Ledger(
            consumed=Counter({"NO": 1}),
            supplied=Counter({"H": 5}),
            released=Counter({"H2O": 1}),
        ),
        "N2O": Ledger(consumed=Counter({"NO": 2})),
    }
    g = {"*": 0.0, "NO@N": -0.5, "NOH": -0.3, "NH3": -0.4, "N2O": -0.2}
    edges = [
        {
            "name": "NO@N->NOH",
            "reactant": "NO@N",
            "product": "NOH",
            "barrier": {"mean": 0.6},
        }
    ]
    links = [("*", "NO@N"), ("NOH", "NH3"), ("NO@N", "N2O")]

    def selectivity(env: Environment | None) -> float:
        pressures = {"NO": 0.1, "H2": 1.0}
        g2, refs2 = (g, refs)
        if env is not None:
            g2, refs2 = kinetics.reprice(
                g, ledgers, refs, t, lambda m: 1.0, env.pressure
            )
        trans, _ = kinetics.build_transitions(
            g2, edges, links, ledgers, refs2, t, pressures, 6.5,
            target="NH3", environment=env,
        )  # fmt: skip
        theta, info = kinetics.steady_state(trans, "*")
        assert info["polished"] and info["nullity"] == 1
        prod = kinetics.production_rates(trans, theta)
        released = sum(r for r in prod.values() if r > 0)
        return prod["NH3"] / released

    one_bar = selectivity(None)  # legacy: products unlisted -> 1 bar
    far = selectivity(Environment(T=t, inputs={"NO": 0.1, "H2": 1.0}, preset="far"))
    assert 0.0 < one_bar < 1.0 and 0.0 < far <= 1.0
    assert far != pytest.approx(one_bar, rel=1e-3)


def test_far_vs_legacy_changes_selectivity_and_the_basis(replay) -> None:
    """pw455722 itself: the bases differ and so does the selectivity. Its
    chain has 13 absorbing components (every NEB step excluded), so the
    selectivity value is noise-dominated; the well-posed check is above."""
    legacy = replay()
    far = replay(
        {"T": 298.15, "gases": {"inputs": {"NO": 1.0, "H2": 1.0}, "outputs": "far"}}
    )
    assert far["selectivity"] != pytest.approx(legacy["selectivity"], abs=1e-3)
    assert far["environment_basis"] != legacy["environment_basis"]
    assert far["environment"]["gases"]["preset"] == "far"
    assert legacy["environment"]["legacy"] is True
    # far from equilibrium: products are not driven back
    assert far["q_over_k"] < legacy["q_over_k"]
    # the stamps reach the scalars precis harvests
    assert (
        _kinetics_scalars({"kinetics": far})["environment_basis"]
        == (far["environment_basis"])
    )


def test_equilibrium_outputs_give_zero_net_flux_on_every_transition(
    fx: dict[str, Any], replay
) -> None:
    ledgers, _ = compute_ledgers(_Net(fx))
    inputs = {"NO": 1.0, "H2": 1.0}
    comp = kinetics.equilibrium_composition(
        ledgers, 298.15, refs=fx["refs"], inputs=inputs, target="NH3", p_target=None
    )
    outputs = {g: p for g, p in comp.items() if g not in inputs}
    data = replay({"T": 298.15, "gases": {"inputs": inputs, "outputs": outputs}})
    k_scale = max(max(t["k_f"], t["k_b"]) for t in data["transitions"])
    for t in data["transitions"]:
        assert abs(t["net_rate"]) < 1e-6 * k_scale, t["name"]
    assert data["q_over_k"] == pytest.approx(1.0, abs=1e-6)


def test_p_total_with_a_composition_reproduces_the_explicit_table(replay) -> None:
    """The ENV table read as a composition at p_total = 8 x its sum is the
    explicit 8x table. The factor 8 keeps the scaling exact in binary, so the
    comparison is bit-for-bit: this network's chain solve has 13 absorbing
    components (no NEB barriers), so its TOF is noise-dominated and a 1e-16
    pressure difference would move it."""
    gases = ENV["gases"]
    total = math.fsum([*gases["inputs"].values(), *gases["outputs"].values()])
    scaled = replay({**ENV, "p_total": 8.0 * total})
    explicit = replay(
        {
            **ENV,
            "gases": {
                side: {g: 8.0 * p for g, p in gases[side].items()}
                for side in ("inputs", "outputs")
            },
        }
    )
    assert scaled["environment"]["gases"] == explicit["environment"]["gases"]
    assert scaled["tof"] == explicit["tof"]
    assert scaled["selectivity"] == explicit["selectivity"]
    assert scaled["coverages"] == explicit["coverages"]
    assert scaled["transitions"] == explicit["transitions"]
    assert scaled["environment_basis"] == explicit["environment_basis"]
    assert scaled["environment"]["p_total_stated"] is True
    assert scaled["environment"]["composition"]["H2"] == pytest.approx(1.0 / total)


def test_legacy_warnings_carry_no_v2_notes(replay) -> None:
    """pw455722's overall NH3 cycle leaves a parked O, which a block solve
    notes beside q_over_k; a legacy solve keeps its pre-v2 ``warnings``."""
    legacy = replay()
    assert not any("q_over_k" in w for w in legacy["warnings"])
    assert not any("sticking uncited" in w for w in legacy["warnings"])
    block = replay(ENV)
    assert any("parked fragments" in w for w in block["warnings"])


def test_uncited_sticking_is_reported_per_gas(replay) -> None:
    data = replay(ENV)
    assert "sticking uncited: H2" in data["warnings"]
    assert "sticking uncited: NO" not in data["warnings"]  # NO cites pa498804
    # a legacy replay never says it (today's trust gate, unchanged)
    assert not any(w.startswith("sticking uncited") for w in replay()["warnings"])
    # pw455722's excluded steps trip the bracket guard first; the harvest
    # is untrusted either way (the uncited note itself: test_pathway_plugin)
    assert _kinetics_scalars({"kinetics": data})["kinetics_trusted"] is False


# --- _prep / run_kinetics carry the block ------------------------------------


def test_prep_builds_the_environment_from_the_raw_config() -> None:
    cfg = runner._prep({"name": "x", "environment": ENV}, None)
    assert cfg.environment is not None
    assert cfg.environment.inputs == {"NO": 0.1, "H2": 1.0}
    assert cfg.conditions.pressures["NH3"] == 1e-3  # the legacy readers see it
    assert runner._prep({"name": "x"}, None).environment is None


def test_run_kinetics_carries_the_new_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_kinetics`` folds the engine's result verbatim and its child
    returns it whole, so the v2 keys need no plumbing — pinned here."""
    import autocatpath.ledger as ledger_mod
    import autocatpath.pipeline as pipeline_mod
    import autocatpath.thermo as thermo_mod

    seen: dict[str, Any] = {}
    canned = {
        "tof": 1e-3,
        "environment": {"T": 298.15},
        "environment_basis": "f" * 64,
        "q_over_k": 1e-20,
        "reversibility": {"a->b": 0.5},
    }

    class _FakeKinetics:
        @staticmethod
        def solve(cfg, nodes, edges, net, refs, *, span, mari, log):
            seen["environment"] = cfg.environment
            return canned

    monkeypatch.setattr(runner, "_import_kinetics", lambda: _FakeKinetics)
    monkeypatch.setattr(pipeline_mod, "_build_net", lambda cfg: None)
    monkeypatch.setattr(pipeline_mod, "ledger_refs", lambda *a, **k: {})
    monkeypatch.setattr(ledger_mod, "compute_ledgers", lambda net: ({}, []))
    monkeypatch.setattr(thermo_mod, "build_table", lambda cfg: None)
    art: Any = {"results_json": {"nodes": {}, "edges": [], "score": {}}}
    runner.run_kinetics({"name": "x", "environment": ENV}, art)
    assert art["results_json"]["kinetics"] == canned
    assert seen["environment"].outputs["NH3"] == 1e-3


# --- put: params + refusal with next= ----------------------------------------


def _put(text: str, args: dict[str, Any] | None = None) -> BadInput:
    # refusals land before any store read, so no hub is needed
    h = PathwayHandler(hub=cast("Hub", SimpleNamespace()))
    with pytest.raises(BadInput) as ei:
        h.put(id="env-x", text=text, mode="preview", args=args)
    return ei.value


def test_put_refuses_a_zero_product_pressure_naming_the_key() -> None:
    err = _put(
        'substrate: "NO"\ntarget: "NH3"\n',
        {"environment": {"T": 298.15, "gases": {"outputs": {"NH3": 0}}}},
    )
    assert "0 bar" in str(err)
    assert err.next and "environment.gases.outputs['NH3']" in str(err.next)


def test_put_refuses_a_bad_objective_naming_the_key() -> None:
    err = _put('substrate: "NO"\ntarget: "NH3"\nobjective: {competing: [CO]}\n')
    assert err.next and "objective.competing" in str(err.next)


def test_put_refuses_two_different_blocks() -> None:
    body = 'substrate: "NO"\ntarget: "NH3"\nenvironment: {T: 298.15}\n'
    err = _put(body, {"environment": {"T": 300.0}})
    assert "both the YAML body and args" in str(err)


# --- the basis fold -----------------------------------------------------------


def _pw(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config,
        "results": {"network_digest": "d1"},
        "autocatpath_version": "0.32.0",
    }


def test_network_basis_projects_the_environment_without_backfill() -> None:
    legacy = {
        "network": "ammonia",
        "conditions": {"temperature": 298.15, "pressures": {}},
    }
    a = _network_basis(_pw(legacy))
    b = _network_basis(_pw(copy.deepcopy(legacy)))
    assert a is not None and b is not None
    assert isinstance(a["environment_basis"], str)
    assert same_network_basis(a, b)  # two legacy measures, equal conditions
    other = _network_basis(
        _pw({**legacy, "conditions": {"temperature": 298.15, "pressures": {"NO": 0.5}}})
    )
    assert other is not None and not same_network_basis(a, other)
    block = _network_basis(_pw({"network": "ammonia", "environment": ENV}))
    far = _network_basis(
        _pw(
            {
                "network": "ammonia",
                "environment": {**ENV, "gases": {**ENV["gases"], "outputs": "far"}},
            }
        )
    )
    again = _network_basis(_pw({"network": "ammonia", "environment": ENV}))
    assert block is not None and far is not None and again is not None
    assert not same_network_basis(block, far)
    assert same_network_basis(block, again)


_PARITY_CONFIGS: list[dict[str, Any]] = [
    {},
    {"conditions": {"temperature": 350.0, "pressures": {"NO": 0.5, "H2": 2}}},
    {"conditions": {"pressures": {}}, "electrochemistry": {"T": 310.0}},
    {"kinetics": {"sticking": {"NO": 0.5}}, "conditions": {"pressures": {"NO": 1}}},
    {"environment": ENV},
    {"environment": {**ENV, "p_total": 10.0}},
    {"environment": {**ENV, "gases": {**ENV["gases"], "outputs": "far"}}},
    {
        "environment": {
            **ENV,
            "gases": {**ENV["gases"], "inerts": {"Ar": 0.9}, "unlisted": "reference"},
            "sticking": {"NO": 0.5, "H2": {"s0": 0.1, "source": None}},
            "electrochemical": {"provider": "che", "U_vs_RHE": -0.2},
        }
    },
    {"environment": {"T": 298.15}},
    # refused by the engine -> None on both sides
    {"environment": {"T": -1}},
    {"environment": {**ENV, "gases": {"outputs": {"NH3": 0}}}},
    {"environment": {**ENV, "gases": {"outputs": "near"}}},
    {"environment": {**ENV, "sticking": {"NO": 1.5}}},
    {"environment": {"T": 298.15, "p_total": 5.0}},
    {"environment": {**ENV, "colour": "blue"}},
    {"conditions": {"pressures": {"NO": "lots"}}},
    {"conditions": "warm"},
    # malformed legacy containers (review of kin-s1): falsy non-mapping
    # sticking reads as empty, a non-mapping kinetics is refused
    {"kinetics": {"sticking": []}},
    {"kinetics": {"sticking": ""}},
    {"kinetics": {"sticking": 0}},
    {"kinetics": {"sticking": ["NO"]}},
    {"kinetics": "fast"},
    {"kinetics": ["t_end"]},
    {"kinetics": ""},
    {"conditions": {"pressures": []}},
    {"conditions": {"pressures": ["NO"]}},
]


@pytest.mark.parametrize("config", _PARITY_CONFIGS)
def test_quest_side_basis_matches_the_engine(config: dict[str, Any]) -> None:
    """The engine-free mirror (:mod:`precis.quest.environment_basis`) and the
    engine's own adapter hash every config identically, refusals included."""
    from autocatpath.environment import environment_basis as engine_basis

    from precis.quest.environment_basis import environment_basis

    assert environment_basis(config) == engine_basis(config)


def test_null_environment_basis_never_compares() -> None:
    bad = _network_basis(_pw({"network": "ammonia", "environment": {"T": -1}}))
    assert bad is not None and bad["environment_basis"] is None
    assert not same_network_basis(bad, dict(bad))
    # a null basis fails against a keyed stamp and against a pre-field one
    keyed = {**bad, "environment_basis": "a" * 64}
    assert not same_network_basis(bad, keyed)
    assert not same_network_basis(keyed, bad)
    pre = {k: v for k, v in bad.items() if k != "environment_basis"}
    assert not same_network_basis(bad, pre)
    assert not same_network_basis(pre, bad)


# --- view='kinetics' ----------------------------------------------------------

_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "A"}, {"id": "B"}],
    "links": [
        {
            "source": "A",
            "target": "B",
            "barrier": 0.5,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        }
    ],
}


def test_kinetics_view_renders_environment_q_over_k_and_reversibility(replay) -> None:
    far = replay(
        {"T": 298.15, "gases": {"inputs": {"NO": 1.0, "H2": 1.0}, "outputs": "far"}}
    )
    meta = {
        "graph": _GRAPH,
        "results": {"pathway": ["A", "B"], "target": "B", "kinetics": far},
    }
    body = kinetics_text(meta)
    assert "A→B" in body  # the Eyring table stays
    assert "Environment: T = 298.15 K" in body
    assert "inputs H2=1.00e+00, NO=1.00e+00" in body
    assert "preset far" in body
    assert f"basis {far['environment_basis'][:12]}" in body
    assert "Q/K (overall NH3 formation)" in body
    assert "Reversibility z" in body and "*->NO@N" in body


def test_kinetics_view_without_a_solve_is_unchanged() -> None:
    body = kinetics_text(
        {"graph": _GRAPH, "results": {"pathway": ["A", "B"], "target": "B"}}
    )
    assert "Environment:" not in body and "Q/K" not in body
