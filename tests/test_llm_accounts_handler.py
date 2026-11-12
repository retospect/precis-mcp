"""Tests for LlmHandler's /accounts, /pools views and the owner-only
account put (migration 0280, S1a).

Covers: the views render every seeded row with no secret value anywhere
(acceptance c); put refuses an unknown secret name, a base_url key, a bad
adapter and a non-owner source, and upserts an account plus its carrier
pool rows (acceptance b); rate_limit.acquire/park/_record_refusal on a
seeded pool key behave like any other carrier row (acceptance e). Real PG.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from precis.errors import BadInput


@pytest.fixture(autouse=True)
def _restore_pool_rows(store: Any) -> Iterator[None]:
    """Both ledger tables persist, including put and park/acquire mutations."""
    snapshots: list[tuple[str, str, list[str], list[Any]]] = []
    for table, key, predicate in (
        ("llm_account", "name", "TRUE"),
        ("external_rate_limits", "provider", "provider LIKE 'llm:%%'"),
    ):
        with store.pool.connection() as conn:
            cursor = conn.execute(
                sql.SQL("SELECT * FROM {} WHERE " + predicate).format(
                    sql.Identifier(table)
                )
            )
            columns = [column.name for column in cursor.description]
            snapshots.append((table, key, columns, cursor.fetchall()))
    with store.pool.connection() as conn:
        profiles_row = conn.execute(
            "SELECT value, updated_at, updated_by FROM app_settings "
            "WHERE key = 'llm.profiles'"
        ).fetchone()
    yield
    with store.pool.connection() as conn:
        for table, key, columns, rows in snapshots:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns
            )
            key_index = columns.index(key)
            keys = [row[key_index] for row in rows]
            predicate = "" if table == "llm_account" else "provider LIKE 'llm:%%' AND "
            conn.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE " + predicate + "NOT ({} = ANY(%s))"
                ).format(sql.Identifier(table), sql.Identifier(key)),
                (keys,),
            )
            for row in rows:
                values = tuple(
                    Jsonb(value) if isinstance(value, dict) else value for value in row
                )
                conn.execute(
                    sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                        sql.Identifier(table), assignments, sql.Identifier(key)
                    ),
                    (*values, row[key_index]),
                )
        conn.execute("DELETE FROM app_settings WHERE key = 'llm.profiles'")
        if profiles_row is not None:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at, updated_by) "
                "VALUES ('llm.profiles', %s, %s, %s)",
                (profiles_row[0], profiles_row[1], profiles_row[2]),
            )


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    return LlmHandler(hub=Hub(store=store))


def _slog_call(
    store: Any,
    *,
    model: str = "suit-view-model",
    task_kind: str,
    source: str = "suit-view-src",
    transport: str = "openai_compat",
    n: int = 1,
    observed: dict[str, Any] | None = None,
) -> None:
    """``n`` validated-success ``llm_call_log`` rows for one task kind."""
    features: dict[str, Any] = {"task_kind": task_kind}
    if observed is not None:
        features["observed"] = observed
    with store.pool.connection() as conn:
        for _ in range(n):
            conn.execute(
                "INSERT INTO llm_call_log "
                "(source, tier, transport, model, errored, data_parsed, "
                "features) VALUES (%s, 'frontier', %s, %s, false, true, %s)",
                (source, transport, model, Jsonb(features)),
            )


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


class TestDecisionsView:
    def test_empty_window_and_supported_view(self, store: Any) -> None:
        handler = _handler(store)
        assert "decisions" in handler._supported_list_views()
        assert handler.get(id="/decisions").body == "# llm decisions (0 calls, 7 d)"

    def test_rollup_agreement_prices_and_excludes_gates(self, store: Any) -> None:
        from precis import llm_catalog, route_log

        cases: tuple[tuple[bool, float | None, bool], ...] = (
            (True, 0.25, False),
            (False, None, False),
            (True, 9.0, True),
        )
        for agree, projected, gated in cases:
            decision: dict[str, Any] = {
                "agree": agree,
                "claude_window": {"legacy": True, "shadow": agree},
            }
            if projected is not None:
                decision["projected_usd"] = projected
            route_log.record_call(
                route_log.LlmCallRecord(
                    source="test:decisions",
                    tier="small",
                    transport="local",
                    model="decision-test",
                    tools_needed=False,
                    request_text="request",
                    response_text="response",
                    cost_usd=0.5,
                    turns_used=None,
                    duration_ms=1,
                    errored=False,
                    error=None,
                    data_parsed=None,
                    features={
                        "task_kind": "test.decisions",
                        "decision": decision,
                        "gated": gated,
                    },
                    store_blobs=False,
                ),
                store=store,
            )
        rows = llm_catalog.decision_rollup(store)
        assert len(rows) == 1
        row = rows[0]
        assert (row.task_kind, row.calls, row.agree) == ("test.decisions", 2, 1)
        assert (row.legacy_claude, row.shadow_claude, row.projected_known) == (2, 1, 1)
        assert row.actual_usd == pytest.approx(0.5)
        assert row.projected_usd == pytest.approx(0.25)
        body = _handler(store).get(id="/decisions").body
        assert "# llm decisions (2 calls, 7 d)" in body
        assert "test.decisions: calls=2 agree=50% claude legacy=2 shadow=1" in body
        assert "usd actual=0.500000 projected=0.250000 (1/2 priced)" in body

    def test_rollup_actual_covers_only_priced_rows(self, store: Any) -> None:
        """actual_usd sums cost over the same rows projected_usd covers —
        an unpriced row's spend never inflates the comparison."""
        from precis import llm_catalog, route_log

        for cost, projected in ((0.5, 0.4), (2.0, None)):
            decision: dict[str, Any] = {
                "agree": True,
                "claude_window": {"legacy": False, "shadow": False},
            }
            if projected is not None:
                decision["projected_usd"] = projected
            route_log.record_call(
                route_log.LlmCallRecord(
                    source="test:rollup",
                    tier="small",
                    transport="local",
                    model="rollup-test",
                    tools_needed=False,
                    request_text="request",
                    response_text="response",
                    cost_usd=cost,
                    turns_used=None,
                    duration_ms=1,
                    errored=False,
                    error=None,
                    data_parsed=None,
                    features={
                        "task_kind": "test.rollup",
                        "decision": decision,
                    },
                    store_blobs=False,
                ),
                store=store,
            )
        (row,) = llm_catalog.decision_rollup(store)
        assert row.actual_usd == pytest.approx(0.5)
        assert row.projected_usd == pytest.approx(0.4)
        assert (row.calls, row.projected_known) == (2, 1)


