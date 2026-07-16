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


def test_gate_tier_cheap_always_passes() -> None:
    store = cast("Store", FakeStore(llm=999.0, fetch=0.0))  # wildly over cap
    assert breaker_mod.gate_tier(Tier.CLOUD_SMALL, store=store) is None
    assert breaker_mod.gate_tier(Tier.LOCAL_SMALL, store=store) is None


def test_gate_tier_expensive_under_cap_passes() -> None:
    store = cast("Store", FakeStore(llm=1.0, fetch=0.0))
    assert breaker_mod.gate_tier(Tier.CLOUD_SUPER, store=store) is None


def test_gate_tier_expensive_over_cap_refused() -> None:
    store = cast("Store", FakeStore(llm=25.0, fetch=0.0))  # over both caps
    reason = breaker_mod.gate_tier(Tier.CLOUD_SUPER, store=store)
    assert reason is not None
    assert "budget" in reason
    assert "/budget" in reason


def test_gate_tier_dark_without_store() -> None:
    # No bound store and none passed → never trips.
    assert breaker_mod.gate_tier(Tier.CLOUD_SUPER) is None


def test_gate_paid_cheap_vs_expensive() -> None:
    store = cast("Store", FakeStore(llm=25.0, fetch=0.0))  # tripped
    # A cheap fetch estimate is never gated, even while tripped.
    assert breaker_mod.gate_paid(0.001, store=store) is None
    # An expensive fetch estimate is refused while tripped.
    assert breaker_mod.gate_paid(0.50, store=store) is not None


def test_gate_paid_expensive_under_cap_passes() -> None:
    store = cast("Store", FakeStore(llm=1.0, fetch=0.0))
    assert breaker_mod.gate_paid(0.50, store=store) is None


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
