"""Per-run features rows (gr595544): the cheap extractors and the
best-effort ``slice_run_features`` writer ``run_slice.submit`` calls
after a launch.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.workers.job_types import _run_features as rf


class _Ref:
    """A todo/job-ref-shaped stand-in."""

    def __init__(
        self, meta: dict[str, Any] | None = None, set_by: str | None = "gripe-loop"
    ) -> None:
        self.meta = meta
        self.set_by = set_by


class _Lane:
    name = "devin"
    cost_class = "free"


class _Conn:
    """Fake psycopg connection: records every statement, answers the
    feature reads with canned rows keyed off the SQL text."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cur:
        self.calls.append((sql, params))
        for marker, rows in self.answers.items():
            if marker in sql:
                return _Cur(rows)
        return _Cur([(0,)])


class _Cur:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)

    def fetchone(self) -> Any:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[Any]:
        rows, self.rows = self.rows, []
        return rows


class _Store:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @contextlib.contextmanager
    def tx(self):
        yield self.conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _store(**answers: Any) -> tuple[_Store, _Conn]:
    conn = _Conn(answers)
    return _Store(conn), conn


def _insert_params(conn: _Conn) -> tuple[Any, ...]:
    """Params of the most recent ``slice_run_features`` insert."""
    return next(
        p for s, p in reversed(conn.calls) if "INSERT INTO slice_run_features" in s
    )


def test_extract_counts_distinct_handles() -> None:
    feats = rf.extract("fix gr123456 and gr654321; `gr123456` again, see me777")
    # the gr123456 repeat folds to one distinct handle
    assert feats["handle_count"] == 3


def test_extract_constraint_clauses() -> None:
    text = (
        "You must never edit scripts/ship.\n"
        "Add the flag. It is optional; always ask first\n"
        "and do not commit."
    )
    feats = rf.extract(text)
    # clause 1 (must/never — one clause), "always ask first", "do not commit"
    assert feats["constraint_clauses"] == 3


def test_op_type_lookup_only_when_no_edit_cue() -> None:
    assert rf._op_type("check whether the lane is free and report") == "lookup"
    assert rf._op_type("create a new table for features") == "create"
    assert rf._op_type("fix the retry counter") == "modify"
    # an edit cue beats a stray read verb
    assert rf._op_type("check the test then fix the bug") == "modify"
    assert rf._op_type("the sky is blue") is None


def test_record_writes_the_row() -> None:
    store, conn = _store(
        **{
            "walk(id, depth)": [(7,)],
            "GREATEST(": [(2,)],
            "attempt:(\\d+)": [(3,)],
            "max(launch)": [(1,)],
        }
    )
    ok = rf.record(
        store,
        job_ref_id=99,
        todo_id=42,
        todo=_Ref(meta={"generate": {}}, set_by="agent-x"),
        task_text="fix gr123456; never touch deploy/",
        brief="rendered brief text",
        lane=_Lane(),
        model="swe-2-high",
        effort="high",
        task_class="code",
    )
    assert ok is True
    p = _insert_params(conn)
    assert p[0] == 99 and p[1] == 42  # job / todo refs
    assert p[2] == 3  # attempt ordinal from the tag read
    assert p[3] == 1  # launch ordinal — first row on this job
    assert p[4] == "code" and p[5] == "generator"
    assert p[6] == "devin" and p[7] == "free"
    assert p[8] == "swe-2-high" and p[9] == "high"
    assert p[10] == "agent-x"
    assert p[11] == len("rendered brief text")
    assert p[12] == 1  # handle_count
    assert p[13] == 7  # fisheye_2hop_nodes
    assert p[14] == 2  # max_dep_depth
    assert p[15] == 1  # constraint_clauses
    assert p[16] == "modify"  # op_type
    assert p[17] is None  # novelty_cosine — no embedder
    assert p[18] is not None and len(p[18]) == 64  # prompt_sha256
    assert p[19].startswith("fix gr123456")  # prompt_text
    assert p[20] is None and p[21] is None  # embedder name / vector


