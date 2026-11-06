"""Bounded parameter space: sample/clip/validate/apply and the Params key.

se-flow-kernel slice 4b §Design representation item 3 (the ``Space``
convenience layer over feature attributes). ``build`` and the feature-graph
recipe are item 1; here the recipe is a minimal ``modify``-only stub.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np
import pytest

from precis.errors import BadInput
from precis_geom.params import Boolean, Continuous, Integer, Params, Space
from precis_geom.steps import _pcanon


@dataclass
class _Recipe:
    """Just the ``modify`` door ``Space.apply`` drives."""

    calls: dict[str, dict[str, Any]] = field(default_factory=dict)

    def modify(self, fid: str, **params: Any) -> None:
        self.calls.setdefault(fid, {}).update(params)


def _space(**kw: Any) -> Space:
    return Space(
        {"w1": Continuous(1e-3, 5e-3), "n": Integer(2, 8), "tip": Boolean()}, **kw
    )


def test_params_is_sorted_and_mapping() -> None:
    p = Params.of({"n": 4, "w1": 2e-3, "tip": True})
    assert list(p) == ["n", "tip", "w1"]  # sorted
    assert p["n"] == 4 and dict(p) == {"n": 4, "tip": True, "w1": 2e-3}
    assert len(p) == 3 and "w1" in p
    # same contents, same key (frozen dataclass eq/hash over sorted pairs)
    assert p == Params.of({"w1": 2e-3, "n": 4, "tip": True})
    assert hash(p) == hash(Params.of({"w1": 2e-3, "n": 4, "tip": True}))
    with pytest.raises(KeyError):
        p["missing"]


def test_pcanon_keys_params_and_refuses_a_proxy() -> None:
    p = Params.of({"n": 4, "w1": 2e-3})
    canon = _pcanon(p, [], {})
    # keyed as a frozen value object, not ignored
    assert canon["dc"][0] == "Params"
    assert _pcanon(p, [], {}) == canon  # stable
    with pytest.raises(BadInput, match="cannot key"):
        _pcanon(MappingProxyType({"n": 4}), [], {})


def test_sample_is_in_range_and_typed() -> None:
    rng = np.random.default_rng(0)
    space = _space()
    for _ in range(100):
        p = space.sample(rng)
        assert 1e-3 <= p["w1"] <= 5e-3 and isinstance(p["w1"], float)
        assert (
            2 <= p["n"] <= 8
            and isinstance(p["n"], int)
            and not isinstance(p["n"], bool)
        )
        assert isinstance(p["tip"], bool)
        space.validate(p)  # a sampled point is always feasible


def test_sample_is_deterministic_per_seed() -> None:
    space = _space()
    assert space.sample(np.random.default_rng(7)) == space.sample(
        np.random.default_rng(7)
    )


def test_clip_clamps_and_integer_rounds() -> None:
    space = _space()
    clipped = space.clip({"w1": 9.0, "n": 4.6, "tip": 1})
    assert clipped["w1"] == 5e-3  # clamped to hi
    assert clipped["n"] == 5 and isinstance(clipped["n"], int)  # rounded
    assert clipped["tip"] is True
    assert space.clip({"w1": -1.0, "n": 0})["w1"] == 1e-3  # clamped to lo
    assert space.clip({"w1": 2e-3, "n": 100})["n"] == 8  # rounded then clamped
    space.validate(space.clip({"w1": 9.0, "n": 4.6, "tip": 1}))
    # an extra, unbound key rides through unchanged
    assert space.clip({"w1": 2e-3, "n": 4, "tip": False, "x": "keep"})["x"] == "keep"


def test_validate_names_the_out_of_range_parameter() -> None:
    space = _space()
    with pytest.raises(BadInput, match="'w1'"):
        space.validate({"w1": 9.0, "n": 4, "tip": True})
    with pytest.raises(BadInput, match="'n'"):
        space.validate({"w1": 2e-3, "n": 99, "tip": True})


def test_validate_names_a_missing_or_mistyped_parameter() -> None:
    space = _space()
    with pytest.raises(BadInput, match="'tip'.*missing"):
        space.validate({"w1": 2e-3, "n": 4})
    with pytest.raises(BadInput, match="'n'.*int"):
        space.validate({"w1": 2e-3, "n": 4.5, "tip": True})
    with pytest.raises(BadInput, match="'tip'.*bool"):
        space.validate({"w1": 2e-3, "n": 4, "tip": 1})
    with pytest.raises(BadInput, match="'w1'.*real"):
        space.validate({"w1": True, "n": 4, "tip": True})


def test_validate_refuses_an_infeasible_constraint() -> None:
    def total_le(params: Mapping[str, Any]) -> float:
        return params["w1"] - 3e-3  # feasible when w1 <= 3 mm

    space = _space(constraints=[total_le])
    space.validate({"w1": 2e-3, "n": 4, "tip": True})  # feasible
    with pytest.raises(BadInput, match="total_le.*infeasible"):
        space.validate({"w1": 4e-3, "n": 4, "tip": True})


def test_apply_writes_bound_parameters_only() -> None:
    space = _space(bindings={"w1": "h7.d", "n": "grid.count"})
    recipe = _Recipe()
    space.apply(recipe, Params.of({"w1": 2e-3, "n": 5, "tip": True}))
    assert recipe.calls == {"h7": {"d": 2e-3}, "grid": {"count": 5}}  # tip unbound


def test_space_rejects_a_malformed_binding() -> None:
    with pytest.raises(BadInput, match="not in the space"):
        _space(bindings={"ghost": "h7.d"})
    with pytest.raises(BadInput, match="feature id"):
        _space(bindings={"w1": "h7"})


def test_dim_bounds_are_checked_at_construction() -> None:
    with pytest.raises(BadInput, match="exceeds"):
        Continuous(5.0, 1.0)
    with pytest.raises(BadInput, match="exceeds"):
        Integer(8, 2)
    with pytest.raises(BadInput, match="finite"):
        Continuous(0.0, float("inf"))
    with pytest.raises(BadInput, match="bools"):
        Integer(True, 8)
