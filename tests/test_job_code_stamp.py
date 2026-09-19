"""A job row says which build ran it (gr346951).

``refs.meta`` on a claimed job carried lease_host/lease_process/
lease_boot_id but no code version, so a session whose MCP container runs
newer code than the cluster could not tell from the job that a re-run
measured stale code. Every claim now stamps ``lease_code``
(``<version>@<sha_short>``, the same collector precis-status uses), the
job view renders it as ``ran_on:``, and the pcb enqueue reply says the
job runs on the cluster's code, not the session's.
"""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.handlers.skill import code_stamp
from precis.store import Store
from precis.store.types import Tag


def test_code_stamp_is_version_at_sha_and_cached() -> None:
    stamp = code_stamp()
    assert re.match(r"^[^@\s]+@[^@\s]+$", stamp), stamp
    assert code_stamp() is stamp


def test_job_view_renders_ran_on_from_lease_meta(store: Store) -> None:
    hub = Hub(store=store, embedder=None)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="routed",
        meta={
            "job_type": "pcb_route",
            "executor": "job_inproc",
            "lease_host": "melchior",
            "lease_code": "8.33.0@8e0099fc",
        },
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="agent")
    body = JobHandler(hub=hub).get(id=job.id).body
    assert "ran_on: melchior (code 8.33.0@8e0099fc)" in body


def test_job_view_flags_an_unstamped_pre_fix_row(store: Store) -> None:
    """Rows claimed before this shipped have a host but no code — say so
    rather than hide the line."""
    hub = Hub(store=store, embedder=None)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="old",
        meta={"job_type": "pcb_route", "lease_host": "melchior"},
    )
    body = JobHandler(hub=hub).get(id=job.id).body
    assert "ran_on: melchior (code unstamped)" in body
