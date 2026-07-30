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
    _set_op_override,
    set_llm_chain,
    set_llm_cloud,
    set_llm_op,
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


# ── Phase 2 (docs/proposals/llm-operation-routing.md item 4) — the
# per-operation override editor ───────────────────────────────────────────


def test_set_op_override_pinned_with_model_writes_row(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.op_key("reading_brief"))
    _set_op_override(store, "reading_brief", "pinned", "claude-opus-4-8")
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief"))
        == '{"model": "claude-opus-4-8"}'
    )


def test_set_op_override_pinned_blank_model_is_noop(store: Any) -> None:
    """ "pinned" with no model chosen leaves the prior override (if any)
    untouched, rather than clearing or writing a bad row."""
    _set_op_override(store, "reading_brief", "pinned", "claude-opus-4-8")
    _set_op_override(store, "reading_brief", "pinned", "")
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief"))
        == '{"model": "claude-opus-4-8"}'
    )


def test_set_op_override_tier_remap_writes_row(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.op_key("meditation"))
    _set_op_override(store, "meditation", "big", "")
    assert (
        budget_settings.get_setting(store, live_config.op_key("meditation"))
        == '{"tier": "big"}'
    )


def test_set_op_override_tier_ignores_stray_model(store: Any) -> None:
    """Finding 2 — tier and model are mutually exclusive: picking a plain
    capability tier must win even if a stale/sticky ``model`` field rides
    along on the same submit (the prior "model always wins" trap)."""
    budget_settings.clear_setting(store, live_config.op_key("meditation"))
    _set_op_override(store, "meditation", "big", "claude-opus-4-8")
    assert (
        budget_settings.get_setting(store, live_config.op_key("meditation"))
        == '{"tier": "big"}'
    )


def test_set_op_override_blank_clears(store: Any) -> None:
    _set_op_override(store, "reading_brief", "pinned", "claude-opus-4-8")
    _set_op_override(store, "reading_brief", "", "")
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief")) is None
    )


def test_set_op_override_default_tier_clears(store: Any) -> None:
    _set_op_override(store, "reading_brief", "big", "")
    _set_op_override(store, "reading_brief", "default", "")
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief")) is None
    )


def test_set_op_override_unknown_tier_is_noop(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.op_key("reading_brief"))
    _set_op_override(store, "reading_brief", "cloud-super", "")
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief")) is None
    )


def test_set_op_override_excluded_source_is_noop(store: Any) -> None:
    """AC8 — a non-steerable source (excluded or unregistered) must never
    get a row, even via a direct write attempt (defense-in-depth; the
    template also never renders a form for it)."""
    budget_settings.clear_setting(store, live_config.op_key("classify"))
    _set_op_override(store, "classify", "pinned", "claude-opus-4-8")
    assert budget_settings.get_setting(store, live_config.op_key("classify")) is None


def test_set_op_override_unregistered_source_is_noop(store: Any) -> None:
    budget_settings.clear_setting(store, live_config.op_key("dream"))
    _set_op_override(store, "dream", "pinned", "claude-opus-4-8")
    assert budget_settings.get_setting(store, live_config.op_key("dream")) is None


def test_post_llm_op_route_writes_and_clears(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(factory_routes, "get_store", lambda request: store)
    budget_settings.clear_setting(store, live_config.op_key("reading_brief"))

    resp = asyncio.run(
        set_llm_op(
            request=cast(Any, None),
            source="reading_brief",
            tier="pinned",
            model="claude-opus-4-8",
        )
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services"
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief"))
        == '{"model": "claude-opus-4-8"}'
    )

    resp = asyncio.run(
        set_llm_op(
            request=cast(Any, None), source="reading_brief", tier="default", model=""
        )
    )
    assert resp.status_code == 303
    assert (
        budget_settings.get_setting(store, live_config.op_key("reading_brief")) is None
    )


def test_post_llm_op_route_excluded_source_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    monkeypatch.setattr(factory_routes, "get_store", lambda request: store)
    budget_settings.clear_setting(store, live_config.op_key("classify"))

    resp = asyncio.run(
        set_llm_op(
            request=cast(Any, None),
            source="classify",
            tier="pinned",
            model="claude-opus-4-8",
        )
    )
    assert resp.status_code == 303
    assert budget_settings.get_setting(store, live_config.op_key("classify")) is None
