"""paper-monitor-ingest-dir — watch a directory and ingest new PDFs.

For every `*.pdf` at the top level of the watched directory:

    1. Run `acatome_extract.pipeline.extract` to produce a `.acatome`
       bundle (PDF → marker layout → embeddings → enrichment).
    2. Insert the bundle into the precis store via
       `Store.ingest_bundle(...)` (idempotent on DOI / pdf_hash /
       arxiv_id).
    3. On success, move the PDF (and the resulting bundle) into
       `<watch_dir>/completed/`.
    4. On failure, move the PDF into `<watch_dir>/errors/` alongside a
       `<stem>.error.log` traceback so the retry queue is hand-curatable.

Loops forever (default poll = 10s). Pass `--once` for a single sweep.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import DEFAULT_INGEST_DIR, make_embedder_for, open_store

# arXiv id embedded in a filename, e.g. ``1705.02630.pdf``,
# ``1705.02630v3.pdf``, ``cond-mat-0211262.pdf``. Matches both the
# post-2007 ``YYMM.NNNN`` form and the pre-2007 ``archive/YYMMNNN`` form
# (the slash often becomes a dash when used as a filename).
_ARXIV_FILENAME_RE = re.compile(
    r"(?:^|[/\-_])(\d{4}\.\d{4,5}|[a-z\-]+[\-/]\d{7})(?:v\d+)?\.pdf$",
    re.IGNORECASE,
)


def _sha256_file(path: Path) -> str:
    """Stream-hash a file. ~50 ms for a typical 5 MB PDF."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug_for_ref_id(store, ref_id: int) -> str | None:
    """Resolve a paper ``refs.id`` back to its slug for log output."""
    ref = store.get_ref(kind="paper", id=ref_id)
    return ref.slug if ref is not None else None


def _arxiv_id_from_filename(pdf: Path) -> str | None:
    """Best-effort arxiv id pull from the filename. Cheap (regex only)."""
    m = _ARXIV_FILENAME_RE.search(pdf.name)
    if m is None:
        return None
    # Normalise pre-2007 dash-form back to slash-form for the lookup
    # path (``cond-mat-0211262`` -> ``cond-mat/0211262``).
    raw = m.group(1)
    if "/" not in raw and re.match(r"^[a-z\-]+\-\d{7}$", raw, re.IGNORECASE):
        prefix, num = raw.rsplit("-", 1)
        raw = f"{prefix}/{num}"
    return raw


def _pre_flight_known_slug(
    pdf: Path,
    *,
    store,
) -> tuple[str | None, str | None]:
    """Cheap dedup probe before the slow extract+ingest path.

    Returns ``(slug, hit_reason)``. ``slug=None`` means "not in
    precis — run the full pipeline". On a hit, the caller should
    move the PDF to ``completed/`` and skip extraction entirely.

    Cost ordering — bail out at the first hit:
    1. PDF SHA-256 (~50 ms; bit-exact ⇒ same paper).
    2. arXiv id parsed from filename (free; ~0 ms).
    3. Embedded DOI via ``acatome_meta.pdf.extract_pdf_meta``
       (~150–300 ms; PyMuPDF info + first-5-pages regex).

    The CrossRef/Semantic-Scholar ``lookup()`` cascade is **not**
    invoked here — that's 1–5 s and would defeat the purpose for
    genuinely-new papers.
    """
    # --- Check 1: PDF SHA-256 ---
    print("  pre-flight: hashing PDF...", flush=True)
    try:
        pdf_hash = _sha256_file(pdf)
    except Exception as exc:
        print(f"  pre-flight: hash failed ({exc}) — falling through", flush=True)
        return None, None
    print(f"  pre-flight: hash={pdf_hash[:12]} — checking precis", flush=True)
    ref_id = store.find_ref_by_identifier("pdfsha256", pdf_hash, kind="paper")
    if ref_id is not None:
        slug = _slug_for_ref_id(store, ref_id)
        if slug is not None:
            return slug, f"pdfsha256={pdf_hash[:12]}"

    # --- Check 2: arXiv id from filename (free) ---
    arxiv_fn = _arxiv_id_from_filename(pdf)
    if arxiv_fn is not None:
        print(
            f"  pre-flight: arxiv id {arxiv_fn!r} parsed from filename — checking precis",
            flush=True,
        )
        ref_id = store.find_ref_by_identifier("arxiv", arxiv_fn, kind="paper")
        if ref_id is not None:
            slug = _slug_for_ref_id(store, ref_id)
            if slug is not None:
                return slug, f"arxiv={arxiv_fn} (filename)"

    # --- Check 3: embedded DOI via PyMuPDF + first-pages regex ---
    print("  pre-flight: reading embedded PDF metadata for DOI...", flush=True)
    try:
        from acatome_meta.pdf import extract_pdf_meta

        pdf_meta = extract_pdf_meta(pdf)
    except Exception as exc:
        print(
            f"  pre-flight: embedded-meta read failed ({exc}) — falling through",
            flush=True,
        )
        return None, None
    doi = pdf_meta.get("doi") or ""
    if not doi:
        print("  pre-flight: no embedded DOI found", flush=True)
        return None, None
    print(f"  pre-flight: embedded doi={doi} — checking precis", flush=True)
    ref_id = store.find_ref_by_identifier("doi", doi, kind="paper")
    if ref_id is not None:
        slug = _slug_for_ref_id(store, ref_id)
        if slug is not None:
            return slug, f"doi={doi}"

    return None, None


