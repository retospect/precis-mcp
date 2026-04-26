"""Pytest fixtures for phase-1 tests."""

from __future__ import annotations

import pytest

from precis.config import PrecisConfig
from precis.hints import HintBus
from precis.protocol import Handler
from precis.registry import Registry, builtins
from precis.runtime import PrecisRuntime


@pytest.fixture
def hints() -> HintBus:
    return HintBus()


@pytest.fixture
def registry() -> Registry:
    handlers: list[Handler] = [cls() for cls in builtins()]
    return Registry(handlers)


@pytest.fixture
def runtime(registry: Registry, hints: HintBus) -> PrecisRuntime:
    return PrecisRuntime(
        config=PrecisConfig(),
        registry=registry,
        hints=hints,
    )
