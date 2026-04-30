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
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import DEFAULT_INGEST_DIR, make_embedder_for, open_store  # noqa: E402


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
    def _handler(signum, _frame):  # noqa: ANN001
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
