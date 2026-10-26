"""Local-first log rollups and cross-process daily alert claims, on dev PG."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from precis.utils.llm import local_first_stats as stats

OP = "gripe_triage:tick"


def _row(
    store: Any,
    *,
    arm: str = "local",
    mode: str = "on",
    stamp: dict[str, Any] | None = None,
    cost: float = 0,
    tokens: tuple[int | None, int | None] = (1000, 2000),
    ts: datetime | None = None,
    source: str | None = None,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (source, model, transport, features, "
            "cost_usd, input_tokens, output_tokens, ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                source or (f"{OP}:local" if arm == "local" else OP),
                "local-model" if arm == "local" else "stats-paid-model",
                "local" if arm == "local" else "claude_p",
                Jsonb(
                    {
                        "local_first": {
                            "mode": mode,
                            "arm": arm,
                            "call": uuid4().hex,
                            **(stamp or {}),
                        }
                    }
                ),
                cost,
                *tokens,
                ts or datetime.now(UTC),
            ),
        )


def _catalog(store: Any) -> None:
    from precis import llm_catalog

    llm_catalog.upsert_card(
        store,
        model_id="stats-paid-model",
        text="Paid price fixture.",
        offerings=[{"transport": "claude_p", "price_in": 2, "price_out": 4}],
    )


def test_rollup_counts_arms_and_prices_only_accepted_on_calls(store: Any) -> None:
    _catalog(store)
    for _ in range(2):
        _row(store, stamp={"accepted": True})
    for reason in ("parse", "parse", "validator"):
        call = uuid4().hex
        _row(store, stamp={"accepted": False, "call": call})
        _row(store, arm="paid", cost=0.25, stamp={"escalated": reason, "call": call})
    for agree in (True, True, False):
        call = uuid4().hex
        _row(
            store,
            mode="shadow",
            stamp={"accepted": True, "shadow": True, "agree": agree, "call": call},
        )
        _row(
            store,
            arm="paid",
            mode="shadow",
            cost=0.9,
            stamp={"shadow": True, "agree": agree, "call": call},
        )
    # Unstamped legacy calls and old accepted rows must not inflate savings.
    with store.pool.connection() as conn:
        conn.execute("INSERT INTO llm_call_log (source) VALUES (%s)", (OP,))
    _row(store, stamp={"accepted": True}, ts=datetime.now(UTC) - timedelta(days=31))
    _row(store, stamp={"accepted": True}, source="unrelated:local")

    result = stats.collect_stats(store, OP)
    assert result.calls == 8
    assert result.on_calls == 5
    assert result.attempts == 8
    assert result.accepted == 5
    assert result.escalated == 3
    assert result.escalation_rate == pytest.approx(3 / 8)
    assert result.reasons == {"parse": 2, "validator": 1}
    assert (result.shadow_agree, result.shadow_pairs) == (2, 3)
    assert (result.shadow_answers, result.shadow_calls) == (3, 3)
    assert result.spent_usd == pytest.approx(0.75)
    assert result.saved_usd == pytest.approx(0.020)
    body = stats.render_stats(store, OP)
    assert "Wilson 95%: 20.8%–93.9%" in body
    assert "shadow answer rate: 3/3 (100.0%)" in body
    assert "37.5%" in body and "parse: 2 (25.0%" in body
    assert "reasoning tokens" in body and "overstates" in body


def test_handler_shows_accepted_local_calls_without_paid_rows(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    _catalog(store)
    _row(store, stamp={"accepted": True})
    monkeypatch.setattr(
        stats, "_configured_paid_model", lambda op: ("stats-paid-model", "claude_p")
    )
    body = LlmHandler(hub=Hub(store=store)).get(id=f"op:{OP}", view="stats").body
    assert "local attempts: 1; accepted: 1" in body
    assert "saved_usd estimate: $0.0100" in body
    assert "no comparable pairs" in body


@pytest.mark.parametrize("tokens", [(1000, 2000), (None, None)])
def test_savings_unavailable_without_prices_or_telemetry(
    store: Any, monkeypatch: pytest.MonkeyPatch, tokens: tuple[int | None, int | None]
) -> None:
    if tokens == (None, None):
        _catalog(store)
    _row(store, stamp={"accepted": True}, tokens=tokens)
    monkeypatch.setattr(
        stats, "_configured_paid_model", lambda op: ("stats-paid-model", "claude_p")
    )
    result = stats.collect_stats(store, OP)
    assert result.saved_usd is None
    assert result.unpriced == 1
    assert "saved_usd estimate: unavailable" in stats.render_stats(store, OP)


def _alert_env(store: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(stats.meter, "active_store", lambda: store)
    monkeypatch.setattr(stats.local_first, "_mode", lambda op: "on")
    posts: list[dict[str, Any]] = []

    def raise_alert(alert_store: Any, **kwargs: Any) -> tuple[int, bool]:
        assert alert_store is store
        posts.append(kwargs)
        return 1, True

    monkeypatch.setattr(stats.alerts, "raise_alert", raise_alert)
    return posts


def _alert_rows(store: Any, *, paid: int = 3, local: int = 5) -> None:
    for index in range(local):
        _row(store, stamp={"call": f"alert-{index}"})
    for index in range(paid):
        _row(
            store,
            arm="paid",
            stamp={"escalated": "parse", "call": f"alert-{index}"},
        )


def test_alert_claim_survives_process_state_and_is_once_per_utc_day(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = _alert_env(store, monkeypatch)
    _alert_rows(store)
    # Yesterday's claim must not suppress today; the log is the durable state.
    _row(
        store,
        arm="paid",
        stamp={
            "alert_day": (datetime.now(UTC) - timedelta(days=1)).date().isoformat(),
            "call": "alert-0",
        },
    )
    assert stats.maybe_post_alert(OP) is True
    assert stats.maybe_post_alert(OP) is False
    assert len(posts) == 1
    assert "3/5 escalations in 24h" in posts[0]["title"]
    assert posts[0]["detail"] == posts[0]["title"]
    assert posts[0]["source"] == "local-first:escalation"
    assert posts[0]["fingerprint"] == OP
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM llm_call_log WHERE source = %s "
            "AND features->'local_first'->>'alert_day' = %s",
            (OP, datetime.now(UTC).date().isoformat()),
        ).fetchone()
    assert row == (1,)


def test_alert_never_claims_a_row_from_before_the_utc_day(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim on yesterday's row would age out of the 24h window mid-day and
    let a second alert through; the claim only lands on today's rows."""
    posts = _alert_env(store, monkeypatch)
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for index in range(5):
        _row(store, stamp={"call": f"y-{index}"})
    for index in range(3):
        _row(
            store,
            arm="paid",
            stamp={"escalated": "parse", "call": f"y-{index}"},
            ts=start - timedelta(minutes=1),
        )
    assert stats.maybe_post_alert(OP) is False
    assert posts == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM llm_call_log WHERE source = %s "
            "AND features->'local_first'->>'alert_day' IS NOT NULL",
            (OP,),
        ).fetchone()
    assert row == (0,)


