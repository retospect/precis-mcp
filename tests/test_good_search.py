"""``good_search`` — coordinator deep-search campaign (thin slice).

Three layers, per the design doc §Testing:

1. **Phase machine** (``good_search._dispatch``) with a fake ctx +
   stubbed ``search_blocks_multi`` / ``spawn_child`` / child
   ``job_result`` rows — plan→triage Yield shape, empty-pool Done,
   heartbeat re-yield, gather on all-terminal, deadline / slice-cap
   force-complete, all-children-failed, cancel.
2. **Triage child** (``good_search_triage``) through the real
   ``claude_p`` subprocess machinery against a ``PRECIS_CLAUDE_BIN``
   stub binary — verdict validation + the retry-then-fail path.
3. **Surface** (``search(kind='paper', good=True)``) against the real
   test DB — async handle, idem-key reuse, concurrency cap, kind gate —
   plus an end-to-end campaign through ``run_coordinator_pass`` +
   ``run_claude_inproc_pass`` + ``run_wake_pass``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import good_search as gs

# ── fakes ───────────────────────────────────────────────────────────


def _hit(slug: str, pos: int, text: str = "chunk text") -> tuple[Any, Any, float]:
    block = SimpleNamespace(
        id=abs(hash((slug, pos))) % 100_000,
        pos=pos,
        text=text,
        chunk_kind="paragraph",
    )
    ref = SimpleNamespace(id=abs(hash(slug)) % 10_000, slug=slug, title=slug)
    return (block, ref, 0.5)


def _result_block(verdicts: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(
        chunk_kind="job_result", pos=0, text=json.dumps({"verdicts": verdicts})
    )


class FakeStore:
    def __init__(
        self,
        hits: list[tuple[Any, Any, float]] | None = None,
        blocks: dict[int, list[Any]] | None = None,
    ) -> None:
        self.hits = hits or []
        self.blocks = blocks or {}
        self.multi_calls: list[dict[str, Any]] = []

    def search_blocks_multi(self, **kw: Any) -> list[tuple[Any, Any, float]]:
        self.multi_calls.append(kw)
        return self.hits

    def list_blocks_for_ref(self, ref_id: int) -> list[Any]:
        return self.blocks.get(ref_id, [])


class FakeCtx:
    def __init__(
        self, store: Any, meta: dict[str, Any], *, cancel: bool = False
    ) -> None:
        self.store = store
        self.ref_id = 500
        self.title = "good_search (unlinked)"
        self.meta = meta
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []
        self.statuses: list[str] = []
        self.spawned: list[dict[str, Any]] = []
        self._cancel = cancel
        self._next_child = 900

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_status(self, value: str) -> None:
        self.statuses.append(value)

    def set_meta(self, **fields: Any) -> None:
        pass

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)

    def is_cancel_requested(self) -> bool:
        return self._cancel

    def spawn_child(
        self,
        job_type: str,
        params: dict[str, Any],
        *,
        model: str | None = None,
        executor: str = "claude_inproc",
        idem_key: str | None = None,
    ) -> int:
        self._next_child += 1
        self.spawned.append(
            {
                "id": self._next_child,
                "job_type": job_type,
                "params": params,
                "model": model,
                "executor": executor,
                "idem_key": idem_key,
            }
        )
        return self._next_child


def _triage_state(**over: Any) -> dict[str, Any]:
    """A plausible post-plan checkpoint, overridable per test."""
    now = time.time()
    state: dict[str, Any] = {
        "phase": "triage",
        "child_job_ids": [901, 902],
        "started_ts": now - 10,
        "deadline_ts": now + 600,
        "slice_count": 1,
        "pool": {
            "pA~0": {"rank": 0, "paper": "pA"},
            "pB~1": {"rank": 1, "paper": "pB"},
        },
        "considered": 2,
        "want": "chunks",
    }
    state.update(over)
    return state


def _meta_for(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_type": "good_search",
        "executor": "coordinator",
        "params": {"q": "x"},
        "coordinator_state": state,
    }


# ── phase: plan ─────────────────────────────────────────────────────


class TestPhasePlan:
    def test_plan_yields_triage_with_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "_heartbeat_s", lambda: 60)
        store = FakeStore(hits=[_hit("pA", 0), _hit("pB", 3)])
        ctx = FakeCtx(
            store,
            {
                "params": {
                    "q": "nitrate ammonia",
                    "queries": ["copper selectivity"],
                    "answers": ["Cu sites raise NH3 efficiency."],
                    "context": "electrocatalysis review",
                    "model": "haiku",
                }
            },
        )
        before = time.time()
        out = gs._dispatch(ctx, gs.SPEC)

        assert isinstance(out, Yield)
        assert out.wake_when.kind == "at_time"
        assert before + 58 <= out.wake_when.payload["ts"] <= time.time() + 62

        state = out.state
        assert state["phase"] == "triage"
        assert state["child_job_ids"] == [s["id"] for s in ctx.spawned]
        assert state["slice_count"] == 1
        assert state["deadline_ts"] > state["started_ts"]
        assert state["considered"] == 2
        assert state["pool"]["pA~0"]["rank"] == 0
        assert state["pool"]["pB~3"]["rank"] == 1

        # Fusion legs: q + queries + answers, lexical-only.
        call = store.multi_calls[0]
        assert call["q_texts"] == [
            "nitrate ammonia",
            "copper selectivity",
            "Cu sites raise NH3 efficiency.",
        ]
        assert call["query_vecs"] == []
        assert call["mode"] == "lexical"
        assert call["kind"] == "paper"

        # One batch (2 candidates < batch size) → one triage child.
        assert len(ctx.spawned) == 1
        child = ctx.spawned[0]
        assert child["job_type"] == "good_search_triage"
        assert child["model"] == "haiku"
        assert child["executor"] == "claude_inproc"
        assert child["idem_key"] == "good_search:500:triage:0"
        p = child["params"]
        assert p["q"] == "nitrate ammonia"
        assert p["context"] == "electrocatalysis review"
        assert [c["handle"] for c in p["candidates"]] == ["pA~0", "pB~3"]

    def test_plan_empty_pool_is_immediate_done(self) -> None:
        ctx = FakeCtx(FakeStore(hits=[]), {"params": {"q": "xyzzy"}})
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert "broaden" in out.summary
        assert out.summary_meta["result"]["considered"] == 0
        assert out.summary_meta["result"]["note"] == "no candidates; broaden q/queries"
        assert ctx.spawned == []

    def test_plan_missing_q_fails(self) -> None:
        ctx = FakeCtx(FakeStore(), {"params": {}})
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is False

    def test_plan_batches_and_caps_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "_triage_batch", lambda: 30)
        hits = [_hit(f"p{i}", i) for i in range(65)]
        # Default cap (4) → ceil(65/30) = 3 children covering all 65.
        ctx = FakeCtx(FakeStore(hits=hits), {"params": {"q": "x"}})
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Yield)
        assert len(ctx.spawned) == 3
        assert sum(len(s["params"]["candidates"]) for s in ctx.spawned) == 65

        # Explicit max_children=2 truncates the fan-out (tail dropped)
        # while ``considered`` stays honest.
        ctx2 = FakeCtx(FakeStore(hits=hits), {"params": {"q": "x", "max_children": 2}})
        out2 = gs._dispatch(ctx2, gs.SPEC)
        assert isinstance(out2, Yield)
        assert len(ctx2.spawned) == 2
        assert sum(len(s["params"]["candidates"]) for s in ctx2.spawned) == 60
        assert out2.state["considered"] == 65


# ── phase: triage (heartbeat wakes) ─────────────────────────────────


class TestPhaseTriage:
    def test_heartbeat_reyields_while_children_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "_heartbeat_s", lambda: 60)
        monkeypatch.setattr(
            gs, "_child_states", lambda s, ids: {901: "succeeded", 902: "running"}
        )
        ctx = FakeCtx(FakeStore(), _meta_for(_triage_state()))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Yield)
        assert out.wake_when.kind == "at_time"
        assert out.state["phase"] == "triage"
        assert out.state["slice_count"] == 2

    def test_gather_on_all_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            gs, "_child_states", lambda s, ids: {901: "succeeded", 902: "succeeded"}
        )
        blocks = {
            901: [
                _result_block(
                    [
                        {
                            "candidate_handle": "pA~0",
                            "keep": True,
                            "relevance": 0.4,
                            "why": "somewhat relevant",
                        },
                        {  # unknown handle — dropped at gather too
                            "candidate_handle": "zz~9",
                            "keep": True,
                            "relevance": 0.9,
                            "why": "hallucinated",
                        },
                    ]
                )
            ],
            902: [
                _result_block(
                    [
                        {
                            "candidate_handle": "pB~1",
                            "keep": True,
                            "relevance": 5.0,  # clamped to 1.0
                            "why": "spot on",
                            "best_quote": "a verbatim quote",
                        }
                    ]
                )
            ],
        }
        ctx = FakeCtx(FakeStore(blocks=blocks), _meta_for(_triage_state()))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        result = out.summary_meta["result"]
        assert result["children"] == 2
        assert result["children_failed"] == 0
        assert result["timed_out"] is False
        assert result["partial"] is False
        assert result["considered"] == 2
        assert result["kept"] == 2
        # pB~1: relevance clamped to 1.0; despite rank 1 it outranks
        # pA~0 (rel 0.4 at rank 0) under relevance × rank-signal.
        assert [c["handle"] for c in result["chunks"]] == ["pB~1", "pA~0"]
        assert result["chunks"][0]["relevance"] == 1.0
        assert result["chunks"][0]["best_quote"] == "a verbatim quote"
        assert "pB~1" in out.summary

    def test_deadline_forces_timed_out_gather(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gs, "_child_states", lambda s, ids: {901: "succeeded", 902: "running"}
        )
        blocks = {
            901: [
                _result_block(
                    [
                        {
                            "candidate_handle": "pA~0",
                            "keep": True,
                            "relevance": 0.8,
                            "why": "good",
                        }
                    ]
                )
            ]
        }
        state = _triage_state(deadline_ts=time.time() - 5)
        ctx = FakeCtx(FakeStore(blocks=blocks), _meta_for(state))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is True
        assert out.summary_meta["timed_out"] is True
        result = out.summary_meta["result"]
        assert result["timed_out"] is True
        assert result["partial"] is True
        # The still-running child counts as a dropped batch.
        assert result["children_failed"] == 1
        # The finished child's verdicts still land.
        assert result["kept"] == 1
        assert result["chunks"][0]["handle"] == "pA~0"

    def test_slice_cap_forces_gather(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gs, "_max_slices", lambda: 3)
        monkeypatch.setattr(gs, "_child_states", lambda s, ids: {901: "running"})
        state = _triage_state(child_job_ids=[901], slice_count=2)
        ctx = FakeCtx(FakeStore(), _meta_for(state))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.summary_meta["result"]["timed_out"] is True

    def test_soft_deleted_child_counts_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child absent from ``_child_states`` (soft-deleted) doesn't
        park the campaign — wake_runner semantics."""
        monkeypatch.setattr(gs, "_child_states", lambda s, ids: {901: "succeeded"})
        blocks = {
            901: [
                _result_block(
                    [
                        {
                            "candidate_handle": "pA~0",
                            "keep": True,
                            "relevance": 0.7,
                            "why": "ok",
                        }
                    ]
                )
            ]
        }
        ctx = FakeCtx(FakeStore(blocks=blocks), _meta_for(_triage_state()))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        # 902 is gone → treated terminal-but-not-succeeded → failed count.
        assert out.summary_meta["result"]["children_failed"] == 1
        assert out.summary_meta["result"]["kept"] == 1

    def test_all_children_failed_is_done_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gs, "_child_states", lambda s, ids: {901: "failed", 902: "failed"}
        )
        ctx = FakeCtx(FakeStore(), _meta_for(_triage_state()))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is False
        assert "all" in out.summary and "failed" in out.summary
        assert out.summary_meta["result"]["children_failed"] == 2

    def test_cancel_requested_terminates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            gs, "_child_states", lambda s, ids: {901: "running", 902: "running"}
        )
        ctx = FakeCtx(FakeStore(), _meta_for(_triage_state()), cancel=True)
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is False
        assert "cancel" in out.summary.lower()
        assert out.summary_meta["cancelled"] is True

    def test_unknown_phase_fails(self) -> None:
        ctx = FakeCtx(FakeStore(), _meta_for({"phase": "verify"}))
        out = gs._dispatch(ctx, gs.SPEC)
        assert isinstance(out, Done)
        assert out.success is False


