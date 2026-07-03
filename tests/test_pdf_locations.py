"""``PdfMixin`` — ``pdfs.storage_path`` accessors + the per-host
``pdf_locations`` presence ledger (migration 0052).

Backed by the shared test DB (the ``store`` fixture auto-skips when no
postgres is reachable).
"""

from __future__ import annotations

from precis.store import Store


def _held_paper(
    store: Store,
    *,
    cite_key: str,
    sha: str,
    storage_path: str = "",
    aliases: tuple[str, ...] = (),
) -> int:
    """A paper ref that holds a PDF: a ``pdfs`` row (with the given
    ``storage_path``), ``refs.pdf_sha256`` set, and its cite_key + aliases
    registered in ``ref_identifiers``."""
    ref = store.insert_ref(kind="paper", slug=cite_key, title="X", meta={})
    with store.pool.connection() as conn:
        for key in (cite_key, *aliases):
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'cite_key', %s, 'manual') ON CONFLICT DO NOTHING",
                (ref.id, key),
            )
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, %s) "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha, storage_path),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
    return ref.id


def _age_location(store: Store, sha: str, host: str, *, days: float) -> None:
    """Back-date a ledger row's ``seen_at`` to simulate an aged verdict."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pdf_locations SET seen_at = now() - %s::interval "
            "WHERE pdf_sha256 = %s AND host = %s",
            (f"{days} days", sha, host),
        )


def test_storage_path_prefer_and_blank_fallback(store: Store) -> None:
    """``pdf_storage_path`` returns a real recorded path, but treats the
    ingest writer's blank ``''`` as unknown (→ ``None``)."""
    sha_a = "a" * 64
    sha_b = "b" * 64
    _held_paper(
        store, cite_key="withpath", sha=sha_a, storage_path="/corpus/w/withpath.pdf"
    )
    _held_paper(store, cite_key="blankpath", sha=sha_b, storage_path="")
    assert store.pdf_storage_path(sha_a) == "/corpus/w/withpath.pdf"
    assert store.pdf_storage_path(sha_b) is None
    # unknown sha → None (no row)
    assert store.pdf_storage_path("c" * 64) is None


def test_set_pdf_storage_path(store: Store) -> None:
    """The /rename correction path: ``set_pdf_storage_path`` overwrites the
    recorded path; blank inputs are no-ops."""
    sha = "a" * 64
    _held_paper(store, cite_key="mover", sha=sha, storage_path="/corpus/m/mover.pdf")
    assert store.set_pdf_storage_path(sha, "/corpus/p/piela07.pdf") is True
    assert store.pdf_storage_path(sha) == "/corpus/p/piela07.pdf"
    assert store.set_pdf_storage_path(sha, "") is False
    assert store.set_pdf_storage_path("", "/x") is False


def test_ledger_present_absent_unknown(store: Store) -> None:
    """The three presence states: unknown (no row), held (fresh present
    row), missing (checked but no fresh present row)."""
    sha = "a" * 64
    _held_paper(store, cite_key="kong24", sha=sha)
    # Never checked → unknown: not held, and NOT flagged missing.
    assert store.pdf_held_anywhere(sha, ttl_days=7) is False
    assert store.pdf_missing(sha, ttl_days=7) is False
    # A present verdict → held, not missing.
    store.record_pdf_location(sha, "melchior", "/corpus/k/kong24.pdf")
    assert store.pdf_held_anywhere(sha, ttl_days=7) is True
    assert store.pdf_missing(sha, ttl_days=7) is False
    # The same host later reports it absent → checked, no fresh copy → missing.
    store.record_pdf_location(sha, "melchior", "")
    assert store.pdf_held_anywhere(sha, ttl_days=7) is False
    assert store.pdf_missing(sha, ttl_days=7) is True


def test_ledger_held_by_any_host(store: Store) -> None:
    """Presence is corpus-wide: one host absent, another present → held."""
    sha = "a" * 64
    _held_paper(store, cite_key="kong24", sha=sha)
    store.record_pdf_location(sha, "caspar", "")  # caspar doesn't mount it
    store.record_pdf_location(sha, "melchior", "/corpus/k/kong24.pdf")
    assert store.pdf_held_anywhere(sha, ttl_days=7) is True
    assert store.pdf_missing(sha, ttl_days=7) is False


def test_ledger_ttl_expiry(store: Store) -> None:
    """A stale present verdict ages out of the TTL window → missing again."""
    sha = "a" * 64
    _held_paper(store, cite_key="kong24", sha=sha)
    store.record_pdf_location(sha, "melchior", "/corpus/k/kong24.pdf")
    _age_location(store, sha, "melchior", days=10)
    assert store.pdf_held_anywhere(sha, ttl_days=7) is False
    # It has still been *checked* (a row exists), just not freshly → missing.
    assert store.pdf_missing(sha, ttl_days=7) is True


def test_pdfs_due_for_host(store: Store) -> None:
    """Due = no verdict for the host, or one older than the refresh window;
    every cite_key alias + slug is aggregated for the resolver to probe."""
    sha = "a" * 64
    _held_paper(store, cite_key="smith2024", sha=sha, aliases=("smithbook",))
    due = store.pdfs_due_for_host("melchior", refresh_hours=6, limit=50)
    assert [d.pdf_sha256 for d in due] == [sha]
    assert set(due[0].cite_keys) == {"smith2024", "smithbook"}
    # Once a fresh verdict is recorded, the sha is no longer due…
    store.record_pdf_location(sha, "melchior", "/corpus/s/smith2024.pdf")
    assert store.pdfs_due_for_host("melchior", refresh_hours=6, limit=50) == []
    # …but a zero refresh window forces a re-check, and another host is
    # always due (it has no verdict of its own).
    assert len(store.pdfs_due_for_host("melchior", refresh_hours=0, limit=50)) == 1
    assert len(store.pdfs_due_for_host("caspar", refresh_hours=6, limit=50)) == 1


def test_location_cascades_on_pdf_delete(store: Store) -> None:
    """A ``pdf_locations`` row is meaningless once the held PDF is gone —
    the FK cascade drops it."""
    sha = "a" * 64
    _held_paper(store, cite_key="kong24", sha=sha)
    store.record_pdf_location(sha, "melchior", "/corpus/k/kong24.pdf")
    with store.pool.connection() as conn:
        # Detach the ref first (refs.pdf_sha256 FK), then drop the pdfs row.
        conn.execute("UPDATE refs SET pdf_sha256 = NULL WHERE pdf_sha256 = %s", (sha,))
        conn.execute("DELETE FROM pdfs WHERE pdf_sha256 = %s", (sha,))
        left = conn.execute(
            "SELECT count(*) FROM pdf_locations WHERE pdf_sha256 = %s", (sha,)
        ).fetchone()
    assert left is not None and left[0] == 0