def test_record_keys_each_launch_on_the_job() -> None:
    """A relaunched job ref gets a second row, not a key collision."""
    store, conn = _store(
        **{
            "walk(id, depth)": [(0,)],
            "GREATEST(": [(0,)],
            "attempt:(\\d+)": [(1,)],
            "max(launch)": [(1,)],
        }
    )
    kwargs: dict[str, Any] = {
        "job_ref_id": 55,
        "todo_id": 42,
        "todo": _Ref(),
        "task_text": "fix gr123456",
        "brief": "b",
        "lane": _Lane(),
        "model": None,
        "effort": "low",
        "task_class": "code",
    }
    assert rf.record(store, **kwargs) is True
    assert _insert_params(conn)[3] == 1
    # the outage relaunch resubmits the same job ref: launch increments,
    # the burned-attempt ordinal does not
    conn.answers["max(launch)"] = [(2,)]
    assert rf.record(store, **kwargs) is True
    p = _insert_params(conn)
    assert p[0] == 55 and p[2] == 1 and p[3] == 2


def test_record_fills_novelty_with_an_embedder() -> None:
    store, conn = _store(
        **{
            "walk(id, depth)": [(1,)],
            "GREATEST(": [(0,)],
            "attempt:(\\d+)": [(1,)],
            "max(launch)": [(1,)],
            "min(prompt_embedding": [(0.42,)],
        }
    )
    ok = rf.record(
        store,
        job_ref_id=100,
        todo_id=42,
        todo=_Ref(),
        task_text="add a features table",
        brief="b",
        lane=_Lane(),
        model=None,
        effort="low",
        task_class="code",
        embedder=MockEmbedder(dim=1024, model="bge-m3"),
    )
    assert ok is True
    p = _insert_params(conn)
    assert p[17] == pytest.approx(0.42)  # novelty_cosine
    assert p[20] == "bge-m3"
    assert p[21] is not None  # prompt_embedding vector


def test_record_is_dark() -> None:
    # no store → False, never raises
    assert (
        rf.record(
            None,
            job_ref_id=1,
            todo_id=1,
            todo=_Ref(),
            task_text="t",
            brief="b",
            lane=_Lane(),
            model=None,
            effort="low",
            task_class="code",
        )
        is False
    )
    # job_ref_id not an int → no ledger row
    store, _ = _store()
    assert (
        rf.record(
            store,
            job_ref_id="x",
            todo_id=1,
            todo=_Ref(),
            task_text="t",
            brief="b",
            lane=_Lane(),
            model=None,
            effort="low",
            task_class="code",
        )
        is False
    )

    # a store whose tx raises → False, swallowed
    class _Dead:
        @contextlib.contextmanager
        def tx(self):
            raise RuntimeError("no table")

            yield

    assert (
        rf.record(
            _Dead(),
            job_ref_id=1,
            todo_id=1,
            todo=_Ref(),
            task_text="t",
            brief="b",
            lane=_Lane(),
            model=None,
            effort="low",
            task_class="code",
        )
        is False
    )


def test_record_survives_a_dead_embedder() -> None:
    class _Boom:
        model = "dead"

        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("embedder down")

    store, conn = _store(
        **{
            "walk(id, depth)": [(0,)],
            "GREATEST(": [(0,)],
            "attempt:(\\d+)": [(1,)],
            "max(launch)": [(1,)],
        }
    )
    ok = rf.record(
        store,
        job_ref_id=7,
        todo_id=42,
        todo=_Ref(),
        task_text="t",
        brief="b",
        lane=_Lane(),
        model=None,
        effort="low",
        task_class="code",
        embedder=_Boom(),
    )
    assert ok is True
    p = _insert_params(conn)
    assert p[17] is None and p[21] is None
