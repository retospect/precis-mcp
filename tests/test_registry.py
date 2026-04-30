"""Registry — kind lookup and the BUILTINS list."""

from __future__ import annotations

import pytest

from precis.errors import NotFound
from precis.registry import Registry, builtins


def test_phase1_has_only_calc() -> None:
    assert [cls.spec.kind for cls in builtins()] == ["calc"]


def test_registry_resolves_calc(registry: Registry) -> None:
    assert "calc" in registry
    assert "calc" in registry.kinds()
    handler = registry.get("calc")
    assert handler.spec.kind == "calc"


def test_unknown_kind_raises_with_options(registry: Registry) -> None:
    with pytest.raises(NotFound) as exc:
        registry.get("nonexistent")
    assert exc.value.options == ["calc"]
    assert exc.value.next is not None


def test_duplicate_kind_rejected() -> None:
    from precis.handlers.calc import CalcHandler

    with pytest.raises(ValueError, match="duplicate"):
        Registry([CalcHandler(), CalcHandler()])


def test_builtins_survives_missing_sympy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare-install regression: registry build must not crash if sympy
    (the [calc] optional dep) isn't installed.

    Simulates the bare `pip install precis-mcp` case where calc.py's
    top-level ``import sympy`` raises ImportError. The registry should
    silently drop the calc kind, exactly the same way it drops math /
    youtube / web / patent when their extras are missing.
    """
    import builtins as _bi

    real_import = _bi.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "sympy" or name.startswith("sympy."):
            raise ImportError("simulated: sympy not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_bi, "__import__", _fake_import)
    # Drop any cached calc module so the new __import__ takes effect.
    import sys as _sys

    monkeypatch.delitem(_sys.modules, "precis.handlers.calc", raising=False)
    monkeypatch.delitem(_sys.modules, "sympy", raising=False)

    handlers = builtins()
    kinds = [h.spec.kind for h in handlers]
    assert "calc" not in kinds


def test_unavailable_kind_skipped() -> None:
    """A handler whose KindSpec.requires_env isn't met must be hidden."""
    from precis.protocol import Handler, KindSpec

    class FakeHandler(Handler):
        spec = KindSpec(
            kind="fake",
            title="Fake",
            description="needs an env var",
            supports_get=True,
            requires_env=("PRECIS_NO_SUCH_ENV_VAR_FOR_TEST",),
        )

    reg = Registry([FakeHandler()])
    assert "fake" not in reg
    assert reg.kinds() == []
