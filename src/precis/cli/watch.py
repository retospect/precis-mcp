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
import os
import shutil
import signal
import subprocess
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
# Conservative: a 5 s window absorbs network stalls during slow
# copies (Wi-Fi pauses, NFS/SMB retries, AirDrop ramp-up) at the
# cost of an extra 5 s of latency between drop and ingest start.
# That trade is right for our workflow — Marker takes minutes per
# paper anyway, so 5 s of front-end debounce is invisible against
# the back-end work. Bumped from 0.1 s on 2026-05-31.
DEFAULT_DEBOUNCE = 5.0
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
    p.add_argument(
        "--subprocess-batch-size",
        type=int,
        default=0,
        help=(
            "If positive, run startup backfill in subprocesses of N PDFs "
            "each — each subprocess exits and reclaims accumulated Marker "
            "memory before the next batch. Default 0 (in-process). "
            "Suggested value: 1 for repeatable leak-isolated ingest."
        ),
    )
    p.add_argument(
        "--subprocess-concurrency",
        type=int,
        default=1,
        help=(
            "Number of parallel subprocess shards during backfill. "
            "Candidates are partitioned round-robin by index; each shard "
            "runs its own subprocess sequence on a dedicated thread. "
            "K * (Marker resident ~3 GB + per-PDF leak) must fit under "
            "the container memory cap. K * 9 cores ≤ host nproc for "
            "Marker to stay CPU-saturated. Default 1 (serial)."
        ),
    )


def add_batch_parser(sub: argparse._SubParsersAction) -> None:
    """Hidden internal subcommand: process a list of PDFs and exit.

    Spawned by :func:`_PdfHandler.backfill` when
    ``--subprocess-batch-size`` is positive. Not user-facing — the
    surface area is unstable on purpose so we can adjust the
    parent/child contract without a CHANGELOG churn.
    """
    p = sub.add_parser(
        "_watch_batch_ingest",
        help=argparse.SUPPRESS,
        description=(
            "Internal: ingest a batch of PDFs into the v2 schema and "
            "exit. Memory leaks accumulated inside Marker / surya are "
            "reclaimed at process exit. See ADR 0015."
        ),
    )
    p.add_argument("pdfs", nargs="+", type=Path)
    p.add_argument("--corpus-dir", type=Path, required=True)
    p.add_argument("--errors-dir", type=Path, required=True)
    p.add_argument("--duplicates-dir", type=Path, required=True)
    p.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE)
    p.add_argument("--user", default="")
    p.add_argument("--database-url", default=None)


