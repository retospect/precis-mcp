"""Tests for ``precis limits`` — the outbound-policy override write door
(S4 of docs/backlog/outbound-policy.md): ``set`` writes a row's override
columns, ``--dry-run`` resolves before/after without touching the DB, and
a real run audits who changed what to ``worker_logs``.
"""

from __future__ import annotations

import argparse
from typing import Any
from uuid import uuid4

import pytest

from precis.cli.limits import _cmd_set
from precis.cli.main import _build_parser
from precis.store import Store


def _ns(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "provider": "test-lim",
        "cls": None,
        "max_wait": None,
        "connect": None,
        "read": None,
        "total": None,
        "on_refuse": None,
        "concurrency": None,
        "clear": [],
        "create": False,
        "dry_run": False,
        "actor": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed(store: Store, provider: str, **cols: object) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.execute(
            "INSERT INTO external_rate_limits "
            "(provider, capacity, refill_per_sec, tokens, daily_cap) "
            "VALUES (%s, 4, 1.0, 4, NULL)",
            (provider,),
        )
        if cols:
            sets = ", ".join(f'"{c}" = %s' for c in cols)
            conn.execute(
                f"UPDATE external_rate_limits SET {sets} WHERE provider = %s",
                (*cols.values(), provider),
            )
        conn.commit()


def _col(store: Store, provider: str, col: str) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            f'SELECT "{col}" FROM external_rate_limits WHERE provider = %s',
            (provider,),
        ).fetchone()
    return row[0] if row else None


def test_set_subcommand_registered() -> None:
    args = _build_parser().parse_args(
        [
            "limits",
            "set",
            "crossref",
            "--max-wait",
            "10",
            "--on-refuse",
            "defer",
            "--concurrency",
            "8",
            "--dry-run",
        ]
    )
    assert args.cmd == "limits"
    assert args.limits_cmd == "set"
    assert args.provider == "crossref"
    assert args.max_wait == 10.0
    assert args.on_refuse == "defer"
    assert args.concurrency == "8"
    assert args.dry_run is True


def test_set_writes_overrides_and_audits(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    prov = f"test-lim-{uuid4().hex[:8]}"
    _seed(store, prov)
    _cmd_set(store, _ns(provider=prov, max_wait=5.0, on_refuse="defer", actor="tester"))
    assert float(_col(store, prov, "max_wait_s")) == 5.0
    assert _col(store, prov, "on_refuse") == "defer"
    out = capsys.readouterr().out
    assert f"external_rate_limits[{prov}]: max_wait_s - -> 5.0" in out
    assert "on_refuse - -> defer" in out
    assert "effective:" in out and "max_wait_s=5 (db)" in out
    with store.pool.connection() as conn:
        log = conn.execute(
            "SELECT payload FROM worker_logs WHERE logger = 'precis.cli.limits' "
            "AND payload->>'provider' = %s",
            (prov,),
        ).fetchone()
    assert log is not None
    payload = log[0]
    assert payload["actor"] == "tester"
    assert payload["provider"] == prov
    assert payload["changes"]["max_wait_s"] == {"old": None, "new": 5.0}
    assert payload["changes"]["on_refuse"] == {"old": None, "new": "defer"}


def test_dry_run_writes_nothing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    prov = f"test-lim-{uuid4().hex[:8]}"
    _seed(store, prov)
    _cmd_set(store, _ns(provider=prov, max_wait=5.0, dry_run=True))
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "before:" in out and "after:" in out
    assert "max_wait_s=5 (db)" in out  # the 'after' resolution
    assert _col(store, prov, "max_wait_s") is None  # nothing written
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM worker_logs WHERE logger = 'precis.cli.limits' "
                "AND payload->>'provider' = %s",
                (prov,),
            ).fetchone()
            is None
        )


def test_clear_nulls_a_column(store: Store) -> None:
    prov = f"test-lim-{uuid4().hex[:8]}"
    _seed(store, prov, max_wait_s=9)
    _cmd_set(store, _ns(provider=prov, clear=["max-wait"]))
    assert _col(store, prov, "max_wait_s") is None


def test_class_override(store: Store) -> None:
    prov = f"test-lim-{uuid4().hex[:8]}"
    _seed(store, prov)
    _cmd_set(store, _ns(provider=prov, cls="llm-api"))
    assert _col(store, prov, "class") == "llm-api"


def test_concurrency_name_writes_column(store: Store) -> None:
    prov = f"test-lim-{uuid4().hex[:8]}"
    _seed(store, prov)
    _cmd_set(store, _ns(provider=prov, concurrency="my-resource"))
    assert _col(store, prov, "concurrency") == "my-resource"


def test_concurrency_number_edits_slot_capacity(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--concurrency N`` edits the resolved resource's resource_slots
    row — the hold row's own count (spec's `--concurrency 8`)."""
    prov = f"test-lim-{uuid4().hex[:8]}"
    resource = f"mres-{uuid4().hex[:8]}"
    _seed(store, prov, concurrency=resource)
    try:
        _cmd_set(store, _ns(provider=prov, concurrency="8"))
        with store.pool.connection() as conn:
            cap = conn.execute(
                "SELECT capacity FROM resource_slots "
                "WHERE host = 'api' AND resource = %s",
                (resource,),
            ).fetchone()
        assert cap is not None and int(cap[0]) == 8
        assert f"resource_slots[api/{resource}] capacity - -> 8" in (
            capsys.readouterr().out
        )
    finally:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM resource_slots WHERE resource = %s", (resource,))
            conn.commit()


def test_missing_named_row_refuses(store: Store) -> None:
    prov = f"test-missing-{uuid4().hex[:8]}"
    with pytest.raises(SystemExit) as exc:
        _cmd_set(store, _ns(provider=prov, max_wait=5.0))
    assert exc.value.code == 2


def test_host_row_auto_seeds(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    host = f"host:test-lim-{uuid4().hex[:8]}.example"
    _cmd_set(store, _ns(provider=host, max_wait=5.0))
    assert float(_col(store, host, "max_wait_s")) == 5.0
    assert "auto-seeded" in capsys.readouterr().out


def test_nothing_to_change_refuses(store: Store) -> None:
    with pytest.raises(SystemExit) as exc:
        _cmd_set(store, _ns(provider="crossref"))
    assert exc.value.code == 2
