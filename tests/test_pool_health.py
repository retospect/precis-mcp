"""gr663490: a pool that cannot connect is not a saturated pool."""

from __future__ import annotations

from typing import Any

import psycopg_pool
import pytest

from precis.errors import Upstream
from precis.runtime import dispatch as d
from precis.store.pool_health import diagnose_pool


class _Pool:
    def __init__(self, **stats: int) -> None:
        base = dict(
            pool_min=2,
            pool_max=4,
            pool_size=2,
            pool_available=1,
            connections_errors=0,
        )
        self.stats: dict[str, Any] = {**base, **stats}

    def get_stats(self) -> dict[str, Any]:
        return dict(self.stats)


def test_below_min_size_is_unreachable() -> None:
    assert diagnose_pool(_Pool(pool_size=0, pool_available=0)) == "unreachable"


def test_errors_growing_is_unreachable() -> None:
    p = _Pool()
    assert diagnose_pool(p) == "unknown"
    p.stats["connections_errors"] = 3
    assert diagnose_pool(p) == "unreachable"


def test_full_pool_is_saturated() -> None:
    assert diagnose_pool(_Pool(pool_size=4, pool_available=0)) == "saturated"


def test_full_pool_with_error_history_is_saturated() -> None:
    p = _Pool(pool_size=4, pool_available=0, connections_errors=5)
    assert diagnose_pool(p) == "saturated"


def test_stats_failure_is_unknown() -> None:
    class Bad:
        def get_stats(self) -> dict[str, Any]:
            raise RuntimeError

    assert diagnose_pool(Bad()) == "unknown"


def test_dispatch_fails_fast_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def inner(self: Any, verb: str, args: dict[str, Any]) -> Any:
        calls.append(1)
        raise psycopg_pool.PoolTimeout("x")

    class _Store:
        pool = _Pool(pool_size=0, pool_available=0)

    class _Rt(d.DispatchMixin):
        @property
        def store(self) -> Any:
            return _Store()

    rt = _Rt.__new__(_Rt)
    monkeypatch.setattr(d.DispatchMixin, "_dispatch_inner", inner)
    with pytest.raises(Upstream) as ei:
        rt._dispatch_with_retry("get", {"kind": "gripe"})
    assert len(calls) == 1
    assert "cannot connect to the database" in ei.value.cause
    assert "Tailscale" in (ei.value.next or "")
