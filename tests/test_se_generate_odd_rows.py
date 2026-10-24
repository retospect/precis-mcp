"""Coded refusals for even-row-count violations (gr503030).

A generator's parity check carries ``code=``/``fix=`` on its
:class:`GeneratorError` — the code renders into the message as
``[code]`` so an agent can branch on it, and the ``generate`` op
forwards ``fix`` as the surfaced refusal's ``next=``. Refusals raised
inside the hexfold library bake an ``odd_rows:`` token into the message
text; :meth:`GeneratorError.lift` hoists it to the ``code`` at the wrap
site, so the same violation is coded at every depth it can surface.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.handler import SeHandler


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


_RAIL_FEATURE: dict[str, Any] = {
    "name": "w",
    "type": "rail-k3-120-z",
    "rails": 3,
}
_CHAN_FEATURE: dict[str, Any] = {"name": "c", "type": "chan-k3-sp3-z"}
_PRISM_FEATURE: dict[str, Any] = {
    "name": "p",
    "type": "prism-k3-120-z",
    "rows": 7,
    "periods": 8,
    "strip_rows": 4,
    "seams": "+++",
}
_TUNNEL_FEATURE: dict[str, Any] = {
    "name": "t",
    "type": "tunnel-reservoirs",
    "sheet": [30, 24],
}

# (params, message match, expected code): the normalize-level checks the
# family generators landed first carry their per-family code; the tunnel
# parity checks and everything lifted out of the hexfold library carry
# the uniform ``odd_rows`` token.
_ODD_PARAMS: list[tuple[dict[str, Any], str, str]] = [
    (
        {"tube": [7, 10], "features": [_RAIL_FEATURE]},
        "rail-tube tube rows must be even",
        "rail_tube.rows.odd",
    ),
    (
        {"tube": [7, 10], "features": [_CHAN_FEATURE]},
        "channel wall rows must be even",
        "channel.rows.odd",
    ),
    (
        {"features": [_PRISM_FEATURE]},
        "prism rows must be an even integer",
        "prism.rows.odd",
    ),
    (
        {
            "tube": {"rail": [7, 10], "rails": 3},
            "features": [_TUNNEL_FEATURE],
        },
        r"rail rows must be even \(got 7",
        "odd_rows",
    ),
    (
        {
            "tube": {"rail": [[6, 10], [7, 10]], "rails": 3},
            "features": [_TUNNEL_FEATURE],
        },
        r"stepped rail rows must be even \(got 7",
        "odd_rows",
    ),
]


@pytest.mark.parametrize(("params", "match", "code"), _ODD_PARAMS)
def test_an_odd_row_count_is_a_coded_refusal(
    params: dict[str, Any], match: str, code: str
) -> None:
    with pytest.raises(GeneratorError, match=match) as exc:
        GENERATORS["hexfold_scene"](params)
    assert exc.value.code == code
    assert f"[{code}]" in str(exc.value)
    assert exc.value.fix


def test_the_generate_op_surfaces_the_code_and_names_the_fix(
    handler: SeHandler,
) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "hexfold_scene",
            "name": "w",
            "params": {"tube": [7, 10], "features": [_RAIL_FEATURE]},
        }
    ]
    with pytest.raises(BadInput) as exc:
        handler.put(id="oddrows", text=json.dumps({"ops": ops}))
    assert "[rail_tube.rows.odd]" in exc.value.cause
    assert exc.value.next


def test_an_even_row_count_reaches_the_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_se.atomic.generators import rail120 as rail120_mod

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("even rows reached the builder")

    monkeypatch.setattr(rail120_mod, "rail_tube", boom)
    with pytest.raises(GeneratorError, match="reached the builder"):
        GENERATORS["hexfold_scene"]({"tube": [8, 10], "features": [_RAIL_FEATURE]})


def test_an_uncoded_refusal_keeps_the_plain_wrap(handler: SeHandler) -> None:
    # Even rows pass the parity gate; a *different* refusal surfaces as
    # uncoded prose, not a false odd_rows branch.
    ops = [
        {
            "op": "generate",
            "generator": "hexfold_scene",
            "name": "w",
            "params": {
                "tube": [8, 10],
                "features": [{**_RAIL_FEATURE, "bogus": 1}],
            },
        }
    ]
    with pytest.raises(BadInput, match="bogus") as exc:
        handler.put(id="evenrows", text=json.dumps({"ops": ops}))
    assert "odd_rows" not in exc.value.cause


def test_lift_passes_a_generator_error_through_unchanged() -> None:
    err = GeneratorError("already coded", code="odd_rows", fix="do x")
    assert GeneratorError.lift(err) is err


def test_lift_hoists_a_message_token_into_the_code() -> None:
    lifted = GeneratorError.lift(
        ValueError("odd_rows: rail tube wall rows must be even (got 7)")
    )
    assert lifted.code == "odd_rows"
    assert "odd_rows:" not in str(lifted)
    assert "got 7" in str(lifted)


def test_lift_prepends_the_prefix_after_the_code() -> None:
    lifted = GeneratorError.lift(ValueError("odd_rows: bad rows"), prefix="w: ")
    assert lifted.code == "odd_rows"
    assert str(lifted) == "[odd_rows] w: bad rows"


def test_lift_leaves_plain_library_errors_alone() -> None:
    lifted = GeneratorError.lift(ValueError("some other refusal"))
    assert lifted.code is None
    assert str(lifted) == "some other refusal"


def test_the_hexfold_library_refusal_carries_the_token() -> None:
    from hexfold.railtube import rail_tube

    with pytest.raises(ValueError, match=r"^odd_rows: .*got 7"):
        rail_tube(10, 7, 4, 3)
