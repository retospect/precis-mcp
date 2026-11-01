"""gr543714: the novel-ideas digest (workers/idea_feed.py).

The feed parser, the HN Firebase fetch and the judge are all injected —
no network, no model anywhere in this file (the real legs live behind
``_default_parse_feed`` / ``_hn_get_json`` → safe_get → shared limiter
rows, and ``_default_judge`` → the router's SMALL tier). Everything else
runs against the real test store.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from precis.workers.idea_feed import (
    JUDGE_SOURCE,
    SERVICE_NAME,
    idea_feed_enabled,
    run_idea_feed_pass,
)
from precis.workers.scheduler import CADENCES
from precis.workers.service_config import set_service_prio


def _feed(entries):
    def parse(url):
        return SimpleNamespace(entries=list(entries))

    return parse


def _entry(link, title, summary="", guid=""):
    return SimpleNamespace(link=link, title=title, summary=summary, id=guid)


def _hn(items=None, lists=None, fail=None):
    def get(path):
        if fail is not None:
            raise fail
        if path.startswith("item/"):
            return (items or {}).get(int(path.split("/")[1].removesuffix(".json")))
        if path.endswith(".json"):
            return list((lists or {}).get(path.removesuffix(".json"), []))
        raise AssertionError(f"unexpected hn path {path}")

    return get


def _hn_item(i, *, title="Show HN: a thing", url="https://example.com/x", typ="story"):
    return {"id": i, "type": typ, "title": title, "url": url, "score": 10}


def _judge(text):
    def call(prompt):
        return text

    return call


def _recording(calls, result):
    """Injectable that records each arg then returns ``result`` —
    the lambda-`append` pattern trips mypy's func-returns-value."""

    def call(arg):
        calls.append(arg)
        return result

    return call


def _enable(store) -> None:
    set_service_prio(store, "*", SERVICE_NAME, 1, actor="test")


def _digest_todos(store):
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT r.ref_id, r.title FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t USING (tag_id) "
            "WHERE r.kind = 'todo' AND r.retired_at IS NULL "
            "AND t.value = %s",
            ("digest:idea-feed",),
        ).fetchall()


def test_dark_until_service_config_lights(store) -> None:
    """No row anywhere → no fetch, no judge, no writes — the
    ship-disabled gate."""
    calls: list[object] = []
    r = run_idea_feed_pass(
        store,
        parse_feed=_recording(calls, SimpleNamespace(entries=[])),
        hn=_recording(calls, []),
        judge=_recording(calls, "[]"),
    )
    assert r == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
        "found": 0,
        "judged": 0,
        "digest": 0,
    }
    assert calls == []
    assert not idea_feed_enabled(store)
    _enable(store)
    assert idea_feed_enabled(store)


def test_hits_mint_one_digest_todo(store) -> None:
    """One arXiv feed + the HN lists; the judge names two hits → one
    digest todo listing them with links and whys."""
    _enable(store)
    feed = _feed(
        [
            _entry(
                "https://arxiv.org/abs/2601.00001v2",
                "A graph memory for agents",
                "abstract one",
            ),
            _entry("https://arxiv.org/abs/2601.00002v1", "Dull incremental work"),
        ]
    )
    hn = _hn(
        items={10: _hn_item(10, title="Show HN: local LLM router")},
        lists={"topstories": [10], "showstories": [10]},
    )
    judge_text = json.dumps(
        [
            {"key": "arxiv:2601.00001", "score": 0.9, "why": "feeds pillar 1"},
            {"key": "hn:10", "score": 0.7, "why": "local compute serving"},
            {"key": "arxiv:2601.00002", "score": 0.1, "why": "boring"},
        ]
    )
    r = run_idea_feed_pass(
        store,
        parse_feed=feed,
        hn=hn,
        judge=_judge(judge_text),
        arxiv_categories="cs.AI",
    )
    assert r["failed"] == 0
    assert r["ok"] == 2
    todos = _digest_todos(store)
    assert len(todos) == 1
    tid = todos[0][0]
    tags = {t.value for t in store.tags_for(tid)}
    assert "idea-feed" in tags
    with store.pool.connection() as conn:
        body = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0", (tid,)
        ).fetchone()[0]
    assert "A graph memory for agents" in body
    assert "feeds pillar 1" in body
    assert "https://arxiv.org/abs/2601.00001v2" in body
    assert "Show HN: local LLM router" in body
    assert store.get_setting("idea_feed:last_poll")
    seen = json.loads(store.get_setting("idea_feed:seen"))
    assert {"arxiv:2601.00001", "arxiv:2601.00002", "hn:10"} <= set(seen)


