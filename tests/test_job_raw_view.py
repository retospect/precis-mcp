"""gr635381: a failed job's ``view='raw'`` must surface the failure
reason instead of burying it under the inlined ``meta.assembled_context``
prompt layers (~45 kB) that pushed the error past the read's own byte
cap — ``_render_raw`` elides oversized top-level meta values behind a
single-key door, and a failed job's raw view leads with a ``failure:``
line.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.store import Store, Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _job(store: Store, *, meta: dict, status: str = "failed") -> int:
    ref = store.insert_ref(kind="job", slug=None, title="t", meta=meta)
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def test_raw_elides_assembled_context_keeps_error(
    jobs: JobHandler, store: Store
) -> None:
    ctx = "persona skill text and tool menu …\n" * 1500  # ~55 kB inlined
    reason = "plan_tick verdict: failed (hit split-cap 3 times)"
    jid = _job(store, meta={"assembled_context": ctx, "error": reason})

    body = jobs.get(id=jid, view="raw").body

    # The meta dump fits the read: the boilerplate value collapses to an
    # elision line while the failure reason still renders.
    assert len(body) < 8_000
    assert "chars elided" in body
    assert "args={'meta_key': 'assembled_context'}" in body
    assert reason in body

    # The elision line's own door serves the value whole.
    full = jobs.get(id=jid, view="raw", meta_key="assembled_context").body
    assert ctx in full


def test_raw_failed_job_leads_with_failure(jobs: JobHandler, store: Store) -> None:
    reason = "plan_tick verdict: failed (hit split-cap 3 times)"
    jid = _job(store, meta={"error": reason})

    body = jobs.get(id=jid, view="raw").body

    lines = body.split("\n")
    assert lines[0].startswith("# raw job")
    assert lines[1].startswith("failure:")
    assert reason in lines[1]


def test_raw_failed_job_failure_line_shows_traceback_tail(
    jobs: JobHandler, store: Store
) -> None:
    """The ``failure:`` line is the reason's LAST line: a
    traceback-shaped ``meta.error`` (``record_failure``'s head +
    ``" … "`` + tail, or the full job_event text) opens with
    ``Traceback (most recent call last):`` — the cause sits at the
    end, where a first-line head would show nothing."""
    error = (
        "Traceback (most recent call last):\n"
        '  File "tick.py", line 41, in plan_tick\n'
        "    verdict = decide(ctx)\n"
        "              ^^^^^^^^^^^\n"
        "ZeroDivisionError: split budget hit zero"
    )
    jid = _job(store, meta={"error": error})

    body = jobs.get(id=jid, view="raw").body

    lines = body.split("\n")
    assert lines[1].startswith("failure:")
    assert "ZeroDivisionError: split budget hit zero" in lines[1]
    assert "Traceback" not in lines[1]


def test_raw_succeeded_job_has_no_failure_line(jobs: JobHandler, store: Store) -> None:
    jid = _job(store, meta={"error": "stale crumb"}, status="succeeded")

    body = jobs.get(id=jid, view="raw").body

    lines = body.split("\n")
    assert lines[0].startswith("# raw job")
    assert not lines[1].startswith("failure:")
