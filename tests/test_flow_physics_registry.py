"""The thin-gap seam: registration, declared-input activation and refusals."""

from __future__ import annotations

import ast
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis_flow import physics as registry
from precis_flow import regime
from precis_geom import physics
from precis_se import flow, flow_registry, properties, workspace_import
from precis_se.measures import MeasureSpec
from precis_se.properties import COMPUTERS, _LazyComputer


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep registrations local to each test, including the live output table."""
    monkeypatch.setattr(registry, "PHYSICS", dict(registry.PHYSICS))
    outputs = dict(registry.MEASURANDS)
    monkeypatch.setattr(registry, "MEASURANDS", outputs)
    monkeypatch.setattr(physics, "MEASURANDS", outputs)
    monkeypatch.setattr(regime, "REGIME_GROUPS", dict(regime.REGIME_GROUPS))
    monkeypatch.setattr(
        flow_registry, "COMPUTER_GROUPS", dict(flow_registry.COMPUTER_GROUPS)
    )
    monkeypatch.setattr(flow, "FLOW_COMPUTERS", dict(flow.FLOW_COMPUTERS))
    monkeypatch.setattr(properties, "COMPUTERS", dict(properties.COMPUTERS))


def _tree(
    *, shape: str = "flow:in=p;out=q;dv=0", measures: list[Any] | None = None
) -> Any:
    return SimpleNamespace(
        blocks={
            "chamber": SimpleNamespace(
                bound_kind=None, pockets={"flow": SimpleNamespace(shape=shape)}
            )
        },
        measures=measures or [],
    )


def test_existing_computer_groups_are_lazy_and_complete() -> None:
    slugs = {
        slug for _, group in flow_registry.COMPUTER_GROUPS.values() for slug in group
    }
    assert slugs == set(flow.FLOW_COMPUTERS)
    assert len(slugs) == 8
    assert all(isinstance(COMPUTERS[slug], _LazyComputer) for slug in slugs)
    # This declaration module must remain solver-free even in a process that
    # has already imported flow. AST imports avoid an import-order-dependent test.
    source = Path(flow_registry.__file__).read_text(encoding="utf-8")
    imports = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    ]
    assert set(imports) == {"__future__", "collections.abc"}


def test_computer_registration_reaches_already_imported_consumers(
    isolated_registry: None,
) -> None:
    flow_registry.register_computers(
        "example", "module_not_imported", {"example-output": "compute"}
    )
    assert "example-output" in flow.FLOW_COMPUTERS
    assert isinstance(properties.COMPUTERS["example-output"], _LazyComputer)


def test_empty_groups_and_duplicate_ownership(isolated_registry: None) -> None:
    registry.register(registry.Physics("empty"))
    flow_registry.register_computers("empty", "unused", {})
    regime.register_group("empty", inputs=(), build=lambda: ())
    assert registry.PHYSICS["empty"].measurands == ()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.Physics("duplicate", token_keys=("dv",)))
    with pytest.raises(ValueError, match="already registered"):
        flow_registry.register_computers("duplicate", "unused", {"evenness": "f"})
    with pytest.raises(ValueError, match="already registered"):
        regime.register_group("species", inputs=(), build=lambda: ())


def test_registered_token_and_evaluate_keys_are_retained(
    isolated_registry: None,
) -> None:
    registry.register(
        registry.Physics(
            "example",
            token_keys=("example",),
            spec_keys=("example",),
            measurands=("example-output",),
        )
    )
    parsed = flow.parse_flow_shape("flow:in=p;out=q;dv=0;example=0")
    assert isinstance(parsed, flow.FlowSpec)
    assert parsed.physics_inputs == {"example": "0"}
    assert "example" in parsed.declared_keys
    assert "example-output" in physics.MEASURANDS["flow"]
    assert set(physics.MEASURANDS) == {"flow"}
    port = {
        "point": [0, 0, 0],
        "normal": [1, 0, 0],
        "u": [0, 1, 0],
        "v": [0, 0, 1],
        "half_u": 1,
        "half_v": 1,
    }
    spec = {"inlet": port, "outlet": port, "dv": 0, "example": False}
    assert physics._flow_case(spec).physics_inputs == {"example": False}
    with pytest.raises(ValueError, match="unknown key"):
        flow.parse_flow_shape("flow:in=p;out=q;dv=0;typo=1")
    with pytest.raises(BadInput, match="unknown spec key"):
        physics._flow_case({**spec, "typo": 1})
    with pytest.raises(BadInput, match="physics.untested_combination") as caught:
        physics.evaluate(None, ["flow"], spec)
    assert "example" in str(caught.value.next)


def test_unknown_token_key_keeps_original_diagnostic() -> None:
    with pytest.raises(ValueError) as caught:
        flow.parse_flow_shape("flow:in=p;out=q;dv=0;typo=1")
    assert str(caught.value) == (
        "flow chamber spec: unknown key(s) typo — "
        "keys are in, out, dv, profile, zplane, fluid, species, "
        "cells, seed, n, seed_z"
    )


def test_tree_free_physics_check_uses_spec(isolated_registry: None) -> None:
    spec = flow.parse_flow_shape("flow:in=p;out=q;dv=0")
    assert isinstance(spec, flow.FlowSpec)
    flow._check_tree_physics(None, "chamber", spec=spec)

    registry.register(registry.Physics("example", token_keys=("example",)))
    spec = flow.parse_flow_shape("flow:in=p;out=q;dv=0;example=0")
    assert isinstance(spec, flow.FlowSpec)
    with pytest.raises(BadInput, match="physics.untested_combination") as caught:
        flow._check_tree_physics(None, "chamber", spec=spec)
    assert "example" in str(caught.value.next)


def test_workspace_accepts_measurand_registered_after_physics_import() -> None:
    outputs = physics.MEASURANDS["flow"]
    registry.register(registry.Physics("example", measurands=("example-output",)))
    try:
        measures = workspace_import._vet_physics_measures(
            [{"measurand": "example-output"}],
            "physics[0].spec",
            outlet="patch:chamber.right",
            region=None,
            block="chamber",
            declared=set(),
        )
        assert len(measures) == 1
        assert measures[0]["measurand"] == "example-output"
    finally:
        registry.PHYSICS.pop("example")
        registry.MEASURANDS["flow"] = outputs


@pytest.mark.parametrize("extra", [False, True])
def test_every_registered_subset_requires_an_explicit_tested_entry(
    extra: bool, isolated_registry: None
) -> None:
    if extra:
        registry.register(registry.Physics("example", spec_keys=("example",)))
    names = tuple(registry.PHYSICS)
    for size in range(len(names) + 1):
        for subset in combinations(names, size):
            active = frozenset(subset)
            refusal = registry.combination_refusal(active)
            if active in registry.TESTED:
                assert refusal is None, subset
            else:
                assert refusal is not None, subset
                assert "physics.untested_combination" in refusal[0]
                assert "drop" in refusal[1]


@pytest.mark.parametrize("surface", ["token", "spec", "measure"])
def test_untested_input_refuses_every_adapter(
    surface: str, isolated_registry: None
) -> None:
    registry.register(
        registry.Physics(
            "example",
            token_keys=("example",),
            spec_keys=("example",),
            input_measures=("example-input",),
        )
    )
    token_keys = ["example"] if surface == "token" else []
    spec_keys = ["example"] if surface == "spec" else []
    measure_keys = ["example-input"] if surface == "measure" else []
    active = registry.active_physics(
        token_keys=token_keys, spec_keys=spec_keys, input_measures=measure_keys
    )
    assert active == frozenset({"flow", "example"})
    shape = "flow:in=p;out=q;dv=0;species=co"
    if surface == "token":
        shape += ";example=0"
    measures = (
        [MeasureSpec(block="chamber", name="co/example-input", value=None)]
        if surface == "measure"
        else []
    )
    tree = _tree(shape=shape, measures=measures)
    args: dict[str, Any] = {"block": "chamber"}
    if surface == "spec":
        args["example"] = False
    with pytest.raises(BadInput, match="physics.untested_combination") as caught:
        flow.render_flow(None, tree, args)
    assert "species" in str(caught.value)
    assert caught.value.next
    if surface != "spec":
        measure = MeasureSpec(
            block="chamber", name="out", measurand="outlet-conversion"
        )
        computer = flow.FLOW_COMPUTERS["outlet-conversion"]
        assert computer(tree, measure) is None
        assert computer.reason == str(caught.value)
        # Output choice does not hide the species/extension declarations.
        other = flow.FLOW_COMPUTERS["resistance-ratio"]
        assert other(tree, measure) is None
        assert other.reason == computer.reason


def test_species_input_measure_activates_without_token() -> None:
    tree = _tree(
        measures=[MeasureSpec(block="chamber", name="co/wall-rate-constant", value=0.0)]
    )
    assert registry.active_physics(input_measures=["wall-rate-constant"]) == frozenset(
        {"flow", "species"}
    )
    flow._check_tree_physics(tree, "chamber")
    tree.blocks["chamber"].bound_kind = "structure"
    with pytest.raises(BadInput, match=r"needs args\.alpha"):
        flow.render_flow(None, tree, {"block": "chamber"})


def test_structure_binding_does_not_make_chamber_token_a_lumen() -> None:
    tree = _tree(shape="flow:in=p;out=q;dv=0;species=co")
    tree.blocks["chamber"].bound_kind = "structure"
    flow._check_tree_physics(tree, "chamber")


def test_lumen_token_refuses_species_input_measure() -> None:
    tree = _tree(
        shape="flow:lumen;alpha=1",
        measures=[
            MeasureSpec(block="chamber", name="co/wall-rate-constant", value=0.0)
        ],
    )
    with pytest.raises(BadInput, match="on flow:lumen") as caught:
        flow._check_tree_physics(tree, "chamber")
    assert "wall-rate-constant" in str(caught.value.next)


def test_registered_groups_reach_card_verdict_and_json(isolated_registry: None) -> None:
    line = regime.RegimeLine("example", 2.0, 1.0, False, "<= 1", "drop example")

    def build(*, example: Any) -> tuple[regime.RegimeLine, ...]:
        return () if example is None else (line,)

    regime.register_group("example", inputs=("example",), build=build)
    base: dict[str, Any] = {"L": 1.0, "h0": 0.01, "mu": 1.0, "rho": 1.0, "u0": 1.0}
    card = regime.RegimeCard.from_physical(**base, molecular_d=0.001, example=0)
    assert card.species
    assert card.lines[-1] is line
    assert not card.ok
    assert "example" in card.failures
    payload = physics.card_dict(card)
    assert payload is not None
    assert payload["lines"][-1]["name"] == "example"
    assert "example" in payload["failures"]
    assert not payload["ok"]
    with pytest.raises(ValueError, match="unknown regime input"):
        regime.RegimeCard.from_physical(**base, typo=1)
