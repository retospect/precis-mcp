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
    assert live_config.model_override(Tier.FRONTIER) is None


def test_dark_with_store_but_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {})
    assert live_config.backend_override() is None
    assert live_config.model_override(Tier.BIG) is None


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
    _bind(monkeypatch, {"llm.model.frontier": "deepseek-ai/DeepSeek-V3"})
    assert live_config.model_override(Tier.FRONTIER) == "deepseek-ai/DeepSeek-V3"


def test_model_key_uses_tier_value() -> None:
    assert live_config.model_key(Tier.FRONTIER) == "llm.model.frontier"
    assert live_config.model_key(Tier.MEDIUM) == "llm.model.medium"


# ── reader: chain override (ADR 0066 §4, Phase A) ───────────────────────


# ── reader: cloud throttle (ADR 0066 §5) ────────────────────────────────


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
    assert router.resolve_model(Tier.FRONTIER) == "claude-opus-4-8"


def test_resolve_model_dark_without_store_is_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No store → no DB tier → pure env/compiled default, byte-identical.
    _bind(monkeypatch, None)
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    assert router.resolve_model(Tier.BIG) == "claude-sonnet-5"


# ── Part 3: resolve_model backend-coherence ─────────────────────────────
# ``glm-fleet-flip-safety`` (git-only) Part 3 — the 4 `dream` api_errors:
# a half-applied flip demotes the backend to ANTHROPIC (no PRECIS_LLM_BASE_URL)
# but the app_settings model override still names an OSS slug, handing a
# claude transport a model it can't run.


def test_resolve_model_drops_oss_override_under_anthropic_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSS app_settings override is INCOHERENT with backend=ANTHROPIC —
    drop it and fall through to env/compiled default (never an OSS slug on a
    claude transport)."""
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    _bind(monkeypatch, {"llm.model.frontier": "z-ai/glm-5.2"})
    assert (
        router.resolve_model(Tier.FRONTIER, backend=Backend.ANTHROPIC)
        == "claude-opus-4-8"
    )


def test_resolve_model_drops_oss_override_falls_through_to_env_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the incoherent override falls through to the env var (not
    straight to the compiled default) — same order resolve_model always uses."""
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "claude-pinned-env")
    _bind(monkeypatch, {"llm.model.frontier": "z-ai/glm-5.2"})
    assert (
        router.resolve_model(Tier.FRONTIER, backend=Backend.ANTHROPIC)
        == "claude-pinned-env"
    )


def test_resolve_model_keeps_claude_override_under_anthropic_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claude-family override is coherent with ANTHROPIC — kept, not dropped."""
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    _bind(monkeypatch, {"llm.model.frontier": "claude-opus-4-9-preview"})
    assert (
        router.resolve_model(Tier.FRONTIER, backend=Backend.ANTHROPIC)
        == "claude-opus-4-9-preview"
    )


def test_resolve_model_keeps_oss_override_under_openai_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under backend=OPENAI the OSS override IS coherent — kept unchanged."""
    _bind(monkeypatch, {"llm.model.frontier": "z-ai/glm-5.2"})
    assert router.resolve_model(Tier.FRONTIER, backend=Backend.OPENAI) == "z-ai/glm-5.2"


def test_resolve_model_keeps_small_override_under_anthropic_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coherence drop is CLOUD-tier only. SMALL (→LOCAL/OPENAI_COMPAT)
    never routes to a claude transport, so its (always-non-claude) override
    is legitimate and must be HONORED even under the default ANTHROPIC
    backend — dropping it would silently ignore a live `llm.model.small` row,
    incl. the one Part 1's remap reads (reviewer finding #1)."""
    monkeypatch.delenv("PRECIS_SUMMARIZE_MODEL", raising=False)
    _bind(monkeypatch, {"llm.model.small": "z-ai/glm-4.7-flash"})
    assert (
        router.resolve_model(Tier.SMALL, backend=Backend.ANTHROPIC)
        == "z-ai/glm-4.7-flash"
    )


def test_resolve_model_big_override_is_cloud_bound_and_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0066 Phase C: BIG joined the cloud-bound tier set — it no longer
    has a location-coupled LOCAL_BIG analogue whose override was always
    non-claude and thus always honored. An OSS `llm.model.big` override under
    the default ANTHROPIC backend is now INCOHERENT and gets dropped, exactly
    like FRONTIER/MEDIUM — not honored unconditionally as the retired
    LOCAL_BIG tier's override was."""
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)
    _bind(monkeypatch, {"llm.model.big": "qwen/qwen3.7-max"})
    assert (
        router.resolve_model(Tier.BIG, backend=Backend.ANTHROPIC) == "claude-sonnet-5"
    )


def test_resolve_model_no_backend_arg_keeps_override_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every caller that never passes ``backend=`` (every call site but
    dispatch/dispatch_async) sees the pre-Part-3 behavior byte-for-byte —
    the coherence check is opt-in via the parameter, not a global change."""
    _bind(monkeypatch, {"llm.model.frontier": "z-ai/glm-5.2"})
    assert router.resolve_model(Tier.FRONTIER) == "z-ai/glm-5.2"


def test_dispatch_openai_override_no_base_url_resolves_claude_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end desync fix (acceptance criterion 3): dispatch with
    backend=openai + an OSS model-override row set, but NO
    PRECIS_LLM_BASE_URL, resolves a CLAUDE model on claude_agent — never the
    OSS slug landing on the claude transport (the `dream` api_error class)."""
    from precis.utils.llm.router import LlmRequest, dispatch
    from precis.utils.llm.router import Tier as _Tier

    _bind(
        monkeypatch,
        {"llm.backend": "openai", "llm.model.frontier": "z-ai/glm-5.2"},
    )
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)

    calls: dict[str, object] = {}

    def fake_agent(prompt: str, **kwargs: object) -> object:
        from precis.utils.claude_agent import AgentResult

        calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="claude ran", cost_usd=None, duration_s=0.0, turns_used=1
        )

    monkeypatch.setattr(router, "call_claude_agent", fake_agent)

    out = dispatch(LlmRequest(tier=_Tier.FRONTIER, prompt="x", tools_needed=True))

    assert calls["model"] == "claude-opus-4-8"  # NOT "z-ai/glm-5.2"
    assert out.text == "claude ran"


def test_dispatch_openai_override_with_base_url_routes_oss_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the invariant: WITH PRECIS_LLM_BASE_URL set, the same
    override is coherent (backend stays OPENAI) and routes OPENAI_TOOLS
    carrying the OSS slug — the flip working as intended."""
    from precis.utils.llm.router import (
        LlmRequest,
        LlmResult,
        Transport,
        dispatch,
    )
    from precis.utils.llm.router import (
        Tier as _Tier,
    )

    _bind(
        monkeypatch,
        {"llm.backend": "openai", "llm.model.frontier": "z-ai/glm-5.2"},
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

    out = dispatch(LlmRequest(tier=_Tier.FRONTIER, prompt="x", tools_needed=True))

    assert calls["model"] == "z-ai/glm-5.2"
    assert out.text == "oss ran"


class _RunFnLC:
    """Minimal provider adapter — mirrors test_llm_router's ``_RunFn`` so this
    file doesn't need to import test internals across modules."""

    def __init__(self, fn: object) -> None:
        self._fn = fn

    def run(self, req: object, *, model: str) -> object:
        return self._fn(req, model=model)  # type: ignore[operator]
