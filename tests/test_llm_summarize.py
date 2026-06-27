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
    MAX_CHUNK_CHARS,
    MAX_SUMMARIZE_ATTEMPTS,
    LlmClient,
    LlmConfig,
    _Claimed,
    build_messages,
    claim_chunks_without_summary,
    parse_summary,
    run_llm_summarize_pass,
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
    def __init__(self, claim_rows: list[tuple[Any, ...]], card_text: str) -> None:
        self.claim_rows = claim_rows
        self.card_text = card_text
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        flat = " ".join(sql.split())
        if "LEFT JOIN chunk_summaries cs" in flat:
            return _Result(self.claim_rows)
        if "card_combined" in flat:
            return _Result([(self.card_text,)] if self.card_text else [])
        if "INSERT INTO chunk_summaries" in flat:
            self.writes.append((flat, params or ()))
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
    class _Boom:
        def post_json(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("backend down")

    conn = _FakeConn(claim_rows=[_claim_row(chunk_id=1)], card_text="")
    store = _FakeStore(conn)
    client = LlmClient(LlmConfig(), transport=_Boom())

    result = run_llm_summarize_pass(store, client=client, batch_size=10)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    # A failure marker row was written (status='failed').
    assert any("'failed'" in sql for sql, _ in conn.writes)


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
    # Exactly one failure marker and one successful insert.
    assert sum(1 for sql, _ in conn.writes if "'failed'" in sql) == 1
    assert sum(1 for sql, _ in conn.writes if "'failed'" not in sql) == 1


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


def test_claim_retries_failed_below_cap_only(store: Any) -> None:
    """A failed summary is re-claimed while attempts < MAX_SUMMARIZE_ATTEMPTS
    (so transient cold-load failures retry) but not once exhausted (so a
    poison chunk can't re-bill the backend forever)."""
    from tests.workers._helpers import seed_chunks

    prose = (
        "We synthesized a molecular catalyst and measured its turnover frequency "
        "across several electrolysis runs, comparing it against the benchmark. "
    ) * 2  # >200 chars prose, claimable

    ref_id, (retry_id, exhausted_id) = seed_chunks(store, [prose, prose])
    with store.pool.connection() as conn:
        # retry_id: one prior failure, below cap → should be re-claimed.
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, status, attempts) "
            "VALUES (%s, 'llm-v1', 'failed', 1)",
            (retry_id,),
        )
        # exhausted_id: failed at the cap → should NOT be re-claimed.
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, status, attempts) "
            "VALUES (%s, 'llm-v1', 'failed', %s)",
            (exhausted_id, MAX_SUMMARIZE_ATTEMPTS),
        )
        conn.commit()
        claimed = claim_chunks_without_summary(conn, summarizer="llm-v1", limit=10)

    ids = {c.chunk_id for c in claimed}
    assert retry_id in ids  # failed, attempts < cap → retried
    assert exhausted_id not in ids  # failed, attempts >= cap → given up