def test_alert_threshold_is_strict_and_ignores_shadow_and_old_rows(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = _alert_env(store, monkeypatch)
    _alert_rows(store, paid=2, local=5)
    _row(store, arm="paid", mode="shadow", stamp={"escalated": "parse"})
    _row(
        store,
        arm="paid",
        stamp={"escalated": "parse"},
        ts=datetime.now(UTC) - timedelta(hours=25),
    )
    assert stats.maybe_post_alert(OP) is False
    assert posts == []
    _row(store, arm="paid", stamp={"escalated": "validator"})
    assert stats.maybe_post_alert(OP) is True


def test_failed_alert_write_consumes_daily_claim(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _alert_env(store, monkeypatch)
    _alert_rows(store)
    calls = 0

    def fail(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise OSError("alert unavailable")

    monkeypatch.setattr(stats.alerts, "raise_alert", fail)
    assert stats.maybe_post_alert(OP) is False
    assert stats.maybe_post_alert(OP) is False
    assert calls == 1


def test_alert_skips_disabled_or_unavailable_accounting(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = _alert_env(store, monkeypatch)
    _alert_rows(store)
    monkeypatch.setattr(stats.local_first, "_mode", lambda op: "shadow")
    assert stats.maybe_post_alert(OP) is False
    monkeypatch.setattr(stats.local_first, "_mode", lambda op: "on")
    monkeypatch.setattr(stats.meter, "active_store", lambda: None)
    assert stats.maybe_post_alert(OP) is False
    assert posts == []


def test_fully_escalated_window_without_local_rows_alerts(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts = _alert_env(store, monkeypatch)
    _alert_rows(store, paid=3, local=0)
    result = stats.collect_stats(store, OP)
    assert result.attempts == 0
    assert result.calls == result.on_calls == result.escalated == 3
    assert result.escalation_rate == pytest.approx(1.0)
    assert "escalated: 3/3 (100.0%)" in stats.render_stats(store, OP)
    assert stats.maybe_post_alert(OP) is True
    assert "3/3 escalations in 24h" in posts[0]["title"]


def test_mixed_skipped_and_local_calls_use_helper_denominator(store: Any) -> None:
    _alert_rows(store, paid=3, local=1)
    result = stats.collect_stats(store, OP)
    assert result.attempts == 1
    assert result.calls == result.on_calls == 3
    assert result.escalated == 3
    assert result.escalation_rate == pytest.approx(1.0)
    assert "escalated: 3/3 (100.0%)" in stats.render_stats(store, OP)


def test_call_counts_fall_back_to_row_id_without_tokens(store: Any) -> None:
    _row(store, stamp={"call": None})
    _row(store, arm="paid", stamp={"call": None, "escalated": "parse"})
    result = stats.collect_stats(store, OP)
    assert result.calls == result.on_calls == 2
    assert result.escalation_rate == pytest.approx(0.5)


def test_shadow_answer_rate_includes_failed_and_skipped_local_arms(store: Any) -> None:
    for call in ("answered", "failed", "skipped"):
        _row(
            store,
            arm="paid",
            mode="shadow",
            stamp={"call": call, "shadow": True, "agree": None},
        )
    _row(
        store,
        mode="shadow",
        stamp={"call": "answered", "shadow": True, "accepted": True, "agree": True},
    )
    _row(
        store,
        mode="shadow",
        stamp={"call": "failed", "shadow": True, "accepted": False, "agree": None},
    )
    result = stats.collect_stats(store, OP)
    assert result.calls == result.shadow_calls == 3
    assert result.on_calls == 0
    assert result.attempts == 2
    assert result.shadow_answers == 1
    assert (result.shadow_agree, result.shadow_pairs) == (1, 1)
    body = stats.render_stats(store, OP)
    assert "shadow answer rate: 1/3 (33.3%)" in body
    assert "shadow agreement: 1/1 (100.0%)" in body


@pytest.mark.parametrize("has_paid_row", [False, True])
def test_handler_preserves_op_stats_when_local_first_rollup_fails(
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    has_paid_row: bool,
) -> None:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    if has_paid_row:
        _row(store, arm="paid", cost=0.25)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("rollup failed\nprivate diagnostic")

    monkeypatch.setattr(stats, "render_stats", fail)
    body = LlmHandler(hub=Hub(store=store)).get(id=f"op:{OP}", view="stats").body
    assert f"# llm op stats — {OP}" in body
    assert body.splitlines().count("local-first stats unavailable.") == 1
    assert "private diagnostic" not in body
    if has_paid_row:
        assert "calls: 1" in body
        assert "realized cost: $0.2500" in body
    else:
        assert "no llm_call_log rows" in body