def run_batch(args: argparse.Namespace) -> None:
    """Top-level handler for ``precis _watch_batch_ingest``."""
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    log.info(
        "precis _watch_batch_ingest: processing %d PDF(s) in this subprocess",
        len(args.pdfs),
    )
    try:
        for pdf in args.pdfs:
            try:
                process_pdf(
                    pdf,
                    store=store,
                    corpus_dir=args.corpus_dir,
                    errors_dir=args.errors_dir,
                    duplicates_dir=args.duplicates_dir,
                    debounce=args.debounce,
                    user=args.user or getpass.getuser(),
                )
            except Exception:
                log.exception("batch ingest: process_pdf failed for %s", pdf)
    finally:
        store.close()


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
            subprocess_batch_size=args.subprocess_batch_size,
            subprocess_concurrency=args.subprocess_concurrency,
            database_url=args.database_url,
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
    subprocess_batch_size: int = 0,
    subprocess_concurrency: int = 1,
    database_url: str | None = None,
) -> None:
    """Run the watcher in the calling process; blocks until SIGINT/SIGTERM.

    Each PDF is processed exactly once thanks to ``_processing_lock``
    in the handler — duplicate FS events for the same path are coalesced.

    ``subprocess_batch_size > 0`` switches the startup backfill to
    spawn ``precis _watch_batch_ingest`` subprocesses of N PDFs each.
    Marker / surya memory leaks accumulate in the long-running watcher
    process; isolating batches in subprocesses bounds the leak per
    batch. ``database_url`` is plumbed into the subprocess as
    ``--database-url`` so the child opens a fresh Store without
    depending on env-var inheritance.
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
        subprocess_batch_size=subprocess_batch_size,
        subprocess_concurrency=subprocess_concurrency,
        database_url=database_url,
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
        subprocess_batch_size: int = 0,
        subprocess_concurrency: int = 1,
        database_url: str | None = None,
    ) -> None:
        super().__init__()
        self.watch_dir = watch_dir
        self.corpus_dir = corpus_dir
        self.errors_dir = errors_dir
        self.duplicates_dir = duplicates_dir
        self.store = store
        self.debounce = debounce
        self.user = user
        self.subprocess_batch_size = subprocess_batch_size
        self.subprocess_concurrency = max(1, subprocess_concurrency)
        self.database_url = database_url
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
        as live events.

        Files are processed **smallest-first** by byte size. Two
        reasons: (1) small files clear quickly, populating the corpus
        early so search starts working before the long tail finishes;
        (2) if a giant PDF OOMs the watcher, only the giant blocks —
        the small files behind it have already been ingested. Stat
        errors (broken symlinks, race-condition deletions) sort
        last so they fail loudly rather than aborting the whole
        backfill.
        """

        def _size_key(p: Path) -> int:
            try:
                return p.stat().st_size
            except OSError:
                # Push errors to the end without crashing the sort.
                return 2**63 - 1

        candidates = [
            p
            for p in sorted(self.watch_dir.rglob("*.pdf"), key=_size_key)
            if not _should_skip(p, self.watch_dir)
        ]

        if self.subprocess_batch_size > 0:
            k = self.subprocess_concurrency
            log.info(
                "precis watch: backfilling %d PDF(s) in %d parallel shard(s), "
                "subprocess batches of %d",
                len(candidates),
                k,
                self.subprocess_batch_size,
            )
            # Round-robin partition: candidates[i::k] for i in [0, k).
            # Each path lands in exactly one shard so no two shards
            # ever fight over the same file. Sort order (smallest-
            # first) is preserved within each shard, so each shard
            # also clears its small PDFs early.
            shards = [candidates[i::k] for i in range(k)]
            self._run_backfill_shards(shards)
            return

        for pdf in candidates:
            self._enqueue(pdf)

    def _run_backfill_shards(self, shards: list[list[Path]]) -> None:
        """Run K shards of subprocess batches in parallel.

        Each shard owns a thread that loops through its assigned PDFs,
        spawning ``precis _watch_batch_ingest`` subprocesses
        sequentially. Threads themselves only block on
        ``subprocess.run``, so the GIL doesn't matter — actual work
        happens in the child processes which run independently.

        Concurrency safety relies on three properties:

        * Each PDF path appears in exactly one shard (round-robin
          partition above), so no two subprocesses race on the same
          file move.
        * Lock files are content-hashed from absolute path, so two
          subprocesses can't collide on the same lock name.
        * DB writes use ``probe_existing`` inside the transaction
          plus ``ON CONFLICT DO NOTHING`` on ``ref_identifiers``, so
          two shards independently discovering byte-different copies
          of the same paper still produce one ref with both
          ``pdf_sha256`` rows pointing at it.
        """
        from concurrent.futures import ThreadPoolExecutor

        def _drain_shard(shard: list[Path]) -> None:
            for start in range(0, len(shard), self.subprocess_batch_size):
                batch = shard[start : start + self.subprocess_batch_size]
                _spawn_batch_subprocess(
                    batch,
                    corpus_dir=self.corpus_dir,
                    errors_dir=self.errors_dir,
                    duplicates_dir=self.duplicates_dir,
                    debounce=self.debounce,
                    user=self.user,
                    database_url=self.database_url,
                )

        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            futures = [pool.submit(_drain_shard, s) for s in shards]
            for f in futures:
                f.result()

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
    success / dedup, ``None`` on error or when another host owns the
    claim for this PDF's content.

    Order of operations:

    1. Wait for the file to settle (size stable across ``debounce`` s).
       Returns ``None`` if the file disappears during the wait.
    2. Call :func:`precis_add` with a :class:`PdfInput`.
       :func:`precis_add` acquires a Postgres advisory-lock claim
       keyed on ``pdf_sha256`` before running Marker. If the claim is
       already held by another host, ``precis_add`` returns ``None``
       and we leave the file in place so the owning host can finish.
    3. On ``inserted=True`` move to corpus.
    4. On ``inserted=False`` move to ``errors/duplicates``.
    5. On exception write ``.error.txt`` next to the PDF in
       ``errors/<ts>/`` and swallow — exceptions are contained
       within process_pdf so the watcher loop survives a single
       bad PDF.
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

    if result is None:
        # Claim contention: another host is processing this content
        # right now (advisory lock held in the DB). Leave the file in
        # place — the owning host will move it on completion.
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


