"""Unit tests for the budget guardrails package (bands / pricing / meter /
breaker) and the router's OSS real-cost capture.

Store-free by design: the meter/breaker take a tiny fake store, so these run
on the torch-free host without a database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

import pytest

from precis.budget import bands, meter

if TYPE_CHECKING:
    # Type-only: the fake stores below duck-type Store for the meter/breaker/
    # settings signatures. Imported under TYPE_CHECKING (and cast via the
    # string "Store") so the runtime stays store-free / torch-free on the host.
    from precis.store import Store
from precis.budget import breaker as breaker_mod
from precis.budget.bands import Cost, Pace
from precis.budget.pricing import PRICE_TABLE, cost_from_tokens
from precis.utils.llm.router import Tier, result_from_openai

# ── fake store ─────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, row: tuple) -> None:
        self._row = row

    def fetchone(self) -> tuple:
        return self._row


class _Conn:
    def __init__(self, llm: float, fetch: float) -> None:
        self._llm = llm
        self._fetch = fetch

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: object = None) -> _Cursor:
        return (
            _Cursor((self._llm,)) if "llm_call_log" in sql else _Cursor((self._fetch,))
        )


class _Pool:
    def __init__(self, llm: float, fetch: float) -> None:
        self._conn = _Conn(llm, fetch)

    def connection(self) -> _Conn:
        return self._conn


class FakeStore:
    def __init__(self, *, llm: float = 0.0, fetch: float = 0.0) -> None:
        self.pool = _Pool(llm, fetch)


@pytest.fixture(autouse=True)
def _reset_budget_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # No env caps → defaults ($5 hourly / $20 daily); clear the meter cache
    # and the breaker's transition memory between tests.
    for var in (
        "PRECIS_BUDGET_HOURLY_USD",
        "PRECIS_BUDGET_DAILY_USD",
        "PRECIS_BUDGET_CHEAP_MAX_USD",
        "PRECIS_QUOTA_CEILING_PCT",
    ):
        monkeypatch.delenv(var, raising=False)
    meter.bind_store(None)
    breaker_mod._reset_alert_state()


# ── bands ──────────────────────────────────────────────────────────────


def test_cost_from_usd_thresholds() -> None:
    assert bands.cost_from_usd(None) is Cost.FREE
    assert bands.cost_from_usd(0.0) is Cost.FREE
    assert bands.cost_from_usd(-1.0) is Cost.FREE
    assert bands.cost_from_usd(0.001) is Cost.CHEAP
    assert bands.cost_from_usd(0.02) is Cost.CHEAP  # at threshold → cheap
    assert bands.cost_from_usd(0.5) is Cost.EXPENSIVE


def test_cheap_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_BUDGET_CHEAP_MAX_USD", "0.10")
    assert bands.cost_from_usd(0.05) is Cost.CHEAP
    assert bands.cost_from_usd(0.20) is Cost.EXPENSIVE


def test_band_table_total_and_labels() -> None:
    for tier in Tier:
        band = bands.band_for_tier(tier)
        assert band.cost in Cost
        assert band.pace in Pace
    assert bands.band_for_tier(Tier.LOCAL_SMALL) == bands.Band(Cost.FREE, Pace.FAST)
    assert bands.is_expensive(Tier.CLOUD_SUPER) is True
    assert bands.is_expensive(Tier.CLOUD_SMALL) is False
    assert bands.band_for_tier(Tier.CLOUD_SUPER).label() == "expensive \u00b7 slow"


# ── pricing ──────────────────────────────────────────────────────────────


def test_cost_from_tokens_known_model() -> None:
    price_in, price_out = PRICE_TABLE["deepseek-ai/DeepSeek-V3"]
    cost = cost_from_tokens(
        "deepseek-ai/DeepSeek-V3", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == pytest.approx(price_in + price_out)


def test_cost_from_tokens_unknown_and_empty() -> None:
    assert (
        cost_from_tokens("summarizer", prompt_tokens=5000, completion_tokens=99) is None
    )
    assert (
        cost_from_tokens(
            "deepseek-ai/DeepSeek-V3", prompt_tokens=None, completion_tokens=None
        )
        is None
    )


# ── result_from_openai capture ───────────────────────────────────────────


class _FakeOpenAIResult:
    def __init__(self, text: str, prompt: int | None, completion: int | None) -> None:
        self.text = text
        self.prompt_tokens = prompt
        self.completion_tokens = completion


def test_result_from_openai_prices_oss_tokens() -> None:
    res = _FakeOpenAIResult("hi", prompt=1_000_000, completion=0)
    out = result_from_openai(res, model="deepseek-ai/DeepSeek-V3", tier=Tier.CLOUD_MID)
    assert out.cost_usd == pytest.approx(PRICE_TABLE["deepseek-ai/DeepSeek-V3"][0])


def test_result_from_openai_local_is_free() -> None:
    res = _FakeOpenAIResult("hi", prompt=500, completion=20)
    out = result_from_openai(res, model="summarizer", tier=Tier.LOCAL_SMALL)
    assert out.cost_usd is None


def test_result_from_openai_bare_text_fake() -> None:
    class _BareText:
        text = "hello"

    out = result_from_openai(_BareText(), model="summarizer", tier=Tier.LOCAL_SMALL)
    assert out.cost_usd is None
    assert out.text == "hello"
    assert out.data is None


def test_result_from_openai_prefers_provider_cost() -> None:
    # OpenRouter returns a real dollar cost; it wins over the token-table
    # estimate (gripe 161849 #2).
    class _WithCost:
        text = "hi"
        prompt_tokens = 1_000_000
        completion_tokens = 1_000_000
        cost_usd = 0.007

    out = result_from_openai(
        _WithCost(), model="deepseek-ai/DeepSeek-V3", tier=Tier.CLOUD_MID
    )
    assert out.cost_usd == pytest.approx(0.007)


def test_result_from_openai_parses_trailing_json_into_data() -> None:
    # OSS judges (chase verify, good_search triage) read LlmResult.data; parse
    # the trailing JSON block so they reach claude parity (gripe 159758).
    class _JudgeText:
        text = 'Reasoning here.\n{"verdict": "yes", "confidence": 0.9}'

    out = result_from_openai(_JudgeText(), model="summarizer", tier=Tier.LOCAL_BIG)
    assert out.data == {"verdict": "yes", "confidence": 0.9}


# ── meter ──────────────────────────────────────────────────────────────


def test_spent_usd_sums_both_ledgers() -> None:
    store = cast("Store", FakeStore(llm=3.0, fetch=1.5))
    assert meter.spent_usd(store, since_seconds=3600) == pytest.approx(4.5)


def test_current_status_none_without_store() -> None:
    assert meter.current_status(None) is None


def test_current_status_under_cap_not_tripped() -> None:
    store = cast("Store", FakeStore(llm=1.0, fetch=0.0))
    status = meter.current_status(store, use_cache=False)
    assert status is not None
    assert status.tripped is False
    assert status.tripped_window is None


def test_current_status_over_hourly_cap() -> None:
    store = cast("Store", FakeStore(llm=6.0, fetch=0.0))  # > $5 hourly default
    status = meter.current_status(store, use_cache=False)
    assert status is not None
    assert status.tripped is True
    assert status.tripped_window == "hourly"


# ── breaker ──────────────────────────────────────────────────────────────


def test_is_paid_covers_every_nonfree_tier() -> None:
    # Only the two local (free-band) tiers are unpaid; every cloud tier is paid.
    assert bands.is_paid(Tier.LOCAL_SMALL) is False
    assert bands.is_paid(Tier.LOCAL_BIG) is False
    assert bands.is_paid(Tier.CLOUD_SMALL) is True
    assert bands.is_paid(Tier.CLOUD_MID) is True
    assert bands.is_paid(Tier.CLOUD_SUPER) is True


def test_gate_tier_free_local_always_passes() -> None:
    store = cast("Store", FakeStore(llm=999.0, fetch=0.0))  # wildly over cap
    # Free local tiers keep flowing even while tripped.
    assert breaker_mod.gate_tier(Tier.LOCAL_SMALL, store=store) is None
    assert breaker_mod.gate_tier(Tier.LOCAL_BIG, store=store) is None


def test_gate_tier_paid_cheap_tiers_gated_over_cap() -> None:
    # The decision: if it costs money, the cap limits it. A tripped cap refuses
    # the cheap CLOUD_MID (sonnet) / CLOUD_SMALL (haiku) rungs too, not just opus.
    store = cast("Store", FakeStore(llm=25.0, fetch=0.0))  # over both caps
    for tier in (Tier.CLOUD_SMALL, Tier.CLOUD_MID, Tier.CLOUD_SUPER):
        reason = breaker_mod.gate_tier(tier, store=store)
        assert reason is not None, tier
        assert "budget" in reason and "/budget" in reason


def test_gate_tier_paid_under_cap_passes() -> None:
    store = cast("Store", FakeStore(llm=1.0, fetch=0.0))
    assert breaker_mod.gate_tier(Tier.CLOUD_SUPER, store=store) is None
    assert breaker_mod.gate_tier(Tier.CLOUD_SMALL, store=store) is None


def test_gate_tier_dark_without_store() -> None:
    # No bound store and none passed → never trips.
    assert breaker_mod.gate_tier(Tier.CLOUD_SUPER) is None


def test_gate_paid_free_vs_paid_over_cap() -> None:
    store = cast("Store", FakeStore(llm=25.0, fetch=0.0))  # tripped
    # A free fetch (0 / None estimate) always runs.
    assert breaker_mod.gate_paid(0.0, store=store) is None
    assert breaker_mod.gate_paid(None, store=store) is None
    # Any non-zero fetch cost — cheap or expensive — is refused while tripped.
    assert breaker_mod.gate_paid(0.001, store=store) is not None
    assert breaker_mod.gate_paid(0.50, store=store) is not None


def test_gate_paid_under_cap_passes() -> None:
    store = cast("Store", FakeStore(llm=1.0, fetch=0.0))
    assert breaker_mod.gate_paid(0.50, store=store) is None
    assert breaker_mod.gate_paid(0.001, store=store) is None


# ── enforcement seams (the two spend chokepoints) ────────────────────────
# The gate helpers above test the *decision*; these test that the two call
# sites actually short-circuit on a trip (the early-returns gripe 161849 #3
# flagged as untested — a regression that dropped either would pass otherwise).


def test_dispatch_returns_error_llmresult_when_breaker_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``router.dispatch`` folds a tripped cap into an error ``LlmResult`` and
    never runs the provider."""
    from precis.utils.llm import router

    monkeypatch.setattr(
        breaker_mod,
        "gate_tier",
        lambda tier, **_k: (
            "budget: daily cap $20.00 reached — paid model call paused. …/budget"
        ),
    )

    class _BoomProvider:
        def run(self, req: object, *, model: str) -> object:
            raise AssertionError("provider ran despite a tripped cap")

    monkeypatch.setattr(router, "provider_for", lambda transport: _BoomProvider())

    out = router.dispatch(router.LlmRequest(tier=Tier.CLOUD_SUPER, prompt="hi"))
    assert out.error is not None
    assert "budget" in out.error
    assert out.text == ""


