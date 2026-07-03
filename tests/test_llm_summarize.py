"""Offline tests for the LLM chunk-summarization pass.

No DB, no network: a fake :class:`Transport` returns canned
completions and a fake store/connection records writes. Covers prompt
assembly (stable-prefix-first), the tolerant parser, the OpenAI client
wire, env config, and the end-to-end pass.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import pytest

from precis.workers.llm_summarize import (
    HOT_WINDOW_MIN,
    MAX_CHUNK_CHARS,
    MAX_SUMMARIZE_ATTEMPTS,
    SUMMARY_LEASE_COOLDOWN_MIN,
    SUMMARY_LEASE_GC_MIN,
    LlmClient,
    LlmConfig,
    _Claimed,
    _mark_failed,
    build_messages,
    claim_chunks_without_summary,
    parse_summary,
    run_llm_summarize_pass,
    write_chunk_summary,
)

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self, content: str, *, total_tokens: int | None = 42) -> None:
        self.content = content
        self.total_tokens = total_tokens
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        usage = (
            {"total_tokens": self.total_tokens} if self.total_tokens is not None else {}
        )
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": usage,
        }


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Fake connection modelling the lease table faithfully enough for the
    pass: claim rows seed a ``leases`` dict (chunk_id → attempts), the
    ``_mark_failed`` attempts bump and lease-release DELETE mutate it, and
    phase-2 ``claimed_at`` heartbeats are counted."""

    def __init__(self, claim_rows: list[tuple[Any, ...]], card_text: str) -> None:
        self.claim_rows = claim_rows
        self.card_text = card_text
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.leases: dict[int, int] = {}  # chunk_id -> attempts
        self.heartbeats = 0
        self._claim_calls = 0

    def execute(self, sql: str, params: Any = None) -> _Result:
        flat = " ".join(sql.split())
        p: tuple[Any, ...] = tuple(params) if params else ()
        if "FOR UPDATE OF c SKIP LOCKED" in flat:
            # claim_chunks_without_summary runs four fresh tiers (draft, conv,
            # hot, rest); serve the seeded rows on the first so totals match
            # production. Claiming inserts the lease (attempts = 0).
            self._claim_calls += 1
            rows = self.claim_rows if self._claim_calls == 1 else []
            for r in rows:
                self.leases.setdefault(int(r[0]), 0)
            return _Result(rows)
        if "FOR UPDATE OF cl SKIP LOCKED" in flat:
            return _Result([])  # reclaim: nothing stale in the fakes
        if "card_combined" in flat:
            return _Result([(self.card_text,)] if self.card_text else [])
        if "INSERT INTO chunk_summaries" in flat:
            self.writes.append((flat, tuple(p)))
            return _Result([])
        if "SET attempts = attempts + 1" in flat:  # _mark_failed lease bump
            cid = int(p[0])
            if cid not in self.leases:
                return _Result([])  # lease gone — reaped by a sibling
            self.leases[cid] += 1
            return _Result([(self.leases[cid],)])
        if "UPDATE chunk_claims SET claimed_at = now() WHERE artifact" in flat:
            self.heartbeats += 1  # phase-2 lease heartbeat
            return _Result([])
        if "DELETE FROM chunk_claims WHERE chunk_id" in flat:
            self.leases.pop(int(p[0]), None)
            return _Result([])
        return _Result([])


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def connection(self) -> Any:
        yield self._conn


class _FakeStore:
    def __init__(self, conn: _FakeConn) -> None:
        self.pool = _FakePool(conn)


def _claim_row(chunk_id: int = 1, ref_id: int = 7) -> tuple[Any, ...]:
    # Order matches claim_chunks_without_summary's SELECT.
    return (
        chunk_id,
        ref_id,
        0,
        "paragraph",
        "We synthesized MOF-5 and measured a band gap of 3.5 eV.",
        ["Results"],
        ["mof-5", "band gap"],
        ["3.5 eV"],
        "paper",
        "A study of MOF-5",
    )


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------


