"""Tests for the diagnose scanner (``workers/diagnose_scan.py``).

Covers: minting a ``diagnose_gripe`` job per undiagnosed open gripe, the
idem_key dedup (never twice for one gripe) and its exclusion from candidate
selection so a failed job can't starve the queue, skipping a gripe that
already carries a ``DIAGNOSIS (auto`` comment, the reused ``no-groom``
opt-out, the per-pass cap, and prio inheritance.
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.diagnose_scan import _CAP, run_diagnose_scan_pass

pytestmark = pytest.mark.db


def _open_gripe(store: Store, title: str, *, prio: int | None = None) -> int:
    """Insert a live gripe tagged STATUS:open; return its id."""
    ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, prio=prio)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _mark_diagnosed(store: Store, gripe_id: int) -> None:
    """Append a DIAGNOSIS (auto...) comment — the marker diagnose_scan skips."""
    store.chunks.insert_chunks(
        gripe_id,
        [
            ChunkInsert(
                ord=1,
                text="DIAGNOSIS (auto, job 1):\nRoot cause: x\nConfidence: 0.9",
                meta={"chunk_kind": "gripe_comment"},
            )
        ],
    )


def _diagnose_jobs(store: Store) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, title, meta, prio FROM refs
             WHERE kind = 'job' AND deleted_at IS NULL
               AND meta ->> 'job_type' = 'diagnose_gripe'
             ORDER BY ref_id
            """
        ).fetchall()
    return [{"id": int(r[0]), "title": r[1], "meta": r[2], "prio": r[3]} for r in rows]


# ── minting ──────────────────────────────────────────────────────


def test_mints_job_for_open_undiagnosed_gripe(store: Store) -> None:
    gid = _open_gripe(store, "embedder health signals lie")

    result = run_diagnose_scan_pass(store)

    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0

    jobs = _diagnose_jobs(store)
    assert len(jobs) == 1
    meta = jobs[0]["meta"]
    assert meta["executor"] == "claude_inproc"
    assert meta["job_type"] == "diagnose_gripe"
    assert meta["params"] == {"gripe_id": gid}
    assert meta["idem_key"] == f"diagnose:{gid}"


def test_idle_when_no_open_gripes(store: Store) -> None:
    result = run_diagnose_scan_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert _diagnose_jobs(store) == []


# ── dedup ────────────────────────────────────────────────────────


def test_no_remint_via_idem_key(store: Store) -> None:
    gid = _open_gripe(store, "already scheduled")
    run_diagnose_scan_pass(store)
    assert len(_diagnose_jobs(store)) == 1

    # A second pass sees the gripe as still open and still undiagnosed (no
    # DIAGNOSIS comment landed — no dispatch ran), but its held idem_key now
    # drops it before selection: any status counts, so it is not even a
    # candidate, let alone a mint.
    result = run_diagnose_scan_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    jobs = _diagnose_jobs(store)
    assert len(jobs) == 1
    assert jobs[0]["meta"]["params"] == {"gripe_id": gid}


def test_held_key_does_not_consume_a_cap_slot(store: Store) -> None:
    """A gripe whose diagnose job failed must not starve the queue behind it.

    The prod wedge (2026-08-16→21): selection asks "has a DIAGNOSIS comment?"
    while minting asks "is the idem_key held?". A failed job answers no to the
    first and yes to the second, so the gripe stayed a candidate forever —
    and since the cap bounds *candidates*, the top _CAP failures re-selected
    every pass, minted nothing, and hid everything further down the list.
    """
    ids = [_open_gripe(store, f"gripe {i}") for i in range(_CAP + 1)]

    # Pass 1 fills the cap with the _CAP newest gripes; every job then fails,
    # so none of them ever writes a DIAGNOSIS comment.
    first = run_diagnose_scan_pass(store)
    assert first.ok == _CAP
    for job in _diagnose_jobs(store):
        store.add_tag(
            job["id"],
            Tag.closed("STATUS", "failed"),
            set_by="agent",
            replace_prefix=True,
        )

    # Pass 2 must skip those _CAP burned keys and reach the one gripe left.
    second = run_diagnose_scan_pass(store)
    assert second.claimed == 1, "burned keys must not be selected as candidates"
    assert second.ok == 1

    minted_for = {j["meta"]["params"]["gripe_id"] for j in _diagnose_jobs(store)}
    assert minted_for == set(ids), "the oldest gripe was starved by failed jobs"


def test_no_remint_even_after_job_terminal(store: Store) -> None:
    """Dedup keys on idem_key existence, not status — a terminal
    (succeeded/failed) diagnose job still suppresses a re-mint (one
    diagnosis per gripe in v1, per the backlog's open-questions log)."""
    gid = _open_gripe(store, "terminal job still blocks remint")
    run_diagnose_scan_pass(store)
    job_id = _diagnose_jobs(store)[0]["id"]
    store.add_tag(
        job_id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )

    run_diagnose_scan_pass(store)
    assert len(_diagnose_jobs(store)) == 1
    assert gid  # keep the fixture referenced


# ── already-diagnosed skip ───────────────────────────────────────


def test_skips_gripe_with_existing_diagnosis(store: Store) -> None:
    gid = _open_gripe(store, "manually diagnosed already")
    _mark_diagnosed(store, gid)

    result = run_diagnose_scan_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert _diagnose_jobs(store) == []


# ── opt-out ──────────────────────────────────────────────────────


def test_no_groom_tag_opts_out(store: Store) -> None:
    gid = _open_gripe(store, "leave me alone")
    store.add_tag(gid, Tag.open("no-groom"), set_by="agent")

    result = run_diagnose_scan_pass(store)
    assert result.claimed == 0
    assert _diagnose_jobs(store) == []


# ── cap / batch bounding ─────────────────────────────────────────


def test_cap_bounds_mints_per_pass(store: Store) -> None:
    for i in range(_CAP + 2):
        _open_gripe(store, f"gripe {i}")

    result = run_diagnose_scan_pass(store)
    assert result.claimed == _CAP
    assert result.ok == _CAP
    assert len(_diagnose_jobs(store)) == _CAP


def test_batch_size_further_bounds_the_cap(store: Store) -> None:
    for i in range(_CAP + 2):
        _open_gripe(store, f"gripe {i}")

    result = run_diagnose_scan_pass(store, batch_size=1)
    assert result.claimed == 1
    assert result.ok == 1
    assert len(_diagnose_jobs(store)) == 1


# ── priority inheritance ─────────────────────────────────────────


def test_minted_job_inherits_gripe_prio(store: Store) -> None:
    _open_gripe(store, "high-prio bug", prio=3)
    run_diagnose_scan_pass(store)
    assert _diagnose_jobs(store)[0]["prio"] == 3


def test_minted_job_defaults_prio_when_gripe_unscored(store: Store) -> None:
    _open_gripe(store, "unscored bug")
    run_diagnose_scan_pass(store)
    assert _diagnose_jobs(store)[0]["prio"] == 8
