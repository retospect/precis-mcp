"""CalcHandler — sympy-backed calculator."""

from __future__ import annotations

import pytest

from precis.errors import BadInput, Unsupported
from precis.handlers.calc import CalcHandler


@pytest.fixture
def handler() -> CalcHandler:
    return CalcHandler()


def test_basic_arithmetic(handler: CalcHandler) -> None:
    r = handler.get(id="2+3*4")
    assert "14" in r.body


def test_uses_q_when_id_absent(handler: CalcHandler) -> None:
    r = handler.get(q="1+1")
    assert "2" in r.body


def test_integer_id_is_coerced(handler: CalcHandler) -> None:
    r = handler.get(id=255)
    # 255 alone evaluates to 255
    assert "255" in r.body


def test_integration(handler: CalcHandler) -> None:
    r = handler.get(id="integrate(sin(x), x)")
    assert "cos" in r.body  # -cos(x)


def test_matrix_determinant(handler: CalcHandler) -> None:
    r = handler.get(id="Matrix([[1,2],[3,4]]).det()")
    assert "-2" in r.body


def test_unparseable(handler: CalcHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(id="this is not math")
    assert exc.value.next is not None


def test_missing_expr_raises(handler: CalcHandler) -> None:
    with pytest.raises(BadInput):
        handler.get()


def test_other_verbs_unsupported(handler: CalcHandler) -> None:
    with pytest.raises(Unsupported):
        handler.search(q="x")
    with pytest.raises(Unsupported):
        handler.put(text="x", mode="append")
    with pytest.raises(Unsupported):
        handler.move(id=1, after=2)


def test_kindspec_declares_only_get() -> None:
    spec = CalcHandler.spec
    assert spec.kind == "calc"
    assert spec.supports_get is True
    assert spec.supports_search is False
    assert spec.supports_put is False
    assert spec.supports_move is False