def test_build_messages_stable_prefix_first() -> None:
    claim = _Claimed(*_claim_row())
    msgs = build_messages(claim, doc_card="Title: A study of MOF-5\nAbstract: ...")
    assert [m["role"] for m in msgs] == ["system", "user"]
    system, user = msgs[0]["content"], msgs[1]["content"]
    # Stable content (instructions + doc header) is in the system turn.
    assert "BRIEF:" in system and "DETAIL:" in system
    assert "A study of MOF-5" in system
    # Per-chunk volatile content (passage + section/keywords/numerics) last.
    assert "Section: Results" in user
    assert "3.5 eV" in user
    assert "band gap of 3.5 eV" in user


def test_build_messages_falls_back_to_title_without_card() -> None:
    claim = _Claimed(*_claim_row())
    msgs = build_messages(claim, doc_card="")
    assert "Title: A study of MOF-5" in msgs[0]["content"]


def test_build_messages_detail_is_additive_contract() -> None:
    """The prompt instructs DETAIL to add only what BRIEF omits and never
    repeat it — the no-duplication contract the reader relies on (BRIEF
    alone, or BRIEF+DETAIL, never DETAIL alone)."""
    system = build_messages(_Claimed(*_claim_row()), doc_card="")[0]["content"]
    assert "appended to BRIEF" in system
    assert "never repeat" in system
    assert "NOT already in BRIEF" in system


def test_build_messages_non_prose_tag_rule() -> None:
    """Non-prose chunks (tables, coordinate dumps, copyright/masthead) get a
    short parenthetical tag instead of a hallucinated summary."""
    system = build_messages(_Claimed(*_claim_row()), doc_card="")[0]["content"]
    assert "set BRIEF to a short" in system  # the rule
    assert "(tabular data)" in system  # demonstrated by example
    assert "(publication metadata)" in system


def test_build_messages_instruction_prefix_is_kind_independent() -> None:
    """The doc kind lives in the per-doc context line, not the instruction
    block — so the cache-hot prefix is byte-identical across paper/patent/conv
    and stays warm on a llama.cpp slot across document switches."""
    paper = _Claimed(*_claim_row())  # ref_kind = "paper"
    conv_fields = list(_claim_row())
    conv_fields[8] = "conv"  # the ref_kind slot
    conv = _Claimed(*conv_fields)

    sys_paper = build_messages(paper, doc_card="")[0]["content"]
    sys_conv = build_messages(conv, doc_card="")[0]["content"]

    marker = "--- Document for context"
    # Everything before the per-doc context divider is identical.
    assert sys_paper.split(marker)[0] == sys_conv.split(marker)[0]
    # The kind appears only in the per-doc context line, after the divider.
    assert "(a scientific paper;" in sys_paper
    assert "(a conversation;" in sys_conv


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def test_parse_summary_labeled() -> None:
    out = parse_summary(
        "BRIEF: synthesis of MOF-5\nDETAIL: band gap 3.5 eV, measured optically."
    )
    assert out == "synthesis of MOF-5\n\nband gap 3.5 eV, measured optically."


def test_parse_summary_multiline_detail() -> None:
    out = parse_summary("BRIEF: a\nDETAIL: one.\ntwo.")
    assert out == "a\n\none. two."


def test_parse_summary_unlabeled_kept_as_brief() -> None:
    assert parse_summary("just a plain summary") == "just a plain summary"


def test_parse_summary_detail_only_promotes_first_sentence() -> None:
    out = parse_summary("DETAIL: first thing. second thing.")
    assert out.startswith("first thing")


def test_parse_summary_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_summary("   ")


def test_parse_summary_sanitizes_nul_and_lone_surrogate() -> None:
    """NUL bytes (psycopg rejects them in text params) and lone UTF-16
    surrogates (fail UTF-8 encoding at send time) are scrubbed from model
    output — one poison character used to abort the whole batch write."""
    out = parse_summary("BRIEF: a\x00b\nDETAIL: c\ud800d.")
    assert "\x00" not in out
    out.encode("utf-8")  # no lone surrogate left — encodable as sent to PG
    assert out == "ab\n\nc?d."


def test_parse_summary_all_nul_raises_not_blank() -> None:
    """Output that is nothing but poison bytes sanitizes to empty and raises
    (→ _mark_failed) rather than storing a blank summary."""
    with pytest.raises(ValueError):
        parse_summary("\x00\x00")


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