class TestSliceDecisionRows:
    """``/decisions`` lists lane-pick shadow agreement per task class off
    the ``meta.decision`` stamp on ``run_slice`` jobs (td572552)."""

    @staticmethod
    def _job(
        store: Any,
        decision: dict[str, Any] | None,
        *,
        job_type: str = "run_slice",
        age_days: int = 0,
    ) -> None:
        meta: dict[str, Any] = {"job_type": job_type}
        if decision is not None:
            meta["decision"] = decision
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO refs (kind, title, meta, created_at) VALUES "
                "('job', 'slice-decision', %s, now() - (%s * interval '1 day'))",
                (Jsonb(meta), age_days),
            )

    def test_agreement_per_task_class(self, store: Any) -> None:
        from precis import llm_catalog

        def stamp(cls: str | None, agree: bool, match: bool) -> dict[str, Any]:
            out: dict[str, Any] = {
                "site": "submit",
                "agree": agree,
                "lane_match": match,
            }
            if cls is not None:
                out["task_class"] = cls
            return out

        for cls, agree, match in (
            ("code", True, True),
            ("code", False, False),
            ("code", True, True),
            ("review", False, True),
            (None, True, True),
        ):
            self._job(store, stamp(cls, agree, match))
        # Outside the window, another job type and an unstamped job: not counted.
        self._job(store, stamp("code", True, True), age_days=30)
        self._job(store, stamp("code", True, True), job_type="other")
        self._job(store, None)
        rows = {r.task_class: r for r in llm_catalog.slice_decision_rollup(store)}
        assert (rows["code"].picks, rows["code"].agree, rows["code"].lane_match) == (
            3,
            2,
            2,
        )
        assert (rows["review"].picks, rows["review"].agree) == (1, 0)
        assert rows["unknown"].picks == 1
        body = _handler(store).get(id="/decisions").body
        assert "# slice lane picks (5 picks, 7 d)" in body
        assert "  code: picks=3 agree=67% lane_match=2" in body
        assert "  review: picks=1 agree=0% lane_match=1" in body

    def test_no_slice_section_without_stamps(self, store: Any) -> None:
        self._job(store, None)
        assert _handler(store).get(id="/decisions").body == (
            "# llm decisions (0 calls, 7 d)"
        )


