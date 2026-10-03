"""Per-process env overrides for the LLM tiers — ``PRECIS_LLM_CHAIN_<TIER>`` /
``PRECIS_LLM_MODEL_<TIER>``.

A one-off CLI run (e.g. ``precis taproot-migrate canary --tier small``) can pin a
tier to another model + transport without touching the fleet-wide
``app_settings`` rows (``llm.chain.<tier>`` / ``llm.model.<tier>``). The env var
holds the exact value format of the row and wins over it, for this process only.

DB-free: the store is faked and ``budget.settings.get_setting`` is stubbed.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from precis import route_log
from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.errors import BadInput
from precis.utils.llm import live_config, router
from precis.utils.llm import local_serving as ls
from precis.utils.llm.router import (
    Backend,
    LlmRequest,
    LlmResult,
    Rung,
    Tier,
    Transport,
)

_QWEN_ID = "qwen3-next-80b-a3b-q4_k_m"
_QWEN_CHAIN = f'[{{"placement": "local", "model": "{_QWEN_ID}", "transport": "local"}}]'
_DB_CHAIN = (
    '[{"placement": "cloud", "model": "z-ai/glm-4.7-flash", '
    '"transport": "openai_compat"}]'
)
_DB_BIG_CHAIN = (
    '[{"placement": "cloud", "model": "claude-opus-4-8", "transport": "claude_agent"}]'
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    for tier in Tier:
        monkeypatch.delenv(live_config.chain_env_var(tier), raising=False)
        monkeypatch.delenv(live_config.model_env_var(tier), raising=False)
    live_config.bust_cache()
    live_config._env_warned.clear()
    yield
    live_config.bust_cache()
    live_config._env_warned.clear()


class _Store:
    """Opaque sentinel — ``get_setting`` is stubbed, so it's never queried."""


def _bind(monkeypatch: pytest.MonkeyPatch, rows: dict[str, str] | None) -> None:
    store = _Store() if rows is not None else None
    monkeypatch.setattr(meter, "active_store", lambda: store)
    monkeypatch.setattr(
        budget_settings, "get_setting", lambda _s, key: (rows or {}).get(key)
    )


def test_env_var_names() -> None:
    assert live_config.chain_env_var(Tier.SMALL) == "PRECIS_LLM_CHAIN_SMALL"
    assert live_config.model_env_var(Tier.FRONTIER) == "PRECIS_LLM_MODEL_FRONTIER"


def test_env_chain_beats_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)
    assert live_config.chain_override(Tier.SMALL) == [
        {"placement": "local", "model": _QWEN_ID, "transport": "local"}
    ]


def test_env_chain_wins_without_a_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, None)
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)
    assert live_config.chain_override(Tier.SMALL) is not None


def test_unset_env_leaves_db_chain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    assert live_config.chain_override(Tier.SMALL) == [
        {
            "placement": "cloud",
            "model": "z-ai/glm-4.7-flash",
            "transport": "openai_compat",
        }
    ]


def test_blank_env_chain_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", "   ")
    out = live_config.chain_override(Tier.SMALL)
    assert out is not None and out[0]["transport"] == "openai_compat"
    assert not live_config.chain_env_active(Tier.SMALL)


def test_env_chain_does_not_leak_across_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.big": _DB_BIG_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)
    assert live_config.chain_override(Tier.MEDIUM) is None
    big = live_config.chain_override(Tier.BIG)
    assert big is not None and big[0]["model"] == "claude-opus-4-8"
    assert not live_config.chain_env_active(Tier.BIG)


@pytest.mark.parametrize("bad", ["not json{{", '{"model": "x"}', "[]", '"str"'])
def test_malformed_env_chain_raises_bad_input_naming_the_var(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    # A DB row exists: the failure must NOT silently fall back to it.
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", bad)
    with pytest.raises(BadInput, match="PRECIS_LLM_CHAIN_SMALL"):
        live_config.chain_override(Tier.SMALL)


def test_bad_rung_in_env_chain_raises_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, None)
    monkeypatch.setenv(
        "PRECIS_LLM_CHAIN_SMALL", '[{"model": "m", "transport": "warp-drive"}]'
    )
    with pytest.raises(BadInput, match="PRECIS_LLM_CHAIN_SMALL rung 0"):
        router.resolve_chain(Tier.SMALL, tools_needed=False, backend=Backend.ANTHROPIC)


