"""``precis jobs ingest-*`` subcommands.

Related ingest jobs share this module:

- ``ingest`` — pre-warm prose-file ingest (md, plaintext, tex) under
  ``PRECIS_ROOT``. Mtime-gated so warm restarts are cheap. Used by
  operators as the "launch script prefix" so the LLM finds every
  workspace file via ``search`` from the first query.
- ``ingest-md`` — deprecated alias for ``ingest --kinds md``, kept
  one release cycle for back-compat.
- ``ingest-oracles`` — seed the ``oracle`` kind from YAML wisdom
  files (defaults to the bundled ``data/oracle/`` directory).

The legacy ``.acatome`` bundle ingest path (``ingest-bundle`` /
``ingest-bundles``) was removed in B7; paper ingest now goes
through :func:`precis.ingest.add.precis_add` via the top-level
``precis add`` command.

Kept together because they all share the DSN resolution + embedder
construction + per-file stats output shape; splitting them further
would duplicate the boilerplate.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register ingest-{md,oracles} + the prose-file ``ingest`` parser."""
    # Phase 6 — prose-file ingest. The handlers ingest lazily on every
    # `get`, but this command lets the operator pre-warm a directory
    # so the LLM can `search` from the first query. Meant to be run
    # before launching the MCP server:
    #     precis jobs ingest && precis serve
    # Mtime-gated, so warm restarts are cheap.
    ip = sub.add_parser(
        "ingest",
        help="Pre-warm the store by ingesting every prose file under PRECIS_ROOT.",
    )
    ip.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Prose-file root (defaults to PRECIS_ROOT).",
    )
    ip.add_argument("--database-url", default=None)
    ip.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest every file even if its mtime hasn't changed.",
    )
    ip.add_argument(
        "--kinds",
        default="md,plaintext,tex",
        help=(
            "Comma-separated list of kinds to ingest. "
            "Choices: md, plaintext, tex. Default: all three."
        ),
    )

    # Deprecated alias kept one release cycle.
    im = sub.add_parser(
        "ingest-md",
        help="[DEPRECATED] alias for `ingest --kinds md`.",
    )
    im.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Markdown root (defaults to PRECIS_ROOT).",
    )
    im.add_argument("--database-url", default=None)
    im.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest every file even if its mtime hasn't changed.",
    )

    # Phase 5 — oracle seed ingest. Reads bundled wisdom YAMLs (or
    # a user-supplied directory) and writes one ``oracle`` ref per
    # tradition with one block per entry. Idempotent: skips refs
    # that already exist unless ``--overwrite`` is passed.
    io = sub.add_parser(
        "ingest-oracles",
        help="Seed the oracle kind from YAML wisdom files.",
    )
    io.add_argument(
        "src",
        nargs="?",
        default=None,
        help=(
            "Directory of oracle YAML files. Defaults to the bundled "
            "data/oracle/ shipped with the package."
        ),
    )
    io.add_argument("--database-url", default=None)
    io.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace existing oracle refs (drops & re-inserts blocks); "
            "default is to skip already-ingested traditions."
        ),
    )
    io.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write - show what would be ingested.",
    )
    io.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the version + sha256 boot-time gate and "
            "re-ingest unconditionally (under advisory lock). Use "
            "after a hand-edit to bundled YAML on a single host. "
            "Implies --overwrite."
        ),
    )


# ---------------------------------------------------------------------------
# ingest — unified prose-file ingest
# ---------------------------------------------------------------------------


# Per-kind walk descriptors. Kept here so adding a new prose-file kind
# is a single-row change: register the handler, the slug encoder, and
# the extension filter. The handler itself owns the mtime/sha gating
# via ``_ensure_ingested``.
_PROSE_KINDS: tuple[str, ...] = ("md", "plaintext", "tex")


