"""The ``memory_triage`` job_type (gr595538): a big-model pass over a
twice-memory-killed job's run logs, deciding too-big (drop) / bug (gripe)
/ unknown (ask-user todo). Only what triage cannot explain reaches the
user; a job the verdict race already resolved is left alone.

DB-backed for :func:`apply_verdict` / :func:`triage`; pure for the reply
parser.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.utils.llm.router import LlmResult, Tier
from precis.workers.executors import _common
from precis.workers.job_types import memory_triage


def _mk_parked_job(store: Store, **meta: Any) -> int:
    started = datetime.now(UTC) - timedelta(seconds=120)
    base: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "struct_relax",
        "started_at": started.isoformat(),
        "footprint_req": 3072,
        "killed_memory": [
            {"at": started.isoformat(), "bumped_req_mb": 3072},
            {"at": datetime.now(UTC).isoformat(), "bumped_req_mb": 4608},
        ],
    }
    base.update(meta)
    ref = store.insert_ref(kind="job", slug=None, title="parked relax", meta=base)
    store.add_tag(ref.id, Tag.parse_strict("STATUS:parked"), set_by="agent")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="container rc=137",
                meta={"chunk_kind": "job_event"},
            ),
            ChunkInsert(
                ord=1,
                text="container rc=137 (second attempt)",
                meta={"chunk_kind": "job_event"},
            ),
        ],
    )
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        return _common.current_status(conn, ref_id)


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _refs_by_fingerprint(store: Store, fingerprint: str) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE retired_at IS NULL "
            "AND meta->>'fingerprint' = %s",
            (fingerprint,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ask_todos(store: Store, job_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta->>'memory_triage_job_id' = %s",
            (str(job_id),),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── pure: the reply parser ────────────────────────────────────────────


def test_parse_verdict_bare_object() -> None:
    payload = memory_triage.parse_verdict(
        '{"verdict": "bug", "explanation": "leak", "confidence": 0.8}'
    )
    assert payload["verdict"] == "bug"


def test_parse_verdict_json_fence() -> None:
    payload = memory_triage.parse_verdict(
        'noise\n```json\n{"verdict": "too_big", "explanation": "x", "confidence": 0.5}\n```\n'
    )
    assert payload["verdict"] == "too_big"


def test_parse_verdict_rejects_garbage_and_bad_verdict() -> None:
    with pytest.raises(ValueError):
        memory_triage.parse_verdict("no json here")
    with pytest.raises(ValueError):
        memory_triage.parse_verdict('{"verdict": "maybe"}')
    with pytest.raises(ValueError, match="explanation"):
        memory_triage.parse_verdict('{"verdict": "bug", "confidence": 0.5}')
    with pytest.raises(ValueError, match="confidence"):
        memory_triage.parse_verdict(
            '{"verdict": "bug", "explanation": "leak", "confidence": true}'
        )


# ── apply_verdict ──────────────────────────────────────────────────────


def test_too_big_fails_the_job(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    minted = memory_triage.apply_verdict(
        store, ref_id, "too_big", explanation="needs 2 TiB"
    )
    assert minted is None
    assert _status(store, ref_id) == "failed"
    assert _meta(store, ref_id)["failure_class"] == "too-big"


def test_bug_files_one_gripe_and_fails_the_job(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    gripe_id = memory_triage.apply_verdict(
        store, ref_id, "bug", explanation="RSS grows without bound"
    )
    assert gripe_id is not None
    assert len(_refs_by_fingerprint(store, f"memory-triage:{ref_id}")) == 1
    with store.pool.connection() as conn:
        grow = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (gripe_id,)
        ).fetchone()
        open_tag = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' AND t.value = 'open'",
            (gripe_id,),
        ).fetchone()
    assert grow is not None
    kind = grow[0]
    assert kind == "gripe"
    assert open_tag is not None
    assert _status(store, ref_id) == "failed"
    assert _meta(store, ref_id)["failure_class"] == "infra"

    # A second application onto the now-failed job is a verdict-race
    # no-op: still exactly one gripe, and it is not duplicated.
    again = memory_triage.apply_verdict(
        store, ref_id, "bug", explanation="RSS grows without bound"
    )
    assert again is None
    assert len(_refs_by_fingerprint(store, f"memory-triage:{ref_id}")) == 1


def test_unknown_mints_ask_todo_and_leaves_the_job_parked(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    todo_id = memory_triage.apply_verdict(
        store, ref_id, "unknown", explanation="logs inconclusive"
    )
    assert todo_id is not None
    assert (
        memory_triage.apply_verdict(
            store, ref_id, "unknown", explanation="still inconclusive"
        )
        == todo_id
    )
    assert _ask_todos(store, ref_id) == [todo_id]
    with store.pool.connection() as conn:
        ask_tag = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value = 'ask-user'",
            (todo_id,),
        ).fetchone()
        ask_row = conn.execute(
            "SELECT meta->'ask' FROM refs WHERE ref_id = %s", (todo_id,)
        ).fetchone()
    assert ask_row is not None
    ask = dict(ask_row[0])
    assert ask_tag is not None
    assert ask["kind"] == "ruling"
    assert ask["blocks"] == "nothing"
    # Only what triage cannot explain stays parked — waiting on the human.
    assert _status(store, ref_id) == "parked"


def test_verdict_race_on_non_parked_job_is_a_no_op(store: Store) -> None:
    started = datetime.now(UTC) - timedelta(seconds=60)
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="done",
        meta={"executor": "ssh_node", "job_type": "struct_relax"},
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:succeeded"), set_by="agent")
    assert (
        memory_triage.apply_verdict(store, int(ref.id), "bug", explanation="x") is None
    )


# ── triage() end to end with a stubbed BIG-tier call ──────────────────


def _llm(text: str) -> Any:
    def _fake(prompt: str, *, ref_id: int | None) -> LlmResult:
        return LlmResult(
            text=text, cost_usd=0.0, turns_used=1, model="t", tier=Tier.BIG
        )

    return _fake


def test_triage_end_to_end_bug(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    record = memory_triage.triage(
        store,
        ref_id,
        llm=_llm(
            '{"verdict": "bug", "explanation": "leak at line 42", "confidence": 0.9}'
        ),
    )
    assert record["status"] == "ok"
    assert record["verdict"] == "bug"
    assert _status(store, ref_id) == "failed"


def test_triage_on_resolved_job_never_calls_the_llm(store: Store) -> None:
    started = datetime.now(UTC) - timedelta(seconds=60)
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="done",
        meta={"executor": "ssh_node", "job_type": "struct_relax"},
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:failed"), set_by="agent")

    def _boom(prompt: str, *, ref_id: int | None) -> LlmResult:
        raise AssertionError("llm must not be called on a resolved job")

    record = memory_triage.triage(store, int(ref.id), llm=_boom)
    assert record["status"] == "resolved"


def test_triage_bad_payload_is_a_record_not_a_raise(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    record = memory_triage.triage(store, ref_id, llm=_llm("garbage"))
    assert record["status"] == "bad_payload"
    assert _status(store, ref_id) == "parked"


def test_llm_failure_raises_for_the_dispatch_to_park(store: Store) -> None:
    ref_id = _mk_parked_job(store)

    def _err(prompt: str, *, ref_id: int | None) -> LlmResult:
        return LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model="t",
            tier=Tier.BIG,
            error="quota",
            paused=True,
        )

    with pytest.raises(memory_triage.LlmCallFailed):
        memory_triage.triage(store, ref_id, llm=_err)
    assert _status(store, ref_id) == "parked"


def test_evidence_masks_and_caps(store: Store) -> None:
    token = "sk-" + "a" * 40
    ref_id = _mk_parked_job(store, params={"token": token})
    ev = memory_triage._evidence(store, ref_id)
    assert ev is not None
    assert token not in ev["forensics"]
    assert "rc=137" in ev["events"]


def test_bug_disposition_and_gripe_roll_back_together(store: Store) -> None:
    ref_id = _mk_parked_job(store)
    with pytest.raises(RuntimeError, match="abort"):
        with store.tx() as conn:
            assert memory_triage.apply_verdict(
                store, ref_id, "bug", explanation="leak", conn=conn
            )
            assert _common.current_status(conn, ref_id) == "failed"
            raise RuntimeError("abort")
    assert _status(store, ref_id) == "parked"
    assert _refs_by_fingerprint(store, f"memory-triage:{ref_id}") == []


@pytest.mark.parametrize("mode", ["bad_payload", "llm_error"])
def test_dispatch_failure_leaves_follow_up(
    store: Store, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    ref_id = _mk_parked_job(store)
    monkeypatch.setattr(
        memory_triage,
        "_llm",
        lambda *args, **kwargs: LlmResult(
            text="garbage",
            error="unavailable" if mode == "llm_error" else None,
            cost_usd=None,
            turns_used=None,
            model="t",
            tier=Tier.BIG,
        ),
    )

    class Context:
        meta = {"params": {"job_id": ref_id}}
        failures: list[str]

        def __init__(self) -> None:
            self.store = store
            self.failures = []

        def append_chunk(self, kind: str, text: str) -> None:
            pass

        def record_failure(self, reason: str, *, failure_class: str) -> None:
            assert failure_class == "infra"
            self.failures.append(reason)

    ctx = Context()
    memory_triage._dispatch(ctx, memory_triage.SPEC)
    assert len(ctx.failures) == 1
    assert _status(store, ref_id) == "parked"
    assert len(_ask_todos(store, ref_id)) == 1