# ── triage child (stub claude binary) ───────────────────────────────


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _json_stub(path: Path, payload: dict[str, Any]) -> None:
    """Stub that prints ``payload`` as JSON regardless of the prompt."""
    _write_stub(
        path,
        "#!/usr/bin/env bash\ncat <<'EOF'\n" + json.dumps(payload) + "\nEOF\n",
    )


_TRIAGE_META = {
    "job_type": "good_search_triage",
    "executor": "claude_inproc",
    "params": {
        "q": "nitrate ammonia",
        "context": "",
        "want": "chunks",
        "candidates": [
            {"handle": "pA~0", "text": "nitrate to ammonia on Cu", "paper": "pA"},
            {"handle": "pB~1", "text": "hydrogen evolution", "paper": "pB"},
        ],
    },
}


class TestTriageChild:
    def test_stub_verdicts_written_as_job_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = tmp_path / "claude_stub.sh"
        _json_stub(
            stub,
            {
                "verdicts": [
                    {
                        "candidate_handle": "pA~0",
                        "keep": True,
                        "relevance": 1.5,  # clamped
                        "why": "direct hit",
                        "best_quote": "nitrate to ammonia on Cu",
                    },
                    {  # unknown handle — dropped
                        "candidate_handle": "zz~7",
                        "keep": True,
                        "relevance": 0.9,
                        "why": "hallucinated",
                    },
                    {
                        "candidate_handle": "pB~1",
                        "keep": False,
                        "relevance": 0.1,
                        "why": "off topic",
                    },
                ]
            },
        )
        monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(stub))

        ctx = FakeCtx(FakeStore(), dict(_TRIAGE_META))
        gs._triage_dispatch(ctx, gs.TRIAGE_SPEC)

        assert ctx.failures == []
        results = [t for k, t in ctx.chunks if k == "job_result"]
        assert len(results) == 1
        verdicts = json.loads(results[0])["verdicts"]
        assert [v["candidate_handle"] for v in verdicts] == ["pA~0", "pB~1"]
        assert verdicts[0]["relevance"] == 1.0  # clamped
        assert verdicts[0]["keep"] is True
        assert verdicts[1]["keep"] is False
        summaries = [t for k, t in ctx.chunks if k == "job_summary"]
        assert summaries and "kept 1" in summaries[0]

    def test_malformed_json_retries_then_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = tmp_path / "claude_stub.sh"
        count = tmp_path / "calls"
        _write_stub(
            stub,
            f"#!/usr/bin/env bash\necho x >> {count}\necho 'no json here at all'\n",
        )
        monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(stub))

        ctx = FakeCtx(FakeStore(), dict(_TRIAGE_META))
        gs._triage_dispatch(ctx, gs.TRIAGE_SPEC)

        assert len(ctx.failures) == 1
        assert "after retry" in ctx.failures[0]
        assert count.read_text().count("x") == 2  # exactly one retry
        assert [t for k, t in ctx.chunks if k == "job_result"] == []

    def test_malformed_then_valid_recovers_on_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = tmp_path / "claude_stub.sh"
        count = tmp_path / "calls"
        good = json.dumps(
            {
                "verdicts": [
                    {
                        "candidate_handle": "pA~0",
                        "keep": True,
                        "relevance": 0.9,
                        "why": "yes",
                    }
                ]
            }
        )
        _write_stub(
            stub,
            "#!/usr/bin/env bash\n"
            f"echo x >> {count}\n"
            f"if [ $(wc -l < {count}) -ge 2 ]; then\n"
            f"cat <<'EOF'\n{good}\nEOF\n"
            "else\n"
            "echo garbage\n"
            "fi\n",
        )
        monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(stub))

        ctx = FakeCtx(FakeStore(), dict(_TRIAGE_META))
        gs._triage_dispatch(ctx, gs.TRIAGE_SPEC)

        assert ctx.failures == []
        results = [t for k, t in ctx.chunks if k == "job_result"]
        assert len(results) == 1
        assert json.loads(results[0])["verdicts"][0]["candidate_handle"] == "pA~0"

    def test_missing_candidates_fails_fast(self) -> None:
        meta = {**_TRIAGE_META, "params": {"q": "x", "candidates": []}}
        ctx = FakeCtx(FakeStore(), meta)
        gs._triage_dispatch(ctx, gs.TRIAGE_SPEC)
        assert len(ctx.failures) == 1


