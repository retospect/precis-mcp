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
        "batch_timeout": 60,
        "skip_fill": False,
        "no_indexes": False,
        "reindex_global": False,
        "database_url": _DSN,
        "maintenance_work_mem": "2GB",
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
    return log


@pytest.mark.parametrize("extra", [{}, {"reindex_global": True, "no_indexes": True}])
def test_pooler_with_index_phase_refuses_before_fill(
    calls: list[tuple[str, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    extra: dict[str, Any],
) -> None:
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
    bf.run(_args(no_indexes=True))
    assert [c[0] for c in calls] == ["connect", "fill"]
    assert all("options" not in c[1] for c in calls)
