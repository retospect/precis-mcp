"""Tests for the ADR 0047 chunk-tag classifier cascade (``workers/classify.py``).

DB-backed (real ``chunks``/``chunk_tags`` via the ``store`` fixture) with a
fake LLM client — no network. Covers the escalate-client wiring: the Tier 2
re-judge must call a *distinct* client, never silently reuse the base one.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.classify import run_classify_pass
from tests.workers._helpers import seed_chunk, seed_chunks, seed_ref


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


def _role3_tags(store: Any, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM chunk_tags ct "
            "JOIN tags t ON t.tag_id = ct.tag_id "
            "JOIN chunks c ON c.chunk_id = ct.chunk_id "
            "WHERE c.ref_id = %s AND t.namespace = 'ROLE3'",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_ref_ids_scopes_the_claim_to_the_named_papers(store: Any) -> None:
    """``ref_ids`` restricts the claim to specific refs — a sibling paper
    outside the scope must be left completely untouched (targeted backfill,
    mirroring ``classify_topics``'s ``ref_ids`` scoping)."""
    ref_a = seed_ref(store, title="paper A")
    seed_chunk(store, ref_id=ref_a, text=_PROSE, ord=0)
    ref_b = seed_ref(store, title="paper B")
    seed_chunk(store, ref_id=ref_b, text=_PROSE, ord=0)

    class _CascadeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            from types import SimpleNamespace

            self.calls += 1
            val = "not_junk" if self.calls % 2 == 1 else "own"
            return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)

    client = _CascadeClient("unused")
    result = run_classify_pass(store, client=client, batch_size=10, ref_ids=[ref_a])

    assert result["claimed"] == 1
    assert result["ok"] == 1
    assert _role3_tags(store, ref_a) == ["own"]
    assert _role3_tags(store, ref_b) == []  # untouched — outside scope


def test_ref_ids_none_matches_global_unscoped_behaviour(store: Any) -> None:
    """``ref_ids=None`` (the default) must sweep every paper, unchanged."""
    seed_chunks(store, [_PROSE])

    class _CascadeClient(_FakeClient):
        def complete(self, messages: list[dict[str, str]]) -> Any:
            from types import SimpleNamespace

            self.calls += 1
            val = "not_junk" if self.calls == 1 else "own"
            return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)

    client = _CascadeClient("unused")
    result = run_classify_pass(store, client=client, batch_size=10, ref_ids=None)
    assert result["claimed"] == 1
    assert result["ok"] == 1


# ---------------------------------------------------------------------------
# Bounded in-pass concurrency
# ---------------------------------------------------------------------------


class _KeyedCascadeClient:
    """Deterministic per-row answer keyed by which seeded marker appears in
    the prompt — safe under a thread pool's non-deterministic submission/
    completion order, unlike a call-counter fake (which assumes "call N ==
    row N"). Junk-gate calls (detected by the junk axis's distinctive prompt
    opener) always answer "not_junk"; role3 calls answer per ``answers``.
    """

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, messages: list[dict[str, str]]) -> Any:
        with self._lock:
            self.calls += 1
        content = messages[1]["content"]
        if "JUNK or SUBSTANTIVE" in content:
            val = "not_junk"
        else:
            # role3's prompt also embeds the PREVIOUS/NEXT chunk's raw text as
            # a "gist" fallback (no chunk_summaries in this test), so a naive
            # substring search can match a neighbor's marker instead of the
            # row's own. The row's own text is always what follows the final
            # "CHUNK TEXT:" line (see ``_build_prompt``), so anchor there.
            own_text = content.rsplit("CHUNK TEXT:\n", 1)[-1]
            marker = next(m for m in self.answers if own_text.startswith(m))
            val = self.answers[marker]
        return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)


def _marker_text(marker: str) -> str:
    return f"{marker} {_PROSE}"


def _seed_marked_chunks(store: Any, answers: dict[str, str]) -> int:
    """Seed one ref with one chunk per marker (order = dict insertion order,
    which is claim order since ``_claim`` orders by ``chunk_id``)."""
    ref_id = seed_ref(store)
    for i, marker in enumerate(answers):
        seed_chunk(store, ref_id=ref_id, text=_marker_text(marker), ord=i)
    return ref_id


def _role3_by_marker(
    store: Any, ref_id: int, answers: dict[str, str]
) -> dict[str, str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.text, t.value FROM chunk_tags ct "
            "JOIN tags t ON t.tag_id = ct.tag_id "
            "JOIN chunks c ON c.chunk_id = ct.chunk_id "
            "WHERE c.ref_id = %s AND t.namespace = 'ROLE3'",
            (ref_id,),
        ).fetchall()
    by_text = {text: value for text, value in rows}
    return {m: by_text[_marker_text(m)] for m in answers}


