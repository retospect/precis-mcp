"""Live configuration reads for the LLM router.

``resolve_model`` reads per-tier model overrides from ``app_settings``, so
operators can change a tier's model without redeploying. With no store or
setting row, resolution falls back to environment and compiled defaults.
These tests cover override validation, TTL caching, and transport selection.

DB-free: the store is faked and ``budget.settings.get_setting`` is stubbed, so
no real ``app_settings`` table is touched.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.utils.llm import live_config, router
from precis.utils.llm.router import Tier


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
    # The budget meter keeps a short-lived fallback memo process-wide, not
    # per store — a tripped status memoised by a neighbouring test would
    # pause these dispatches via gate_tier. Clear it.
    meter._cached = None

    def fake_get(_store: object, key: str) -> str | None:
        calls["n"] += 1
        return (rows or {}).get(key)

    monkeypatch.setattr(budget_settings, "get_setting", fake_get)
    return calls


# ── reader: dark path ──────────────────────────────────────────────────


def test_dark_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, None)
    assert live_config.model_override(Tier.FRONTIER) is None


def test_dark_with_store_but_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {})
    assert live_config.model_override(Tier.BIG) is None


# ── reader: overrides + validation ─────────────────────────────────────


def test_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.model.frontier": "deepseek-ai/DeepSeek-V3"})
    assert live_config.model_override(Tier.FRONTIER) == "deepseek-ai/DeepSeek-V3"


def test_model_key_uses_tier_value() -> None:
    assert live_config.model_key(Tier.FRONTIER) == "llm.model.frontier"
    assert live_config.model_key(Tier.MEDIUM) == "llm.model.medium"


# ── reader: chain override ───────────────────────


# ── reader: cloud throttle ────────────────────────────────


def test_cloud_enabled_default_true_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, None)
    assert live_config.cloud_enabled() is True


def test_cloud_enabled_default_true_without_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, {})
    assert live_config.cloud_enabled() is True


@pytest.mark.parametrize("falsey", ["false", "False", "0", "no", "off", "  OFF  "])
def test_cloud_enabled_false_on_explicit_falsey(
    monkeypatch: pytest.MonkeyPatch, falsey: str
) -> None:
    _bind(monkeypatch, {"llm.cloud_enabled": falsey})
    assert live_config.cloud_enabled() is False


@pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "garbage"])
def test_cloud_enabled_true_on_anything_else(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    # Only an unambiguous falsey turns cloud off — a typo can't strand the
    # fleet on local.
    _bind(monkeypatch, {"llm.cloud_enabled": truthy})
    assert live_config.cloud_enabled() is True


def test_chain_key_uses_tier_value() -> None:
    assert live_config.chain_key(Tier.FRONTIER) == "llm.chain.frontier"
    assert live_config.chain_key(Tier.BIG) == "llm.chain.big"


def test_chain_override_dark_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, None)
    assert live_config.chain_override(Tier.FRONTIER) is None


def test_chain_override_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {})
    assert live_config.chain_override(Tier.FRONTIER) is None


def test_chain_override_parses_valid_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        "llm.chain.frontier": (
            '[{"placement": "cloud", "model": "z-ai/glm-5.2", '
            '"transport": "openai_tools"}]'
        )
    }
    _bind(monkeypatch, rows)
    out = live_config.chain_override(Tier.FRONTIER)
    assert out == [
        {"placement": "cloud", "model": "z-ai/glm-5.2", "transport": "openai_tools"}
    ]


def test_chain_override_malformed_json_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.frontier": "not json{{"})
    assert live_config.chain_override(Tier.FRONTIER) is None


def test_chain_override_non_list_json_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.frontier": '{"placement": "cloud"}'})
    assert live_config.chain_override(Tier.FRONTIER) is None


# ── reader: TTL cache ──────────────────────────────────────────────────


def test_reads_are_cached_until_busted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _bind(monkeypatch, {"llm.model.big": "qwen-mid"})
    assert live_config.model_override(Tier.BIG) == "qwen-mid"
    assert live_config.model_override(Tier.BIG) == "qwen-mid"
    assert calls["n"] == 1  # second read served from cache
    live_config.bust_cache()
    assert live_config.model_override(Tier.BIG) == "qwen-mid"
    assert calls["n"] == 2  # re-queried after the bust


# ── resolvers: DB tier over env ────────────────────────────────────────


def test_resolve_model_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    _bind(monkeypatch, {"llm.model.frontier": "oss/x"})
    assert router.resolve_model(Tier.FRONTIER) == "oss/x"


def test_resolve_model_falls_back_to_env_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, {})  # store bound, no override row
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "pinned-opus")
    assert router.resolve_model(Tier.FRONTIER) == "pinned-opus"
    live_config.bust_cache()
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    assert router.resolve_model(Tier.FRONTIER) == "claude-opus-5-5"


def test_resolve_model_dark_without_store_is_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No store → no DB tier → pure env/compiled default, byte-identical.
    _bind(monkeypatch, None)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    assert router.resolve_model(Tier.BIG) == "claude-sonnet-5-5"


# Live model overrides determine the transport, which validates its endpoint.


def test_oss_override_without_endpoint_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, {"llm.model.frontier": "z-ai/glm-5.2"})
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("PRECIS_LLM_FAILOVER", raising=False)

    result = router.route(
        router.LlmRequest(tier=Tier.FRONTIER, prompt="x", tools_needed=True)
    )

    assert result.model == "z-ai/glm-5.2"
    assert "PRECIS_LLM_BASE_URL" in (result.error or "")


def test_dispatch_openai_override_with_base_url_routes_oss_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live model override selects its wire despite an obsolete backend row."""
    from precis.utils.llm.router import (
        LlmRequest,
        LlmResult,
        Transport,
        route,
    )
    from precis.utils.llm.router import (
        Tier as _Tier,
    )

    _bind(
        monkeypatch,
        {"llm.model.frontier": "z-ai/glm-5.2", "llm.backend": "anthropic"},
    )
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    calls: dict[str, object] = {}

    def fake_tools(req: object, *, model: str) -> LlmResult:
        calls["model"] = model
        calls["transport"] = "openai_tools"
        return LlmResult(
            text="oss ran",
            cost_usd=0.01,
            turns_used=1,
            model=model,
            tier=_Tier.FRONTIER,
        )

    monkeypatch.setitem(router._PROVIDERS, Transport.OPENAI_TOOLS, _RunFnLC(fake_tools))

    out = route(LlmRequest(tier=_Tier.FRONTIER, prompt="x", tools_needed=True))

    assert calls["model"] == "z-ai/glm-5.2"
    assert out.text == "oss ran"


class _RunFnLC:
    """Minimal provider adapter — mirrors test_llm_router's ``_RunFn`` so this
    file doesn't need to import test internals across modules."""

    def __init__(self, fn: object) -> None:
        self._fn = fn

    def run(self, req: object, *, model: str) -> object:
        return self._fn(req, model=model)  # type: ignore[operator]
