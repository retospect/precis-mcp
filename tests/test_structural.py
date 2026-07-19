"""Tests for the Slice-3 structural reviewer.

The LLM call is mocked at the ``call_claude_agent`` boundary —
we're testing the worker's wiring (gate, dedup, digest write), not
the model's output.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.utils.claude_agent import AgentResult, ClaudeAgentError
from precis.workers.structural import (
    MIN_INTERVAL_HOURS,
    _build_prompt,
    _gate_enabled,
    _recent_digest_exists,
    _strategic_layer_snapshot,
    _write_digest,
    run_structural_pass,
)
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


# ── gate ──────────────────────────────────────────────────────────


def test_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_STRUCTURAL_REVIEW", raising=False)
    assert _gate_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "True", "YES"])
def test_gate_truthy_values(monkeypatch: pytest.MonkeyPatch, v: str) -> None:
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", v)
    assert _gate_enabled() is True


@pytest.mark.parametrize("v", ["", "0", "false", "no", "off"])
def test_gate_falsy_values(monkeypatch: pytest.MonkeyPatch, v: str) -> None:
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", v)
    assert _gate_enabled() is False


def test_pass_skips_when_gate_disabled(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_STRUCTURAL_REVIEW", raising=False)
    result = run_structural_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert result.failed == 0


def test_pass_skips_under_high_load(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the gate on, the load-avg check should short-circuit."""
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")
    monkeypatch.setenv("PRECIS_LOAD_CEILING", "0.0001")
    # Stub current_load to a value above the tiny ceiling.
    from unittest.mock import patch as _patch

    called = {"hit": False}

    def _spy(*a, **kw) -> object:
        called["hit"] = True
        from precis.utils.claude_agent import AgentResult

        return AgentResult(final_text="ran", cost_usd=0, duration_s=0, turns_used=None)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _spy)
    with _patch("precis.utils.load_gate.current_load", return_value=10.0):
        result = run_structural_pass(store)
    assert result.claimed == 0
    assert called["hit"] is False


# ── dedup ─────────────────────────────────────────────────────────


def test_recent_digest_detects_existing_within_window(
    store: Store,
) -> None:
    # Manually mint a tier:structural digest by going through the
    # writer (uses real handlers + tags).
    _write_digest(store, "first digest", cost_usd=0.5)
    assert _recent_digest_exists(store, 1) is True


def test_recent_digest_returns_false_when_none(store: Store) -> None:
    assert _recent_digest_exists(store, 6) is False


# ── failure backoff (the spark 124k/24h spin-loop guard) ──────────


def _err_result() -> object:
    """A non-paused dispatch error (config/transport failure)."""
    from precis.utils.llm.router import LlmResult, Tier

    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model="test",
        tier=Tier.CLOUD_SUPER,
        error="claude -p (agent) exited 1: [entrypoint] ERROR: PRECIS_DATABASE_URL not set",
        paused=False,
    )


def test_dispatch_failure_backs_off_instead_of_spinning(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-paused dispatch error must NOT re-dispatch every tick: it records a
    cooldown marker and the next pass within the interval backs off. Without
    this, one persistent config gap (spark's agent container missing
    PRECIS_DATABASE_URL) spun review[structural] into 124k ERROR/24h."""
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")
    from precis.workers.review import _recent_failure
    from precis.workers.structural import STRUCTURAL

    calls = {"n": 0}

    def _fake_dispatch(*a: object, **kw: object) -> object:
        calls["n"] += 1
        return _err_result()

    monkeypatch.setattr("precis.workers.review.dispatch", _fake_dispatch)

    # First pass: dispatch runs, errors, records a failure (a real failed=1).
    r1 = run_structural_pass(store)
    assert calls["n"] == 1
    assert r1.failed == 1 and r1.claimed == 1
    assert _recent_failure(store, STRUCTURAL) is True

    # Second pass within the interval: backs off WITHOUT re-dispatching —
    # the spin is gone (this assertion is the regression).
    r2 = run_structural_pass(store)
    assert calls["n"] == 1  # dispatch NOT called again
    assert r2.claimed == 0 and r2.failed == 0


def test_pass_skips_when_recent_digest_exists(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the gate on, a fresh digest blocks a re-run."""
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")
    _write_digest(store, "fresh", cost_usd=0.1)
    # Mock the LLM so we'd notice if it were called.
    called = {"hit": False}

    def _spy(*a, **kw) -> AgentResult:
        called["hit"] = True
        return AgentResult(final_text="x", cost_usd=0, duration_s=0, turns_used=None)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _spy)
    result = run_structural_pass(store, min_interval_hours=MIN_INTERVAL_HOURS)
    assert result.claimed == 0
    assert called["hit"] is False