def test_client_complete_parses_choice_and_usage() -> None:
    t = _FakeTransport("BRIEF: x\nDETAIL: y.", total_tokens=99)
    client = LlmClient(LlmConfig(model="summarizer"), transport=t)
    res = client.complete([{"role": "user", "content": "hi"}])
    assert res.text == "BRIEF: x\nDETAIL: y."
    assert res.total_tokens == 99
    # Wire: posts to /chat/completions with the configured model.
    assert t.calls[0]["url"].endswith("/v1/chat/completions")
    assert t.calls[0]["payload"]["model"] == "summarizer"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_config_from_env_overrides() -> None:
    cfg = LlmConfig.from_env(
        {
            "PRECIS_SUMMARIZE_LLM": "1",
            "PRECIS_SUMMARIZE_MODEL": "reasoner-alt",
            "PRECIS_SUMMARIZE_MAX_TOKENS": "300",
        }
    )
    assert cfg.enabled is True
    assert cfg.model == "reasoner-alt"
    assert cfg.max_tokens == 300


def test_config_default_disabled() -> None:
    assert LlmConfig.from_env({}).enabled is False


def test_config_concurrency_from_env() -> None:
    assert LlmConfig.from_env({}).concurrency == 1  # default
    assert LlmConfig.from_env({"PRECIS_SUMMARIZE_CONCURRENCY": "3"}).concurrency == 3
    # Floors at 1 — 0 / negative would make the thread pool meaningless.
    assert LlmConfig.from_env({"PRECIS_SUMMARIZE_CONCURRENCY": "0"}).concurrency == 1


# --------------------------------------------------------------------------
# end-to-end pass (offline)
# --------------------------------------------------------------------------


def test_run_pass_writes_summary() -> None:
    conn = _FakeConn(
        claim_rows=[_claim_row(chunk_id=1), _claim_row(chunk_id=2)],
        card_text="Title: A study of MOF-5",
    )
    store = _FakeStore(conn)
    client = LlmClient(LlmConfig(), transport=_FakeTransport("BRIEF: g\nDETAIL: d."))

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 2, "ok": 2, "failed": 0}
    assert len(conn.writes) == 2
    # Each write carries the parsed two-part text + the summarizer name.
    for _sql, params in conn.writes:
        assert params[1] == "llm-v1"  # summarizer
        assert params[2] == "g\n\nd."  # text


