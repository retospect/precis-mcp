"""The briefing's no-news path (gr626716): an empty news window used to
exit silently, hiding the days the feed itself was the outage. Now an
empty window still produces a dated, deterministic brief when unresolved
health/decision work is open (open alerts, todo attention signals); a
truly quiet day stays an explicit no-op."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from precis.alerts import raise_alert
from precis.store import Store
from precis.store.types import Tag
from precis.workers import briefing

_NOW = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)


class _NoLLM:
    """A client that must never be called — the no-news path is LLM-free."""

    def complete(self, messages: Any) -> Any:  # pragma: no cover
        raise AssertionError("no-news brief must not call the LLM")


def _brief_text(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s ORDER BY ord", (ref_id,)
        ).fetchall()
    return "\n".join(r[0] for r in rows)


def _run(store: Store, **kw: Any) -> dict[str, Any]:
    kw.setdefault("client", _NoLLM())
    kw.setdefault("now", _NOW)
    kw.setdefault("weather_fn", lambda: "")
    return briefing.run_briefing(store, **kw)


def test_quiet_day_stays_explicit(store: Store) -> None:
    r = _run(store)
    assert r == {"articles": 0, "brief_chars": 0, "ref_id": None}
    assert store.list_refs(kind="news") == []


def test_open_alert_still_lands_a_dated_brief(store: Store) -> None:
    raise_alert(
        store,
        source="watchdog",
        fingerprint="embed-backlog",
        title="embed backlog not draining",
        severity="warn",
    )
    sent: list[str] = []
    r = _run(store, sink=sent.append, deliver_to="conv:discord/g/c/t")
    assert r["articles"] == 0 and r["ref_id"] is not None
    assert r["unresolved"] == 1
    text = _brief_text(store, r["ref_id"])
    assert "## Quiet wire" in text and "embed backlog not draining" in text
    assert sent and "## Quiet wire" in sent[0]
    with store.pool.connection() as conn:
        delivered = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'message' AND meta->>'briefing_date' = %s",
            (_NOW.date().isoformat(),),
        ).fetchone()
    assert delivered is not None


def test_decision_item_still_lands_a_dated_brief(store: Store) -> None:
    ask = store.insert_ref(kind="todo", slug=None, title="Which db?", meta={})
    store.add_tag(ask.id, Tag.open("ask-user:which db?"), set_by="system")
    r = _run(store)
    assert r["articles"] == 0 and r["ref_id"] is not None
    text = _brief_text(store, r["ref_id"])
    assert "Waiting on your input (1)" in text and "Which db?" in text


def test_child_failed_flag_lands_a_dated_brief(store: Store) -> None:
    todo = store.insert_ref(kind="todo", slug=None, title="Fix embedder", meta={})
    store.add_tag(todo.id, Tag.open("child-failed:4242"), set_by="system")
    r = _run(store)
    assert r["ref_id"] is not None
    text = _brief_text(store, r["ref_id"])
    assert "Child jobs failed" in text and "Fix embedder" in text


def test_stale_info_alert_is_still_a_quiet_day(store: Store) -> None:
    """An old info-severity nudge is not fresh news — same staleness rule
    the reading brief applies."""
    aid, _ = raise_alert(
        store,
        source="watchdog",
        fingerprint="stuck-doable",
        title="stuck doable todo",
        severity="info",
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = %s WHERE ref_id = %s",
            (_NOW - timedelta(hours=72), aid),
        )
    r = _run(store)
    assert r == {"articles": 0, "brief_chars": 0, "ref_id": None}


def test_fresh_info_alert_counts(store: Store) -> None:
    raise_alert(
        store,
        source="watchdog",
        fingerprint="stuck-doable",
        title="stuck doable todo",
        severity="info",
    )
    r = _run(store)
    assert r["ref_id"] is not None


def test_news_still_takes_the_llm_path(store: Store) -> None:
    """Headlines present → the unresolved probe never runs; the LLM brief
    is unchanged."""
    store.insert_ref(kind="news", slug="n-1", title="overnight", meta={})
    calls = []

    class _LLM:
        def complete(self, messages: Any) -> Any:
            calls.append(messages)
            return type("R", (), {"text": "the digest", "model": "m"})()

    r = briefing.run_briefing(store, client=_LLM(), now=_NOW, weather_fn=lambda: "")
    assert r["articles"] == 1 and r["ref_id"] is not None
    assert r["unresolved"] == 0 and len(calls) == 1
