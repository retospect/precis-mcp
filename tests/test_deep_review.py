"""Tests for the Slice-3 deep reviewer.

Mirrors :mod:`tests.test_structural` — gate / dedup / prompt /
happy-path with the LLM mocked at the ``call_claude_agent``
boundary.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.utils.claude_agent import AgentResult, ClaudeAgentError
from precis.workers.deep_review import (
    MIN_INTERVAL_HOURS,
    _build_prompt,
    _gate_enabled,
    _recent_digest_exists,
    _strategic_dashboard,
    _write_digest,
    run_deep_review_pass,
)
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


# ── gate ──────────────────────────────────────────────────────────


def test_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DEEP_REVIEW", raising=False)
    assert _gate_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "True", "YES"])
def test_gate_truthy_values(monkeypatch: pytest.MonkeyPatch, v: str) -> None:
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", v)
    assert _gate_enabled() is True


def test_pass_skips_when_gate_disabled(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_DEEP_REVIEW", raising=False)
    result = run_deep_review_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


# ── dedup ─────────────────────────────────────────────────────────


def test_recent_digest_detected(store: Store) -> None:
    _write_digest(store, "weekly digest", cost_usd=2.0)
    assert _recent_digest_exists(store, 1) is True


def test_recent_digest_returns_false_when_none(store: Store) -> None:
    assert _recent_digest_exists(store, MIN_INTERVAL_HOURS) is False


def test_pass_skips_when_recent_digest_exists(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", "1")
    _write_digest(store, "fresh weekly", cost_usd=0.5)
    called = {"hit": False}

    def _spy(*a, **kw) -> AgentResult:
        called["hit"] = True
        return AgentResult(final_text="x", cost_usd=0, duration_s=0, turns_used=None)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _spy)
    result = run_deep_review_pass(store)
    assert result.claimed == 0
    assert called["hit"] is False


# ── prompt ────────────────────────────────────────────────────────


def test_strategic_dashboard_empty(store: Store) -> None:
    snap = _strategic_dashboard(store)
    assert "no strategic todos" in snap


def test_strategic_dashboard_renders_picks(handler: TodoHandler, store: Store) -> None:
    root = handler.put(text="Main", tags=["level:strategic"])
    root_id = id_of(root.body)
    a = handler.put(text="leaf", parent_id=root_id)
    aid = id_of(a.body)
    # Marking done emits a status:done event the dashboard counts.
    handler.tag(id=aid, add=["STATUS:done"])

    snap = _strategic_dashboard(store)
    assert f"#{root_id} Main" in snap
    # 2 descendants under root (a + the done marker on a), 1 pick in 7d.
    assert "picks in 7d" in snap


def test_build_prompt_has_all_directive_sections(store: Store) -> None:
    prompt = _build_prompt(store)
    assert "DEEP REVIEW" in prompt
    assert "Strategic dashboard" in prompt
    assert "Recent review summary" in prompt
    assert "Archive candidates" in prompt
    assert "Pruning candidates" in prompt
    assert "Rotation rebalancing" in prompt


def test_build_prompt_includes_recent_reviews(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An orphan todo → nursery raises a nursery:orphan alert, which the
    # deep-review prompt surfaces under "Open nursery alerts".
    handler.put(text="dangling orphan")
    from precis.workers.nursery import run_nursery_pass

    run_nursery_pass(store)
    prompt = _build_prompt(store)
    assert "Open nursery alerts" in prompt
    assert "nursery:orphan" in prompt


# ── full pass with stubbed LLM ───────────────────────────────────


def test_pass_writes_digest_on_happy_path(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", "1")
    handler.put(text="Strategic A", tags=["level:strategic"])

    def _ok(*a, **kw) -> AgentResult:
        return AgentResult(
            final_text="Weekly review: nothing to archive.",
            cost_usd=1.4,
            duration_s=180.0,
            turns_used=22,
        )

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _ok)
    result = run_deep_review_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.title,
                   r.meta->>'deep_review_cost_usd'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:deep'
             ORDER BY r.created_at DESC LIMIT 1
            """,
        ).fetchone()
    assert row is not None
    assert "nothing to archive" in row[0]
    assert float(row[1]) == pytest.approx(1.4)


def test_pass_records_failure_on_llm_error(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", "1")
    handler.put(text="Strategic B", tags=["level:strategic"])

    def _err(*a, **kw):
        raise ClaudeAgentError("timeout", stdout="", stderr="took too long")

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _err)
    result = run_deep_review_pass(store)
    assert result.claimed == 1
    assert result.failed == 1
    with store.pool.connection() as conn:
        n = conn.execute(
            """
            SELECT count(*) FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:deep'
            """,
        ).fetchone()
    assert n is not None and int(n[0]) == 0


def test_pass_skips_on_breaker_pause(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A window-scoped budget/quota trip is a pause, not a failure: the reviewer
    # skips (claimed/ok/failed all 0, no digest, model never invoked) so a capped
    # budget doesn't spin failures onto the FAILED-PASSES panel.
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", "1")
    handler.put(text="Strategic C", tags=["level:strategic"])

    monkeypatch.setattr(
        "precis.budget.breaker.gate_tier",
        lambda *a, **kw: "budget: daily cap $20.00 reached ($85.06 spent) — paused",
    )

    def _boom(*a, **kw):
        raise AssertionError("model must not be called while paused")

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _boom)
    result = run_deep_review_pass(store)
    assert result.claimed == 0 and result.ok == 0 and result.failed == 0
    with store.pool.connection() as conn:
        n = conn.execute(
            """
            SELECT count(*) FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:deep'
            """,
        ).fetchone()
    assert n is not None and int(n[0]) == 0