def test_run_pass_marks_failed_on_transport_error() -> None:
    """A sub-cap failure bumps the lease's attempts and keeps the lease
    (backoff via the cooldown reaper) — no chunk_summaries row is written."""

    class _Boom:
        def post_json(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("backend down")

    conn = _FakeConn(claim_rows=[_claim_row(chunk_id=1)], card_text="")
    store = _FakeStore(conn)
    client = LlmClient(LlmConfig(), transport=_Boom())

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert conn.leases == {1: 1}  # attempts bumped, lease kept for retry
    assert conn.writes == []  # sub-cap: nothing written to chunk_summaries


def test_mark_failed_lease_gone_writes_nothing() -> None:
    """Stale-worker clobber regression: when the lease is GONE a sibling
    already reaped it and reached a terminal state (usually a fresh 'ok'
    summary), so a late failure report must write NOTHING — the old code
    upserted a terminal 'failed' marker over the sibling's 'ok' row."""
    conn = _FakeConn(claim_rows=[], card_text="")  # no lease for chunk 1
    _mark_failed(conn, 1, summarizer="llm-v1", error="late failure")
    assert conn.writes == []


def test_mark_failed_at_cap_writes_guarded_terminal_marker() -> None:
    """At the attempts cap the failure goes terminal: a 'failed' marker is
    upserted (guarded so it can never overwrite an 'ok' row) and the lease
    is released."""
    conn = _FakeConn(claim_rows=[], card_text="")
    conn.leases[5] = MAX_SUMMARIZE_ATTEMPTS - 1  # one failure short of the cap
    _mark_failed(conn, 5, summarizer="llm-v1", error="boom")
    assert len(conn.writes) == 1
    sql, params = conn.writes[0]
    assert "'failed'" in sql
    # Belt-and-braces guard: the terminal upsert never clobbers an 'ok' row.
    assert "chunk_summaries.status <> 'ok'" in sql
    assert params[:2] == (5, "llm-v1")
    assert 5 not in conn.leases  # terminal → lease released


# --------------------------------------------------------------------------
# concurrency (thread-pooled completion phase)
# --------------------------------------------------------------------------


def test_run_pass_concurrent_summarizes_all() -> None:
    """concurrency>1 completes every chunk; one HTTP call per chunk."""
    rows = [_claim_row(chunk_id=i, ref_id=7) for i in range(1, 6)]
    conn = _FakeConn(claim_rows=rows, card_text="Title: A study of MOF-5")
    store = _FakeStore(conn)
    t = _FakeTransport("BRIEF: g\nDETAIL: d.")
    client = LlmClient(LlmConfig(), transport=t)

    result = run_llm_summarize_pass(store, client=client, batch_size=10, concurrency=3)

    assert result == {"claimed": 5, "ok": 5, "failed": 0}
    assert len(conn.writes) == 5
    assert len(t.calls) == 5  # exactly one completion per chunk


def test_run_pass_concurrent_isolates_failure() -> None:
    """A poison chunk fails on its own; its concurrent siblings still succeed."""

    class _Selective:
        """Raises only for the passage carrying the POISON marker."""

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.n = 0

        def post_json(
            self,
            url: str,
            payload: dict[str, Any],
            *,
            headers: dict[str, str],
            timeout: float,
        ) -> dict[str, Any]:
            with self.lock:
                self.n += 1
            if "POISON" in payload["messages"][-1]["content"]:
                raise RuntimeError("backend rejected this passage")
            return {
                "choices": [{"message": {"content": "BRIEF: g\nDETAIL: d."}}],
                "usage": {"total_tokens": 5},
            }

    good = _claim_row(chunk_id=1, ref_id=7)
    poison: tuple = (
        2,
        7,
        1,
        "paragraph",
        "POISON passage long enough to clear the min-chars gate for a summary.",
        ["Results"],
        ["k"],
        [],
        "paper",
        "A study of MOF-5",
    )
    conn = _FakeConn(claim_rows=[good, poison], card_text="Title: x")
    store = _FakeStore(conn)
    client = LlmClient(LlmConfig(), transport=_Selective())

    result = run_llm_summarize_pass(store, client=client, batch_size=10, concurrency=2)

    assert result == {"claimed": 2, "ok": 1, "failed": 1}
    # One successful insert; the sub-cap failure stays on the lease.
    assert sum(1 for sql, _ in conn.writes if "'failed'" in sql) == 0
    assert sum(1 for sql, _ in conn.writes if "'failed'" not in sql) == 1
    assert conn.leases == {2: 1}  # poison keeps its lease; good one released


def test_run_pass_sanitizes_nul_from_model_output() -> None:
    """Model output carrying a NUL byte is written sanitized, not failed."""
    conn = _FakeConn(claim_rows=[_claim_row(chunk_id=1)], card_text="Title: x")
    store = _FakeStore(conn)
    client = LlmClient(
        LlmConfig(), transport=_FakeTransport("BRIEF: g\x00lo\nDETAIL: d\x00.")
    )

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    (_sql, params), *_ = conn.writes
    assert params[2] == "glo\n\nd."
    assert "\x00" not in params[2]


def test_run_pass_heartbeats_leases_after_each_completion() -> None:
    """Fix for cooldown < worst-case batch wall-time: the main thread bumps
    ``claimed_at`` on the batch's leases after every per-chunk LLM completion,
    so a live worker's leases never age past the cooldown no matter how long
    the batch runs (sequential and thread-pooled paths alike)."""
    rows = [_claim_row(chunk_id=i, ref_id=7) for i in (1, 2, 3)]

    conn_seq = _FakeConn(claim_rows=list(rows), card_text="Title: x")
    client = LlmClient(LlmConfig(), transport=_FakeTransport("BRIEF: g\nDETAIL: d."))
    run_llm_summarize_pass(_FakeStore(conn_seq), client=client, batch_size=10)
    assert conn_seq.heartbeats == 3  # one per completed chunk

    conn_par = _FakeConn(claim_rows=list(rows), card_text="Title: x")
    run_llm_summarize_pass(
        _FakeStore(conn_par), client=client, batch_size=10, concurrency=2
    )
    assert conn_par.heartbeats == 3


def test_run_pass_poison_write_does_not_lose_siblings() -> None:
    """Phase-3 isolation: a per-row DB write error fails only its own chunk —
    the sibling's summary still lands and the poison chunk's lease attempts
    increment via the normal _mark_failed path (so the retry cap engages)."""

    class _PoisonWriteConn(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _Result:
            flat = " ".join(sql.split())
            if (
                "INSERT INTO chunk_summaries" in flat
                and "'failed'" not in flat
                and params
                and params[0] == 2
            ):
                raise RuntimeError("db rejected row")  # e.g. DataError
            return super().execute(sql, params)

    rows = [_claim_row(chunk_id=1), _claim_row(chunk_id=2)]
    conn = _PoisonWriteConn(claim_rows=rows, card_text="Title: x")
    store = _FakeStore(conn)
    client = LlmClient(LlmConfig(), transport=_FakeTransport("BRIEF: g\nDETAIL: d."))

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 2, "ok": 1, "failed": 1}
    # The sibling's summary was recorded despite the poison row.
    assert [params[0] for _sql, params in conn.writes] == [1]
    # The poison chunk went through _mark_failed: attempts bumped, lease kept.
    assert conn.leases == {2: 1}


# --------------------------------------------------------------------------
# claim filters (real PG — the FakeConn above doesn't parse SQL)
# --------------------------------------------------------------------------


def test_claim_skips_oversized_chunks(store: Any) -> None:
    """The claim filters cheaply in SQL — over-long mis-chunks are excluded.
    Numeric dumps are NOT filtered here (the digit-fraction regexp made the
    claim ~74s/batch); they're claimed and tagged in the pass instead."""
    from tests.workers._helpers import seed_chunks

    prose = (
        "We synthesized a molecular catalyst and measured its turnover frequency "
        "across several electrolysis runs, comparing it against the benchmark. "
    ) * 2  # ~280 chars, low digit fraction
    numeric = " ".join(f"{i}.{i}{i}" for i in range(120))  # >200 chars, mostly digits
    oversized = "word " * (MAX_CHUNK_CHARS // 4)  # well over MAX_CHUNK_CHARS, prose

    ref_id, (p_id, n_id, o_id) = seed_chunks(store, [prose, numeric, oversized])
    with store.pool.connection() as conn:
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)

    ids = {c.chunk_id for c in claimed}
    assert p_id in ids  # prose is summarized
    assert n_id in ids  # numeric dump now claimed (tagged in the pass, not SQL)
    assert o_id not in ids  # oversized skipped (> MAX_CHUNK_CHARS)


def test_numeric_dump_tagged_without_llm_call() -> None:
    """A numeric/coordinate dump is tagged ``(tabular data)`` in the pass with
    no LLM call (the transport is never hit)."""
    numeric_text = " ".join(f"{i}.{i}{i}" for i in range(80))  # mostly digits
    row: tuple = (
        9,
        7,
        0,
        "paragraph",
        numeric_text,
        [],
        None,
        [],
        "paper",
        "Coordinates",
    )
    conn = _FakeConn(claim_rows=[row], card_text="Title: x")
    store = _FakeStore(conn)
    t = _FakeTransport("BRIEF: should-not-be-used\nDETAIL: x.")
    client = LlmClient(LlmConfig(), transport=t)

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert len(t.calls) == 0  # no LLM call for a numeric dump
    assert any("(tabular data)" in (params[2] or "") for _sql, params in conn.writes)


def test_claim_reclaims_stale_lease_not_terminal_failed(store: Any) -> None:
    """Retry is driven by the lease, not a chunk_summaries attempts check.

    Under the lease model a sub-cap failure keeps its ``chunk_claims`` row
    (attempts on the lease; ``claimed_at`` is the backoff clock) and gets
    re-claimed once that row ages past the cooldown. A cap-exhausted failure
    has NO lease and a terminal ``chunk_summaries`` 'failed' row, so it is
    never re-claimed (the fresh claim excludes any summarized chunk). A lease
    still inside the cooldown is left alone — that is the retry backoff.
    """
    from tests.workers._helpers import seed_chunks

    prose = (
        "We synthesized a molecular catalyst and measured its turnover frequency "
        "across several electrolysis runs, comparing it against the benchmark. "
    ) * 2  # >200 chars prose, claimable

    ref_id, (stale_id, fresh_id, exhausted_id) = seed_chunks(
        store, [prose, prose, prose]
    )
    aged = f"now() - interval '{SUMMARY_LEASE_COOLDOWN_MIN + 5} minutes'"
    with store.pool.connection() as conn:
        # stale_id: sub-cap failure, lease aged past the cooldown → re-claimed.
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact, attempts, claimed_at) "
            f"VALUES (%s, 'llm-v1', 1, {aged})",
            (stale_id,),
        )
        # fresh_id: sub-cap failure, lease still inside the cooldown → backing off.
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact, attempts, claimed_at) "
            "VALUES (%s, 'llm-v1', 1, now())",
            (fresh_id,),
        )
        # exhausted_id: failed at the cap → terminal chunk_summaries row, no lease.
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, status, attempts) "
            "VALUES (%s, 'llm-v1', 'failed', %s)",
            (exhausted_id, MAX_SUMMARIZE_ATTEMPTS),
        )
        conn.commit()
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)

    ids = {c.chunk_id for c in claimed}
    assert stale_id in ids  # lease past cooldown → reclaimed
    assert fresh_id not in ids  # lease inside cooldown → still backing off
    assert exhausted_id not in ids  # terminal failed, no lease → given up


