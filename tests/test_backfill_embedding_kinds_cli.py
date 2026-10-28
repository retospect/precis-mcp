"""``backfill-embedding-kinds`` refuses a pooler DSN before any fill work."""

from __future__ import annotations

import argparse
from typing import Any

import psycopg
import pytest

from precis.cli import embedding_kind_backfill as bf

_SECRET = "s3cret-pw"
_DSN = f"postgresql://u:{_SECRET}@pooler.invalid:6432/db"


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "apply": True,
        "window": 500,
        "after": 0,
        "resume_pass": "non-paper",
        "sleep": 0,
        "batch_timeout": 300,
        "skip_fill": False,
        "no_btree": False,
        "no_indexes": False,
        "reindex_global": False,
        "until_pass": "paper",
        "database_url": _DSN,
        "maintenance_work_mem": "18GB",
        "max_parallel_maintenance_workers": 4,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, *a: object, **k: object) -> None:
        return None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    log: list[tuple[str, dict[str, Any]]] = []

    def connect(dsn: str, **kw: Any) -> _Conn:
        log.append(("connect", kw))
        if "options" in kw:  # what pgbouncer does with a startup parameter
            raise psycopg.OperationalError(f"bad startup parameter for {_DSN}")
        return _Conn()

    def fill(*a: object, **k: object) -> bf.BackfillResult:
        log.append(("fill", {}))
        return bf.BackfillResult(0, 0, 0, True, "paper", 0)

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(bf, "backfill_embedding_kinds", fill)
    monkeypatch.setattr(bf, "resolve_dsn", lambda url: url)
    # the router-readiness probe runs on the stub connection (no cursor)
    monkeypatch.setattr(bf, "_check_validated", lambda conn: True)
    return log


@pytest.mark.parametrize(
    "extra",
    [{}, {"reindex_global": True, "no_indexes": True}, {"no_indexes": True}],
)
def test_pooler_with_index_phase_refuses_before_fill(
    calls: list[tuple[str, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    extra: dict[str, Any],
) -> None:
    """Even a fill run needs a direct DSN unless the pre-fill btree is off."""
    with pytest.raises(SystemExit) as e:
        bf.run(_args(**extra))
    assert e.value.code == 2
    assert [c[0] for c in calls] == ["connect"] and "options" in calls[0][1]
    err = capsys.readouterr().err
    assert "--database-url" in err and "--no-indexes" in err
    assert _SECRET not in err and "pooler.invalid" not in err


def test_pooler_with_no_indexes_is_allowed(
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    """A fill-only run (--no-btree --no-indexes) still works via the pooler."""
    bf.run(_args(no_indexes=True, no_btree=True))
    assert [c[0] for c in calls] == ["connect", "fill"]
    assert all("options" not in c[1] for c in calls)


def _bounded_fill_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[list[str]], frozenset[str]]:
    """Wire ``run()`` for an apply-mode --until-pass scenario: permissive
    connect (the probe needs options=), a fill that completed the
    non-paper pass with one partial kind ('gripe') still unfilled, and a
    recording ``build_kind_indexes``. Returns ``(built_kinds_calls,
    marked)``."""
    built: list[list[str]] = []
    marked = frozenset(k for k in bf._ANN_PARTIAL_KINDS if k != "gripe")

    def connect(dsn: str, **kw: Any) -> _Conn:
        return _Conn()

    def fill(*a: object, **k: object) -> bf.BackfillResult:
        return bf.BackfillResult(
            10,
            5,
            0,
            False,
            "non-paper",
            3,
            unfilled_by_kind={"gripe": 3},
            marked=marked,
        )

    def build(conn: Any, kinds: list[str] | None = None) -> dict[str, bool]:
        built.append(list(kinds or []))
        return {k: True for k in (kinds or [])}

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(bf, "backfill_embedding_kinds", fill)
    monkeypatch.setattr(bf, "resolve_dsn", lambda url: url)
    monkeypatch.setattr(bf, "_check_validated", lambda conn: False)
    monkeypatch.setattr(bf, "build_kind_indexes", build)
    monkeypatch.setattr(bf, "_unfilled_rows", lambda conn: [])
    return built, marked


def test_until_pass_bounded_run_with_unready_kind_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gr516948: ``--until-pass non-paper`` bounds the fill, but an
    unready *partial* kind afterwards is a real fill failure — ``paper``
    is a global kind and never lands in ``unready``, so every unfilled
    partial kind is one the non-paper pass was asked to fill. The run
    exits 1 with the unfilled-row listing and builds no index."""
    built, _marked = _bounded_fill_stub(monkeypatch)

    with pytest.raises(SystemExit) as e:
        bf.run(_args(until_pass="non-paper", no_btree=True))
    assert e.value.code == 1
    assert built == []
    err = capsys.readouterr().err
    assert "still have unfilled rows" in err
    assert "fill incomplete" in err
    assert "--skip-fill" in err
    assert "fill-complete kinds:" in err
    assert "gripe" not in err.split("fill-complete kinds:")[1]


def test_full_fill_with_unready_kind_still_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure path is kept for a run that was meant to fill
    everything (default ``--until-pass paper``): unready partial kinds
    still refuse the index phase with exit 1, and the hint still names
    --skip-fill and only the fill-complete kinds."""
    built, _marked = _bounded_fill_stub(monkeypatch)

    with pytest.raises(SystemExit) as e:
        bf.run(_args(until_pass="paper", no_btree=True))
    assert e.value.code == 1
    assert built == []
    err = capsys.readouterr().err
    assert "re-run with --skip-fill" in err
    assert "fill-complete kinds:" in err
    assert "gripe" not in err.split("fill-complete kinds:")[1]
