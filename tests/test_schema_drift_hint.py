"""Schema-drift recovery hint + loud cross-kind fan-out failures.

The gr281493 outage family: a long-lived MCP server keeps serving after
a deploy migrates the DB under it, and every ref-backed verb dies with
an opaque ``[error:Internal] … UndefinedColumn (see server log)`` —
agents filed dozens of duplicate gripes because the envelope named
neither the cause nor the recovery. Two fixes pinned here:

1. ``DispatchMixin._schema_drift_note``: when the exception chain
   carries ``psycopg.errors.UndefinedColumn``/``UndefinedTable``, the
   error's ``next:`` hint compares the migration head captured at
   runtime construction (``PrecisRuntime.boot_migration_head``) against
   the live ``_migrations`` ledger and names the real recovery —
   "restart the server" when the DB is ahead of the process, "run
   precis migrate" when the DB is behind the code.

2. Cross-kind fan-out: a kind whose ``search_hits`` raises is no longer
   silently reported as 0 matches. Partial failure gets a ⚠ footer;
   when EVERY kind errored the whole call raises ``Internal`` (the old
   behaviour returned a clean "0 matches" — a false negative anyone
   using the fan-out as a health check was misled by).
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import UndefinedColumn

from precis.runtime.core import PrecisRuntime
from precis.store import Store


def _db_migration_head(store: Store) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT max(version) FROM public._migrations WHERE plugin = 'precis'"
        ).fetchone()
    assert row is not None and row[0], "test DB has no migration ledger"
    return str(row[0])


def _raise_undefined_column(**_kw: Any) -> Any:
    raise UndefinedColumn("column refs.retired_at does not exist")


def test_stale_server_gets_restart_hint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB head ahead of the boot-time head ⇒ the envelope says restart."""
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None
    monkeypatch.setattr(handler, "get", _raise_undefined_column)
    runtime_with_store.boot_migration_head = "0001_initial"

    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": "1"})
    assert "[error:Internal]" in out
    assert "UndefinedColumn" in out
    assert "Restart the precis MCP server" in out
    # Names both heads so the operator can see the drift, not just
    # trust the verdict.
    assert "0001_initial" in out


def test_db_behind_build_gets_migrate_hint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot head ahead of the DB ⇒ the envelope says run precis migrate."""
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None
    monkeypatch.setattr(handler, "get", _raise_undefined_column)
    runtime_with_store.boot_migration_head = "9999_future_migration"

    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": "1"})
    assert "[error:Internal]" in out
    assert "precis migrate" in out


def test_no_drift_keeps_generic_envelope(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heads equal ⇒ a genuine schema bug; no misleading restart advice."""
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None
    monkeypatch.setattr(handler, "get", _raise_undefined_column)
    runtime_with_store.boot_migration_head = _db_migration_head(store)

    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": "1"})
    assert "[error:Internal]" in out
    assert "UndefinedColumn" in out
    assert "Restart" not in out
    assert "precis migrate" not in out


def test_non_schema_error_untouched(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary crash never triggers the drift probe's advice."""
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None

    def _boom(**_kw: Any) -> Any:
        raise ValueError("plain bug")

    monkeypatch.setattr(handler, "get", _boom)
    runtime_with_store.boot_migration_head = "0001_initial"

    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": "1"})
    assert "[error:Internal]" in out
    assert "Restart" not in out
    assert "precis migrate" not in out


# ---------------------------------------------------------------------------
# Cross-kind fan-out: errors must not masquerade as zero matches
# ---------------------------------------------------------------------------


def test_fanout_all_kinds_errored_raises_not_zero_matches(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """During a full read-path outage the fan-out used to report "0
    matches" (gr281493's false-quiet). Now: a loud Internal that also
    carries the drift hint, since the psycopg error is chained on."""
    for k in ("paper", "memory"):
        handler = runtime_with_store.hub.handler_for(k)
        assert handler is not None
        monkeypatch.setattr(handler, "search_hits", _raise_undefined_column)
    runtime_with_store.boot_migration_head = "0001_initial"

    out = runtime_with_store.dispatch(
        "search", {"kind": "paper,memory", "q": "anything"}
    )
    assert "[error:Internal]" in out
    assert "every kind tried" in out
    assert "no matches" not in out
    # The raise chains ``from`` the per-kind psycopg error, so the
    # stale-server recovery hint rides on the fan-out failure too.
    assert "Restart the precis MCP server" in out


def test_fanout_partial_error_gets_warning_footer(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken kind must be named, not folded into a 0 count."""
    handler = runtime_with_store.hub.handler_for("paper")
    assert handler is not None
    monkeypatch.setattr(handler, "search_hits", _raise_undefined_column)
    # memory side succeeds (may legitimately have zero hits — a
    # successful empty stream still counts as "ran").
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "fanout partial marker qzx"}
    )

    out = runtime_with_store.dispatch(
        "search", {"kind": "paper,memory", "q": "fanout partial marker qzx"}
    )
    assert "[error:" not in out
    assert "errored, omitted from this merge" in out
    assert "paper (UndefinedColumn)" in out