_PROSE = (
    "We synthesized a molecular catalyst and measured its turnover frequency "
    "across several electrolysis runs, comparing it against the benchmark. "
) * 2  # >200 chars of prose, claimable


def test_stale_worker_failure_does_not_clobber_fresh_ok(store: Any) -> None:
    """Stale-worker clobber regression (real PG). Worker A leases a chunk and
    stalls past the cooldown; worker B reaps the lease, summarizes and writes
    'ok' (deleting the lease). When A finally reports its failure the lease is
    gone — it must write NOTHING. The old code upserted a terminal 'failed'
    over B's fresh 'ok' row, and nothing ever repaired it (the fresh claim's
    NOT EXISTS excludes any existing chunk_summaries row regardless of
    status)."""
    from tests.workers._helpers import seed_chunks

    _ref, (cid,) = seed_chunks(store, [_PROSE])
    with store.pool.connection() as conn:
        # Worker A's lease.
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact) VALUES (%s, 'llm-v1')",
            (cid,),
        )
    with store.pool.connection() as conn:
        # Worker B reaps and succeeds: upserts 'ok', deletes the lease.
        write_chunk_summary(
            conn,
            cid,
            summarizer="llm-v1",
            text="good summary",
            prompt_hash="h",
            token_count=1,
        )
    with store.pool.connection() as conn:
        # Worker A's late failure report — lease gone → no-op.
        _mark_failed(conn, cid, summarizer="llm-v1", error="slow worker lost race")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT status, text FROM chunk_summaries "
            "WHERE chunk_id = %s AND summarizer = 'llm-v1'",
            (cid,),
        ).fetchone()
    assert row == ("ok", "good summary")


