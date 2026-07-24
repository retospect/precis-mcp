"""Assembled-context capture — the INPUT-side twin of ``meta.transcript``.

ADR 0038's :func:`~precis.utils.prompt.assembler.assemble` builds the full
prompt input for the planner tick and the structural/deep-tree reviewers;
:func:`~precis.utils.prompt.persist_assembled_context` (``precis.utils.
prompt.capture``) writes that block list onto a ref's ``meta`` so a
debugging surface can render "what the LLM actually saw last time".

Tests:

* the helper writes the contract shape (``assembled_context`` +
  ``assembled_context_at``) and is a no-op-with-log on bad input
* a real planner assembly (``build_planner_prompts``) persists onto a job
  ref with the expected block ids
* a real structural-reviewer assembly persists onto a digest ref with the
  expected block ids
* the sweeper GC strips both keys past the retention window
"""

from __future__ import annotations

import logging

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis.utils.prompt import Block, Layer, persist_assembled_context
from precis.workers.review import _assemble_reviewer_blocks
from precis.workers.structural import STRUCTURAL
from precis.workers.sweeper import _gc_transcripts

# ── the helper itself ────────────────────────────────────────────────


def test_persist_writes_contract_shape(store: Store) -> None:
    ref = store.insert_ref(kind="job", slug=None, title="capture test job")
    blocks = [
        Block(id="a.cached", layer=Layer.CACHED, text="cached body"),
        Block(id="b.variable", layer=Layer.VARIABLE, text="variable body"),
    ]

    persist_assembled_context(store, ref.id, blocks)

    got = store.get_ref(kind="job", id=ref.id)
    assert got is not None
    ctx = got.meta["assembled_context"]
    assert ctx == [
        {"id": "a.cached", "layer": "cached", "text": "cached body"},
        {"id": "b.variable", "layer": "variable", "text": "variable body"},
    ]
    assert "assembled_context_at" in got.meta


def test_persist_empty_blocks_is_silent_noop(store: Store) -> None:
    ref = store.insert_ref(kind="job", slug=None, title="capture test job")

    persist_assembled_context(store, ref.id, [])

    got = store.get_ref(kind="job", id=ref.id)
    assert got is not None
    assert "assembled_context" not in got.meta


def test_persist_bad_input_is_noop_with_log(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed ``blocks`` (not real ``Block`` instances) must not raise —
    logged and swallowed, matching the never-fatal capture contract."""
    ref = store.insert_ref(kind="job", slug=None, title="capture test job")

    with caplog.at_level(logging.ERROR, logger="precis.utils.prompt.capture"):
        persist_assembled_context(store, ref.id, ["not", "a", "block"])  # type: ignore[list-item]

    assert "failed to capture assembled context" in caplog.text
    got = store.get_ref(kind="job", id=ref.id)
    assert got is not None
    assert "assembled_context" not in got.meta


def test_persist_accepts_an_open_connection(store: Store) -> None:
    """The connection-shaped overload folds into the caller's own tx."""
    ref = store.insert_ref(kind="job", slug=None, title="capture test job")
    blocks = [Block(id="x", layer=Layer.VARIABLE, text="body")]

    with store.pool.connection() as conn:
        persist_assembled_context(conn, ref.id, blocks)
        conn.commit()

    got = store.get_ref(kind="job", id=ref.id)
    assert got is not None
    assert got.meta["assembled_context"][0]["id"] == "x"


def test_persist_caps_oversized_blocks(store: Store) -> None:
    ref = store.insert_ref(kind="job", slug=None, title="capture test job")
    huge = "x" * 900_000
    blocks = [
        Block(id="small", layer=Layer.CACHED, text="tiny"),
        Block(id="huge1", layer=Layer.VARIABLE, text=huge),
        Block(id="huge2", layer=Layer.VARIABLE, text=huge),
    ]

    persist_assembled_context(store, ref.id, blocks)

    got = store.get_ref(kind="job", id=ref.id)
    assert got is not None
    ctx = got.meta["assembled_context"]
    total = sum(len(b["text"]) for b in ctx)
    assert total <= 1_000_000
    # the small block survives untouched; the two oversized ones got trimmed
    assert next(b for b in ctx if b["id"] == "small")["text"] == "tiny"
    assert "…(truncated)" in next(b for b in ctx if b["id"] == "huge1")["text"]


# ── real planner assembly persists onto the job ref ───────────────────


def test_planner_assembly_persists_onto_job_ref(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.store.insert_ref(kind="todo", slug=None, title="do a thing")
    job = hub.store.insert_ref(
        kind="job", slug=None, title="plan_tick job", parent_id=todo.id
    )

    prompts = build_planner_prompts(hub.store, ref_id=todo.id, model="opus")
    persist_assembled_context(hub.store, job.id, prompts.blocks)

    got = hub.store.get_ref(kind="job", id=job.id)
    assert got is not None
    ids = [b["id"] for b in got.meta["assembled_context"]]
    # cached-layer modules land first, then the variable-layer body module —
    # both halves of PlannerPrompts.blocks made it onto the ref.
    assert "contract" in ids
    assert "body" in ids
    assert "assembled_context_at" in got.meta


# ── real structural-reviewer assembly persists onto the digest ref ────


def test_structural_assembly_persists_onto_digest_ref(store: Store) -> None:
    blocks = _assemble_reviewer_blocks(STRUCTURAL, store)
    digest = store.insert_ref(kind="memory", slug=None, title="structural digest")

    persist_assembled_context(store, digest.id, blocks)

    got = store.get_ref(kind="memory", id=digest.id)
    assert got is not None
    ids = {b["id"] for b in got.meta["assembled_context"]}
    assert "structural.body" in ids
    assert "reviewer.abbreviations" in ids
    assert "reviewer.footer" in ids


# ── sweeper GC strips both keys past retention ─────────────────────────


def _backdate_ref(store: Store, ref_id: int, *, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - %s::interval WHERE ref_id = %s",
            (f"{days} days", ref_id),
        )
        conn.commit()


def test_sweeper_gc_strips_assembled_context_past_retention(store: Store) -> None:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="old plan_tick job",
        meta={
            "assembled_context": [{"id": "a", "layer": "cached", "text": "x"}],
            "assembled_context_at": "2020-01-01T00:00:00+00:00",
        },
    )
    digest = store.insert_ref(
        kind="memory",
        slug=None,
        title="old structural digest",
        meta={
            "assembled_context": [{"id": "b", "layer": "variable", "text": "y"}],
            "assembled_context_at": "2020-01-01T00:00:00+00:00",
        },
    )
    _backdate_ref(store, job.id, days=40)
    _backdate_ref(store, digest.id, days=40)

    reaped = _gc_transcripts(store)

    assert reaped >= 2
    got_job = store.get_ref(kind="job", id=job.id)
    got_digest = store.get_ref(kind="memory", id=digest.id)
    assert got_job is not None and "assembled_context" not in got_job.meta
    assert got_job is not None and "assembled_context_at" not in got_job.meta
    assert got_digest is not None and "assembled_context" not in got_digest.meta


def test_sweeper_gc_leaves_fresh_assembled_context_alone(store: Store) -> None:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="fresh plan_tick job",
        meta={
            "assembled_context": [{"id": "a", "layer": "cached", "text": "x"}],
            "assembled_context_at": "2026-01-01T00:00:00+00:00",
        },
    )

    reaped = _gc_transcripts(store)

    assert reaped == 0
    got = store.get_ref(kind="job", id=job.id)
    assert got is not None
    assert "assembled_context" in got.meta