def test_seen_and_ingested_dedupe(store) -> None:
    """An item already in ``idea_feed:seen`` or already an ingested ref
    never reaches the judge; only fresh keys do."""
    _enable(store)
    store.set_setting(
        "idea_feed:seen",
        json.dumps({"arxiv:2601.00009": "2026-10-31"}),
    )
    # Already-ingested arXiv ref: an identifier row on a live paper.
    held = store.insert_ref(kind="paper", slug="held-one", title="held", meta={})
    store.insert_ref_identifiers(held.id, [("arxiv", "2601.00008", "test")])
    prompts: list[str] = []
    feed = _feed(
        [
            _entry("https://arxiv.org/abs/2601.00009v1", "seen already"),
            _entry("https://arxiv.org/abs/2601.00008v1", "already ingested"),
            _entry("https://arxiv.org/abs/2601.00007v1", "fresh one"),
        ]
    )
    r = run_idea_feed_pass(
        store,
        parse_feed=feed,
        hn=_hn(lists={"topstories": [], "showstories": []}),
        judge=_recording(prompts, "[]"),
        arxiv_categories="cs.AI",
    )
    assert r["found"] == 1
    assert r["judged"] == 1
    assert prompts and "fresh one" in prompts[0]
    assert "seen already" not in prompts[0]
    assert "already ingested" not in prompts[0]


def test_known_url_dedupe(store) -> None:
    """A candidate whose canonical URL is already a ref's ``meta.url``
    (the news/web ingest record) is skipped."""
    _enable(store)
    store.insert_ref(
        kind="news",
        slug="held-news-article",
        title="held news",
        meta={"url": "https://example.com/x"},
    )
    r = run_idea_feed_pass(
        store,
        parse_feed=_feed([]),
        hn=_hn(items={5: _hn_item(5)}, lists={"topstories": [5]}),
        judge=lambda p: "[]",
        arxiv_categories="",
    )
    assert r["found"] == 0
    assert r["judged"] == 0
    assert r["digest"] == 0


def test_judge_daily_cap_skips_call(store) -> None:
    """When today's ``llm_call_log`` spend for the op is at the cap the
    judge never runs — and candidates are still marked seen."""
    _enable(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log (source, cost_usd, placement) "
            "VALUES (%s, %s, 'cloud')",
            (JUDGE_SOURCE, 5.0),
        )
        conn.commit()
    calls: list[object] = []
    r = run_idea_feed_pass(
        store,
        parse_feed=_feed([_entry("https://arxiv.org/abs/2601.00003v1", "new")]),
        hn=_hn(lists={"topstories": [], "showstories": []}),
        judge=_recording(calls, "[]"),
        arxiv_categories="cs.AI",
        daily_usd=1.0,
    )
    assert calls == []
    assert r["digest"] == 0
    assert r["failed"] == 0
    assert "arxiv:2601.00003" in json.loads(store.get_setting("idea_feed:seen"))


def test_judge_error_marks_failed_no_digest(store) -> None:
    """A raised judge call fails the pass slice and mints nothing — the
    cursor stays parked so the fire re-polls."""
    _enable(store)
    r = run_idea_feed_pass(
        store,
        parse_feed=_feed([_entry("https://arxiv.org/abs/2601.00004v1", "new")]),
        hn=_hn(lists={"topstories": [], "showstories": []}),
        judge=lambda p: (_ for _ in ()).throw(RuntimeError("down")),
        arxiv_categories="cs.AI",
    )
    assert r["failed"] == 1
    assert r["digest"] == 0
    assert store.get_setting("idea_feed:last_poll") is None


def test_feed_failure_counts_failed(store) -> None:
    """A feed that raises counts ``failed``; the clean HN leg still runs."""
    _enable(store)

    def bad(url):
        raise RuntimeError("feed down")

    r = run_idea_feed_pass(
        store,
        parse_feed=bad,
        hn=_hn(items={1: _hn_item(1)}, lists={"topstories": [1]}),
        judge=_judge("[]"),
        arxiv_categories="cs.AI",
    )
    assert r["failed"] == 1
    assert store.get_setting("idea_feed:last_poll") is None


def test_cadence_registered_daily_dark_safe(store) -> None:
    """The cadence exists (the standing trigger), fires daily, and gates
    on the spend flag — the dark gate lives inside the pass."""
    cad = next(c for c in CADENCES if c.name == "idea_feed")
    assert 24 * 3600 < cad.interval_s < 26 * 3600
    assert cad.service is None  # gate lives inside the pass, not the lease
    assert cad.spends


def test_llm_op_registered_small(store) -> None:
    """The judge is a steerable op on the smallest router tier."""
    from precis.utils.llm.operations import LLM_OPERATIONS
    from precis.utils.llm.router import Tier

    op = LLM_OPERATIONS[JUDGE_SOURCE]
    assert op.tier is Tier.SMALL


def test_seen_map_ages_out(store) -> None:
    """Entries older than the retention window are pruned on read."""
    _enable(store)
    old = "2020-01-01"
    store.set_setting("idea_feed:seen", json.dumps({"arxiv:1999.00001": old}))
    run_idea_feed_pass(
        store,
        parse_feed=_feed([]),
        hn=_hn(lists={"topstories": [], "showstories": []}),
        judge=_judge("[]"),
        arxiv_categories="cs.AI",
    )
    assert json.loads(store.get_setting("idea_feed:seen")) == {}
