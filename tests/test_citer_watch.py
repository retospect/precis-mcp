"""gr513769: the OpenAlex cited-by watch (workers/citer_watch.py).

``fetch_works`` is injected with recorded OpenAlex work dicts — no
network anywhere in this file (the real fetcher lives behind
``openalex_meta.fetch_citing_works`` and the shared ``openalex``
limiter). Everything else runs against the real test store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from precis.store.types import ChunkInsert
from precis.workers.citer_watch import (
    DAILY_INGEST_CAP,
    FIRST_LOOKBACK_DAYS,
    SERVICE_NAME,
    citer_watch_enabled,
    run_citer_watch_pass,
)
from precis.workers.scheduler import CADENCES
from precis.workers.service_config import set_service_prio


def _paper(
    store,
    *,
    oa: str | None = "W1",
    accesses: int = 2,
    seen_days_ago: int = 0,
    pdf: bool = True,
) -> int:
    """A held paper seed: openalex id in meta, pdf_sha256 set, body
    chunk bumped ``accesses`` times (the use weight the seed gate reads).
    ``pdf=False`` mints a stub, which is not "held"."""
    meta = {"openalex": {"id": oa}} if oa else {}
    ref = store.insert_ref(
        kind="paper",
        slug=f"cwp{uuid4().hex[:8]}",
        title="Seed paper",
        meta=meta,
    )
    if pdf:
        sha = uuid4().hex + uuid4().hex[:32]
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 1, 'x.pdf')",
                (sha, sha),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (sha, ref.id),
            )
    chunks = store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="body text")])
    for _ in range(accesses):
        store.chunks.bump_salience([chunks[0].id])
    if seen_days_ago:
        with store.tx() as conn:
            conn.execute(
                "UPDATE chunks SET last_seen = %s WHERE ref_id = %s",
                (datetime.now(UTC) - timedelta(days=seen_days_ago), ref.id),
            )
    return int(ref.id)


def _work(
    oid: str,
    cites: list[str],
    *,
    title: str = "Citing work",
    doi: str | None = None,
    year: int = 2026,
    retracted: bool = False,
) -> dict:
    return {
        "id": f"https://openalex.org/{oid}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "publication_year": year,
        "referenced_works": [f"https://openalex.org/{w}" for w in cites],
        "is_retracted": retracted,
    }


def _enable(store) -> None:
    set_service_prio(store, "*", SERVICE_NAME, 1, actor="test")


def _stubs(store) -> list[tuple[int, str]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT ref_id, title FROM refs WHERE kind = 'paper' "
            "AND pdf_sha256 IS NULL AND retired_at IS NULL"
        ).fetchall()


def _digest_count(store) -> int:
    with store.pool.connection() as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM refs r "
                "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
                "AND t.value = 'digest:citer-watch'",
            ).fetchone()[0]
        )


def test_dark_until_service_config_lights(store) -> None:
    """No row anywhere → no fetch, no writes — the ship-disabled gate."""
    _paper(store)
    calls: list = []

    def fetch(ids, since):
        calls.append(ids)
        return []

    r = run_citer_watch_pass(store, fetch_works=fetch)
    assert r == {"claimed": 0, "ok": 0, "failed": 0, "found": 0, "digest": 0}
    assert calls == []
    assert not citer_watch_enabled(store)
    _enable(store)
    assert citer_watch_enabled(store)


def test_poll_mints_scored_stub_and_digest(store) -> None:
    _enable(store)
    _paper(store, oa="W11")
    _paper(store, oa="W22")

    works = [
        _work("W900", ["W11", "W22"], title="Cites both", doi="10.1/x"),
        _work("W901", ["W11"], title="Cites one", doi="10.1/y"),
        _work("W902", ["W555"], title="Cites none held"),  # no seed -> dropped
        _work("W903", ["W11"], retracted=True),  # retracted -> dropped
    ]

    def fetch(ids, since):
        assert set(ids) == {"W11", "W22"}
        return works

    r = run_citer_watch_pass(store, fetch_works=fetch)
    assert r["failed"] == 0
    assert r["found"] == 2
    assert r["ok"] == 2

    stubs = dict(_stubs(store))
    assert len(stubs) == 2
    # Both seeds' works minted as stubs carrying ids + provenance tags.
    with store.pool.connection() as conn:
        ids = conn.execute(
            "SELECT id_kind, id_value, ref_id FROM ref_identifiers "
            "WHERE id_kind IN ('openalex', 'doi') ORDER BY id_value"
        ).fetchall()
    by_val = {(k, v): rid for k, v, rid in ids}
    assert ("openalex", "W900") in by_val
    assert ("doi", "10.1/x") in by_val
    w900 = by_val[("openalex", "W900")]
    with store.pool.connection() as conn:
        tags = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t "
            "ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s",
            (w900,),
        ).fetchall()
    assert {t[0] for t in tags} >= {"source:openalex", "discovered-via:citer-watch"}

    assert _digest_count(store) == 1
    assert r["digest"] > 0
    # Clean run -> last_poll advanced to today.
    assert store.get_setting("citer_watch:last_poll")


def test_seed_gate_use_weight_and_recency(store) -> None:
    _enable(store)
    hot = _paper(store, oa="W1")  # qualifies
    _paper(store, oa="W2", accesses=1)  # under SEED_MIN_ACCESSES
    _paper(store, oa="W3", seen_days_ago=400)  # used, but dormant > window
    _paper(store, oa=None)  # no OpenAlex id to poll on
    _paper(store, oa="W4", pdf=False)  # a stub is not held

    seen_ids: list[list[str]] = []

    def fetch(ids, since):
        seen_ids.append(list(ids))
        return [_work("W9", ["W1"])]

    run_citer_watch_pass(store, fetch_works=fetch)
    assert seen_ids == [["W1"]]
    del hot


def test_score_prefers_multi_seed_citers_under_cap(store) -> None:
    _enable(store)
    _paper(store, oa="W1")
    _paper(store, oa="W2")

    works = [
        _work("WA", ["W1"], title="one seed"),
        _work("WB", ["W1", "W2"], title="two seeds"),  # ranks above WA
    ]

    def fetch(ids, since):
        return works

    r = run_citer_watch_pass(store, fetch_works=fetch, daily_cap=1)
    assert r["ok"] == 1
    stubs = dict(_stubs(store))
    assert list(stubs.values()) == ["two seeds"]


def test_daily_cap_is_a_spend_cap(store) -> None:
    _enable(store)
    _paper(store, oa="W1")
    works = [
        _work(f"WX{i}", ["W1"], title=f"citer {i}") for i in range(DAILY_INGEST_CAP + 5)
    ]

    def fetch(ids, since):
        return works

    r = run_citer_watch_pass(store, fetch_works=fetch)
    assert r["ok"] == DAILY_INGEST_CAP
    spent = int(store.get_setting(f"citer_watch:minted:{datetime.now(UTC).date()}"))
    assert spent == DAILY_INGEST_CAP

    # A second fire the same UTC day mints nothing more.
    r2 = run_citer_watch_pass(store, fetch_works=fetch)
    assert r2["ok"] == 0


def test_collapse_hit_does_not_spend_cap(store) -> None:
    """A citer we already hold (its doi is registered) is a no-op mint —
    dedup, not spend."""
    _enable(store)
    _paper(store, oa="W1")
    held = _paper(store, oa="W7")  # another held paper
    store.insert_ref_identifiers(held, [("doi", "10.9/held", "test")])

    works = [
        _work("WNEW1", ["W1"], title="already held citer", doi="10.9/held"),
        _work("WNEW2", ["W1"], title="new citer"),
    ]

    def fetch(ids, since):
        return works

    r = run_citer_watch_pass(store, fetch_works=fetch, daily_cap=1)
    # The held one collapses (created=False, free), the new one mints.
    assert r["ok"] == 1
    stubs = dict(_stubs(store))
    assert list(stubs.values()) == ["new citer"]


def test_failed_batch_keeps_poll_window(store) -> None:
    """A raised batch means last_poll does NOT advance — that slice of
    the window is re-polled next fire, never lost."""
    _enable(store)
    _paper(store, oa="W1")

    def fetch(ids, since):
        raise RuntimeError("openalex 500")

    r = run_citer_watch_pass(store, fetch_works=fetch)
    assert r["failed"] == 1
    assert store.get_setting("citer_watch:last_poll") is None


def test_first_run_lookback_window(store) -> None:
    """No last_poll row -> the first fire looks back FIRST_LOOKBACK_DAYS,
    not to the dawn of time."""
    _enable(store)
    _paper(store, oa="W1")
    seen_since: list[str] = []

    def fetch(ids, since):
        seen_since.append(since)
        return []

    run_citer_watch_pass(store, fetch_works=fetch)
    assert seen_since == [
        (datetime.now(UTC).date() - timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()
    ]


def test_cadence_registered_daily_dark_safe(store) -> None:
    """The cadence exists (the standing trigger) and fires daily —
    the pass dark-gates inside, so the lease advancing while dark is
    the design, not a live schedule."""
    cad = next(c for c in CADENCES if c.name == "citer_watch")
    assert 24 * 3600 <= cad.interval_s < 25 * 3600
    assert cad.service is None  # gate lives inside the pass, not the lease
    assert not cad.spends