def test_terminal_failed_upsert_never_overwrites_ok(store: Any) -> None:
    """Belt-and-braces guard: even when _mark_failed DOES reach the terminal
    branch (lease present, attempts at the cap) while an 'ok' row already
    exists, the guarded upsert leaves the 'ok' row untouched — only the lease
    is released."""
    from tests.workers._helpers import seed_chunks

    _ref, (cid,) = seed_chunks(store, [_PROSE])
    with store.pool.connection() as conn:
        write_chunk_summary(
            conn,
            cid,
            summarizer="llm-v1",
            text="good summary",
            prompt_hash="h",
            token_count=1,
        )
        # A racing lease one failure short of the cap.
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact, attempts) "
            "VALUES (%s, 'llm-v1', %s)",
            (cid, MAX_SUMMARIZE_ATTEMPTS - 1),
        )
    with store.pool.connection() as conn:
        _mark_failed(conn, cid, summarizer="llm-v1", error="boom")  # → terminal
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT status, text FROM chunk_summaries "
            "WHERE chunk_id = %s AND summarizer = 'llm-v1'",
            (cid,),
        ).fetchone()
        lease = conn.execute(
            "SELECT 1 FROM chunk_claims WHERE chunk_id = %s AND artifact = 'llm-v1'",
            (cid,),
        ).fetchone()
    assert row == ("ok", "good summary")  # never clobbered
    assert lease is None  # terminal path still releases the lease


