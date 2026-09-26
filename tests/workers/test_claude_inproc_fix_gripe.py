"""``claude_inproc._run_fix_gripe`` — the executor arm's own gripe-linking
and failure-rollback bookkeeping (gr451352, gr451170), mirroring
``test_claude_inproc_doctor_tick.py``'s ``_FakeSpec`` pattern: ``spec.run``
is stubbed so this only exercises the executor's side, not
``fix_gripe.run`` itself (covered by ``tests/test_fix_gripe.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc as ci
from precis.workers.job_types.fix_gripe import RunOutcome

pytestmark = pytest.mark.db


def _open_gripe(store: Store, title: str = "a bug") -> int:
    ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={})
    store.add_tag(
        ref.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _mk_job(store: Store, *, params: dict[str, Any] | None = None) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="fix_gripe",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "params": params or {},
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    return int(job.id)


def _job_status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return ci._current_status(conn, ref_id)


def _gripe_status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return ci._current_status(conn, ref_id)


def _job_event_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_event'",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _gripe_comment_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'gripe_comment' ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


class _FakeSpec:
    name = "fix_gripe"

    def __init__(self, outcome: RunOutcome | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def run(self, **kw: Any) -> RunOutcome:
        self.calls.append(kw)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


# ── gr451352: link-fallback + backfill ─────────────────────────────────


class TestLinkFallbackAndBackfill:
    def test_params_gripe_id_used_when_link_absent_and_link_backfilled(
        self, store: Store
    ) -> None:
        gripe_id = _open_gripe(store)
        job_id = _mk_job(store, params={"gripe_id": gripe_id})
        spec = _FakeSpec(
            RunOutcome(
                status="succeeded",
                summary_text="ok",
                gripe_comment_text="[worker] fixed",
                branch="gripe_1",
                sha="deadbeef",
                wall_seconds=1.0,
            )
        )

        ci._run_fix_gripe(store, job_id, spec)

        # spec.run() was invoked with the gripe id recovered from params,
        # not left unrun.
        assert spec.calls == [
            {
                "store": store,
                "job_id": job_id,
                "gripe_id": gripe_id,
                "params": {"gripe_id": gripe_id},
            }
        ]
        # The missing `fixes` link is backfilled so a later query (a
        # second run, a UI render) sees it too.
        links = store.links_for(job_id, direction="out")
        fixes = [l for l in links if l.relation == "fixes"]
        assert len(fixes) == 1
        assert fixes[0].dst_ref_id == gripe_id
        # And the job actually completed successfully.
        assert _job_status(store, job_id) == ci._SUCCEEDED

    def test_neither_link_nor_params_records_failure(self, store: Store) -> None:
        job_id = _mk_job(store, params={})
        spec = _FakeSpec(
            RunOutcome(
                status="succeeded",
                summary_text="ok",
                gripe_comment_text="ok",
                branch=None,
                sha=None,
                wall_seconds=0.1,
            )
        )

        ci._run_fix_gripe(store, job_id, spec)

        assert spec.calls == []  # never ran — no gripe to work on
        assert _job_status(store, job_id) == ci._FAILED
        events = _job_event_texts(store, job_id)
        assert any("no link" in e and "no params.gripe_id" in e for e in events)


# ── gr451170 fix 2: a failure must never re-open a terminal gripe ─────


class TestNeverReopensTerminalGripe:
    def test_exception_branch_leaves_done_gripe_done(self, store: Store) -> None:
        gripe_id = _open_gripe(store)
        job_id = _mk_job(store, params={})
        store.add_link(
            src_ref_id=job_id, dst_ref_id=gripe_id, relation="fixes", set_by="system"
        )
        # The gripe was closed by something else (a human, an earlier
        # attempt) WHILE this job's spec.run() was in flight.
        store.add_tag(
            gripe_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )
        spec = _FakeSpec(RuntimeError("boom"))

        ci._run_fix_gripe(store, job_id, spec)

        assert _job_status(store, job_id) == ci._FAILED
        assert _gripe_status(store, gripe_id) == "done"  # NOT reopened
        comments = _gripe_comment_texts(store, gripe_id)
        assert any("crashed" in c and "boom" in c for c in comments)

    def test_failed_outcome_branch_leaves_wontfix_gripe_wontfix(
        self, store: Store
    ) -> None:
        gripe_id = _open_gripe(store)
        job_id = _mk_job(store, params={})
        store.add_link(
            src_ref_id=job_id, dst_ref_id=gripe_id, relation="fixes", set_by="system"
        )
        store.add_tag(
            gripe_id,
            Tag.closed("STATUS", "wontfix"),
            set_by="agent",
            replace_prefix=True,
        )
        spec = _FakeSpec(
            RunOutcome(
                status="failed",
                summary_text="failed",
                gripe_comment_text="[worker] fix attempt failed: no commits",
                branch="gripe_1",
                sha=None,
                wall_seconds=1.0,
            )
        )

        ci._run_fix_gripe(store, job_id, spec)

        assert _job_status(store, job_id) == ci._FAILED
        assert _gripe_status(store, gripe_id) == "wontfix"  # NOT reopened
        comments = _gripe_comment_texts(store, gripe_id)
        assert any("no commits" in c for c in comments)

    def test_failure_on_a_live_gripe_still_reopens_it(self, store: Store) -> None:
        """Byte-identical to the pre-gr451170 behaviour when the gripe is
        NOT terminal: a real failure still rolls it back to open for a
        re-attempt."""
        gripe_id = _open_gripe(store)
        job_id = _mk_job(store, params={})
        store.add_link(
            src_ref_id=job_id, dst_ref_id=gripe_id, relation="fixes", set_by="system"
        )
        store.add_tag(
            gripe_id,
            Tag.closed("STATUS", "in_review"),
            set_by="agent",
            replace_prefix=True,
        )
        spec = _FakeSpec(RuntimeError("boom"))

        ci._run_fix_gripe(store, job_id, spec)

        assert _gripe_status(store, gripe_id) == "open"
