"""``corpus_reconcile`` worker — resolution + the ledger round-trip."""

from __future__ import annotations

from pathlib import Path

from precis.corpus_layout import rebase_onto_local
from precis.store import Store
from precis.store._pdf_ops import DuePdf
from precis.workers.corpus_reconcile import (
    _resolve_local,
    run_corpus_reconcile_pass,
)


def test_rebase_onto_local_crosses_mount_prefix(tmp_path: Path) -> None:
    """An absolute ``storage_path`` written by a differently-mounted host
    resolves here by re-anchoring its ``/papers/…`` suffix under the local
    corpus root's own ``papers`` dir (ADR 0029: Macs /opt/nas, Linux /nas)."""
    # This node mounts the NAS at ``tmp_path`` → corpus root ``.../papers/corpus``.
    local_root = tmp_path / "papers" / "corpus"
    (local_root / "i").mkdir(parents=True)
    f = local_root / "i" / "irie2000.pdf"
    f.write_bytes(b"%PDF")
    # A path a *Mac* recorded — valid there, but the wrong prefix locally.
    foreign = "/opt/nas/botshome/papers/corpus/i/irie2000.pdf"
    assert rebase_onto_local(foreign, (local_root,)) == f
    # storage_path with no ``/papers/`` pivot → no rebase.
    assert rebase_onto_local("/etc/passwd", (local_root,)) is None


def test_resolve_local_rebases_foreign_absolute_path(tmp_path: Path) -> None:
    """`_resolve_local` uses the mount-rebase when the raw absolute
    ``storage_path`` isn't a file on this host."""
    local_root = tmp_path / "papers" / "corpus"
    (local_root / "k").mkdir(parents=True)
    f = local_root / "k" / "kong24.pdf"
    f.write_bytes(b"%PDF")
    due = DuePdf(
        pdf_sha256="a" * 64,
        storage_path="/opt/nas/botshome/papers/corpus/k/kong24.pdf",
        cite_keys=("kong24",),
    )
    assert _resolve_local((local_root,), due) == f


def test_resolve_local_prefers_storage_path(tmp_path: Path) -> None:
    """The recorded ``storage_path`` wins when it points at a real file."""
    f = tmp_path / "elsewhere" / "book.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF")
    due = DuePdf(pdf_sha256="a" * 64, storage_path=str(f), cite_keys=("kong24",))
    assert _resolve_local((tmp_path,), due) == f


def test_resolve_local_falls_back_to_convention(tmp_path: Path) -> None:
    """A blank/stale ``storage_path`` falls through to the cite_key
    convention, trying every alias across every root."""
    # display slug has no file; the alias is filed under its own shard
    (tmp_path / "s").mkdir()
    filed = tmp_path / "s" / "smithbook.pdf"
    filed.write_bytes(b"%PDF")
    due = DuePdf(
        pdf_sha256="a" * 64,
        storage_path="/gone/smith2024.pdf",  # recorded path no longer exists
        cite_keys=("smith2024", "smithbook"),
    )
    assert _resolve_local((tmp_path,), due) == filed
    # nothing on disk anywhere → None
    missing = DuePdf(pdf_sha256="b" * 64, storage_path="", cite_keys=("nope",))
    assert _resolve_local((tmp_path,), missing) is None


def _held_paper(store: Store, *, cite_key: str, sha: str) -> int:
    ref = store.insert_ref(kind="paper", slug=cite_key, title="X", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'cite_key', %s, 'manual') ON CONFLICT DO NOTHING",
            (ref.id, cite_key),
        )
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
    return ref.id


def test_reconcile_pass_records_present_then_absent(
    store: Store, tmp_path: Path
) -> None:
    """End to end: a held PDF on disk records a present verdict (→ not
    missing); once the file is gone a re-check records absent (→ missing)."""
    sha = "a" * 64
    _held_paper(store, cite_key="kong24", sha=sha)
    # File present under the convention shard.
    (tmp_path / "k").mkdir()
    pdf = tmp_path / "k" / "kong24.pdf"
    pdf.write_bytes(b"%PDF")

    r1 = run_corpus_reconcile_pass(store, (tmp_path,), "melchior", limit=50)
    assert (r1.claimed, r1.ok, r1.failed) == (1, 1, 0)
    assert store.pdf_missing(sha, ttl_days=7) is False

    # File vanishes; age the fresh verdict past the refresh window so the
    # pass re-checks it (mirrors real time passing — the default refresh is
    # floored at 0.1h, so we can't force immediacy via env).
    pdf.unlink()
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pdf_locations SET seen_at = now() - interval '1 day' "
            "WHERE pdf_sha256 = %s AND host = 'melchior'",
            (sha,),
        )
    r2 = run_corpus_reconcile_pass(store, (tmp_path,), "melchior", limit=50)
    assert (r2.claimed, r2.ok, r2.failed) == (1, 0, 1)
    assert store.pdf_missing(sha, ttl_days=7) is True


def test_reconcile_no_corpus_dirs_is_noop(store: Store) -> None:
    """No roots configured on this node → the pass claims nothing."""
    r = run_corpus_reconcile_pass(store, (), "melchior", limit=50)
    assert (r.claimed, r.ok, r.failed) == (0, 0, 0)