def test_nul_model_output_lands_sanitized(store: Any) -> None:
    """End-to-end on real PG: a completion carrying NUL bytes — which psycopg
    rejects in text params — is sanitized and stored as 'ok'."""
    from tests.workers._helpers import seed_chunks

    _ref, (cid,) = seed_chunks(store, [_PROSE])
    client = LlmClient(
        LlmConfig(), transport=_FakeTransport("BRIEF: g\x00lo\nDETAIL: d\x00.")
    )

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT status, text FROM chunk_summaries "
            "WHERE chunk_id = %s AND summarizer = 'llm-v1'",
            (cid,),
        ).fetchone()
    assert row == ("ok", "glo\n\nd.")


def test_poison_write_isolated_per_chunk(store: Any, monkeypatch: Any) -> None:
    """Phase-3 blast-radius regression (real PG): a row whose write genuinely
    fails at the DB (raw NUL injected past the sanitizer) loses only itself —
    the sibling's summary commits, and the poison chunk's lease attempts
    increment via _mark_failed so the retry cap engages instead of looping
    forever."""
    import precis.workers.llm_summarize as mod
    from tests.workers._helpers import seed_chunks

    _ref, (good_id, poison_id) = seed_chunks(store, [_PROSE, _PROSE])
    real_write = mod.write_chunk_summary

    def nul_write(conn: Any, chunk_id: int, **kw: Any) -> None:
        if chunk_id == poison_id:
            kw["text"] = "poison\x00row"  # bypasses the sanitizer → psycopg raises
        real_write(conn, chunk_id, **kw)

    monkeypatch.setattr(mod, "write_chunk_summary", nul_write)
    client = LlmClient(LlmConfig(), transport=_FakeTransport("BRIEF: g\nDETAIL: d."))

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 2, "ok": 1, "failed": 1}
    with store.pool.connection() as conn:
        summaries = dict(
            conn.execute(
                "SELECT chunk_id, status FROM chunk_summaries "
                "WHERE summarizer = 'llm-v1'"
            ).fetchall()
        )
        leases = dict(
            conn.execute(
                "SELECT chunk_id, attempts FROM chunk_claims WHERE artifact = 'llm-v1'"
            ).fetchall()
        )
    assert summaries == {good_id: "ok"}  # sibling survived the poison row
    assert leases == {poison_id: 1}  # attempts bumped → cap will engage


def test_reclaim_reserved_slice_beats_fresh_backlog(store: Any) -> None:
    """Reclaim starvation regression: with more fresh work than the batch can
    hold, a stale lease is still picked up via the reserved reclaim slice
    (previously reclaim ran only when fresh ran dry — months at ~1M fresh)."""
    from tests.workers._helpers import seed_chunks

    _ref, chunk_ids = seed_chunks(store, [_PROSE] * 17)
    stale_id = chunk_ids[-1]
    aged = f"now() - interval '{SUMMARY_LEASE_COOLDOWN_MIN + 5} minutes'"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact, claimed_at) "
            f"VALUES (%s, 'llm-v1', {aged})",
            (stale_id,),
        )
        conn.commit()
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=16)

    ids = {c.chunk_id for c in claimed}
    assert len(claimed) == 16
    assert stale_id in ids  # reclaimed despite 16 fresh chunks being available