def _ingest_one_kind(
    *,
    kind: str,
    root: Path,
    store: object,
    handler: object,
    force: bool,
) -> tuple[int, int, int]:
    """Walk ``root`` for one kind's files, run its handler's ingest.

    Returns ``(ingested, skipped, failed)``. The handler's
    ``_ensure_ingested`` already mtime-gates, so re-runs on an
    unchanged tree are cheap.
    """
    # File-slug encoders are shared across kinds (one implementation in
    # md_parse). The difference per kind is purely the extension set
    # the walker matches + which handler DB row prefix to use.
    from precis.utils.md_parse import (
        file_slug_from_path as _slug,
    )
    from precis.utils.md_parse import (
        is_valid_file_slug as _valid,
    )

    if kind == "md":
        extensions = (".md", ".markdown")
        handler_kind = "markdown"
    elif kind == "plaintext":
        extensions = (".txt", ".log")
        handler_kind = "plaintext"
    elif kind == "tex":
        extensions = (".tex",)
        handler_kind = "tex"
    else:
        raise ValueError(f"unknown kind {kind!r}")

    def slug_for(rel: str) -> str | None:
        """Derive the ref slug for *rel* (relative to root).

        Markdown historically kept its own encoder that drops the
        ``.md`` / ``.markdown`` suffix; plaintext/tex share the same
        encoder but need us to strip the extension first. Handle both
        uniformly by stripping a known extension before slug encoding.
        """
        base = rel
        for ext in extensions:
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        s = _slug(base)
        return s if _valid(s) else None

    ingested = skipped = failed = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.lower().endswith(extensions):
                continue
            p = Path(dirpath) / name
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                failed += 1
                print(f"  fail  {p}  - outside root")
                continue
            slug = slug_for(rel)
            if slug is None:
                failed += 1
                print(f"  fail  {rel}  - invalid slug for path")
                continue
            ref_before = store.get_ref(kind=handler_kind, id=slug)  # type: ignore[attr-defined]
            ref = handler._ensure_ingested(slug, force=force)  # type: ignore[attr-defined]
            if ref is None:
                failed += 1
                print(f"  fail  {rel}  - ingest returned None")
                continue
            if ref_before is None:
                ingested += 1
                n_blocks = store.count_blocks(ref.id)  # type: ignore[attr-defined]
                print(f"  ok    [{kind:<9}] {slug}  ({n_blocks} blocks)")
            else:
                before_sha = (ref_before.meta or {}).get("sha256")
                after_sha = (ref.meta or {}).get("sha256")
                if force or before_sha != after_sha:
                    ingested += 1
                    n_blocks = store.count_blocks(ref.id)  # type: ignore[attr-defined]
                    print(f"  upd   [{kind:<9}] {slug}  ({n_blocks} blocks)")
                else:
                    skipped += 1
    return ingested, skipped, failed


