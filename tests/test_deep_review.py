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
    root = handler.put(text="Main", meta={"rotation_root": True})
    root_id = id_of(root.body)
    a = handler.put(text="leaf", parent_id=root_id)
    aid = id_of(a.body)
    # Marking done emits a status:done event the dashboard counts.
    handler.tag(id=aid, add=["STATUS:done"])

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] Main" in snap
    # 2 descendants under root (a + the done marker on a), 1 pick in 7d.
    assert "picks in 7d" in snap
    # gripe: bare `#<id>` neither round-trips as a copyable handle nor
    # triggers memory auto-linking — must be the bracketed td-handle.
    assert f"#{root_id} " not in snap


def test_strategic_dashboard_excludes_done_root(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Finished rollout", meta={"rotation_root": True})
    root_id = id_of(root.body)
    handler.tag(id=root_id, add=["STATUS:done"])

    snap = _strategic_dashboard(store)
    assert f"td{root_id}" not in snap


def test_strategic_dashboard_excludes_wont_do_root(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Abandoned idea", meta={"rotation_root": True})
    root_id = id_of(root.body)
    handler.tag(id=root_id, add=["STATUS:won't-do"])

    snap = _strategic_dashboard(store)
    assert f"td{root_id}" not in snap


def test_strategic_dashboard_keeps_open_root_with_no_picks(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Still grinding", meta={"rotation_root": True})
    root_id = id_of(root.body)

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] Still grinding" in snap
    assert "0 picks in 7d" in snap


def test_strategic_dashboard_flags_stale_root_all_todos_done(
    handler: TodoHandler, store: Store
) -> None:
    """All-descendant-todos-done, no jobs, no parked leaf → STALE-ROOT."""
    root = handler.put(text="All done", meta={"rotation_root": True})
    root_id = id_of(root.body)
    leaf = handler.put(text="leaf", parent_id=root_id)
    leaf_id = id_of(leaf.body)
    handler.tag(id=leaf_id, add=["STATUS:done"])

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] All done" in snap
    assert "STALE-ROOT" in snap


def test_strategic_dashboard_not_stale_with_open_descendant_job(
    handler: TodoHandler, store: Store
) -> None:
    """Todos all done, but a non-terminal ``kind='job'`` descendant is
    still queued under the root — must NOT flag STALE-ROOT (gr451821:
    the ``subtree`` CTE used to be ``kind='todo'``-only, so a dispatched
    ``plan_tick`` job never counted against "subtree all done")."""
    root = handler.put(text="Job in flight", meta={"rotation_root": True})
    root_id = id_of(root.body)
    leaf = handler.put(text="leaf", parent_id=root_id)
    leaf_id = id_of(leaf.body)
    handler.tag(id=leaf_id, add=["STATUS:done"])
    store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        meta={"job_type": "plan_tick"},
        parent_id=root_id,
    )
    # freshly minted job carries no STATUS: tag yet — must still count as open.

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] Job in flight" in snap
    assert "STALE-ROOT" not in snap


def test_strategic_dashboard_stale_with_terminal_descendant_job(
    handler: TodoHandler, store: Store
) -> None:
    """Same shape, but the job has reached a terminal STATUS → STALE-ROOT
    still fires (a finished job doesn't keep the root stale forever)."""
    from precis.store.types import Tag

    root = handler.put(text="Job finished", meta={"rotation_root": True})
    root_id = id_of(root.body)
    leaf = handler.put(text="leaf", parent_id=root_id)
    leaf_id = id_of(leaf.body)
    handler.tag(id=leaf_id, add=["STATUS:done"])
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        meta={"job_type": "plan_tick"},
        parent_id=root_id,
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] Job finished" in snap
    assert "STALE-ROOT" in snap


def test_strategic_dashboard_not_stale_with_unresolved_decision_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """A descendant todo parked on ``ask-user:`` — even one that also
    carries a terminal ``STATUS:done``/``won't-do`` tag — disqualifies
    STALE-ROOT: the standing "parked, needs a human" marker
    (``_doable_exclusion_clause``'s registry, same one ``dispatch.py``
    gates candidacy on) means the leaf isn't actually resolved."""
    from precis.store.types import Tag

    root = handler.put(text="Waiting on a human", meta={"rotation_root": True})
    root_id = id_of(root.body)
    leaf = handler.put(text="leaf", parent_id=root_id)
    leaf_id = id_of(leaf.body)
    handler.tag(id=leaf_id, add=["STATUS:done"])
    store.add_tag(leaf_id, Tag.open("ask-user:which vendor?"), set_by="agent")

    snap = _strategic_dashboard(store)
    assert f"[td{root_id}] Waiting on a human" in snap
    assert "STALE-ROOT" not in snap


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
    # A stale claim → nursery raises a nursery:stale-claim alert, which
    # the deep-review prompt surfaces under "Open nursery alerts".
    # (``orphan`` is detected but never alerted — ``_NO_ALERT`` — so it
    # can't be the fixture here.)
    from precis.store.types import Tag
    from precis.workers.nursery import STALE_CLAIM_HOURS, run_nursery_pass
    from tests.test_nursery import _backdate_tag

    r = handler.put(text="Long-claimed task")
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_tag(store, rid, "claimed-by:asa-worker", STALE_CLAIM_HOURS + 1)

    run_nursery_pass(store)
    prompt = _build_prompt(store)
    assert "Open nursery alerts" in prompt
    assert "nursery:stale-claim" in prompt


# ── full pass with stubbed LLM ───────────────────────────────────


def test_pass_writes_digest_on_happy_path(
    handler: TodoHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_DEEP_REVIEW", "1")
    handler.put(text="Strategic A", meta={"rotation_root": True})

    captured: dict = {}

    def _ok(*a, **kw) -> AgentResult:
        captured["disallowed"] = kw.get("disallowed_tools")
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
    # gr179501: reviewers get an explicit tier-1 deny — no mutate/fs-write/
    # shell/web — but keep ``put`` for the _footer_block gripe carve-out.
    disallowed = captured["disallowed"] or ()
    for denied in (
        "Bash",
        "mcp__precis__edit",
        "mcp__precis__delete",
        "mcp__precis__tag",
        "mcp__precis__link",
    ):
        assert denied in disallowed
    assert "mcp__precis__put" not in disallowed
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.title,
                   r.meta->>'deep_review_cost_usd'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'digest:deep'
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
    handler.put(text="Strategic B", meta={"rotation_root": True})

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
             WHERE r.kind = 'memory' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'digest:deep'
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
    handler.put(text="Strategic C", meta={"rotation_root": True})

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
             WHERE r.kind = 'memory' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'digest:deep'
            """,
        ).fetchone()
    assert n is not None and int(n[0]) == 0
