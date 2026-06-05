"""``precis worker`` — drive the derived-artifact queue (ADR 0007).

Run continuously to keep ``chunk_embeddings`` and ``chunk_summaries``
up-to-date as new chunks land. Two modes:

* ``precis worker`` — start the loop, processing batches forever.
  ``Ctrl-C`` exits cleanly between batches.
* ``precis worker --status`` — print one ``(total | ok | failed |
  pending)`` row per registered handler and exit. No work claimed.

By default both handlers run: ``embed:bge-m3`` and
``summarize:rake-lemma``. ``--only embed`` / ``--only summarize``
isolates one. For CI / tests, ``--embedder mock`` swaps the heavy
sentence-transformers model for the deterministic
:class:`precis.embedder.MockEmbedder` so the worker can be exercised
without downloading weights.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Literal

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.embedder import make_embedder
from precis.format import serialize
from precis.store import Store
from precis.workers import (
    EmbedHandler,
    RakeLemmaHandler,
    WorkerHandler,
    run_loop,
)

# Column order for ``precis worker --status``. Keeping it in one
# place means every renderer (TOON, JSON, table) sees the same
# shape, and adding a column lands in exactly one spot.
_STATUS_SCHEMA: list[str] = ["handler", "total", "ok", "failed", "pending"]

log = logging.getLogger(__name__)


HandlerKey = Literal["embed", "summarize", "chunk_keywords", "chase", "fetch"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis worker`` subcommand on ``sub``."""
    p = sub.add_parser(
        "worker",
        help="Drive the derived-artifact queue (embeddings, summaries).",
        description=(
            "Process chunks that lack a derived artifact (embedding or "
            "summary) and write the result back. Without a separate "
            "queue table — see ADR 0007 — the worker discovers work by "
            "LEFT JOIN-ing chunks against the output tables."
        ),
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Print one (total | ok | failed | pending) row per handler "
        "and exit. No work is claimed.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass (one batch per handler) and exit. "
        "Useful for smoke tests and ad-hoc backfills.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Chunks claimed per handler per pass (default 32). Larger "
        "batches amortise commit overhead but hold row locks longer.",
    )
    p.add_argument(
        "--idle-seconds",
        type=float,
        default=2.0,
        help="Sleep between passes when all handlers reported zero "
        "claimed rows (default 2.0).",
    )
    p.add_argument(
        "--only",
        choices=("embed", "summarize", "chunk_keywords", "chase", "fetch"),
        default=None,
        help="Restrict to one handler kind. Default: all of them — "
        "embed + summarize (chunk-level), segments (ref-level "
        "segment_toc), chase (ref-level finding-chase), and fetch "
        "(ref-level Unpaywall OA fetch for stub papers). Each "
        "--only value drains its queue alone; useful for ad-hoc "
        "backfills.",
    )
    p.add_argument(
        "--with-llm",
        action="store_true",
        help="Enable the chase worker's LLM hooks (claude -p via "
        "precis.utils.claude_p) for multi-candidate disambiguation, "
        "chunk-localisation confirmation, and verifier-with-caveats. "
        "Default: deterministic chase only (no LLM cost). Also "
        "honoured via env PRECIS_CHASE_LLM=1.",
    )
    p.add_argument(
        "--fetch-inbox",
        default=None,
        help="Directory where the fetcher worker drops downloaded OA "
        "PDFs (default: PRECIS_WATCH_INBOX env, else "
        "~/work/new_papers/_oa_fetched). The watcher should be "
        "configured to scan this path so fetched PDFs land in the "
        "normal ingest flow.",
    )
    p.add_argument(
        "--unpaywall-email",
        default=None,
        help="Email to send as Unpaywall's required identification "
        "parameter (default: PRECIS_UNPAYWALL_EMAIL env). Without "
        "one, the fetch pass is skipped.",
    )
    p.add_argument(
        "--embedder",
        default="bge-m3",
        help="Embedder name (default 'bge-m3'). Use 'mock' for tests / "
        "CI to skip the model download.",
    )
    p.add_argument(
        "--summarizer-model",
        default="rake-lemma",
        help="Summarizer model name as registered in the 'summarizers' "
        "table (default 'rake-lemma').",
    )
    p.add_argument(
        "--max-keywords",
        type=int,
        default=50,
        help="RAKE max_keywords (default 50). Honour the registered "
        "summarizer config if present.",
    )
    p.add_argument(
        "--min-phrase-words",
        type=int,
        default=1,
        help="RAKE min_phrase_words (default 1).",
    )
    p.add_argument(
        "--max-phrase-words",
        type=int,
        default=4,
        help="RAKE max_phrase_words (default 4).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    # ``--status`` is the only emit-tabular-data verb on this
    # subcommand; ``--format`` is meaningless for the run loop but
    # registering it on the worker parser keeps the flag visible in
    # ``precis worker --help`` so operators discover it without
    # hunting through ``--status`` alone.
    add_format_argument(p)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    """Top-level handler for ``precis worker``."""
    if args.batch_size <= 0:
        print("worker: --batch-size must be positive", file=sys.stderr)
        sys.exit(2)

    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        handlers = _build_handlers(args)
        if args.status:
            _print_status(handlers, store, format=resolve_format(args))
            return

        # Chunk-keybert pass (F20). Replaces the v1 segment_toc worker.
        # Runs after embeddings exist (the claim query requires
        # ``chunk_embeddings.status='ok'``). Default (no ``--only``)
        # runs the chunk-level handlers + this pass each cycle; the
        # ``--only chunk_keywords`` choice drops chunk-level work and
        # drains this queue alone.
        from precis.workers.runner import RefPass

        ref_passes: list[RefPass] = []
        if args.only in (None, "chunk_keywords"):
            from precis.workers.chunk_keywords import run_chunk_keywords_pass

            # Narrow to EmbedHandler so mypy sees the .embedder
            # attribute; the abstract WorkerHandler doesn't carry it.
            from precis.workers.embed import EmbedHandler
            from precis.workers.runner import BatchResult

            embed_handler = next(
                (
                    h
                    for h in handlers
                    if isinstance(h, EmbedHandler) and h.name.startswith("embed:")
                ),
                None,
            )
            kw_embedder = (
                embed_handler.embedder
                if embed_handler is not None
                else make_embedder(args.embedder)
            )

            def _chunk_keywords_pass(batch_size: int) -> BatchResult:
                r = run_chunk_keywords_pass(store, kw_embedder, batch_size=batch_size)
                return BatchResult(
                    handler="chunk_keywords",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_chunk_keywords_pass)

        # Finding-chase pass — same sibling-worker pattern, but for
        # STATUS:tracing findings. Default-off LLM hooks via
        # --with-llm or PRECIS_CHASE_LLM=1. See ADR 0018 §"Worker"
        # for the sibling-vs-base-class rationale.
        if args.only in (None, "chase"):
            from precis.workers.chase import run_finding_chase_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _chase_pass(batch_size: int) -> _BatchResult:
                r = run_finding_chase_pass(
                    store, limit=batch_size, with_llm=args.with_llm
                )
                return _BatchResult(
                    handler="finding_chase",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_chase_pass)

        # Unpaywall OA fetcher — turns stub paper refs (DOI known,
        # pdf_sha256 IS NULL) into landed PDFs by checking Unpaywall
        # for an OA URL and downloading to the watch inbox. The
        # watcher's existing ingest path picks the file up and C7's
        # stub-upgrade promotes the row in place.
        if args.only in (None, "fetch"):
            from precis.workers.fetch_oa import run_oa_fetch_pass
            from precis.workers.runner import BatchResult as _BatchResult

            fetch_inbox = args.fetch_inbox  # may be None → worker uses env default
            fetch_email = args.unpaywall_email  # same

            def _fetch_pass(batch_size: int) -> _BatchResult:
                r = run_oa_fetch_pass(
                    store,
                    limit=batch_size,
                    inbox_dir=fetch_inbox,
                    email=fetch_email,
                )
                return _BatchResult(
                    handler="fetch_oa",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_fetch_pass)

        stop_flag = _install_signal_handlers()
        run_loop(
            handlers,
            store,
            batch_size=args.batch_size,
            idle_seconds=args.idle_seconds,
            once=args.once,
            should_stop=lambda: stop_flag["stop"],
            ref_passes=ref_passes,
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_handlers(args: argparse.Namespace) -> list[WorkerHandler]:
    """Materialise the handler list per ``--only`` / model flags."""
    handlers: list[WorkerHandler] = []
    if args.only in (None, "embed"):
        # MockEmbedder.dim defaults to 1024 to match the seeded
        # bge-m3 embedder column dim, so swapping it in for tests
        # does not require schema changes.
        embedder = make_embedder(args.embedder)
        handlers.append(EmbedHandler(embedder))
    if args.only in (None, "summarize"):
        handlers.append(
            RakeLemmaHandler(
                max_keywords=args.max_keywords,
                min_phrase_words=args.min_phrase_words,
                max_phrase_words=args.max_phrase_words,
                model_name=args.summarizer_model,
            )
        )
    return handlers


def _print_status(
    handlers: list[WorkerHandler],
    store: Store,
    *,
    format: str = "toon",
) -> None:
    """Render one row per handler in *format* and print to stdout.

    The row schema is :data:`_STATUS_SCHEMA` — pinned in one place
    so TOON, JSON, and the ASCII table renderer all see the same
    column order. Defaulting to ``"toon"`` matches the pipe
    default chosen by :func:`resolve_format`; callers passing a
    TTY-bound process get ``"table"`` instead.

    The output is one document (header + N rows for tabular
    formats; a JSON array for ``"json"``); we deliberately do not
    emit a leading ``#`` comment any more — TOON's first line is
    the header, and ``awk -F'\\t' 'NR>1'`` works the same way.
    """
    rows: list[dict[str, object]] = []
    with store.pool.connection() as conn:
        for handler in handlers:
            status = handler.status(conn)
            rows.append(
                {
                    "handler": status.name,
                    "total": status.total,
                    "ok": status.ok,
                    "failed": status.failed,
                    "pending": status.pending,
                }
            )
    print(serialize(rows, format=format, schema=_STATUS_SCHEMA))


def _install_signal_handlers() -> dict[str, bool]:
    """Wire SIGINT/SIGTERM to a flag the loop polls between batches.

    A dict-of-bool — boring but works as a closure cell across the
    signal handlers and ``run_loop``'s ``should_stop`` callable
    without having to introduce a singleton or threading.Event.
    """
    flag = {"stop": False}

    def _handler(signum: int, _frame: object) -> None:
        log.info("worker: signal %d received; finishing batch", signum)
        flag["stop"] = True

    # SIGINT for interactive Ctrl-C; SIGTERM for systemd / docker
    # stop. We deliberately do NOT install SIGHUP — most operators
    # use it for "reload config" and we have no config to reload.
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return flag


__all__ = ["add_parser", "run"]