def run_ingest(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest`` — walk ``PRECIS_ROOT`` and
    pre-warm every prose-file kind.

    Meant to be composed into the operator's launch script::

        precis jobs ingest && precis serve

    so the LLM sees every workspace file via ``search`` from its
    first query. Mtime-gated, so warm restarts are fast.
    """
    from precis.config import load_config
    from precis.dispatch import Hub
    from precis.embedder import make_embedder
    from precis.handlers.markdown import MarkdownHandler
    from precis.handlers.plaintext import PlaintextHandler
    from precis.handlers.tex import TexHandler
    from precis.store import Store

    cfg = load_config()
    root_str = args.root or cfg.root
    if not root_str:
        print(
            "ingest: root not specified and PRECIS_ROOT not set",
            file=sys.stderr,
        )
        sys.exit(2)
    root = Path(root_str).resolve()
    if not root.is_dir():
        print(f"ingest: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    # Parse and validate --kinds.
    requested = [k.strip() for k in (args.kinds or "").split(",") if k.strip()]
    if not requested:
        print(
            "ingest: --kinds must name at least one kind",
            file=sys.stderr,
        )
        sys.exit(2)
    unknown = [k for k in requested if k not in _PROSE_KINDS]
    if unknown:
        print(
            f"ingest: unknown kind(s) {unknown!r}; valid choices: {list(_PROSE_KINDS)}",
            file=sys.stderr,
        )
        sys.exit(2)

    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    handler_ctors: dict[str, type] = {
        "md": MarkdownHandler,
        "plaintext": PlaintextHandler,
        "tex": TexHandler,
    }
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        hub = Hub(store=store, embedder=embedder)
        total_i = total_s = total_f = 0
        per_kind: dict[str, tuple[int, int, int]] = {}
        for kind in requested:
            handler = handler_ctors[kind](hub=hub, root=root)
            i, s, f = _ingest_one_kind(
                kind=kind,
                root=root,
                store=store,
                handler=handler,
                force=bool(args.force),
            )
            per_kind[kind] = (i, s, f)
            total_i += i
            total_s += s
            total_f += f

        summary_parts = [f"{kind}={i}/{i + s}" for kind, (i, s, _f) in per_kind.items()]
        print(
            f"ingest: total ingested={total_i}  skipped={total_s}  "
            f"failed={total_f}  [embedder={cfg.embedder}]  "
            f"per-kind: {', '.join(summary_parts)}"
        )
        if total_f:
            sys.exit(1)
    finally:
        store.close()


def run_md(args: argparse.Namespace) -> None:
    """Deprecated shim for ``precis jobs ingest-md``.

    Prints a one-line deprecation notice and forwards to
    :func:`run_ingest` with ``--kinds md``.
    """
    print(
        "ingest-md: deprecated - use `precis jobs ingest --kinds md` instead",
        file=sys.stderr,
    )
    args.kinds = "md"
    run_ingest(args)


# ---------------------------------------------------------------------------
# ingest-oracles
# ---------------------------------------------------------------------------


def run_oracles(args: argparse.Namespace) -> None:
    """Implements ``precis jobs ingest-oracles``.

    Walks a directory of YAML files (defaulting to the bundled
    ``data/oracle/``) and inserts one ``oracle`` ref per tradition
    with one block per entry. Idempotent: existing refs are skipped
    unless ``--overwrite`` is passed; ``--dry-run`` reports without
    touching the DB.
    """
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.jobs.ingest_oracles import (
        bundled_oracle_dir,
        ingest_directory,
    )
    from precis.store import Store

    if args.src is not None:
        src = Path(args.src).expanduser()
    else:
        bundled = bundled_oracle_dir()
        if bundled is None:
            print(
                "ingest-oracles: bundled oracle dir not found and no path "
                "supplied; pass <src> as the first argument",
                file=sys.stderr,
            )
            sys.exit(2)
        src = bundled
    if not src.is_dir():
        print(f"ingest-oracles: not a directory: {src}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config()

    if args.dry_run:
        # Dry-run still parses every YAML to validate the schema, but
        # never opens a DB connection — useful before pointing the
        # CLI at a fresh deploy.
        try:
            agg = ingest_directory(
                src,
                store=None,  # type: ignore[arg-type]
                embedder=None,
                overwrite=args.overwrite,
                dry_run=True,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ingest-oracles: {exc}", file=sys.stderr)
            sys.exit(2)
        print(
            f"ingest-oracles: dry-run from {src}\n"
            f"  files={agg['files']}  would-create={agg['created']}  "
            f"chunks={agg['chunks']}"
        )
        for fname, stats in agg["per_file"].items():
            print(f"  {fname:<28}  entries={stats['chunks']}")
        return

    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())

        # ``--force`` routes through the gated reconcile so the
        # advisory lock + system-table state stay in sync. The
        # legacy direct ``ingest_directory`` path is preserved
        # without ``--force`` so explicit user-supplied dirs and
        # ``--overwrite=False`` semantics keep working unchanged.
        if args.force:
            from precis.jobs.oracle_sync import maybe_reingest

            outcome = maybe_reingest(
                store=store, embedder=embedder, src_dir=src, force=True
            )
            print(
                f"ingest-oracles: forced reconcile from {src}  "
                f"[embedder={cfg.embedder}]\n"
                f"  status={outcome['status']}  "
                + " ".join(
                    f"{k}={v}"
                    for k, v in outcome.items()
                    if k not in ("status", "sha256")
                )
            )
            if outcome["status"] == "error":
                sys.exit(1)
            return

        try:
            agg = ingest_directory(
                src,
                store=store,
                embedder=embedder,
                overwrite=args.overwrite,
                dry_run=False,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ingest-oracles: {exc}", file=sys.stderr)
            sys.exit(2)

        print(
            f"ingest-oracles: from {src}  [embedder={cfg.embedder}]\n"
            f"  files={agg['files']}  created={agg['created']}  "
            f"replaced={agg['replaced']}  skipped={agg['skipped']}  "
            f"errors={agg['errors']}  total chunks={agg['chunks']}"
        )
        for fname, stats in agg["per_file"].items():
            print(
                f"  {fname:<28}  "
                f"created={stats['created']} replaced={stats['replaced']} "
                f"chunks={stats['chunks']} skipped={stats['skipped']} "
                f"errors={stats['errors']}"
            )
        if agg["errors"]:
            sys.exit(1)
    finally:
        store.close()


__all__ = [
    "add_parsers",
    "run_ingest",
    "run_md",
    "run_oracles",
]
