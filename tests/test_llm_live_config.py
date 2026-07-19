"""ADR 0046 unit-4b piece ① — the ``/factory`` live LLM switch (read side).

``resolve_backend`` / ``resolve_model`` layer an ``app_settings`` DB override
over the env default, so an operator flips the fleet's backend or a per-tier
model without a redeploy. Dark by construction: with no store bound or no row
written, every read is ``None`` and the router falls back to env — byte-
identical to before. Covered here: the reader (dark path, override, validation,
TTL cache) and the two resolvers honoring / falling back through the DB tier.

DB-free: the store is faked and ``budget.settings.get_setting`` is stubbed, so
no real ``app_settings`` table is touched.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.utils.llm import live_config, router
from precis.utils.llm.router import Backend, Tier


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    # The TTL cache is a module global — clear it around every test so a
    # cached value (or negative cache) can't leak between cases.
    live_config.bust_cache()
    yield
    live_config.bust_cache()


class _Store:
    """Opaque sentinel — ``get_setting`` is stubbed, so it's never queried."""


def _bind(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, str] | None
) -> dict[str, int]:
    """Point live_config at a fake store returning ``rows`` (a ``None`` store
    when ``rows is None``). Returns a dict whose ``n`` counts get_setting hits."""
    calls = {"n": 0}
    store = _Store() if rows is not None else None
    monkeypatch.setattr(meter, "active_store", lambda: store)

    def fake_get(_store: object, key: str) -> str | None:
        calls["n"] += 1
        return (rows or {}).get(key)

    monkeypatch.setattr(budget_settings, "get_setting", fake_get)
    return calls


# ── reader: dark path ──────────────────────────────────────────────────


def test_dark_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, None)
    assert live_config.backend_override() is None
    assert live_config.model_override(Tier.CLOUD_SUPER) is None


def test_dark_with_store_but_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {})
    assert live_config.backend_override() is None
    assert live_config.model_override(Tier.CLOUD_MID) is None


# ── reader: overrides + validation ─────────────────────────────────────


def test_backend_override_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.backend": "openai"})
    assert live_config.backend_override() == "openai"


def test_backend_override_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.backend": "OpenAI"})
    assert live_config.backend_override() == "openai"


def test_backend_override_unknown_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.backend": "gpt-5"})
    assert live_config.backend_override() is None


def test_backend_override_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.backend": "   "})
    assert live_config.backend_override() is None


def test_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.model.cloud-super": "deepseek-ai/DeepSeek-V3"})
    assert live_config.model_override(Tier.CLOUD_SUPER) == "deepseek-ai/DeepSeek-V3"


def test_model_key_uses_tier_value() -> None:
    assert live_config.model_key(Tier.CLOUD_SUPER) == "llm.model.cloud-super"
    assert live_config.model_key(Tier.CLOUD_SMALL) == "llm.model.cloud-small"


# ── reader: TTL cache ──────────────────────────────────────────────────


def test_reads_are_cached_until_busted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _bind(monkeypatch, {"llm.backend": "openai"})
    assert live_config.backend_override() == "openai"
    assert live_config.backend_override() == "openai"
    assert calls["n"] == 1  # second read served from cache
    live_config.bust_cache()
    assert live_config.backend_override() == "openai"
    assert calls["n"] == 2  # re-queried after the bust


# ── resolvers: DB tier over env ────────────────────────────────────────


def test_resolve_backend_override_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env says anthropic (unset), DB says openai → openai.
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    _bind(monkeypatch, {"llm.backend": "openai"})
    assert router.resolve_backend() is Backend.OPENAI


def test_resolve_backend_db_anthropic_overrides_env_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env says openai, DB says anthropic → DB wins (the live "switch back").
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    _bind(monkeypatch, {"llm.backend": "anthropic"})
    assert router.resolve_backend() is Backend.ANTHROPIC


def test_resolve_backend_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {})  # store bound, no row
    monkeypatch.setenv("PRECIS_LLM_BACKEND", "openai")
    assert router.resolve_backend() is Backend.OPENAI
    live_config.bust_cache()
    monkeypatch.delenv("PRECIS_LLM_BACKEND", raising=False)
    assert router.resolve_backend() is Backend.ANTHROPIC


def test_resolve_model_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    _bind(monkeypatch, {"llm.model.cloud-super": "oss/x"})
    assert router.resolve_model(Tier.CLOUD_SUPER) == "oss/x"


def test_resolve_model_falls_back_to_env_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, {})  # store bound, no override row
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "pinned-opus")
    assert router.resolve_model(Tier.CLOUD_SUPER) == "pinned-opus"
    live_config.bust_cache()
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    assert router.resolve_model(Tier.CLOUD_SUPER) == "claude-opus-4-8"


def test_resolve_model_dark_without_store_is_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No store → no DB tier → pure env/compiled default, byte-identical.
    _bind(monkeypatch, None)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    assert router.resolve_model(Tier.CLOUD_MID) == "claude-sonnet-5"
