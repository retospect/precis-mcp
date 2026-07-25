"""Live-PG tests for the ``/factory/llm/*`` operator placement-chain editor.

The fake-store route smoke tests (wiring + redirect) live in
``test_factory.py`` alongside the other ``/factory/*`` write endpoints —
the FakeStore's SQL is a no-op, so it can't prove a real write. This file
exercises the actual ``app_settings`` round-trip (``_set_chain_override`` /
``_set_cloud_enabled``) against real Postgres.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from precis.budget import settings as budget_settings
from precis.utils.llm import live_config
from precis.utils.llm.router import Tier
from precis_web.routes import factory as factory_routes
from precis_web.routes.factory import (
    _CHAIN_TIERS,
    _set_chain_override,
    _set_cloud_enabled,
    set_llm_chain,
    set_llm_cloud,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    # The TTL cache is a module global — clear it around every test so a
    # cached value (or negative cache) can't leak between cases.
    live_config.bust_cache()
    yield
    live_config.bust_cache()


# ── ADR 0066 Phase B step 2 — operator placement-chain editor ────────────


_VALID_CHAIN_JSON = (
    '[{"placement": "cloud", "model": "claude-sonnet-5", "transport": "claude_agent"}]'
)


def test_set_chain_override_writes_valid_list(store: Any) -> None:
    _set_chain_override(store, Tier.BIG, _VALID_CHAIN_JSON)
    assert (
        budget_settings.get_setting(store, live_config.chain_key(Tier.BIG))
        == _VALID_CHAIN_JSON
    )


def test_set_chain_override_blank_clears(store: Any) -> None:
    _set_chain_override(store, Tier.BIG, _VALID_CHAIN_JSON)
    _set_chain_override(store, Tier.BIG, "   ")
    assert budget_settings.get_setting(store, live_config.chain_key(Tier.BIG)) is None


def test_set_chain_override_malformed_json_is_noop(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.chain_key(Tier.BIG))
    _set_chain_override(store, Tier.BIG, "{not json")
    assert budget_settings.get_setting(store, live_config.chain_key(Tier.BIG)) is None


def test_set_chain_override_non_list_is_noop(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.chain_key(Tier.BIG))
    _set_chain_override(store, Tier.BIG, '{"a": 1}')
    assert budget_settings.get_setting(store, live_config.chain_key(Tier.BIG)) is None


def test_chain_tiers_excludes_unknown_values() -> None:
    assert "cloud-super" not in _CHAIN_TIERS
    assert {"frontier", "big", "medium", "small"} == set(_CHAIN_TIERS)


def test_post_llm_chain_route_invalid_tier_is_noop(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(factory_routes, "get_store", lambda request: store)
    budget_settings.clear_setting(store, "llm.chain.cloud-super")
    resp = asyncio.run(
        set_llm_chain(
            request=cast(Any, None), tier="cloud-super", chain_json=_VALID_CHAIN_JSON
        )
    )
    assert resp.status_code == 303
    assert budget_settings.get_setting(store, "llm.chain.cloud-super") is None


def test_post_llm_chain_route_valid_tier_writes(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(factory_routes, "get_store", lambda request: store)
    budget_settings.clear_setting(store, live_config.chain_key(Tier.SMALL))
    resp = asyncio.run(
        set_llm_chain(
            request=cast(Any, None), tier="small", chain_json=_VALID_CHAIN_JSON
        )
    )
    assert resp.status_code == 303
    assert (
        budget_settings.get_setting(store, live_config.chain_key(Tier.SMALL))
        == _VALID_CHAIN_JSON
    )


def test_set_cloud_enabled_false_writes_row(store: Any) -> None:
    _set_cloud_enabled(store, False)
    assert budget_settings.get_setting(store, live_config.CLOUD_ENABLED_KEY) == "false"


def test_set_cloud_enabled_true_clears_row(store: Any) -> None:
    _set_cloud_enabled(store, False)
    _set_cloud_enabled(store, True)
    assert budget_settings.get_setting(store, live_config.CLOUD_ENABLED_KEY) is None


def test_post_llm_cloud_route_false_and_true(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(factory_routes, "get_store", lambda request: store)

    resp = asyncio.run(set_llm_cloud(request=cast(Any, None), enabled="false"))
    assert resp.status_code == 303
    assert budget_settings.get_setting(store, live_config.CLOUD_ENABLED_KEY) == "false"

    resp = asyncio.run(set_llm_cloud(request=cast(Any, None), enabled="true"))
    assert resp.status_code == 303
    assert budget_settings.get_setting(store, live_config.CLOUD_ENABLED_KEY) is None
