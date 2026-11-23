"""The writer-relabel one-off (gr727770): migration 0356's door and
scripts/writer-relabel's scope.

The door (``precis.allow_writer_relabel`` in ``precis_measures_frozen``)
admits exactly ``model 'gpt-6-astra' -> 'gpt-6.2-astra'`` with every other
column preserved; the script rewrites the findings/memories filed_by stamp
keyed on client + model + day and the frozen measure ``model`` keyed on
verified ID + model + day + cutoff, refusing while a measure carries a review pinned
on the current content sha. Real PG (the ``store`` fixture).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from precis.store import Store
from tests.workers._helpers import seed_ref

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _common
import _writer_relabel as relabel

_AT = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_AFTER_CUTOFF = datetime(2026, 11, 18, 18, 0, tzinfo=UTC)
_DAY_BEFORE = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def _ref(
    conn: Any,
    kind: str,
    *,
    client: str = "codex-mcp-client",
    model: str = relabel.OLD_MODEL,
    created_at: datetime = _AT,
    title: str = "a record",
) -> int:
    row = conn.execute(
        "INSERT INTO refs (kind, title, meta, created_at) "
        "VALUES (%s, %s, %s, %s) RETURNING ref_id",
        (
            kind,
            title,
            Jsonb({"filed_by": {"client": client, "model": model}}),
            created_at,
        ),
    ).fetchone()
    return int(row[0])


def _measure(
    store: Store,
    conn: Any,
    *,
    model: str = relabel.OLD_MODEL,
    created_at: datetime = _AT,
) -> int:
    subject = seed_ref(store, title="a subject", kind="paper")
    row = conn.execute(
        "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
        "                       run_key, actor, model, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (subject, subject, "1.0", "run:x", "reader", model, created_at),
    ).fetchone()
    return int(row[0])


def _measure_model(conn: Any, mid: int) -> Any:
    return conn.execute("SELECT model FROM measures WHERE id = %s", (mid,)).fetchone()[
        0
    ]


def _filed_by_model(conn: Any, ref_id: int) -> Any:
    return conn.execute(
        "SELECT meta -> 'filed_by' ->> 'model' FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()[0]


def _refuses(conn: Any, sql: str, *params: Any) -> None:
    """A statement the frozen trigger must reject; the savepoint keeps the
    aborted-state inside so the outer transaction stays usable."""
    with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
        with conn.transaction():
            conn.execute(sql, params)


class _NoClose:
    """The script closes the store it opened; the test's store must survive
    the call, so close() is swallowed."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def close(self) -> None:
        pass