def _list_pdfs(base: Path) -> list[Path]:
    """Top-level PDFs only — `completed/` and `errors/` are skipped."""
    return sorted(p for p in base.glob("*.pdf") if p.is_file())


def _ensure_dirs(base: Path) -> tuple[Path, Path]:
    completed = base / "completed"
    errors = base / "errors"
    completed.mkdir(exist_ok=True)
    errors.mkdir(exist_ok=True)
    return completed, errors


def _move(src: Path, dst_dir: Path) -> Path:
    """`shutil.move` with a noop guard — returns the final destination."""
    if not src.exists():
        return src
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    return dst


def _process_one(
    pdf: Path,
    *,
    store,
    embedder,
    completed: Path,
    errors: Path,
    doc_type: str,
    tags: list[str],
    verify: bool,
) -> tuple[str, str]:
    """Returns ``(status, detail)`` where status is ``"ok" | "error"``."""
    from acatome_extract.pipeline import extract

    # --- Pre-flight: cheap dedup probe before the expensive marker pass ---
    # Hash + filename arxiv id + embedded DOI cost ~250 ms total; running
    # marker on a paper precis already has costs 60–300 s. The probe
    # short-circuits the common "user re-dropped the same PDF" case.
    pre_slug, pre_reason = _pre_flight_known_slug(pdf, store=store)
    if pre_slug is not None:
        print(
            f"  pre-flight: HIT — {pre_slug} via {pre_reason}; skipping extract",
            flush=True,
        )
        _move(pdf, completed)
        # Move companion .acatome bundle if the user dropped one alongside.
        companion = pdf.with_suffix(".acatome")
        if companion.is_file():
            _move(companion, completed)
        return "ok", f"skipped (pre-flight, {pre_reason}) {pre_slug}"
    print("  pre-flight: miss — running full extract+ingest", flush=True)

    bundle_path: Path | None = None
    try:
        bundle_path = extract(pdf, doc_type=doc_type, verify=verify)
    except Exception as exc:
        _move(pdf, errors)
        (errors / f"{pdf.stem}.error.log").write_text(traceback.format_exc())
        return "error", f"extract failed: {exc}"

    try:
        result = store.ingest_bundle(bundle_path, embedder=embedder)
    except Exception as exc:
        _move(pdf, errors)
        if bundle_path is not None:
            _move(bundle_path, errors)
        (errors / f"{pdf.stem}.error.log").write_text(traceback.format_exc())
        return "error", f"ingest failed: {exc}"

    # Apply user-supplied open tags (best-effort; failure shouldn't
    # back out the ingest).
    if tags:
        try:
            from precis.store.types import Tag

            for raw in tags:
                store.add_tag(result.ref_id, Tag.parse(raw))
        except Exception as exc:
            print(f"  warn: tag attach failed: {exc}", file=sys.stderr)

    _move(pdf, completed)
    if bundle_path is not None:
        _move(bundle_path, completed)

    verb = "inserted" if result.inserted else "skipped (already present)"
    return "ok", f"{verb} {result.slug} ({result.block_count} blocks)"


_should_stop = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _should_stop
        _should_stop = True
        print(f"\nreceived signal {signum}, draining and exiting...")

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Watch a directory and ingest new PDFs into precis-mcp.",
    )
    p.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_INGEST_DIR,
        help=f"Directory to watch (default: {DEFAULT_INGEST_DIR}).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Process current contents and exit (no polling loop).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Seconds between scans when looping (default: 10).",
    )
    p.add_argument(
        "--doc-type",
        default="paper",
        help="acatome-extract doc_type (default: paper).",
    )
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        metavar="TAG",
        help="Repeatable: open tag to attach to each ingested ref.",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip metadata verification (acatome verify=False).",
    )
    args = p.parse_args()

    base: Path = args.dir.expanduser().resolve()
    if not base.is_dir():
        print(
            f"paper-monitor-ingest-dir: not a directory: {base}",
            file=sys.stderr,
        )
        sys.exit(2)

    completed, errors = _ensure_dirs(base)
    _install_signal_handlers()

    store, cfg = open_store()
    try:
        embedder = make_embedder_for(store, cfg)
        mode = "once" if args.once else f"loop (every {args.interval:g}s)"
        print(
            f"watching {base} — embedder={cfg.embedder} doc_type={args.doc_type} "
            f"mode={mode}",
            flush=True,
        )

        first_idle_print = True
        while not _should_stop:
            pdfs = _list_pdfs(base)
            if pdfs:
                first_idle_print = True
                for pdf in pdfs:
                    if _should_stop:
                        break
                    print(f"\n→ {pdf.name}", flush=True)
                    status, detail = _process_one(
                        pdf,
                        store=store,
                        embedder=embedder,
                        completed=completed,
                        errors=errors,
                        doc_type=args.doc_type,
                        tags=list(args.tags),
                        verify=not args.no_verify,
                    )
                    marker = "ok" if status == "ok" else "FAIL"
                    print(f"  {marker}: {detail}", flush=True)
            elif args.once:
                print("no PDFs to ingest; exiting.")
                break
            elif first_idle_print:
                print(
                    f"idle (no PDFs at top level of {base}); polling...",
                    flush=True,
                )
                first_idle_print = False

            if args.once:
                break
            # Sleep in short slices so SIGINT is responsive.
            slept = 0.0
            while slept < args.interval and not _should_stop:
                time.sleep(min(0.5, args.interval - slept))
                slept += 0.5
    finally:
        store.close()


if __name__ == "__main__":
    main()