# ── prompt construction ──────────────────────────────────────────


def test_strategic_layer_snapshot_empty(store: Store) -> None:
    snap = _strategic_layer_snapshot(store)
    assert "no strategic todos" in snap


def test_strategic_layer_snapshot_renders_tree(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Main goal", tags=["level:strategic"])
    root_id = id_of(root.body)
    a = handler.put(text="Tactic A", parent_id=root_id)
    aid = id_of(a.body)
    # Add 2 subtasks under a, so direct_children count == 2.
    handler.put(text="Subtask 1", parent_id=aid)
    handler.put(text="Subtask 2", parent_id=aid)

    snap = _strategic_layer_snapshot(store)
    assert f"#{root_id} Main goal" in snap
    assert f"#{aid} Tactic A (2 direct children)" in snap


def test_build_prompt_includes_directive_sections(
    handler: TodoHandler, store: Store
) -> None:
    handler.put(text="Strategic 1", tags=["level:strategic"])
    prompt = _build_prompt(store)
    assert "STRUCTURAL REVIEW" in prompt
    assert "Strategic + tactical layer" in prompt
    assert "What to look for" in prompt
    assert "Output format" in prompt
    # Open-nursery-alerts section gets the placeholder when none exist.
    assert "(no open nursery alerts)" in prompt


def test_build_prompt_includes_recent_nursery(
    handler: TodoHandler, store: Store
) -> None:
    # An orphan todo → nursery raises a nursery:orphan alert, which the
    # structural prompt surfaces as an open-alert line.
    handler.put(text="orphan one")  # → orphan in nursery detection
    from precis.workers.nursery import run_nursery_pass

    run_nursery_pass(store)
    prompt = _build_prompt(store)
    assert "nursery:orphan" in prompt


# ── full pass with stubbed LLM ───────────────────────────────────


def test_pass_writes_digest_on_happy_path(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")
    handler.put(text="Strategic A", tags=["level:strategic"])

    def _ok_call(*a, **kw) -> AgentResult:
        return AgentResult(
            final_text="No structural issues this pass.",
            cost_usd=0.07,
            duration_s=12.0,
            turns_used=3,
        )

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _ok_call)
    result = run_structural_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0
    # Verify the memory landed with the right tags.
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.title,
                   r.meta->>'structural_cost_usd'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:structural'
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
        ).fetchone()
    assert row is not None
    assert "No structural issues" in row[0]
    assert float(row[1]) == pytest.approx(0.07)


def test_pass_records_failure_on_llm_error(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")
    handler.put(text="Strategic B", tags=["level:strategic"])

    def _err(*a, **kw):
        raise ClaudeAgentError(
            "boom", stdout="", stderr="model unavailable", returncode=2
        )

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _err)
    result = run_structural_pass(store)
    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 1
    # No digest written.
    with store.pool.connection() as conn:
        n = conn.execute(
            """
            SELECT count(*) FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:structural'
            """,
        ).fetchone()
    assert n is not None and int(n[0]) == 0


def test_pass_writes_empty_digest_with_placeholder_title(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the model returns whitespace, the digest still lands with a placeholder."""
    monkeypatch.setenv("PRECIS_STRUCTURAL_REVIEW", "1")

    def _blank(*a, **kw) -> AgentResult:
        return AgentResult(final_text="   \n", cost_usd=0, duration_s=1, turns_used=1)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _blank)
    result = run_structural_pass(store)
    assert result.ok == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.title FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:structural'
             ORDER BY r.created_at DESC LIMIT 1
            """,
        ).fetchone()
    assert row is not None
    # Unified review driver names the empty digest with the reviewer's
    # display name ("Structural") + the date + an (empty) marker.
    assert "Structural" in row[0]
    assert "(empty)" in row[0]