def _spawn_batch_subprocess(
    pdfs: list[Path],
    *,
    corpus_dir: Path,
    errors_dir: Path,
    duplicates_dir: Path,
    debounce: float,
    user: str,
    database_url: str | None,
) -> None:
    """Run ``precis _watch_batch_ingest`` in a child process for one
    batch of PDFs, wait for it to finish, return.

    Sequential, not concurrent — we want the OS to fully reclaim the
    subprocess's heap before the next batch starts, which is the whole
    point. Parallelism here would defeat the leak isolation. If the
    child OOMs the kernel SIGKILLs *just the child*; the parent keeps
    running. Any per-PDF advisory-lock claims held by the dead child
    auto-release when the Postgres session closes — no filesystem
    recovery sweep needed.
    """
    if not pdfs:
        return
    cmd: list[str] = [
        sys.executable,
        "-m",
        "precis",
        "_watch_batch_ingest",
        "--corpus-dir",
        str(corpus_dir),
        "--errors-dir",
        str(errors_dir),
        "--duplicates-dir",
        str(duplicates_dir),
        "--debounce",
        str(debounce),
    ]
    if user:
        cmd += ["--user", user]
    if database_url:
        cmd += ["--database-url", database_url]
    cmd += [str(p) for p in pdfs]

    log.info("precis watch: spawning batch subprocess for %d PDF(s)", len(pdfs))
    result = subprocess.run(cmd, env=os.environ.copy(), check=False)
    if result.returncode != 0:
        log.warning(
            "precis watch: batch subprocess exited with code %d "
            "(advisory-lock claim auto-released; the next watcher run "
            "will retry any unmoved files)",
            result.returncode,
        )


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
    Returns the post-move path; or the destination it would have
    taken if ``src`` was already moved (race on a shared SMB inbox
    where two hosts both see the same file)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
    try:
        shutil.move(str(src), str(dest))
    except FileNotFoundError:
        # Race: another host moved ``src`` between our check and our
        # move. That's exactly the multi-host case we're meant to
        # tolerate — the other host is handling the file, just
        # report the would-be destination.
        log.info("precis watch: %s already moved by another host", src.name)
    return dest


def _move_to_corpus(pdf: Path, *, cite_key: str, corpus_dir: Path) -> Path:
    """Move ``pdf`` to ``<corpus_dir>/<letter>/<cite_key>.pdf``. The
    letter shard is the lower-case first character of ``cite_key``,
    or ``_`` if it isn't ASCII alphanumeric — matches the layout
    described in ``docs/design/pip-merge.md``.

    Tolerates ``FileNotFoundError`` on the rename: another host on a
    shared inbox may have moved the same file between our existence
    check and our shutil.move call (rare but possible). Logs and
    returns the would-be destination in that case."""
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    bucket = corpus_dir / letter
    bucket.mkdir(parents=True, exist_ok=True)
    dest = bucket / f"{cite_key}{pdf.suffix.lower()}"
    if dest.exists() and dest.resolve() != pdf.resolve():
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = bucket / f"{cite_key}_{ts}{pdf.suffix.lower()}"
    try:
        shutil.move(str(pdf), str(dest))
    except FileNotFoundError:
        log.info(
            "precis watch: %s already moved by another host (cite_key=%s)",
            pdf.name,
            cite_key,
        )
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
