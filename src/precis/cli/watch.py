"""``precis watch`` — directory watcher that auto-ingests PDFs.

Loops over a directory using ``watchdog``. For every PDF that
arrives (new file event, or any of the existing files at startup
when ``--backfill`` is on):

1. Wait for the file size to stabilise (debounce — avoids
   processing partially-written downloads).
2. Call :func:`precis.ingest.add.precis_add` with a
   :class:`PdfInput`.
3. On success, move the PDF to
   ``<corpus>/<letter>/<cite_key>.pdf`` (one-letter shard, lower-case).
4. On failure, move the PDF to
   ``<watch_dir>/errors/<YYYYMMDD-HHMMSS>/`` and write a sibling
   ``<filename>.error.txt`` with the traceback.

The result of every successful ingest is appended to
``<corpus>/ingest.log`` as a TSV line so operators can grep through
"who added what when". Idempotency hits (``inserted=False``) get a
``status=existed`` column rather than ``inserted`` and the source
PDF moves to ``errors/duplicates/`` (the file isn't useless — it's
just already known).

Note on the watchdog dependency: B5 pulls it in transitively via
``acatome-extract[embeddings]``. B8's pyproject cleanup promotes
``watchdog`` to a direct dep so the package keeps resolving after
the acatome dep is dropped.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shutil
import signal
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from precis.cli._common import resolve_dsn
from precis.ingest.add import IngestResult, PdfInput, precis_add
from precis.store import Store

log = logging.getLogger(__name__)

# Default debounce — wait this many seconds between size-stability
# checks before declaring a file "settled" and ready to process.
# Tuned for typical browser-download speeds where the first byte
# arrives before the rest; ~100 ms is a reasonable trade-off
# between responsiveness and false positives on large files.
DEFAULT_DEBOUNCE = 0.1
DEFAULT_POLL_INTERVAL = 1.0

# Subdirectories of the watch dir that are managed by precis-watch
# itself; events on these never trigger ingest. Explicit list so
# operators can drop cooperative dirs without breaking anything.
_MANAGED_DIRS: frozenset[str] = frozenset({"errors", "completed"})


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``precis watch`` on ``sub``."""
    p = sub.add_parser(
        "watch",
        help="Watch a directory and auto-ingest PDFs into the v2 schema.",
        description=(
            "Long-running watcher: every PDF dropped into <watch-dir> is "
            "ingested via precis_add() and moved to the corpus on success "
            "or to <watch-dir>/errors/ on failure."
        ),
    )
    p.add_argument(
        "watch_dir",
        type=Path,
        help="Directory to monitor for new PDFs.",
    )
    p.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help=(
            "Where to move PDFs after successful ingest. Defaults to ~/work/corpus/."
        ),
    )
    p.add_argument(
        "--no-backfill",
        action="store_true",
        help="Skip processing of PDFs already present at startup.",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Don't watch subdirectories.",
    )
    p.add_argument(
        "--polling",
        action="store_true",
        help=(
            "Force the polling observer (use on network mounts or "
            "containers where inotify isn't reliable)."
        ),
    )
    p.add_argument(
        "--debounce",
        type=float,
        default=DEFAULT_DEBOUNCE,
        help="Seconds to wait for file size to stabilise before processing.",
    )
    p.add_argument(
        "--user",
        default="",
        help="Operator name written to ingest.log (defaults to OS user).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def run(args: argparse.Namespace) -> None:
    """Top-level handler for ``precis watch``."""
    dsn = resolve_dsn(args.database_url)
    corpus_dir = args.corpus_dir or (Path.home() / "work" / "corpus")
    user = args.user or getpass.getuser()

    store = Store.connect(dsn)
    try:
        watch(
            watch_dir=args.watch_dir,
            corpus_dir=corpus_dir,
            store=store,
            backfill=not args.no_backfill,
            recursive=not args.no_recursive,
            use_polling=args.polling,
            debounce=args.debounce,
            user=user,
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Public watch() — used by both the CLI and (eventually) tests.
# ---------------------------------------------------------------------------


def watch(
    watch_dir: Path,
    *,
    corpus_dir: Path,
    store: Store,
    backfill: bool = True,
    recursive: bool = True,
    use_polling: bool = False,
    debounce: float = DEFAULT_DEBOUNCE,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    user: str = "",
) -> None:
    """Run the watcher in the calling process; blocks until SIGINT/SIGTERM.

    Each PDF is processed exactly once thanks to ``_processing_lock``
    in the handler — duplicate FS events for the same path are coalesced.
    """
    watch_dir = Path(watch_dir).resolve()
    corpus_dir = Path(corpus_dir).resolve()
    if not watch_dir.is_dir():
        raise FileNotFoundError(f"Watch directory not found: {watch_dir}")

    errors_dir = watch_dir / "errors"
    duplicates_dir = errors_dir / "duplicates"
    errors_dir.mkdir(exist_ok=True)
    duplicates_dir.mkdir(exist_ok=True)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    user = user or getpass.getuser()

    log.info(
        "precis watch starting: watch=%s corpus=%s recursive=%s",
        watch_dir,
        corpus_dir,
        recursive,
    )

    handler = _PdfHandler(
        watch_dir=watch_dir,
        corpus_dir=corpus_dir,
        errors_dir=errors_dir,
        duplicates_dir=duplicates_dir,
        store=store,
        debounce=debounce,
        user=user,
    )

    observer_cls = PollingObserver if use_polling else Observer
    observer: Any = observer_cls(timeout=poll_interval)
    observer.schedule(handler, str(watch_dir), recursive=recursive)
    observer.start()

    stop = Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("precis watch: received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        if backfill:
            handler.backfill()
        while not stop.wait(timeout=1.0):
            pass
    finally:
        observer.stop()
        observer.join()
        log.info("precis watch: stopped")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _PdfHandler(FileSystemEventHandler):
    """watchdog handler that coalesces duplicate FS events and routes
    each PDF through :func:`process_pdf` exactly once."""

    def __init__(
        self,
        *,
        watch_dir: Path,
        corpus_dir: Path,
        errors_dir: Path,
        duplicates_dir: Path,
        store: Store,
        debounce: float,
        user: str,
    ) -> None:
        super().__init__()
        self.watch_dir = watch_dir
        self.corpus_dir = corpus_dir
        self.errors_dir = errors_dir
        self.duplicates_dir = duplicates_dir
        self.store = store
        self.debounce = debounce
        self.user = user
        self._processing_lock = Lock()
        self._inflight: set[Path] = set()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if not _is_pdf(path):
            return
        if _should_skip(path, self.watch_dir):
            return
        self._enqueue(path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # ``dest_path`` exists for FileMovedEvent; coerce to Path.
        dest = getattr(event, "dest_path", None)
        if not dest:
            return
        path = Path(str(dest))
        if not _is_pdf(path):
            return
        if _should_skip(path, self.watch_dir):
            return
        self._enqueue(path)

    def backfill(self) -> None:
        """Process PDFs that were already present when the watcher
        started. Called once at startup; respects the same skip rules
        as live events."""
        for pdf in sorted(self.watch_dir.rglob("*.pdf")):
            if _should_skip(pdf, self.watch_dir):
                continue
            self._enqueue(pdf)

    def _enqueue(self, path: Path) -> None:
        # Idempotent against duplicate FS events: at most one in-flight
        # call per path, even if the editor / browser fires "created"
        # plus several "modified" while writing.
        with self._processing_lock:
            if path in self._inflight:
                return
            self._inflight.add(path)
        try:
            process_pdf(
                path,
                store=self.store,
                corpus_dir=self.corpus_dir,
                errors_dir=self.errors_dir,
                duplicates_dir=self.duplicates_dir,
                debounce=self.debounce,
                user=self.user,
            )
        finally:
            with self._processing_lock:
                self._inflight.discard(path)


def process_pdf(
    pdf: Path,
    *,
    store: Store,
    corpus_dir: Path,
    errors_dir: Path,
    duplicates_dir: Path,
    debounce: float = DEFAULT_DEBOUNCE,
    user: str = "",
) -> Path | None:
    """Process one PDF end-to-end. Returns the post-move path on
    success / dedup, ``None`` on error.

    Order of operations:

    1. Wait for the file to settle (size stable across ``debounce`` s).
       Returns ``None`` if the file disappears during the wait.
    2. Call :func:`precis_add` with a :class:`PdfInput`.
    3. On ``inserted=True`` move to corpus.
    4. On ``inserted=False`` move to ``errors/duplicates``.
    5. On exception write ``.error.txt`` next to the PDF in
       ``errors/<ts>/`` and re-raise nothing — exceptions are
       contained within process_pdf so the watcher loop survives
       a single bad PDF.
    """
    if not _wait_stable(pdf, debounce=debounce):
        log.warning("precis watch: file disappeared before stable: %s", pdf)
        return None

    try:
        result = precis_add(PdfInput(pdf_path=pdf), store=store)
    except Exception as exc:
        log.exception("precis watch: ingest failed for %s", pdf.name)
        _handle_failure(pdf, exc, errors_dir=errors_dir)
        return None

    return _handle_success(
        pdf,
        result,
        corpus_dir=corpus_dir,
        duplicates_dir=duplicates_dir,
        user=user,
    )


def _handle_success(
    pdf: Path,
    result: IngestResult,
    *,
    corpus_dir: Path,
    duplicates_dir: Path,
    user: str,
) -> Path:
    """Move the PDF based on ``result.inserted`` and append a TSV log
    line. Returns the post-move path."""
    if result.inserted:
        dest = _move_to_corpus(pdf, cite_key=result.cite_key, corpus_dir=corpus_dir)
        log.info(
            "precis watch: ingested %s as %s (ref_id=%d)",
            pdf.name,
            result.cite_key,
            result.ref_id,
        )
        status = "inserted"
    else:
        dest = _move_to(pdf, duplicates_dir)
        log.info(
            "precis watch: duplicate %s (existing ref_id=%d, cite_key=%s)",
            pdf.name,
            result.ref_id,
            result.cite_key,
        )
        status = "existed"

    _append_ingest_log(corpus_dir, user=user, result=result, pdf=pdf, status=status)
    return dest


def _handle_failure(
    pdf: Path,
    exc: Exception,
    *,
    errors_dir: Path,
) -> None:
    """Move the failed PDF into ``errors/<ts>/`` and write a sibling
    ``.error.txt``. Returns ``None`` (the caller's contract for the
    error path)."""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    bucket = errors_dir / ts
    bucket.mkdir(parents=True, exist_ok=True)
    moved = _move_to(pdf, bucket)
    _write_error(bucket, moved, exc)
    return None


# ---------------------------------------------------------------------------
# Filesystem helpers (kept module-level so tests can exercise them
# without spinning up a watchdog observer).
# ---------------------------------------------------------------------------


def _is_pdf(path: Path) -> bool:
    """True iff the path's suffix is ``.pdf`` (case-insensitive)."""
    return path.suffix.lower() == ".pdf"


def _should_skip(path: Path, watch_dir: Path) -> bool:
    """True iff ``path`` is inside one of the watcher-managed
    subdirectories (``errors/``, ``completed/``). Backfill and live
    events both use this guard so previously-failed PDFs aren't
    retried automatically."""
    try:
        rel = path.resolve().relative_to(watch_dir.resolve())
    except ValueError:
        return True  # outside the watch dir
    parts = rel.parts
    return bool(parts) and parts[0] in _MANAGED_DIRS


def _wait_stable(path: Path, *, debounce: float) -> bool:
    """Wait until the file's size is stable across two consecutive
    polls. Returns ``False`` if the file disappears during the wait."""
    prev_size = -1
    while True:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == prev_size and size > 0:
            return True
        prev_size = size
        time.sleep(debounce)


def _move_to(src: Path, dest_dir: Path) -> Path:
    """Move ``src`` into ``dest_dir``. On filename conflict, append a
    UTC timestamp before the suffix so the original isn't clobbered.
    Returns the post-move path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


def _move_to_corpus(pdf: Path, *, cite_key: str, corpus_dir: Path) -> Path:
    """Move ``pdf`` to ``<corpus_dir>/<letter>/<cite_key>.pdf``. The
    letter shard is the lower-case first character of ``cite_key``,
    or ``_`` if it isn't ASCII alphanumeric — matches the layout
    described in ``docs/design/pip-merge.md``."""
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    bucket = corpus_dir / letter
    bucket.mkdir(parents=True, exist_ok=True)
    dest = bucket / f"{cite_key}{pdf.suffix.lower()}"
    if dest.exists() and dest.resolve() != pdf.resolve():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = bucket / f"{cite_key}_{ts}{pdf.suffix.lower()}"
    shutil.move(str(pdf), str(dest))
    return dest


def _write_error(errors_dir: Path, pdf: Path, error: Exception) -> Path:
    """Write a sibling ``<stem>.error.txt`` next to the failed PDF
    with the exception message, traceback, and a UTC timestamp.
    Returns the error file's path."""
    error_file = errors_dir / f"{pdf.stem}.error.txt"
    error_file.write_text(
        f"PDF: {pdf.name}\n"
        f"Time: {datetime.now(UTC).isoformat()}\n"
        f"Error: {error}\n\n"
        f"Traceback:\n{traceback.format_exc()}",
        encoding="utf-8",
    )
    return error_file


def _append_ingest_log(
    corpus_dir: Path,
    *,
    user: str,
    result: IngestResult,
    pdf: Path,
    status: str,
) -> None:
    """Append a TSV line to ``<corpus_dir>/ingest.log``.

    Format: ``<ts>\\t<user>\\t<cite_key>\\t<ref_id>\\t<status>\\t<pdf_name>``

    Greppable by user (``grep '\\treto\\t' ingest.log``) or by status
    (``grep -c '\\tinserted\\t' ingest.log``). Created on first append."""
    log_file = corpus_dir / "ingest.log"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts}\t{user}\t{result.cite_key}\t{result.ref_id}\t{status}\t{pdf.name}\n"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        # Logging-only failure — don't fail the ingest because we
        # couldn't open the log file (read-only mount, etc.).
        log.warning("precis watch: failed to append ingest.log: %s", exc)


__all__ = [
    "DEFAULT_DEBOUNCE",
    "DEFAULT_POLL_INTERVAL",
    "add_parser",
    "process_pdf",
    "run",
    "watch",
]


if __name__ == "__main__":  # pragma: no cover
    sys.stderr.write("Use `precis watch …` instead of running this module.\n")
    sys.exit(2)