class TestSuitabilityView:
    """``/suitability`` renders the stored ``meta.suitability`` posteriors
    for the ten busiest task kinds — it never recomputes them."""

    def test_empty_log_renders_header_only(self, store: Any) -> None:
        handler = _handler(store)
        assert "suitability" in handler._supported_list_views()
        assert (
            handler.get(id="/suitability").body
            == "# llm suitability (0 task kinds, 7 d)"
        )

    def test_busy_kind_without_posteriors_renders_no_outcomes(self, store: Any) -> None:
        _slog_call(store, task_kind="test.suit.none", n=3)
        body = _handler(store).get(id="/suitability").body
        assert "  test.suit.none: no outcomes" in body

    def test_stored_posterior_renders_label_and_interval(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm.suitability import rollup

        tag = uuid4().hex[:8]
        model = f"suit-view-{tag}"
        llm_catalog.upsert_card(store, model_id=model, text="A model.")
        _slog_call(
            store,
            model=model,
            task_kind="test.suit.view",
            source=f"suit-view-{tag}",
            n=4,
        )
        rollup(store)
        body = _handler(store).get(id="/suitability").body
        line = next(ln for ln in body.splitlines() if "test.suit.view" in ln)
        assert f"{model}@openai_compat" in line
        assert "band=any" in line
        assert "label=unqualified" in line
        assert "n=4" in line
        assert "L90=" in line and "interval=[" in line
        assert "identity=requested only" in line
        assert "drift=no" in line

    def test_observed_identity_renders_observed(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm.suitability import rollup

        tag = uuid4().hex[:8]
        model = f"suit-obs-{tag}"
        llm_catalog.upsert_card(store, model_id=model, text="A model.")
        _slog_call(
            store,
            model=model,
            task_kind="test.suit.obs",
            source=f"suit-obs-{tag}",
            n=4,
            observed={"model": "wire-m", "provider": "p"},
        )
        rollup(store)
        body = _handler(store).get(id="/suitability").body
        line = next(ln for ln in body.splitlines() if "test.suit.obs" in ln)
        assert "identity=observed" in line

    def test_view_caps_at_ten_busiest_kinds(self, store: Any) -> None:
        for i in range(11):
            _slog_call(store, model=f"suit-cap-m{i}", task_kind=f"test.suit.k{i:02d}")
        body = _handler(store).get(id="/suitability").body
        assert body.startswith("# llm suitability (10 task kinds, 7 d)")
        kind_lines = [
            ln for ln in body.splitlines() if ln.strip().startswith("test.suit.k")
        ]
        assert len(kind_lines) == 10
        assert all("test.suit.k10" not in ln for ln in kind_lines)


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
            "llm:devin:acu",
            "llm:openrouter:credits",
        ):
            assert key in body

    def test_pacing_reads_each_pool(self, store: Any) -> None:
        """Seeds carry no report or reset, so every one reads unknown; a
        7-day pool at 96 % three days out reads ahead."""
        from precis.utils.llm.accounts import record_pool_report

        body = _handler(store).get(id="/pools").body
        lines = [line for line in body.splitlines() if line.strip().startswith("llm:")]
        assert len(lines) == 7
        assert all(line.endswith("pacing=unknown") for line in lines), lines
        key = "llm:claude-sub:7d"
        now = datetime.now(UTC)
        assert record_pool_report(
            store,
            key,
            used=96,
            remaining=4,
            limit=100,
            source="provider",
            reported_at=now,
            resets_at=now + timedelta(days=3),
        )
        body = _handler(store).get(id="/pools").body
        line = next(line for line in body.splitlines() if line.strip().startswith(key))
        assert line.endswith("pacing=ahead")

    def test_retry_at_and_stale_render(self, store: Any) -> None:
        body = _handler(store).get(id="/pools").body
        lines = [line for line in body.splitlines() if line.strip().startswith("llm:")]
        assert len(lines) == 7
        for line in lines:
            assert "retry_at=" in line
            assert "stale=true" in line

    def test_known_retry_and_fresh_report_render(self, store: Any) -> None:
        from precis.utils.llm.accounts import record_pool_failure, record_pool_report

        key = "llm:codex:weekly"
        retry = datetime(2099, 1, 1, tzinfo=UTC)
        record_pool_report(
            store, key, used=37, source="provider", reported_at=datetime.now(UTC)
        )
        record_pool_failure(
            store, key, reason_class="quota", retry_at=retry, notice="provider"
        )
        body = _handler(store).get(id="/pools").body
        line = next(line for line in body.splitlines() if line.strip().startswith(key))
        assert "retry_at=2099-01-01T00:00:00Z" in line
        assert "stale=false" in line

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
        assert a.default_model == "claude-sonnet-5-5"
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


def _hosted_payload(**overrides: Any) -> dict[str, Any]:
    """A door-complete hosted-provider account payload (groq by default):
    endpoint, per-model price, free-tier limits and the privacy stance,
    each with a source URL and a verification date."""
    payload: dict[str, Any] = {
        "name": "groq-free",
        "vendor": "groq",
        "plan": "free",
        "adapter": "openai_compat",
        "endpoint": {"base_url": "https://api.groq.com/openai/v1"},
        "api_key_ref": "GROQ_API_KEY",
        "models": ["m-groq"],
        "privacy": {
            "training_use": "no",
            "retention": "zdr",
            "source_url": "https://groq.com/privacy/",
            "verified_at": "2026-11-01T00:00:00Z",
        },
        "entitlement": {
            "models": {
                "m-groq": {
                    "allowed": True,
                    "price_in": 0,
                    "price_out": 0,
                    "source_url": "https://groq.com/pricing/",
                    "verified_at": "2026-11-01T00:00:00Z",
                }
            }
        },
        "limits": [
            {
                "pool": "llm:groq-free:rpm",
                "unit": "calls",
                "limit": 30,
                "reset_rule": "rolling:60",
                "source_url": "https://groq.com/docs/rate-limits/",
                "verified_at": "2026-11-01T00:00:00Z",
            },
            {
                "pool": "llm:groq-free:rpd",
                "unit": "calls",
                "limit": 1000,
                "reset_rule": "rolling:86400",
                "source_url": "https://groq.com/docs/rate-limits/",
                "verified_at": "2026-11-01T00:00:00Z",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _profiles_map(store: Any) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'llm.profiles'"
        ).fetchone()
    return json.loads(row[0]) if row else {}


def _write_profiles(store: Any, entries: dict[str, Any]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES ('llm.profiles', %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps(entries),),
        )


class TestPutHostedAccount:
    """The owner door for a hosted OpenAI-compatible provider (td573668):
    endpoint, per-model price, free-tier limits and the privacy stance
    land in one put; the account is born disabled and enabled by a
    second explicit put."""

    def test_groq_account_created_disabled_with_profile_and_limits(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account, list_pools
        from precis.utils.llm.router import resolve_profile

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        h.put(args={"account": _hosted_payload()})

        acct = get_account(store, "groq-free")
        assert acct is not None
        assert acct.enabled is False
        assert acct.profile == "groq-free"

        profiles = _profiles_map(store)
        assert profiles == {
            "groq-free": {
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_ref": "GROQ_API_KEY",
                "kind": "metered",
            }
        }
        monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps(profiles))
        assert resolve_profile("groq-free") is not None

        pools = {p.provider: p for p in list_pools(store)}
        for key, limit, rule in (
            ("llm:groq-free:rpm", 30, "rolling:60"),
            ("llm:groq-free:rpd", 1000, "rolling:86400"),
        ):
            pool = pools[key]
            assert pool.unit == "calls"
            assert pool.limit_reported == limit
            assert pool.reset_rule == rule
            assert pool.source == "user"
            assert pool.used_reported is None
        assert set(acct.pools) == {"llm:groq-free:rpm", "llm:groq-free:rpd"}
        limits = acct.entitlement["limits"]
        for key in ("llm:groq-free:rpm", "llm:groq-free:rpd"):
            entry = limits[key]
            assert entry["unit"] == "calls"
            assert entry["limit"] > 0
            assert entry["source_url"].startswith("https://")
            assert entry["verified_at"]

    def test_openrouter_account_created_beside_groq(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        h.put(args={"account": _hosted_payload()})
        groq_entry = _profiles_map(store)["groq-free"]
        h.put(
            args={
                "account": _hosted_payload(
                    name="openrouter-free",
                    vendor="openrouter",
                    endpoint={"base_url": "https://openrouter.ai/api/v1"},
                    api_key_ref="OPENROUTER_API_KEY",
                    models=["m-or"],
                    privacy={
                        "training_use": "unknown",
                        "source_url": "https://openrouter.ai/privacy",
                        "verified_at": "2026-11-01T00:00:00Z",
                    },
                    entitlement={
                        "models": {
                            "m-or": {
                                "allowed": True,
                                "price_in": 0,
                                "price_out": 0,
                                "source_url": "https://openrouter.ai/models",
                                "verified_at": "2026-11-01T00:00:00Z",
                            }
                        }
                    },
                    limits=[
                        {
                            "pool": "llm:openrouter-free:rpd",
                            "unit": "calls",
                            "limit": 50,
                            "reset_rule": "rolling:86400",
                            "source_url": "https://openrouter.ai/docs/limits",
                            "verified_at": "2026-11-01T00:00:00Z",
                        }
                    ],
                )
            }
        )
        acct = get_account(store, "openrouter-free")
        assert acct is not None and acct.enabled is False
        assert acct.profile == "openrouter-free"
        profiles = _profiles_map(store)
        assert profiles["groq-free"] == groq_entry
        assert profiles["openrouter-free"] == {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_ref": "OPENROUTER_API_KEY",
            "kind": "metered",
        }

    def test_endpoint_host_must_match_key(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        mismatched = _hosted_payload(
            endpoint={"base_url": "https://openrouter.ai/api/v1"}
        )
        unlisted_key = _hosted_payload(api_key_ref="DEEPINFRA_API_KEY")
        no_key = _hosted_payload()
        del no_key["api_key_ref"]
        http = _hosted_payload(endpoint={"base_url": "http://api.groq.com/openai/v1"})
        for payload in (mismatched, unlisted_key, no_key, http):
            with pytest.raises(BadInput):
                h.put(args={"account": payload})
            assert get_account(store, "groq-free") is None
        assert _profiles_map(store) == {}

    def test_existing_profile_entry_is_never_overwritten(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        _write_profiles(
            store,
            {
                "groq-free": {
                    "base_url": "https://api.groq.com/other",
                    "api_key_ref": "GROQ_API_KEY",
                    "kind": "metered",
                }
            },
        )
        with pytest.raises(BadInput, match="profiles"):
            h.put(args={"account": _hosted_payload()})
        assert get_account(store, "groq-free") is None
        assert (
            _profiles_map(store)["groq-free"]["base_url"]
            == "https://api.groq.com/other"
        )
        _write_profiles(store, {})
        h.put(args={"account": _hosted_payload()})
        h.put(args={"account": _hosted_payload()})
        assert get_account(store, "groq-free") is not None

    def test_create_refuses_enabled_and_missing_evidence(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        bad: list[dict[str, Any]] = []
        bad.append(_hosted_payload(enabled=True))
        bad.append(_hosted_payload(privacy={}))
        bad.append(
            _hosted_payload(
                privacy={
                    "training_use": "maybe",
                    "source_url": "https://groq.com/privacy/",
                    "verified_at": "2026-11-01T00:00:00Z",
                }
            )
        )
        bad.append(_hosted_payload(privacy={"training_use": "no"}))
        bad.append(
            _hosted_payload(
                privacy={
                    "training_use": "no",
                    "source_url": "https://groq.com/privacy/",
                }
            )
        )
        bad.append(
            _hosted_payload(
                privacy={
                    "training_use": "no",
                    "source_url": "http://groq.com/privacy/",
                    "verified_at": "2026-11-01T00:00:00Z",
                }
            )
        )
        no_endpoint = _hosted_payload()
        del no_endpoint["endpoint"]
        bad.append(no_endpoint)
        bad_unit = _hosted_payload()
        bad_unit["limits"][0]["unit"] = "usd"
        bad.append(bad_unit)
        zero_limit = _hosted_payload()
        zero_limit["limits"][0]["limit"] = 0
        bad.append(zero_limit)
        expiry = _hosted_payload()
        expiry["limits"][0]["reset_rule"] = "expires:2027-01-01T00:00:00Z"
        bad.append(expiry)
        foreign_pool = _hosted_payload()
        foreign_pool["limits"][0]["pool"] = "llm:other:rpm"
        bad.append(foreign_pool)
        no_source = _hosted_payload()
        del no_source["limits"][0]["source_url"]
        bad.append(no_source)
        no_verified = _hosted_payload()
        del no_verified["limits"][0]["verified_at"]
        bad.append(no_verified)
        no_model_source = _hosted_payload()
        del no_model_source["entitlement"]["models"]["m-groq"]["source_url"]
        bad.append(no_model_source)
        no_model_verified = _hosted_payload()
        del no_model_verified["entitlement"]["models"]["m-groq"]["verified_at"]
        bad.append(no_model_verified)
        negative_price = _hosted_payload()
        negative_price["entitlement"]["models"]["m-groq"]["price_in"] = -1
        bad.append(negative_price)
        for payload in bad:
            with pytest.raises(BadInput):
                h.put(args={"account": payload})
            assert get_account(store, "groq-free") is None

    def test_enable_is_a_second_explicit_put(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)

        def seed_account(
            name: str, *, privacy: dict[str, Any], models: list[str]
        ) -> None:
            with store.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO llm_account (name, vendor, plan, adapter, "
                    "profile, api_key_ref, models, privacy) VALUES "
                    "(%s, 'groq', 'free', 'openai_compat', %s, "
                    "'GROQ_API_KEY', %s, %s::jsonb)",
                    (name, name, models, json.dumps(privacy)),
                )

        profile_entry = {
            "base_url": "https://api.groq.com/openai/v1",
            "api_key_ref": "GROQ_API_KEY",
            "kind": "metered",
        }
        sourced_stance = {
            "training_use": "no",
            "source_url": "https://groq.com/privacy/",
            "verified_at": "2026-11-01T00:00:00Z",
        }
        seed_account("doorless", privacy=sourced_stance, models=["m"])
        _write_profiles(store, {"unrelated": profile_entry})
        with pytest.raises(BadInput, match="profiles"):
            h.put(args={"account": {"name": "doorless", "enabled": True}})
        seed_account("no-stance", privacy={}, models=["m"])
        _write_profiles(store, {"no-stance": profile_entry})
        with pytest.raises(BadInput, match="training_use"):
            h.put(args={"account": {"name": "no-stance", "enabled": True}})
        bad_stance = {**sourced_stance, "training_use": "sometimes"}
        seed_account("bad-stance", privacy=bad_stance, models=["m"])
        _write_profiles(store, {"bad-stance": profile_entry})
        with pytest.raises(BadInput, match="training_use"):
            h.put(args={"account": {"name": "bad-stance", "enabled": True}})
        seed_account("no-models", privacy=sourced_stance, models=[])
        _write_profiles(store, {"no-models": profile_entry})
        with pytest.raises(BadInput, match="models"):
            h.put(args={"account": {"name": "no-models", "enabled": True}})

        h.put(args={"account": _hosted_payload()})
        before = get_account(store, "groq-free")
        assert before is not None and before.enabled is False
        h.put(args={"account": {"name": "groq-free", "enabled": True}})
        after = get_account(store, "groq-free")
        assert after is not None and after.enabled is True
        assert replace(after, enabled=False, updated_at=before.updated_at) == before

    def test_update_puts_revalidate_evidence(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The evidence contract is a property of the stored row, not of
        the onboarding put: a later put on a hosted account can't land a
        negative or unsourced price, nor a bare or out-of-vocab stance
        (td573741)."""
        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        h = _handler(store)
        h.put(args={"account": _hosted_payload()})
        stored = get_account(store, "groq-free")
        assert stored is not None

        bad_prices: list[dict[str, Any]] = []
        negative = _hosted_payload()
        del negative["endpoint"]
        negative["entitlement"]["models"]["m-groq"]["price_in"] = -1
        bad_prices.append(negative)
        unsourced = _hosted_payload()
        del unsourced["endpoint"]
        del unsourced["entitlement"]["models"]["m-groq"]["source_url"]
        bad_prices.append(unsourced)
        unverified = _hosted_payload()
        del unverified["endpoint"]
        del unverified["entitlement"]["models"]["m-groq"]["verified_at"]
        bad_prices.append(unverified)
        bare_stance = _hosted_payload()
        del bare_stance["endpoint"]
        bare_stance["privacy"] = {"training_use": "no"}
        bad_prices.append(bare_stance)
        bad_stance = _hosted_payload()
        del bad_stance["endpoint"]
        bad_stance["privacy"] = {
            "training_use": "sometimes",
            "source_url": "https://groq.com/privacy/",
            "verified_at": "2026-11-01T00:00:00Z",
        }
        bad_prices.append(bad_stance)
        for payload in bad_prices:
            with pytest.raises(BadInput):
                h.put(args={"account": payload})

        reprieved = get_account(store, "groq-free")
        assert reprieved is not None
        assert reprieved.entitlement == stored.entitlement
        assert reprieved.privacy == stored.privacy

        # A compliant price update lands.
        h.put(
            args={
                "account": {
                    "name": "groq-free",
                    "entitlement": {
                        "models": {
                            "m-groq": {
                                "allowed": True,
                                "price_in": 0.05,
                                "price_out": 0.08,
                                "source_url": "https://groq.com/pricing/",
                                "verified_at": "2026-11-06T00:00:00Z",
                            }
                        }
                    },
                }
            }
        )
        updated = get_account(store, "groq-free")
        assert updated is not None
        assert updated.entitlement["models"]["m-groq"]["price_in"] == 0.05

    def test_concurrent_first_creates_keep_both_profiles(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no llm.profiles row, two racing door puts must both land
        their profile entry — the seed-then-lock serializes them
        (td573741). Without it, FOR UPDATE locks nothing and the second
        upsert overwrites the first."""
        import threading

        from precis.utils.llm.accounts import get_account

        monkeypatch.setenv("PRECIS_SOURCE", "cli")
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = 'llm.profiles'")
        h = _handler(store)
        errors: list[BaseException] = []

        def create(payload: dict[str, Any]) -> None:
            try:
                h.put(args={"account": payload})
            except Exception as exc:
                errors.append(exc)

        openrouter = _hosted_payload(
            name="openrouter-free",
            vendor="openrouter",
            endpoint={"base_url": "https://openrouter.ai/api/v1"},
            api_key_ref="OPENROUTER_API_KEY",
            models=["m-or"],
            privacy={
                "training_use": "unknown",
                "source_url": "https://openrouter.ai/privacy",
                "verified_at": "2026-11-01T00:00:00Z",
            },
            entitlement={
                "models": {
                    "m-or": {
                        "allowed": True,
                        "price_in": 0,
                        "price_out": 0,
                        "source_url": "https://openrouter.ai/models",
                        "verified_at": "2026-11-01T00:00:00Z",
                    }
                }
            },
            limits=[
                {
                    "pool": "llm:openrouter-free:rpd",
                    "unit": "calls",
                    "limit": 50,
                    "reset_rule": "rolling:86400",
                    "source_url": "https://openrouter.ai/docs/limits",
                    "verified_at": "2026-11-01T00:00:00Z",
                }
            ],
        )
        threads = [
            threading.Thread(target=create, args=(payload,))
            for payload in (_hosted_payload(), openrouter)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert get_account(store, "groq-free") is not None
        assert get_account(store, "openrouter-free") is not None
        profiles = _profiles_map(store)
        assert profiles["groq-free"]["base_url"] == ("https://api.groq.com/openai/v1")
        assert profiles["openrouter-free"]["base_url"] == (
            "https://openrouter.ai/api/v1"
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
