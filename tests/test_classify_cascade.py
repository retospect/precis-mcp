"""Tests for the ADR 0047 chunk-tag classifier cascade (``workers/classify.py``).

DB-backed (real ``chunks``/``chunk_tags`` via the ``store`` fixture) with a
fake LLM client — no network. Covers the escalate-client wiring: the Tier 2
re-judge must call a *distinct* client, never silently reuse the base one.
"""

from __future__ import annotations

from typing import Any

from precis.workers.classify import run_classify_pass
from tests.workers._helpers import seed_chunks


class _FakeClient:
    """Records every prompt it's given; always answers with a fixed value."""

    def __init__(self, value: str, *, label: str = "base") -> None:
        self.value = value
        self.label = label
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> Any:
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(text=f'{{"value": "{self.value}"}}', total_tokens=5)


_PROSE = (
    "We synthesized a Pd(111) catalyst and measured its NO to NH3 selectivity "
    "across several electrolysis runs against the literature benchmark carefully."
)


def test_escalate_re_judge_calls_the_escalate_client_not_the_base_one(
    store: Any,
) -> None:
    """The real bug this pins: an 'own' verdict from the base client must be
    re-judged by a *distinct* ``escalate_client`` — reusing the base client
    for the "escalate" call is a no-op disguised as a Tier 2 re-judge."""
    seed_chunks(store, [_PROSE])

    base = _FakeClient("junk_never", label="base")

    # junk-gate: not junk (first call on `base`); role3: "own" (second call).
    class _CascadeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            from types import SimpleNamespace

            self.calls += 1
            # 1st call = junk gate -> not junk; 2nd call = role3 -> own
            val = "not_junk" if self.calls == 1 else "own"
            return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)

    base_client = _CascadeClient("unused")
    escalate_client = _FakeClient("background", label="escalate")

    result = run_classify_pass(
        store, client=base_client, batch_size=10, escalate_client=escalate_client
    )

    assert result["ok"] == 1
    assert escalate_client.calls == 1  # the escalate client WAS used
    assert base_client.calls == 2  # junk-gate + role3, not a 3rd re-judge call
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
            "WHERE t.namespace = 'ROLE3'"
        ).fetchone()
    assert row is not None and row[0] == "background"  # the escalate verdict won


def test_no_escalate_client_leaves_the_base_verdict(store: Any) -> None:
    seed_chunks(store, [_PROSE])

    class _CascadeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            from types import SimpleNamespace

            self.calls += 1
            val = "not_junk" if self.calls == 1 else "own"
            return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)

    base_client = _CascadeClient("unused")
    result = run_classify_pass(
        store, client=base_client, batch_size=10, escalate_client=None
    )
    assert result["ok"] == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
            "WHERE t.namespace = 'ROLE3'"
        ).fetchone()
    assert row is not None and row[0] == "own"