def test_bad_rung_in_db_chain_still_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.small": '[{"model": "m", "transport": "nope"}]'})
    chain = router.resolve_chain(
        Tier.SMALL, tools_needed=False, backend=Backend.ANTHROPIC
    )
    assert chain  # fell back to the default chain, no raise


def test_env_chain_resolves_to_local_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)
    chain = router.resolve_chain(
        Tier.SMALL, tools_needed=False, backend=Backend.ANTHROPIC
    )
    assert chain == [Rung(Transport.LOCAL, model=_QWEN_ID, label="local")]


def test_env_override_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _bind(monkeypatch, None)
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)
    with caplog.at_level("WARNING", logger=live_config.log.name):
        live_config.chain_override(Tier.SMALL)
        live_config.chain_override(Tier.SMALL)
    msgs = [r.getMessage() for r in caplog.records if "overridden by" in r.getMessage()]
    assert msgs == ["llm chain for small overridden by PRECIS_LLM_CHAIN_SMALL"]


def test_env_model_beats_db_model_and_does_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, {"llm.model.small": "db-model", "llm.model.big": "db-big"})
    monkeypatch.setenv("PRECIS_LLM_MODEL_SMALL", "env-model")
    assert live_config.model_override(Tier.SMALL) == "env-model"
    assert router.resolve_model(Tier.SMALL) == "env-model"
    assert live_config.model_override(Tier.BIG) == "db-big"


def test_unset_env_model_leaves_db_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, {"llm.model.small": "db-model"})
    assert live_config.model_override(Tier.SMALL) == "db-model"


def test_route_with_env_chain_acquires_served_slot_and_logs_real_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a SMALL call pinned to the ``summarizer`` alias, with the env
    chain naming the served qwen id on the local transport, acquires the
    ``llm:<qwen>`` slot (the served_by / resource_slots path), dispatches to the
    slot's endpoint under the served name, and writes that model + transport to
    the route-log record."""
    _bind(monkeypatch, {"llm.chain.small": _DB_CHAIN})
    monkeypatch.setenv("PRECIS_LLM_CHAIN_SMALL", _QWEN_CHAIN)

    asked: list[str] = []

    def fake_acquire(model: str) -> ls.LocalSlot:
        asked.append(model)
        return ls.LocalSlot(
            host="h",
            resource=f"llm:{model}",
            reserved=True,
            paused=False,
            endpoint="http://slot.invalid/v1",
            served_model=model,
        )

    monkeypatch.setattr(ls, "acquire", fake_acquire)
    monkeypatch.setattr(ls, "release", lambda slot: None)

    seen: dict[str, object] = {}

    class _Run:
        def run(self, req: LlmRequest, *, model: str) -> LlmResult:
            seen["model"] = model
            seen["local_url"] = req.local_url
            return LlmResult(
                text="ok", cost_usd=None, turns_used=1, model=model, tier=req.tier
            )

    monkeypatch.setitem(router._PROVIDERS, Transport.LOCAL, _Run())

    records: list[route_log.LlmCallRecord] = []
    monkeypatch.setattr(route_log, "enabled", lambda: True)
    monkeypatch.setattr(
        route_log, "record_call", lambda rec, **_kw: records.append(rec)
    )

    res = router.route(LlmRequest(tier=Tier.SMALL, prompt="x", model="summarizer"))

    assert asked == [_QWEN_ID]
    assert seen == {"model": _QWEN_ID, "local_url": "http://slot.invalid/v1"}
    assert res.placement == "local"
    assert len(records) == 1
    assert records[0].model == _QWEN_ID
    assert records[0].transport == "local"
    assert records[0].tier == "small"
