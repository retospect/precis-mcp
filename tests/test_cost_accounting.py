"""measure_tick_tokens folds the llm_call_log aggregate row (gr690184)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, cast

from precis.budget.cost_accounting import TickTokens, measure_tick_tokens


class _Conn:
    def __init__(self, row: Any) -> None:
        self.row = row

    def execute(self, sql: str, params: Any) -> Any:
        self.params = params
        return self

    def fetchone(self) -> Any:
        return self.row


class _Pool:
    def __init__(self, row: Any) -> None:
        self.conn = _Conn(row)

    @contextmanager
    def connection(self):
        yield self.conn


class _Store:
    def __init__(self, row: Any) -> None:
        self.pool = _Pool(row)


def test_measure_tick_tokens_sums_row() -> None:
    store = _Store((3, 1000, 20, 0.5))
    got = measure_tick_tokens(cast(Any, store), days=2)
    assert got == TickTokens(rows=3, input_tokens=1000, output_tokens=20, cost_usd=0.5)
    assert got.total_tokens == 1020
    assert store.pool.conn.params[0] == 2


def test_measure_tick_tokens_empty() -> None:
    got = measure_tick_tokens(cast(Any, _Store(None)))
    assert got.rows == 0 and got.total_tokens == 0
