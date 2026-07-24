"""Live-PG tests for the ``/factory/llm`` cloud-LLM backend flip.

The fake-store route smoke tests (wiring + redirect) live in
``test_factory.py`` alongside the other ``/factory/*`` write endpoints —
the FakeStore's SQL is a no-op, so it can't prove a real write. This file
exercises the actual ``app_settings`` round-trip (``_apply_glm_preset`` /
``_revert_glm_preset``) against real Postgres, plus the resolver round-trip
that proves the write is what the router actually reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.budget import settings as budget_settings
from precis.utils.llm import live_config, router
from precis.utils.llm.router import Backend, Tier
from precis_web.routes.factory import _apply_glm_preset, _revert_glm_preset


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    # The TTL cache is a module global — clear it around every test so a
    # cached value (or negative cache) can't leak between cases.
    live_config.bust_cache()
    yield
    live_config.bust_cache()


def test_apply_glm_preset_writes_backend_and_all_three_tiers(store: Any) -> None:
    _apply_glm_preset(store)
    assert budget_settings.get_setting(store, live_config.BACKEND_KEY) == "openai"
    for tier, slug in live_config.GLM_OPENROUTER_PRESET.items():
        assert budget_settings.get_setting(store, live_config.model_key(tier)) == slug


def test_revert_glm_preset_clears_all_four_keys(store: Any) -> None:
    _apply_glm_preset(store)
    _revert_glm_preset(store)
    assert budget_settings.get_setting(store, live_config.BACKEND_KEY) is None
    for tier in live_config.GLM_OPENROUTER_PRESET:
        assert budget_settings.get_setting(store, live_config.model_key(tier)) is None


def test_glm_preset_resolver_roundtrip(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """After the flip the router resolves to the GLM roster; after revert,
    back to the compiled claude defaults — with ambient env cleared so the
    DB override (not an env var) is what's under test."""
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_HAIKU", raising=False)

    from precis.budget import meter

    monkeypatch.setattr(meter, "active_store", lambda: store)

    _apply_glm_preset(store)
    assert router.resolve_backend() is Backend.OPENAI
    assert router.resolve_model(Tier.CLOUD_SUPER) == "z-ai/glm-5.2"
    assert router.resolve_model(Tier.CLOUD_MID) == "z-ai/glm-4.7"
    assert router.resolve_model(Tier.CLOUD_SMALL) == "z-ai/glm-4.7-flash"

    _revert_glm_preset(store)
    assert router.resolve_backend() is Backend.ANTHROPIC
    assert router.resolve_model(Tier.CLOUD_SUPER) == "claude-opus-4-8"
