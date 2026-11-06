"""Tests for LlmHandler's /accounts, /pools views and the owner-only
account put (migration 0280, S1a).

Covers: the views render every seeded row with no secret value anywhere
(acceptance c); put refuses an unknown secret name, a base_url key, a bad
adapter and a non-owner source, and upserts an account plus its carrier
pool rows (acceptance b); rate_limit.acquire/park/_record_refusal on a
seeded pool key behave like any other carrier row (acceptance e). Real PG.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    return LlmHandler(hub=Hub(store=store))


class TestAccountsView:
    def test_renders_every_seed_account(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/accounts").body
        for name in ("claude-sub", "codex", "devin", "kimi", "metered"):
            assert name in body

    def test_unknown_default_model_renders_unknown(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/accounts").body
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("codex:")]
        assert lines, body
        assert "default_model=unknown" in lines[0]

    def test_vault_secret_named_never_valued(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/accounts").body
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("devin:")]
        assert lines, body
        assert "DEVIN_API_TOKEN" in lines[0]
        # Presence is reported as a tag, never a decrypted value.
        assert "MISSING" in lines[0] or "in vault" in lines[0]

    def test_no_api_key_ref_renders_host_login(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/accounts").body
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("claude-sub:")]
        assert lines, body
        assert "key=none" in lines[0]


class TestPoolsView:
    def test_renders_every_seed_pool(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/pools").body
        for key in (
            "llm:claude-sub:5h",
            "llm:claude-sub:7d",
            "llm:codex:weekly",
            "llm:kimi:monthly",
            "llm:devin:sessions",
            "llm:openrouter:credits",
        ):
            assert key in body

    def test_pacing_is_literal_na(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/pools").body
        for line in body.splitlines():
            if line.strip().startswith("llm:"):
                assert "pacing=n/a" in line

    def test_unreported_fields_render_unknown(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/pools").body
        lines = [
            ln for ln in body.splitlines() if ln.strip().startswith("llm:codex:weekly")
        ]
        assert lines, body
        line = lines[0]
        assert "limit=unknown" in line
        assert "used=unknown" in line
        assert "reset_rule=unknown" in line

    def test_devin_sessions_limit_reported(self, store: Any) -> None:
        h = _handler(store)
        body = h.get(id="/pools").body
        lines = [
            ln
            for ln in body.splitlines()
            if ln.strip().startswith("llm:devin:sessions")
        ]
        assert lines, body
        assert "limit=1" in lines[0]


class TestPutAccount:
    def test_refuses_non_owner(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(
                args={
                    "account": {
                        "name": "new-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "api",
                    }
                }
            )

    def test_refuses_base_url_key(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput, match="profiles"):
            h.put(
                args={
                    "account": {
                        "name": "new-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "api",
                        "base_url": "https://evil.example/",
                    }
                }
            )

    def test_refuses_bad_adapter(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(
                args={
                    "account": {
                        "name": "new-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "not-a-real-adapter",
                    }
                }
            )

    def test_refuses_unknown_secret_name(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(
                args={
                    "account": {
                        "name": "new-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "api",
                        "api_key_ref": "NOT_A_REGISTERED_SECRET",
                    }
                }
            )

    def test_upserts_account_and_creates_pool_rows(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account, list_pools

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        resp = h.put(
            args={
                "account": {
                    "name": "new-account",
                    "vendor": "newvendor",
                    "plan": "metered",
                    "adapter": "api",
                    "concurrency": 3,
                    "task_classes": ["code", "docs"],
                    "models": ["m1", "m2"],
                    "default_model": "m1",
                    "enabled": True,
                    "pools": ["llm:new-account:weekly"],
                }
            }
        )
        assert "new-account" in resp.body

        a = get_account(store, "new-account")
        assert a is not None
        assert a.vendor == "newvendor"
        assert a.concurrency == 3
        assert a.task_classes == ("code", "docs")
        assert a.default_model == "m1"
        assert a.enabled is True

        pools = {p.provider: p for p in list_pools(store)}
        new_pool = pools["llm:new-account:weekly"]
        assert new_pool.capacity == 1
        assert new_pool.refill_per_sec == 1.0
        assert new_pool.cls == "llm-api"
        assert new_pool.daily_cap is None

    def test_update_is_read_modify_write_not_a_patch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Updating one field keeps every other column at its prior value —
        a whole-row replace, never a jsonb merge."""
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        h.put(
            args={
                "account": {
                    "name": "claude-sub",
                    "concurrency": 4,
                }
            }
        )
        a = get_account(store, "claude-sub")
        assert a is not None
        assert a.concurrency == 4
        # Everything else from the seed survives untouched.
        assert a.vendor == "anthropic"
        assert a.plan == "subscription"
        assert a.adapter == "claude_p"
        assert a.default_model == "claude-sonnet-5"
        assert set(a.pools) == {"llm:claude-sub:5h", "llm:claude-sub:7d"}

    def test_create_requires_vendor_plan_adapter(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(args={"account": {"name": "incomplete-account"}})

    def test_refuses_bad_pool_key(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(
                args={
                    "account": {
                        "name": "bad-pool-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "api",
                        "pools": ["host:example.com"],
                    }
                }
            )

    def test_refuses_bad_task_class(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(
                args={
                    "account": {
                        "name": "bad-class-account",
                        "vendor": "v",
                        "plan": "p",
                        "adapter": "api",
                        "task_classes": ["not-a-real-class"],
                    }
                }
            )


class TestRateLimitUnaffected:
    def test_acquire_park_refusal_on_seeded_pool_key(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """llm:codex:weekly, seeded by this migration, behaves exactly like
        any other carrier-shape external_rate_limits row — no routing
        behaviour changed by adding the reporting columns."""
        from precis.utils import rate_limit

        monkeypatch.setattr(rate_limit, "_resolve_dsn", lambda: store.dsn)
        monkeypatch.setattr(rate_limit, "_refusals", {})
        rate_limit.close()
        try:
            assert rate_limit.acquire("llm:codex:weekly", seed_row=None) is True
            rate_limit.park("llm:codex:weekly", 1.0)
            rate_limit._record_refusal("llm:codex:weekly", "test refusal")
            assert rate_limit.refusal_counts().get("llm:codex:weekly", 0) == 1
        finally:
            rate_limit.close()
