"""Deterministic paper-hygiene heals — card drift, superseded chains, links."""

from __future__ import annotations

from typing import Any

from precis.ingest.paper_hygiene import (
    collapse_superseded_chains,
    heal_drifted_cards,
    migrate_dangling_paper_links,
)
from precis.store import Store


def _paper(store: Store, *, slug: str, title: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title).id


def _card(store: Store, ref_id: int, text: str) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, -1, 'card_combined', %s)",
                (ref_id, text),
            )


def _card_text(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id=%s AND chunk_kind='card_combined'",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
    assert ref is not None
    return ref.meta or {}


# ── card drift ────────────────────────────────────────────────────


def test_heal_rebuilds_stale_card(store: Store) -> None:
    rid = _paper(store, slug="fixed20", title="A Properly Recovered Paper Title")
    _card(store, rid, "wang.dvi\n\nsome stale junk from the old import")

    healed = heal_drifted_cards(store, dry_run=False)
    assert rid in healed
    assert _card_text(store, rid).startswith("A Properly Recovered Paper Title")


def test_heal_skips_card_that_matches_modulo_punctuation(store: Store) -> None:
    """An en-dash / markup difference is not drift — leave it alone."""
    rid = _paper(store, slug="ok20", title="Non-Watson-Crick Interactions in DNA")
    # Card carries the same title with different punctuation/markup.
    _card(store, rid, "Non–Watson–Crick Interactions in DNA\n\nA. Author")

    assert heal_drifted_cards(store, dry_run=False) == []


def test_heal_dry_run_writes_nothing(store: Store) -> None:
    rid = _paper(store, slug="dry20", title="Another Real Title For The Paper")
    _card(store, rid, "cgibbs.dvi\n\nstale")
    assert heal_drifted_cards(store, dry_run=True) == [rid]
    assert _card_text(store, rid).startswith("cgibbs.dvi")  # untouched


# ── superseded-chain collapse ─────────────────────────────────────


def test_collapse_points_chain_at_terminal_survivor(store: Store) -> None:
    final = _paper(store, slug="final20", title="Survivor Paper")
    mid = _paper(store, slug="mid20", title="Middle Stub")
    head = _paper(store, slug="head20", title="Head Stub")
    with store.tx() as conn:
        store.stamp_ref_meta(mid, {"superseded_by": final}, conn=conn)
        store.stamp_ref_meta(head, {"superseded_by": mid}, conn=conn)
        store.soft_delete_ref(mid, conn=conn)
        store.soft_delete_ref(head, conn=conn)

    fixed = collapse_superseded_chains(store, dry_run=False)
    assert (head, final) in fixed
    assert _meta(store, head)["superseded_by"] == final


# ── dangling links ────────────────────────────────────────────────


def test_migrate_repoints_dangling_link_to_survivor(store: Store) -> None:
    survivor = _paper(store, slug="surv20", title="The Held Survivor")
    dead = _paper(store, slug="dead20", title="Retired Duplicate")
    citer = _paper(store, slug="citer20", title="A Citing Paper")
    with store.tx() as conn:
        store.add_link(
            src_ref_id=citer,
            dst_ref_id=dead,
            relation="related-to",
            set_by="system",
            conn=conn,
        )
        store.stamp_ref_meta(dead, {"superseded_by": survivor}, conn=conn)
        store.soft_delete_ref(dead, conn=conn)

    acted = migrate_dangling_paper_links(store, dry_run=False)
    assert len(acted) == 1
    with store.pool.connection() as conn:
        dst = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id=%s AND relation='related-to'",
            (citer,),
        ).fetchone()
    assert dst is not None and int(dst[0]) == survivor


def test_migrate_leaves_supersedes_edge_alone(store: Store) -> None:
    """The supersedes audit edge legitimately points at the dead ref."""
    survivor = _paper(store, slug="surv21", title="Survivor Two")
    dead = _paper(store, slug="dead21", title="Retired Two")
    with store.tx() as conn:
        store.add_link(
            src_ref_id=survivor,
            dst_ref_id=dead,
            relation="supersedes",
            set_by="system",
            conn=conn,
        )
        store.stamp_ref_meta(dead, {"superseded_by": survivor}, conn=conn)
        store.soft_delete_ref(dead, conn=conn)

    assert migrate_dangling_paper_links(store, dry_run=False) == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT dst_ref_id FROM links WHERE relation='supersedes' AND src_ref_id=%s",
            (survivor,),
        ).fetchone()
    assert row is not None and int(row[0]) == dead  # untouched


# ── stranded OA fetches ───────────────────────────────────────────


def _fetch_event(
    store: Store,
    ref_id: int,
    event: str,
    *,
    hours_ago: float,
    source: str = "fetcher:s2",
) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, ts) "
                "VALUES (%s, %s, %s, now() - make_interval(hours => %s))",
                (ref_id, source, event, hours_ago),
            )


def _fetcher_event_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_events "
            "WHERE ref_id=%s AND source LIKE 'fetcher:%%'",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def test_requeue_clears_backoff_on_stranded_fetch(store: Store) -> None:
    """A stub with an old fetch_ok but no PDF is re-queued: fetcher events
    deleted (backoff reset) and a one-shot guard stamped."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="stranded18", title="Stranded Fetch Paper")
    # Several failed legs plus the black-holed fetch_ok, all >48h old.
    _fetch_event(store, rid, "no_oa_version", hours_ago=100)
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)

    out = requeue_stranded_fetches(store, dry_run=False)
    assert rid in out
    assert _fetcher_event_count(store, rid) == 0  # backoff reset
    marker = _meta(store, rid).get("oa_requeued")
    assert marker and marker["prior_attempts"] == 2


def test_requeue_is_one_shot_guarded(store: Store) -> None:
    """A stub already carrying the oa_requeued marker is never swept again."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="guarded18", title="Already Requeued Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    assert requeue_stranded_fetches(store, dry_run=False) == [rid]
    # A fresh black-holed fetch_ok arrives after the re-queue…
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    # …but the marker blocks a second re-queue (so it can't spin).
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1  # left intact this time


def test_requeue_skips_recent_fetch_ok(store: Store) -> None:
    """A just-downloaded PDF still mid-ingest (fetch_ok < threshold) is left
    alone — no premature re-queue."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="recent18", title="Recently Fetched Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=1)
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1


def test_requeue_skips_held_paper(store: Store) -> None:
    """A paper that actually landed a PDF is not a stranded stub."""
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="held18", title="Successfully Ingested Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    sha = f"{rid:064d}"
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
                "ON CONFLICT (pdf_sha256) DO NOTHING",
                (sha, sha),
            )
            conn.execute("UPDATE refs SET pdf_sha256=%s WHERE ref_id=%s", (sha, rid))
    assert requeue_stranded_fetches(store, dry_run=False) == []
    assert _fetcher_event_count(store, rid) == 1  # untouched


def test_requeue_dry_run_writes_nothing(store: Store) -> None:
    from precis.ingest.paper_hygiene import requeue_stranded_fetches

    rid = _paper(store, slug="drystr18", title="Dry Run Stranded Paper")
    _fetch_event(store, rid, "fetch_ok", hours_ago=96)
    assert requeue_stranded_fetches(store, dry_run=True) == [rid]
    assert _fetcher_event_count(store, rid) == 1  # untouched
    assert "oa_requeued" not in _meta(store, rid)
