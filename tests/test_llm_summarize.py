"""Offline tests for the LLM chunk-summarization pass.

No DB, no network: a fake :class:`Transport` returns canned
completions and a fake store/connection records writes. Covers prompt
assembly (stable-prefix-first), the tolerant parser, the OpenAI client
wire, env config, and the end-to-end pass.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from precis.workers.llm_summarize import (
    LlmClient,
    LlmConfig,
    _Claimed,
    build_messages,
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