_ANSWERS = {
    "MARKER0": "own",
    "MARKER1": "background",
    "MARKER2": "own",
    "MARKER3": "background",
    "MARKER4": "own",
    "MARKER5": "background",
}


class _AlwaysOwnClient(_KeyedCascadeClient):
    """Answers "own" to any role3 prompt, regardless of chunk content —
    used where the test only cares about claim/pool plumbing, not the
    per-row verdict."""

    def __init__(self) -> None:
        super().__init__({})

    def complete(self, messages: list[dict[str, str]]) -> Any:
        with self._lock:
            self.calls += 1
        content = messages[1]["content"]
        val = "not_junk" if "JUNK or SUBSTANTIVE" in content else "own"
        return SimpleNamespace(text=f'{{"value": "{val}"}}', total_tokens=5)


def test_concurrency_gt_1_classifies_every_claimed_row_correctly(store: Any) -> None:
    """``concurrency>1`` must classify ALL claimed rows and write the same
    tag each row's serial verdict would — order-independent, no dropped or
    duplicated chunks."""
    ref_id = _seed_marked_chunks(store, _ANSWERS)
    client = _KeyedCascadeClient(_ANSWERS)

    result = run_classify_pass(store, client=client, batch_size=10, concurrency=4)

    assert result["claimed"] == len(_ANSWERS)
    assert result["ok"] == len(_ANSWERS)
    assert result["failed"] == 0
    assert _role3_by_marker(store, ref_id, _ANSWERS) == _ANSWERS
    # junk-gate + role3 per row, no extra/missing calls from the fan-out.
    assert client.calls == 2 * len(_ANSWERS)


def test_concurrency_1_and_gt_1_agree(store: Any) -> None:
    """Concurrency>1 reproduces the same per-row verdicts a concurrency=1
    (default, serial) run produces over identical input."""
    ref_serial = _seed_marked_chunks(store, _ANSWERS)
    serial_result = run_classify_pass(
        store, client=_KeyedCascadeClient(_ANSWERS), batch_size=10, concurrency=1
    )
    ref_concurrent = _seed_marked_chunks(store, _ANSWERS)
    concurrent_result = run_classify_pass(
        store, client=_KeyedCascadeClient(_ANSWERS), batch_size=10, concurrency=6
    )

    assert serial_result["ok"] == concurrent_result["ok"] == len(_ANSWERS)
    assert _role3_by_marker(store, ref_serial, _ANSWERS) == _role3_by_marker(
        store, ref_concurrent, _ANSWERS
    )


def test_default_concurrency_claim_limit_is_unchanged(store: Any) -> None:
    """With no ``concurrency=`` (default 1), the claim limit stays
    ``batch_size`` — no regression from the ``max(batch_size, concurrency)``
    claim-feeding change."""
    seed_chunks(store, [_PROSE, _PROSE, _PROSE])
    result = run_classify_pass(store, client=_AlwaysOwnClient(), batch_size=2)
    assert result["claimed"] == 2  # not 3 — concurrency defaults to 1


def test_concurrency_is_clamped_to_hard_ceiling(store: Any, monkeypatch: Any) -> None:
    """A caller/`service_config` value above ``PRECIS_CLASSIFY_MAX_CONCURRENCY``
    is clamped to the ceiling, not honored verbatim."""
    import precis.workers.classify as classify_mod

    monkeypatch.setenv("PRECIS_CLASSIFY_MAX_CONCURRENCY", "2")
    captured: dict[str, int] = {}
    real_executor = classify_mod.ThreadPoolExecutor

    class _SpyExecutor(real_executor):  # type: ignore[misc,valid-type]
        def __init__(self, max_workers: int | None = None, **kw: Any) -> None:
            captured["max_workers"] = max_workers or 0
            super().__init__(max_workers=max_workers, **kw)

    monkeypatch.setattr(classify_mod, "ThreadPoolExecutor", _SpyExecutor)
    seed_chunks(store, [_PROSE, _PROSE, _PROSE])

    result = run_classify_pass(
        store, client=_AlwaysOwnClient(), batch_size=10, concurrency=1000
    )
    assert captured["max_workers"] == 2  # clamped, not 1000
    assert result["ok"] == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_concurrency_below_1_floors_to_1(store: Any, bad: int) -> None:
    """A non-positive concurrency (a stale/bad config value) floors to 1
    rather than raising or passing 0 to the thread pool."""
    seed_chunks(store, [_PROSE])
    result = run_classify_pass(
        store, client=_AlwaysOwnClient(), batch_size=5, concurrency=bad
    )
    assert result["ok"] == 1
