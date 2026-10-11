"""gr462088: the WATCH:* cfp re-check (workers/cfp_watch.py), fake fetch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from precis.store import Tag
from precis.workers.cfp_watch import META_KEY, run_cfp_watch_pass, snapshot
from precis.workers.scheduler import CADENCES

PAGE = "<html><body><p>Submission deadline: 15 March 2027</p><p>Hello</p></body></html>"
MOVED = PAGE.replace("15 March 2027", "1 April 2027")


def _cfp(store, *, url: str | None = "https://example.org/call", watch=True):
    meta = {"source_url": url} if url else {}
    ref = store.insert_ref(
        kind="cfp", slug=f"cw{uuid4().hex[:8]}", title="Call X", meta=meta
    )
    if watch:
        store.add_tag(ref.id, Tag.closed("WATCH", "weekly"))
    return ref


def _alerts(store, ref_id: int) -> int:
    with store.pool.connection() as c:
        return c.execute(
            "SELECT count(*) FROM refs WHERE alert_source = 'cfp_watch' "
            "AND fingerprint = %s AND resolved_at IS NULL",
            (f"changed:{ref_id}",),
        ).fetchone()[0]


def test_snapshot_extracts_deadline_dates() -> None:
    assert snapshot(PAGE)["dates"] == ["15 march 2027"]
    assert snapshot(PAGE)["hash"] != snapshot(MOVED)["hash"]


def test_baseline_then_change_alerts(store) -> None:
    ref = _cfp(store)
    pages = [PAGE, PAGE, MOVED]
    t0 = datetime.now(UTC)

    def fetch(url: str) -> str:
        assert url == "https://example.org/call"
        return pages.pop(0)

    r = run_cfp_watch_pass(store, fetch=fetch, now=t0)
    assert (r.claimed, r.ok, r.failed) == (1, 1, 0)
    assert _alerts(store, ref.id) == 0  # baseline only

    run_cfp_watch_pass(store, fetch=fetch, now=t0 + timedelta(days=7))
    assert _alerts(store, ref.id) == 0  # unchanged

    run_cfp_watch_pass(store, fetch=fetch, now=t0 + timedelta(days=14))
    assert _alerts(store, ref.id) == 1
    meta = store.get_ref(kind="cfp", id=ref.id).meta[META_KEY]
    assert meta["dates"] == ["1 april 2027"]
    assert meta["history"][0]["old_dates"] == ["15 march 2027"]


def test_recent_check_throttled_and_missing_url_skipped(store) -> None:
    _cfp(store, url=None)
    ref = _cfp(store)
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return PAGE

    now = datetime.now(UTC)
    run_cfp_watch_pass(store, fetch=fetch, now=now)
    run_cfp_watch_pass(store, fetch=fetch, now=now + timedelta(hours=1))
    assert len(calls) == 1  # second run inside MIN_RECHECK_S; no-url ref skipped
    assert store.get_ref(kind="cfp", id=ref.id).meta[META_KEY]["hash"]


def test_fetch_cap_and_error_isolation(store) -> None:
    refs = [_cfp(store, url=f"https://example.org/{i}") for i in range(3)]

    def fetch(url: str) -> str:
        if url.endswith("/0"):
            raise RuntimeError("boom")
        return PAGE

    r = run_cfp_watch_pass(store, fetch=fetch, max_fetches=2)
    assert r.claimed <= 2
    assert r.failed + r.ok == r.claimed
    del refs


def test_edit_sets_source_url_validated(store) -> None:
    import pytest

    from precis.dispatch import Hub
    from precis.errors import BadInput
    from precis.handlers.cfp import CfpHandler

    ref = _cfp(store, url=None)
    h = CfpHandler(hub=Hub(store=store))
    with pytest.raises(BadInput):
        h.edit(id=ref.id, args={"source_url": "ftp://x/y"})
    assert "source_url" not in store.get_ref(kind="cfp", id=ref.id).meta
    ack = h.edit(id=ref.id, args={"source_url": "https://example.org/call"})
    assert "source_url=https://example.org/call" in ack.body
    assert store.get_ref(kind="cfp", id=ref.id).meta["source_url"] == (
        "https://example.org/call"
    )
    # with another field: both applied
    h.edit(id=ref.id, year=2027, args={"source_url": "https://example.org/b"})
    got = store.get_ref(kind="cfp", id=ref.id)
    assert got.year == 2027 and got.meta["source_url"] == "https://example.org/b"


def test_cadence_registered_weekly() -> None:
    cad = next(c for c in CADENCES if c.name == "cfp_watch")
    assert 7 * 24 * 3600 <= cad.interval_s < 8 * 24 * 3600