# ── surface: search(kind='paper', good=True) ───────────────────────


def _paper_handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.paper import PaperHandler

    return PaperHandler(hub=Hub(store=store))


def _job_row(store: Any, job_id: int) -> tuple[int | None, dict[str, Any]]:
    with store.pool.connection() as conn:
        r = conn.execute(
            "SELECT parent_id, meta FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    assert r is not None
    return (int(r[0]) if r[0] is not None else None, dict(r[1] or {}))


def _job_id_from(body: str) -> int:
    m = re.search(r"\bjob=(\d+)", body)
    assert m is not None, body
    return int(m.group(1))


class TestGoodSearchSurface:
    def test_good_returns_async_handle_without_searching(self, store: Any) -> None:
        h = _paper_handler(store)
        resp = h.search(q="oxygen evolution on NiFe", good=True)
        body = resp.body
        assert "deep search queued: job=" in body
        assert "status=queued" in body
        assert "poll: get(kind='job', id=" in body
        assert "block hit" not in body  # no inline search ran

        job_id = _job_id_from(body)
        parent_id, meta = _job_row(store, job_id)
        assert meta["job_type"] == "good_search"
        assert meta["executor"] == "coordinator"
        assert meta["params"]["q"] == "oxygen evolution on NiFe"
        assert str(meta["idem_key"]).startswith("good_search:")
        assert "STATUS:queued" in {str(t) for t in store.tags_for(job_id)}

        # Ephemeral parent todo with the auto-close check.
        assert parent_id is not None
        with store.pool.connection() as conn:
            r = conn.execute(
                "SELECT kind, title, meta FROM refs WHERE ref_id = %s", (parent_id,)
            ).fetchone()
        assert r[0] == "todo"
        assert r[1].startswith("deep search: oxygen evolution")
        assert r[2]["auto_check"] == {"type": "child_job_succeeded"}
        assert "ephemeral" in {str(t) for t in store.tags_for(parent_id)}

    def test_second_identical_call_reuses_inflight_campaign(self, store: Any) -> None:
        h = _paper_handler(store)
        first = h.search(q="same question", good=True)
        second = h.search(q="same question", good=True)
        assert "already in flight" in second.body
        assert _job_id_from(first.body) == _job_id_from(second.body)
        # No duplicate campaign row, no second ephemeral todo.
        with store.pool.connection() as conn:
            n_jobs = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='job' "
                "AND meta->>'job_type'='good_search' AND deleted_at IS NULL"
            ).fetchone()[0]
            n_todos = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='todo' "
                "AND title LIKE 'deep search:%' AND deleted_at IS NULL"
            ).fetchone()[0]
        assert n_jobs == 1
        assert n_todos == 1

    def test_long_q_truncates_todo_title(self, store: Any) -> None:
        h = _paper_handler(store)
        q = "very " * 60 + "long question"
        resp = h.search(q=q, good=True)
        parent_id, _ = _job_row(store, _job_id_from(resp.body))
        with store.pool.connection() as conn:
            title = conn.execute(
                "SELECT title FROM refs WHERE ref_id = %s", (parent_id,)
            ).fetchone()[0]
        assert len(title) <= 96
        assert title.endswith("…")

    def test_cap_exceeded_is_bad_input(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.errors import BadInput

        monkeypatch.setenv("PRECIS_GOOD_SEARCH_MAX_CONCURRENT", "1")
        h = _paper_handler(store)
        h.search(q="first question", good=True)
        with pytest.raises(BadInput, match="in flight"):
            h.search(q="second question", good=True)

    def test_good_on_cfp_kind_rejected(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.errors import BadInput
        from precis.handlers.cfp import CfpHandler

        h = CfpHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="paper-only"):
            h.search(q="x", good=True)

    def test_good_on_non_paper_kind_rejected_at_mcp_boundary(
        self, runtime_with_store: Any
    ) -> None:
        from precis.tools import core as tools_core

        saved = tools_core._runtime
        tools_core._runtime = runtime_with_store
        try:
            # Declared ``-> str`` but validation paths return a
            # ``CallToolResult`` — keep mypy out of the narrowing.
            out: Any = tools_core.search(q="x", kind="memory", good=True)
        finally:
            tools_core._runtime = saved
        # Pre-dispatch validation → CallToolResult with isError=True.
        assert getattr(out, "isError", False) is True
        text = "".join(c.text for c in out.content)
        assert "paper-only" in text
        assert "BadInput" in text


# ── end-to-end: submit → coordinator → children → wake → Done ──────

_E2E_BLOCKS_A = [
    "Single-atom copper boosts nitrate to ammonia selectivity.",
    "Hydrogen evolution competes with nitrate to ammonia pathways.",
]
_E2E_BLOCKS_B = [
    "Isolated Cu sites raise nitrate to ammonia faradaic efficiency.",
]

#: Python stub judge: pulls every ``handle=<h>`` out of the prompt and
#: keeps them all at relevance 0.9.
_E2E_STUB = """\
#!/usr/bin/env python3
import json, re, sys
prompt = sys.argv[2] if len(sys.argv) > 2 else ""
handles = re.findall(r"handle=(\\S+)", prompt)
verdicts = [
    {"candidate_handle": h, "keep": True, "relevance": 0.9, "why": "on point"}
    for h in handles
]
print(json.dumps({"verdicts": verdicts}))
"""


def _seed_paper(store: Any, slug: str, blocks: list[str]) -> None:
    from precis.store import BlockInsert

    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    store.insert_blocks(
        ref.id, [BlockInsert(pos=i, text=t) for i, t in enumerate(blocks)]
    )


def _expire_lease(store: Any, ref_id: int) -> None:
    """Coordinator claim requires an expired lease; wake_runner's
    re-queue leaves the 5-min slice lease in place, so the test (unlike
    prod, which just waits) expires it by hand."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'lease_until', (now() - interval '1 minute')::text) "
            "WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()


class TestEndToEnd:
    def test_campaign_runs_to_done(
        self, store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.executors.claude_inproc import run_claude_inproc_pass
        from precis.workers.executors.coordinator import run_coordinator_pass
        from precis.workers.wake_runner import run_wake_pass

        _seed_paper(store, "e2eA", _E2E_BLOCKS_A)
        _seed_paper(store, "e2eB", _E2E_BLOCKS_B)

        stub = tmp_path / "claude_stub.py"
        _write_stub(stub, _E2E_STUB)
        monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(stub))
        # Fire the heartbeat immediately so the wake pass re-queues
        # without waiting out the real 3-min cadence.
        monkeypatch.setattr(gs, "_heartbeat_s", lambda: 0)

        h = _paper_handler(store)
        body = h.search(q="nitrate ammonia", good=True).body
        job_id = _job_id_from(body)

        # Slice 1: plan → children spawned, campaign parked waiting_time.
        r1 = run_coordinator_pass(store)
        assert (r1["claimed"], r1["ok"]) == (1, 1)
        tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:waiting_time" in tags

        # Children run under claude_inproc against the stub judge.
        r2 = run_claude_inproc_pass(store, limit=8)
        assert r2["claimed"] >= 1
        assert r2["failed"] == 0

        # Wake: at_time fired → campaign re-queued.
        r3 = run_wake_pass(store)
        assert r3["ok"] >= 1
        assert "STATUS:queued" in {str(t) for t in store.tags_for(job_id)}

        # Slice 2: triage → gather → Done.
        _expire_lease(store, job_id)
        r4 = run_coordinator_pass(store)
        assert (r4["claimed"], r4["ok"]) == (1, 1)

        tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in tags
        _, meta = _job_row(store, job_id)
        result = meta["result"]
        assert result["kept"] >= 1
        assert result["children"] >= 1
        assert result["children_failed"] == 0
        assert result["timed_out"] is False
        # The merged verdict is on the job as a job_summary chunk.
        blocks = store.list_blocks_for_ref(job_id)
        summaries = [b.text for b in blocks if b.chunk_kind == "job_summary"]
        assert summaries and "deep search: kept" in summaries[-1]