def test_fetch_guarded_raises_upstream_and_skips_fetch_on_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CacheBackedHandler._fetch_guarded`` raises ``Upstream`` and does NOT
    call ``_fetch`` once the cap trips."""
    pytest.importorskip("fastapi")  # handler import chain
    from precis.budget import breaker as breaker_pkg
    from precis.errors import Upstream
    from precis.handlers._cache_base import CacheBackedHandler, FetchResult

    monkeypatch.setattr(
        breaker_pkg, "gate_paid", lambda cost, *, store=None: "budget: cap reached"
    )

    calls: list[str] = []

    class _Handler(CacheBackedHandler):
        cost_per_call_usd = 0.50

        def __init__(self) -> None:  # bypass Hub wiring for the unit
            self.store = None

        def _canonical_key(self, query: str) -> str:
            return query

        def _fetch(self, key: str) -> FetchResult:
            calls.append(key)
            raise AssertionError("_fetch ran despite a tripped cap")

    with pytest.raises(Upstream):
        _Handler()._fetch_guarded("some-key")
    assert calls == []


# ── settings (app_settings key/value overrides) ──────────────────────────


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows


class _KVConn:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def __enter__(self) -> _KVConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Rows:
        s = sql.upper()
        if s.startswith("SELECT VALUE"):
            val = self._data.get(params[0])
            return _Rows([(val,)] if val is not None else [])
        if "INSERT INTO APP_SETTINGS" in s:
            self._data[params[0]] = params[1]
            return _Rows([])
        if s.startswith("DELETE"):
            self._data.pop(params[0], None)
            return _Rows([])
        return _Rows([])


class _KVPool:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def connection(self) -> _KVConn:
        return _KVConn(self._data)


class KVStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.pool = _KVPool(self.data)


def test_settings_roundtrip() -> None:
    from precis.budget import settings

    store = cast("Store", KVStore())
    assert settings.get_float(store, settings.HOURLY_KEY) is None
    settings.set_float(store, settings.HOURLY_KEY, 7.5)
    assert settings.get_float(store, settings.HOURLY_KEY) == 7.5
    settings.clear_setting(store, settings.HOURLY_KEY)
    assert settings.get_float(store, settings.HOURLY_KEY) is None


def test_settings_rejects_nonpositive() -> None:
    from precis.budget import settings

    store = cast("Store", KVStore())
    with pytest.raises(ValueError):
        settings.set_float(store, settings.DAILY_KEY, 0)


def test_settings_invalid_stored_value_is_none() -> None:
    from precis.budget import settings

    kv = KVStore()
    kv.data[settings.DAILY_KEY] = "not-a-number"
    assert settings.get_float(cast("Store", kv), settings.DAILY_KEY) is None


def test_settings_none_store() -> None:
    from precis.budget import settings

    assert settings.get_float(None, settings.DAILY_KEY) is None


class _OverrideConn:
    def __init__(self, llm: float, kv: dict[str, str]) -> None:
        self._llm = llm
        self._kv = kv

    def __enter__(self) -> _OverrideConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Rows:
        if "app_settings" in sql:
            val = self._kv.get(params[0])
            return _Rows([(val,)] if val is not None else [])
        return _Rows([(self._llm,)] if "llm_call_log" in sql else [(0.0,)])


class _OverridePool:
    def __init__(self, llm: float, kv: dict[str, str]) -> None:
        self._llm = llm
        self._kv = kv

    def connection(self) -> _OverrideConn:
        return _OverrideConn(self._llm, self._kv)


def test_meter_db_override_beats_env() -> None:
    # A DB override caps hourly at $1; llm spend $2 → tripped despite $5 default.
    from precis.budget import settings

    class _Store:
        pool = _OverridePool(2.0, {settings.HOURLY_KEY: "1.0"})

    status = meter.current_status(cast("Store", _Store()), use_cache=False)
    assert status is not None
    assert status.hourly_cap == 1.0
    assert status.tripped is True


# ── status-page tote shaping ─────────────────────────────────────────────


class _ToteConn:
    def __enter__(self) -> _ToteConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: tuple = ()) -> _Rows:
        if "llm_call_log" in sql and "LIMIT 12" in sql:
            return _Rows([("claude-opus-4-8", 3, 1.5), ("summarizer", 10, 0.0)])
        if "llm_call_log" in sql:
            return _Rows([("dream", 3, 1.5), ("chase:verify", 2, 0.3)])
        return _Rows([("perplexity-research", 1, 0.5)])


class _TotePool:
    def connection(self) -> _ToteConn:
        return _ToteConn()


class _ToteStore:
    pool = _TotePool()


def test_budget_tote_shapes_windows_and_breakdowns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from precis.budget.meter import BudgetStatus
    from precis_web.routes import status as status_mod

    monkeypatch.setattr(
        meter,
        "current_status",
        lambda store, use_cache=False: BudgetStatus(
            hourly_spent=2.0, daily_spent=17.0, hourly_cap=5.0, daily_cap=20.0
        ),
    )
    tote = status_mod._budget_tote(_ToteStore())
    assert [w["label"] for w in tote["windows"]] == ["Hourly", "24h"]
    assert tote["windows"][0]["state"] == "green"  # 2/5 = 40%
    assert tote["windows"][1]["state"] == "amber"  # 17/20 = 85%
    assert tote["tripped"] is False
    # by_source merges llm sources + paid providers, sorted by cost desc.
    labels = [s["label"] for s in tote["by_source"]]
    assert labels[0] == "dream"  # $1.50, the top spender
    assert "perplexity-research" in labels  # paid fetch folded in
    assert tote["by_model"][0]["label"] == "claude-opus-4-8"


def test_budget_tote_empty_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from precis_web.routes import status as status_mod

    monkeypatch.setattr(meter, "current_status", lambda store, use_cache=False: None)
    assert status_mod._budget_tote(_ToteStore()) == {}


# ── transport-split gating: meter excludes notional OAuth $ ──────────────

from datetime import UTC, datetime

from precis.budget import quota as quota_mod
from precis.budget import settings as budget_settings
from precis.budget.meter import OAUTH_TRANSPORTS


def test_oauth_transports_are_the_claude_lane() -> None:
    assert OAUTH_TRANSPORTS == ("claude_agent", "claude_p")


def test_spent_usd_excludes_oauth_transports_in_sql() -> None:
    """The dollar meter must exclude the notional claude rows so the cap
    reflects real money (query carries the transport exclusion + params)."""
    captured: dict[str, object] = {}

    class _CapConn:
        def __enter__(self) -> _CapConn:
            return self

        def __exit__(self, *a: object) -> Literal[False]:
            return False

        def execute(self, sql: str, params: object = None) -> _Cursor:
            if "llm_call_log" in sql:
                captured["sql"] = sql
                captured["params"] = params
            return _Cursor((0.0,))

    class _CapPool:
        def connection(self) -> _CapConn:
            return _CapConn()

    class _CapStore:
        pool = _CapPool()

    meter.spent_usd(cast("Store", _CapStore()), since_seconds=3600)
    assert "transport" in str(captured["sql"])
    assert cast("tuple", captured["params"])[1] == list(OAUTH_TRANSPORTS)


# ── claude-OAuth quota gate ──────────────────────────────────────────────


class _SqlConn:
    def __init__(self, llm: float, fetch: float, settings: dict[str, str]) -> None:
        self._llm, self._fetch, self._settings = llm, fetch, settings

    def __enter__(self) -> _SqlConn:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def execute(self, sql: str, params: object = None) -> _Cursor:
        if "llm_call_log" in sql:
            return _Cursor((self._llm,))
        if "cache_state" in sql:
            return _Cursor((self._fetch,))
        if "app_settings" in sql:
            key = cast("tuple", params)[0] if params else None
            val = self._settings.get(cast("str", key))
            return _Cursor((val,) if val is not None else None)  # type: ignore[arg-type]
        return _Cursor(None)  # type: ignore[arg-type]


class _SqlPool:
    def __init__(self, llm: float, fetch: float, settings: dict[str, str]) -> None:
        self._conn = _SqlConn(llm, fetch, settings)

    def connection(self) -> _SqlConn:
        return self._conn


class SqlStore:
    """Fake store that answers the dollar sums, ``app_settings`` reads, and the
    claude quota snapshot — enough to drive the whole breaker."""

    _NO_SNAPSHOT = "__none__"

    def __init__(
        self,
        *,
        llm: float = 0.0,
        fetch: float = 0.0,
        settings: dict[str, str] | None = None,
        windows: object = _NO_SNAPSHOT,
    ) -> None:
        self.pool = _SqlPool(llm, fetch, settings or {})
        self._windows = windows

    def read_claude_quota(self, scope: str = "unified") -> object:
        from precis.store._claude_quota_ops import ClaudeQuotaRow

        if self._windows == self._NO_SNAPSHOT:
            return None
        return ClaudeQuotaRow(
            scope="unified",
            ts=datetime(2026, 7, 16, tzinfo=UTC),
            data={"windows": self._windows},
        )


def test_quota_gate_allowed_passes() -> None:
    store = SqlStore(windows={"five_hour": {"status": "allowed"}})
    assert quota_mod.evaluate(cast("Store", store)) is None


def test_quota_gate_rejected_pauses() -> None:
    store = SqlStore(
        windows={
            "five_hour": {
                "status": "rejected",
                "resets_at": "2026-07-17T00:00:00+00:00",
            }
        }
    )
    pause = quota_mod.evaluate(cast("Store", store))
    assert pause is not None
    assert pause.window == "five_hour"
    assert "quota" in pause.reason


def test_quota_gate_over_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_QUOTA_CEILING_PCT", "90")
    store = SqlStore(
        windows={"seven_day": {"status": "allowed", "used_percentage": 95}}
    )
    pause = quota_mod.evaluate(cast("Store", store))
    assert pause is not None
    assert pause.window == "seven_day"


def test_quota_gate_under_ceiling_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_QUOTA_CEILING_PCT", "90")
    store = SqlStore(
        windows={"seven_day": {"status": "allowed", "used_percentage": 50}}
    )
    assert quota_mod.evaluate(cast("Store", store)) is None


def test_quota_gate_dark_without_snapshot() -> None:
    assert quota_mod.evaluate(cast("Store", SqlStore())) is None
    assert quota_mod.evaluate(None) is None


# ── gate_tier is transport-aware ─────────────────────────────────────────


def test_gate_tier_claude_transport_uses_quota_not_dollars() -> None:
    # Dollars wildly over cap, but the claude lane is gated on quota (allowed).
    store = SqlStore(llm=999.0, windows={"five_hour": {"status": "allowed"}})
    assert (
        breaker_mod.gate_tier(
            Tier.CLOUD_SUPER, transport="claude_agent", store=cast("Store", store)
        )
        is None
    )


def test_gate_tier_claude_transport_rejected_pauses() -> None:
    store = SqlStore(windows={"five_hour": {"status": "rejected"}})
    reason = breaker_mod.gate_tier(
        Tier.CLOUD_SUPER, transport="claude_agent", store=cast("Store", store)
    )
    assert reason is not None
    assert "quota" in reason


def test_gate_tier_metered_transport_uses_dollars() -> None:
    store = SqlStore(llm=25.0)  # over both caps
    reason = breaker_mod.gate_tier(
        Tier.CLOUD_SUPER, transport="openai_tools", store=cast("Store", store)
    )
    assert reason is not None
    assert "cap" in reason


def test_gate_tier_resume_override_bypasses_dollars() -> None:
    store = SqlStore(
        llm=25.0,
        settings={budget_settings.RESUME_UNTIL_KEY: "2999-01-01T00:00:00+00:00"},
    )
    assert (
        breaker_mod.gate_tier(
            Tier.CLOUD_SUPER, transport="openai_tools", store=cast("Store", store)
        )
        is None
    )


def test_gate_tier_resume_override_bypasses_quota() -> None:
    store = SqlStore(
        windows={"five_hour": {"status": "rejected"}},
        settings={budget_settings.RESUME_UNTIL_KEY: "2999-01-01T00:00:00+00:00"},
    )
    assert (
        breaker_mod.gate_tier(
            Tier.CLOUD_SUPER, transport="claude_agent", store=cast("Store", store)
        )
        is None
    )


def test_resume_active_expired_is_inactive() -> None:
    store = SqlStore(
        settings={budget_settings.RESUME_UNTIL_KEY: "2000-01-01T00:00:00+00:00"}
    )
    assert budget_settings.resume_active(cast("Store", store)) is False