def _run(store: Store, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(_common, "open_store", lambda: (_NoClose(store), None))
    return relabel.main(list(argv))


@pytest.fixture
def conn(store: Store) -> Any:
    with store.pool.connection() as c:
        yield c


def test_door_admits_only_the_pinned_pair(store: Store, conn: Any) -> None:
    mid = _measure(store, conn)
    other = _measure(store, conn)
    conn.commit()

    with conn.transaction():
        conn.execute("SELECT set_config('precis.allow_writer_relabel', 'on', true)")
        conn.execute(
            "UPDATE measures SET model = %s WHERE id = %s",
            (relabel.NEW_MODEL, mid),
        )
    assert _measure_model(conn, mid) == relabel.NEW_MODEL

    # Any other movement refuses even under the flag.
    with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
        with conn.transaction():
            conn.execute("SELECT set_config('precis.allow_writer_relabel', 'on', true)")
            conn.execute(
                "UPDATE measures SET model = 'other-model' WHERE id = %s",
                (other,),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
        with conn.transaction():
            conn.execute("SELECT set_config('precis.allow_writer_relabel', 'on', true)")
            conn.execute("UPDATE measures SET model = NULL WHERE id = %s", (other,))
    # The relabel never rides along with another column.
    with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
        with conn.transaction():
            conn.execute("SELECT set_config('precis.allow_writer_relabel', 'on', true)")
            conn.execute(
                "UPDATE measures SET model = %s, literal = 'changed' WHERE id = %s",
                (relabel.NEW_MODEL, other),
            )

    # Without the flag the pinned pair refuses too.
    _refuses(
        conn,
        "UPDATE measures SET model = %s WHERE id = %s",
        relabel.NEW_MODEL,
        other,
    )
    assert _measure_model(conn, other) == relabel.OLD_MODEL


def test_apply_relabels_exact_scope(
    store: Store,
    conn: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = _ref(conn, "finding")
    memory = _ref(conn, "memory", created_at=_AFTER_CUTOFF)
    other_client = _ref(conn, "finding", client="claude-mcp-client")
    other_day = _ref(conn, "memory", created_at=_DAY_BEFORE)
    in_measure = _measure(store, conn)
    unverified_measure = _measure(store, conn)
    late_measure = _measure(store, conn, created_at=_AFTER_CUTOFF)
    early_measure = _measure(store, conn, created_at=_DAY_BEFORE)
    other_model_measure = _measure(store, conn, model="claude-sonnet")
    conn.execute(
        "INSERT INTO reviews (target_kind, target_id, actor, version, "
        "                     content_sha, verdict) "
        "VALUES ('ref', %s, 'checker', '0', precis_target_sha('ref', %s), 'approved')",
        (finding, finding),
    )
    original_sha = store.target_sha("ref", finding, conn=conn)
    conn.commit()

    assert _run(store, monkeypatch) == 0
    output = capsys.readouterr().out
    assert f"    {memory} [memory]" in output
    assert f"    {in_measure} 2026-11-18T09:00:00Z actor=reader run=run:x" in output
    with pytest.raises(SystemExit) as exc:
        _run(store, monkeypatch, "--apply")
    assert exc.value.code == 2
    assert _filed_by_model(conn, finding) == relabel.OLD_MODEL
    assert _measure_model(conn, in_measure) == relabel.OLD_MODEL

    ids = tmp_path / "verified-ids"
    ids.write_text(f"{in_measure} {late_measure}\n", encoding="utf-8")
    assert _run(store, monkeypatch, "--apply", "--measure-ids", str(ids)) == 1
    assert _filed_by_model(conn, finding) == relabel.OLD_MODEL
    ids.write_text(f"{in_measure}\n", encoding="utf-8")
    assert _run(store, monkeypatch, "--apply", "--measure-ids", str(ids)) == 0

    assert _filed_by_model(conn, finding) == relabel.NEW_MODEL
    assert _filed_by_model(conn, memory) == relabel.NEW_MODEL
    assert store.target_sha("ref", finding, conn=conn) == original_sha
    assert _filed_by_model(conn, other_client) == relabel.OLD_MODEL
    assert _filed_by_model(conn, other_day) == relabel.OLD_MODEL
    assert _measure_model(conn, in_measure) == relabel.NEW_MODEL
    assert _measure_model(conn, unverified_measure) == relabel.OLD_MODEL
    assert _measure_model(conn, late_measure) == relabel.OLD_MODEL
    assert _measure_model(conn, early_measure) == relabel.OLD_MODEL
    assert _measure_model(conn, other_model_measure) == "claude-sonnet"

    # Re-runnable: a second pass finds nothing to write.
    assert _run(store, monkeypatch, "--apply", "--measure-ids", str(ids)) == 0
    assert _filed_by_model(conn, other_client) == relabel.OLD_MODEL


def test_apply_refuses_pinned_review(
    store: Store, conn: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mid = _measure(store, conn)
    conn.execute(
        "INSERT INTO reviews (target_kind, target_id, actor, version, "
        "                     content_sha, verdict, note) "
        "VALUES ('measure', %s, 'checker', '0', "
        "        precis_target_sha('measure', %s), 'approved', 'pinned')",
        (mid, mid),
    )
    conn.commit()

    ids = tmp_path / "verified-ids"
    ids.write_text(f"{mid}\n", encoding="utf-8")
    assert _run(store, monkeypatch, "--apply", "--measure-ids", str(ids)) == 1
    assert _measure_model(conn, mid) == relabel.OLD_MODEL


def test_apply_serializes_with_review_inserts(
    store: Store, conn: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mid = _measure(store, conn)
    conn.commit()
    check_pins = relabel._pinned_reviews

    def check_with_competing_writer(repair_conn: Any, ids: list[int]) -> list[int]:
        with pytest.raises(psycopg.errors.LockNotAvailable), conn.transaction():
            # Review INSERT acquires this lock before reading the target SHA.
            conn.execute("LOCK TABLE reviews IN ROW EXCLUSIVE MODE NOWAIT")
        return check_pins(repair_conn, ids)

    monkeypatch.setattr(relabel, "_pinned_reviews", check_with_competing_writer)
    ids = tmp_path / "verified-ids"
    ids.write_text(f"{mid}\n", encoding="utf-8")
    assert _run(store, monkeypatch, "--apply", "--measure-ids", str(ids)) == 0
    assert _measure_model(conn, mid) == relabel.NEW_MODEL