def test_hot_tier_claims_recently_viewed_paper_first(store: Any) -> None:
    """Reader-salience priority: a paper a human just opened (its chunks
    freshly heated via ``last_seen``) is claimed ahead of an older paper
    with a lower ref_id, even though the plain ``ref_id, ord`` rest order
    would take the older one first. Reuses the dreamer's heat signal."""
    from tests.workers._helpers import seed_chunks

    # Cold paper first (lower ref_id) — would win a pure ref_id order.
    cold_ref, cold_ids = seed_chunks(store, [_PROSE] * 4)
    # Hot paper second (higher ref_id) — opened by a human just now.
    hot_ref, hot_ids = seed_chunks(store, [_PROSE] * 3)
    aged = f"now() - interval '{HOT_WINDOW_MIN + 60} minutes'"
    with store.pool.connection() as conn:
        # Age the cold paper out of the hot window; the hot paper keeps its
        # default last_seen = now().
        conn.execute(
            f"UPDATE chunks SET last_seen = {aged} WHERE ref_id = %s", (cold_ref,)
        )
        conn.commit()
        # A batch that can hold only the hot paper's chunks.
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=3)

    assert {c.chunk_id for c in claimed} == set(hot_ids)
    assert not (set(cold_ids) & {c.chunk_id for c in claimed})


def test_hot_tier_orders_before_rest_but_keeps_both(store: Any) -> None:
    """With room for everything the cold backlog still gets claimed — the hot
    tier reorders, it doesn't drop. Hot chunks come first in claim order."""
    from tests.workers._helpers import seed_chunks

    cold_ref, cold_ids = seed_chunks(store, [_PROSE] * 3)
    hot_ref, hot_ids = seed_chunks(store, [_PROSE] * 2)
    aged = f"now() - interval '{HOT_WINDOW_MIN + 60} minutes'"
    with store.pool.connection() as conn:
        conn.execute(
            f"UPDATE chunks SET last_seen = {aged} WHERE ref_id = %s", (cold_ref,)
        )
        conn.commit()
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)

    ids = [c.chunk_id for c in claimed]
    assert set(ids) == set(hot_ids) | set(cold_ids)  # nothing dropped
    # The two hot chunks are claimed before any cold one.
    assert set(ids[: len(hot_ids)]) == set(hot_ids)


def test_bump_salience_for_ref_heats_body_chunks(store: Any) -> None:
    """The reader's on-open hook advances ``last_seen`` on a ref's body
    chunks — the signal the hot tier + dreamer read."""
    from tests.workers._helpers import seed_chunks

    ref_id, chunk_ids = seed_chunks(store, [_PROSE, _PROSE])
    aged = "now() - interval '10 days'"
    with store.pool.connection() as conn:
        conn.execute(
            f"UPDATE chunks SET last_seen = {aged} WHERE ref_id = %s", (ref_id,)
        )
        conn.commit()
    n = store.bump_salience_for_ref(ref_id)
    assert n == len(chunk_ids)
    with store.pool.connection() as conn:
        fresh = conn.execute(
            "SELECT count(*) FROM chunks "
            "WHERE ref_id = %s AND last_seen > now() - interval '1 minute'",
            (ref_id,),
        ).fetchone()[0]
    assert fresh == len(chunk_ids)


def test_gc_drops_orphaned_leases(store: Any) -> None:
    """Permanent-lease-leak regression: a lease whose chunk is gone or no
    longer passes the claim filters can never be reclaimed (the reclaim
    inner-joins chunks re-applying them) — the GC drops it once it is far past
    the cooldown. A still-qualifying lease survives any age and is reclaimed
    normally."""
    from tests.workers._helpers import seed_chunks

    _ref, (short_id, quali_id) = seed_chunks(store, ["too short", _PROSE])
    orphan_id = 987654321  # no chunks row at all (deleted chunk)
    aged = f"now() - interval '{SUMMARY_LEASE_GC_MIN + 5} minutes'"
    with store.pool.connection() as conn:
        for cid in (orphan_id, short_id, quali_id):
            conn.execute(
                "INSERT INTO chunk_claims (chunk_id, artifact, claimed_at) "
                f"VALUES (%s, 'llm-v1', {aged})",
                (cid,),
            )
        conn.commit()
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)
        conn.commit()
        leases = {
            int(r[0])
            for r in conn.execute(
                "SELECT chunk_id FROM chunk_claims WHERE artifact = 'llm-v1'"
            ).fetchall()
        }

    assert orphan_id not in leases  # chunk deleted → lease GC'd
    assert short_id not in leases  # chunk filtered (< min chars) → lease GC'd
    assert quali_id in leases  # still-qualifying lease is never GC'd…
    assert quali_id in {c.chunk_id for c in claimed}  # …it is reclaimed instead
