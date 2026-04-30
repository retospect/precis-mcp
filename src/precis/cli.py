"""Single CLI entry point: ``precis serve | migrate | jobs ...``.

Subcommands:
    serve     Run the MCP server on stdio.
    migrate   Apply pending DB migrations.
    jobs      Run a one-shot maintenance job:
              - ingest-bundle         one .acatome file
              - ingest-bundles        walk a directory of bundles
              - ingest-md             walk a directory of markdown files
              - ingest-oracles        seed the oracle kind from
                                      bundled (or supplied) YAMLs
              - dedupe-papers         remove duplicate paper refs
                                      sharing pdf_hash / doi / arxiv_id
              - import-perplexity     bulk put(mode='import') over a
                                      directory of Perplexity reports
              - watch-patents         create a saved CQL patent watch
              - list-patent-watches   list saved patent watches
              - run-patent-watches    run all due (or one) watch passes

All DB-touching subcommands require ``PRECIS_DATABASE_URL`` (or a
``--database-url`` override).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "serve":
        from precis.server import main as serve

        serve()
        return

    if args.cmd == "migrate":
        _run_migrate(args)
        return

    if args.cmd == "jobs":
        _run_jobs(args)
        return

    parser.error(f"unknown command: {args.cmd!r}")


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="precis",
        description="precis-mcp v2 — paper, document, state, and tool access.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the MCP server (stdio).")

    migrate = sub.add_parser("migrate", help="Apply pending DB migrations.")
    migrate.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without applying.",
    )

    jobs = sub.add_parser("jobs", help="Run a one-shot maintenance job.")
    jobs_sub = jobs.add_subparsers(dest="job", required=True)

    ib = jobs_sub.add_parser(
        "ingest-bundle",
        help="Ingest a single .acatome bundle.",
    )
    ib.add_argument("path", help="Path to .acatome file.")
    ib.add_argument("--database-url", default=None)

    ibs = jobs_sub.add_parser(
        "ingest-bundles",
        help="Walk a directory of .acatome bundles.",
    )
    ibs.add_argument("dir", help="Directory containing .acatome files.")
    ibs.add_argument("--database-url", default=None)
    ibs.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate bundle parsing without writing.",
    )
    ibs.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N bundles (sorted lexicographically).",
    )

    # Phase 6 — markdown ingest. The handler ingests lazily on every
    # `get`, but this command lets the operator pre-warm a directory
    # (useful before launching long-running searches).
    im = jobs_sub.add_parser(
        "ingest-md",
        help="Pre-warm the store by ingesting every .md under a root.",
    )
    im.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Markdown root (defaults to PRECIS_MARKDOWN_ROOT).",
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
    io = jobs_sub.add_parser(
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
        help="Don't write — show what would be ingested.",
    )

    # Dedupe helper — removes duplicate paper refs that share a
    # pdf_hash / doi / arxiv_id but ended up with distinct slug-
    # suffixed rows (created by the pre-fix ingest path that only
    # deduped on DOI). Safe by default: dry-run prints what would go.
    dp = jobs_sub.add_parser(
        "dedupe-papers",
        help="Remove duplicate paper refs sharing pdf_hash / doi / arxiv_id.",
    )
    dp.add_argument("--database-url", default=None)
    dp.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicates (default: dry-run only prints).",
    )
    dp.add_argument(
        "--key",
        choices=("pdf_hash", "doi", "arxiv_id", "all"),
        default="all",
        help="Which identity key to dedupe on (default: all).",
    )

    # Bulk import of Perplexity-generated reports. The typical source
    # is a directory of markdown files exported from the Perplexity
    # web UI by a Pro subscriber — free content that populates the
    # same cache rows paid API calls would have created.
    ip = jobs_sub.add_parser(
        "import-perplexity",
        help="Bulk put(mode='import') a directory of Perplexity reports.",
    )
    ip.add_argument(
        "dir",
        help="Directory to walk (recursively) for report files.",
    )
    ip.add_argument(
        "--kind",
        choices=("websearch", "think", "research"),
        default="research",
        help="Which Perplexity tier to import under (default: research).",
    )
    ip.add_argument(
        "--glob",
        default="*.md",
        help="Filename glob within the directory (default: *.md).",
    )
    ip.add_argument(
        "--query-from",
        choices=("h1", "filename"),
        default="h1",
        help=(
            "How to derive the `id=` query for each file: use the "
            "first H1 heading when present (falls back to filename), "
            "or always use the filename (default: h1)."
        ),
    )
    ip.add_argument("--database-url", default=None)
    ip.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N files (sorted lexicographically).",
    )
    ip.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse each file and print the derived query; don't write.",
    )

    # ── patent watches ────────────────────────────────────────────────
    # Phase 2 of the patent kind. The runner is hidden from the agent
    # surface — these CLI subcommands are how operators create / list
    # / drive watches. Hidden from the help text when EPO_OPS_*
    # vars are missing isn't worth the complexity; we surface the
    # subcommands always and rely on the runtime env-var check
    # inside the runner itself.

    wp = jobs_sub.add_parser(
        "watch-patents",
        help="Create a saved CQL patent watch (or delete one with --delete).",
    )
    wp.add_argument(
        "cql",
        nargs="?",
        default=None,
        help=(
            "CQL string (strict, no bare keywords). Required unless --delete is given."
        ),
    )
    wp.add_argument(
        "--name",
        required=True,
        help="Watch slug; used by run-patent-watches --name and --delete.",
    )
    wp.add_argument(
        "--every",
        default="7d",
        help=(
            "How often the watch should re-run. Accepts 'Nh' (hours), "
            "'Nd' (days), 'Nw' (weeks). Default: 7d."
        ),
    )
    wp.add_argument(
        "--auto-get",
        action="store_true",
        help=(
            "Ingest hits directly into the patent kind. "
            "Default is to open a quest summarising new hits."
        ),
    )
    wp.add_argument(
        "--max-per-pass",
        type=int,
        default=None,
        help=(
            "Cap how many patents this watch ingests / surfaces "
            "in a single pass. Overflow drops and resurfaces "
            "next pass. Default: no cap."
        ),
    )
    wp.add_argument(
        "--delete",
        action="store_true",
        help="Delete the watch with --name (cql positional ignored).",
    )
    wp.add_argument("--database-url", default=None)

    lp = jobs_sub.add_parser(
        "list-patent-watches",
        help="List every saved patent watch.",
    )
    lp.add_argument(
        "--show-cql",
        action="store_true",
        help="Include each watch's CQL in the output (long lines).",
    )
    lp.add_argument("--database-url", default=None)

    rp = jobs_sub.add_parser(
        "run-patent-watches",
        help="Run a one-shot pass over all due patent watches.",
    )
    rp.add_argument(
        "--name",
        default=None,
        help=(
            "Run exactly one watch by name, regardless of due-ness. "
            "Useful for debugging."
        ),
    )
    rp.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen; don't write quests, ingest, or update last_run_at.",
    )
    rp.add_argument(
        "--fair-use-limit-gb",
        type=float,
        default=None,
        help=(
            "Override the rolling 7-day fair-use cap (default 3 GiB, "
            "or PRECIS_PATENT_FAIR_USE_LIMIT_GB env var)."
        ),
    )
    rp.add_argument("--database-url", default=None)

    return parser


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _run_migrate(args: argparse.Namespace) -> None:
    from precis.store import Migrator

    dsn = _resolve_dsn(getattr(args, "database_url", None))
    migrations_dir = Path(__file__).parent / "migrations"

    m = Migrator(dsn, migrations_dir)
    pending = m.pending()
    if args.dry_run:
        if not pending:
            print("migrate: nothing to apply")
            return
        print(f"migrate: would apply {len(pending)} migration(s):")
        for v in pending:
            print(f"  - {v}")
        return

    if not pending:
        print("migrate: nothing to apply")
        return

    applied = m.apply_all()
    print(f"migrate: applied {len(applied)} migration(s):")
    for v in applied:
        print(f"  - {v}")


def _run_jobs(args: argparse.Namespace) -> None:
    if args.job == "ingest-bundle":
        _run_ingest_bundle(args)
        return
    if args.job == "ingest-bundles":
        _run_ingest_bundles(args)
        return
    if args.job == "ingest-md":
        _run_ingest_md(args)
        return
    if args.job == "ingest-oracles":
        _run_ingest_oracles(args)
        return
    if args.job == "dedupe-papers":
        _run_dedupe_papers(args)
        return
    if args.job == "import-perplexity":
        _run_import_perplexity(args)
        return
    if args.job == "watch-patents":
        _run_watch_patents(args)
        return
    if args.job == "list-patent-watches":
        _run_list_patent_watches(args)
        return
    if args.job == "run-patent-watches":
        _run_run_patent_watches(args)
        return
    print(f"jobs: unknown subcommand {args.job!r}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Patent watch CLI helpers
# ---------------------------------------------------------------------------


def _parse_interval(spec: str) -> int:
    """Parse ``--every`` into seconds.

    Accepts ``Nh`` (hours), ``Nd`` (days), ``Nw`` (weeks), or a bare
    number of seconds. The CLI helper rejects anything else with a
    short error message — saved-watch intervals run for years, so a
    typo at create time would silently bake in a wrong cadence.
    """
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty interval")
    if s.isdigit():
        return int(s)
    unit = s[-1]
    head = s[:-1]
    if not head.isdigit():
        raise ValueError(f"invalid interval {spec!r} — expected like '7d', '1h', '1w'")
    n = int(head)
    if n <= 0:
        raise ValueError(f"invalid interval {spec!r} — must be positive")
    multipliers = {"h": 3600, "d": 86_400, "w": 604_800}
    if unit not in multipliers:
        raise ValueError(f"invalid interval unit {unit!r} — use 'h', 'd', or 'w'")
    return n * multipliers[unit]


def _run_watch_patents(args: argparse.Namespace) -> None:
    """Implements ``precis jobs watch-patents`` — create or delete a watch."""
    from precis.config import load_config
    from precis.errors import PrecisError
    from precis.handlers import _patent_watch_db as watch_db
    from precis.store import Store

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        if args.delete:
            try:
                watch_db.delete(store, args.name)
            except PrecisError as e:
                print(f"watch-patents: {e}", file=sys.stderr)
                if e.next:
                    print(f"  next: {e.next}", file=sys.stderr)
                sys.exit(1)
            print(f"watch-patents: deleted {args.name!r}")
            return

        if args.cql is None:
            print(
                "watch-patents: cql is required (or pass --delete to remove a watch)",
                file=sys.stderr,
            )
            sys.exit(2)

        try:
            interval_s = _parse_interval(args.every)
        except ValueError as e:
            print(f"watch-patents: {e}", file=sys.stderr)
            sys.exit(2)

        try:
            watch = watch_db.create(
                store,
                name=args.name,
                cql=args.cql,
                interval_s=interval_s,
                auto_get=args.auto_get,
                max_per_pass=args.max_per_pass,
                created_by="cli",
            )
        except PrecisError as e:
            print(f"watch-patents: {e}", file=sys.stderr)
            if e.next:
                print(f"  next: {e.next}", file=sys.stderr)
            sys.exit(1)
        mode = "auto-get" if watch.auto_get else "quest-on-new-hits"
        days = watch.interval_s / 86_400
        cap = (
            f"{watch.max_per_pass}/pass" if watch.max_per_pass is not None else "no cap"
        )
        print(
            f"watch-patents: created {watch.name!r} "
            f"[{mode}, every {days:g}d, {cap}]\n"
            f"  cql: {watch.cql}"
        )
    finally:
        store.close()


def _run_list_patent_watches(args: argparse.Namespace) -> None:
    """Implements ``precis jobs list-patent-watches``."""
    from precis.config import load_config
    from precis.handlers import _patent_watch_db as watch_db
    from precis.store import Store

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        watches = watch_db.list_all(store)
        if not watches:
            print("list-patent-watches: no saved watches")
            return
        # Header
        cols = f"{'NAME':<24}  {'EVERY':>6}  {'MODE':<8}  {'LAST RUN':<19}  {'SEEN':>5}"
        print(cols)
        print("-" * len(cols))
        for w in watches:
            mode = "auto" if w.auto_get else "quest"
            days = w.interval_s / 86_400
            every_str = f"{days:g}d" if days >= 1 else f"{w.interval_s // 3600}h"
            last = (
                w.last_run_at.strftime("%Y-%m-%d %H:%M:%S")
                if w.last_run_at is not None
                else "(never)"
            )
            print(
                f"{w.name:<24}  {every_str:>6}  {mode:<8}  "
                f"{last:<19}  {len(w.last_seen_pn):>5}"
            )
            if args.show_cql:
                print(f"    cql: {w.cql}")
        print(f"-- total: {len(watches)}")
    finally:
        store.close()


def _run_run_patent_watches(args: argparse.Namespace) -> None:
    """Implements ``precis jobs run-patent-watches``."""
    from pathlib import Path

    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers._patent_ops import OpsClient
    from precis.jobs.patent_watch import (
        DEFAULT_FAIR_USE_LIMIT_GB,
        run_one_pass,
    )
    from precis.store import Store

    # Env vars must be set; without them OPS calls would fail
    # immediately with auth errors.
    epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
    epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
    raw_root_str = os.environ.get("PRECIS_PATENT_RAW_ROOT")
    if not (epo_key and epo_secret and raw_root_str):
        print(
            "run-patent-watches: EPO_OPS_CLIENT_KEY, "
            "EPO_OPS_CLIENT_SECRET, and PRECIS_PATENT_RAW_ROOT must all "
            "be set",
            file=sys.stderr,
        )
        sys.exit(2)

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        ops = OpsClient(
            key=epo_key,
            secret=epo_secret,
            user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
        )

        fair_use_limit_gb = args.fair_use_limit_gb
        if fair_use_limit_gb is None:
            env_lim = os.environ.get("PRECIS_PATENT_FAIR_USE_LIMIT_GB")
            fair_use_limit_gb = float(env_lim) if env_lim else DEFAULT_FAIR_USE_LIMIT_GB

        summary = run_one_pass(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=Path(raw_root_str).expanduser(),
            only_name=args.name,
            dry_run=args.dry_run,
            fair_use_limit_gb=fair_use_limit_gb,
        )

        if summary.paused_global:
            gb = summary.fair_use_bytes_before / (1024**3)
            print(
                f"run-patent-watches: paused — rolling 7d fair-use "
                f"{gb:.2f} GiB ≥ limit {fair_use_limit_gb:.2f} GiB"
            )
            return
        if not summary.results:
            print("run-patent-watches: no watches due")
            return
        for r in summary.results:
            if r.error is not None:
                print(f"  fail  {r.watch_name}  — {r.error}")
                continue
            if r.skipped_dry_run:
                print(
                    f"  dry   {r.watch_name}  "
                    f"({len(r.new_pn)} new patent{'s' if len(r.new_pn) != 1 else ''})"
                )
                continue
            quest_part = f"quest={r.quest_slug}" if r.quest_slug is not None else ""
            ingest_part = f"ingested={len(r.ingested_pn)}" if r.ingested_pn else ""
            overflow_part = f"overflow={len(r.overflow_pn)}" if r.overflow_pn else ""
            details = (
                " ".join(p for p in (quest_part, ingest_part, overflow_part) if p)
                or "no new hits"
            )
            print(f"  ok    {r.watch_name}  new={len(r.new_pn)}  {details}")
    finally:
        store.close()


def _run_ingest_md(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers.markdown import MarkdownHandler
    from precis.store import Store

    cfg = load_config()
    root_str = args.root or cfg.markdown_root
    if not root_str:
        print(
            "ingest-md: root not specified and PRECIS_MARKDOWN_ROOT not set",
            file=sys.stderr,
        )
        sys.exit(2)
    root = Path(root_str).resolve()
    if not root.is_dir():
        print(f"ingest-md: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        handler = MarkdownHandler(store=store, root=root, embedder=embedder)

        ingested = 0
        skipped = 0
        failed = 0
        # Walk the same way the handler's index does — keeps slug
        # derivation identical.
        from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith((".md", ".markdown")):
                    continue
                p = Path(dirpath) / name
                try:
                    rel = str(p.relative_to(root))
                    slug = file_slug_from_path(rel)
                except ValueError:
                    failed += 1
                    print(f"  fail  {p}  — invalid path")
                    continue
                if not is_valid_file_slug(slug):
                    failed += 1
                    print(f"  fail  {p}  — invalid slug {slug!r}")
                    continue
                ref_before = store.get_ref(kind="markdown", id=slug)
                ref = handler._ensure_ingested(slug, force=args.force)
                if ref is None:
                    failed += 1
                    print(f"  fail  {p}  — ingest returned None")
                    continue
                if ref_before is None:
                    ingested += 1
                    print(f"  ok    {slug}  ({store.count_blocks(ref.id)} blocks)")
                else:
                    if args.force or (ref_before.meta or {}).get("sha256") != (
                        ref.meta or {}
                    ).get("sha256"):
                        ingested += 1
                        print(f"  upd   {slug}  ({store.count_blocks(ref.id)} blocks)")
                    else:
                        skipped += 1

        print(
            f"ingest-md: ingested={ingested}  skipped={skipped}  "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _run_ingest_oracles(args: argparse.Namespace) -> None:
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

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
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


def _run_ingest_bundle(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.store import Store

    path = Path(args.path)
    if not path.is_file():
        print(f"ingest-bundle: file not found: {path}", file=sys.stderr)
        sys.exit(2)

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        result = store.ingest_bundle(path, embedder=embedder)
        verb = "inserted" if result.inserted else "skipped (already present)"
        print(
            f"ingest-bundle: {verb} {result.slug} "
            f"({result.block_count} blocks) [embedder={cfg.embedder}]"
        )
    finally:
        store.close()


def _run_ingest_bundles(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.errors import PrecisError
    from precis.ingest import parse_bundle, read_bundle
    from precis.store import Store

    base = Path(args.dir)
    if not base.is_dir():
        print(f"ingest-bundles: not a directory: {base}", file=sys.stderr)
        sys.exit(2)

    bundles = sorted(base.rglob("*.acatome"))
    if args.limit is not None:
        bundles = bundles[: args.limit]
    if not bundles:
        print(f"ingest-bundles: no .acatome files under {base}")
        return

    cfg = load_config()

    if args.dry_run:
        ok = bad = 0
        for path in bundles:
            try:
                raw = read_bundle(path)
                parse_bundle(raw, embedding_dim=1024)
                ok += 1
            except PrecisError as e:
                print(f"  FAIL  {path}  — {e.cause}", file=sys.stderr)
                bad += 1
            except Exception as e:
                print(f"  FAIL  {path}  — {e}", file=sys.stderr)
                bad += 1
        print(f"ingest-bundles: dry-run  ok={ok}  failed={bad}")
        if bad:
            sys.exit(1)
        return

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        inserted = skipped = failed = 0
        for path in bundles:
            try:
                result = store.ingest_bundle(path, embedder=embedder)
            except PrecisError as e:
                print(f"  FAIL  {path.name}  — {e.cause}", file=sys.stderr)
                failed += 1
                continue
            except Exception as e:
                log.exception("unexpected error ingesting %s", path)
                print(f"  FAIL  {path.name}  — {e}", file=sys.stderr)
                failed += 1
                continue

            if result.inserted:
                inserted += 1
                print(f"  ok    {result.slug}  ({result.block_count} blocks)")
            else:
                skipped += 1
                print(f"  skip  {result.slug}  (already present)")
        print(
            f"ingest-bundles: inserted={inserted}  skipped={skipped}  "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _run_dedupe_papers(args: argparse.Namespace) -> None:
    """Implements ``precis jobs dedupe-papers`` — find and remove
    duplicate paper refs that share a content-identity key.

    Pre-fix ingest only deduped on DOI, so DOI-less bundles (e.g.
    ``text_rescue`` extracts) accumulated one extra row per rerun
    with a ``-N`` slug suffix. Each group is collapsed to its
    lowest-id row; duplicates are hard-deleted (blocks/tags/links
    cascade via ON DELETE CASCADE on refs.id).

    Dry-run by default. Pass ``--apply`` to actually delete.
    """
    from precis.config import load_config
    from precis.store import Store

    cfg = load_config()
    dsn = _resolve_dsn(args.database_url, cfg=cfg)

    keys = ("pdf_hash", "doi", "arxiv_id") if args.key == "all" else (args.key,)

    store = Store.connect(dsn)
    try:
        total_groups = 0
        total_dupes = 0
        # Track IDs already scheduled for deletion so that a paper
        # surfaced under multiple keys (e.g. shared pdf_hash AND
        # shared doi) isn't double-counted and doesn't mess with the
        # "keep lowest id" rule across keys.
        victim_ids: set[int] = set()

        with store.pool.connection() as conn:
            for key in keys:
                rows = conn.execute(
                    "WITH live AS ("
                    "  SELECT id, slug, meta->>%s AS v "
                    "  FROM refs "
                    "  WHERE kind = 'paper' AND deleted_at IS NULL "
                    "    AND meta ? %s AND meta->>%s <> ''"
                    ") "
                    "SELECT v, "
                    "       array_agg(id ORDER BY id) AS ids, "
                    "       array_agg(slug ORDER BY id) AS slugs "
                    "FROM live "
                    "GROUP BY v "
                    "HAVING count(*) > 1",
                    (key, key, key),
                ).fetchall()
                if not rows:
                    continue
                print(f"== Duplicates by {key} ({len(rows)} groups) ==")
                for value, ids, slugs in rows:
                    # Skip groups whose survivor is already a victim
                    # of an earlier-key pass — pathological, but cheap
                    # to guard against.
                    keep, *rest = zip(ids, slugs, strict=True)
                    keep_id, keep_slug = keep
                    dupe_pairs = [
                        (rid, rslug) for rid, rslug in rest if rid not in victim_ids
                    ]
                    if not dupe_pairs:
                        continue
                    total_groups += 1
                    total_dupes += len(dupe_pairs)
                    preview = (value or "")[:60]
                    print(
                        f"  {key}={preview!r}: keep #{keep_id} "
                        f"{keep_slug!r}, remove "
                        + ", ".join(f"#{rid} {rslug!r}" for rid, rslug in dupe_pairs)
                    )
                    for rid, _slug in dupe_pairs:
                        victim_ids.add(rid)

        verb = "would remove" if not args.apply else "removing"
        print(
            f"dedupe-papers: {verb} {total_dupes} duplicate refs across "
            f"{total_groups} groups [keys={','.join(keys)}]"
        )

        if args.apply and victim_ids:
            with store.tx() as conn:
                conn.execute(
                    "DELETE FROM refs WHERE id = ANY(%s)",
                    (sorted(victim_ids),),
                )
            print(f"dedupe-papers: deleted {len(victim_ids)} refs")
        elif args.apply:
            print("dedupe-papers: nothing to delete")
        else:
            print("dedupe-papers: dry-run (pass --apply to delete)")
    finally:
        store.close()


def _run_import_perplexity(args: argparse.Namespace) -> None:
    """Walk a directory and bulk-import every matching file as a
    perplexity ref via ``put(mode='import')``.

    Dry-run prints the derived query per file without touching the DB
    — useful for sanity-checking the ``--query-from`` heuristic
    before a real run.
    """
    from precis.config import load_config
    from precis.embedder import make_embedder
    from precis.handlers.perplexity import (
        ResearchHandler,
        ThinkHandler,
        WebsearchHandler,
    )
    from precis.store import Store

    base = Path(args.dir)
    if not base.is_dir():
        print(f"import-perplexity: not a directory: {base}", file=sys.stderr)
        sys.exit(2)

    files = sorted(p for p in base.rglob(args.glob) if p.is_file())
    if args.limit is not None:
        files = files[: args.limit]
    if not files:
        print(f"import-perplexity: no files matched {args.glob!r} under {base}")
        return

    handler_cls = {
        "websearch": WebsearchHandler,
        "think": ThinkHandler,
        "research": ResearchHandler,
    }[args.kind]

    cfg = load_config()

    # Dry run: parse + derive query per file; don't open a DB.
    if args.dry_run:
        for p in files:
            query = _derive_perplexity_query(p, strategy=args.query_from, base=base)
            print(f"  {p.relative_to(base)} -> {query!r}")
        print(f"import-perplexity: dry-run  {len(files)} file(s) would import")
        return

    dsn = _resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        handler = handler_cls(store=store, embedder=embedder)

        imported = failed = 0
        for p in files:
            rel = p.relative_to(base)
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                failed += 1
                print(f"  fail  {rel}  — read error: {exc}", file=sys.stderr)
                continue
            if not text.strip():
                failed += 1
                print(f"  fail  {rel}  — empty file", file=sys.stderr)
                continue
            query = _derive_perplexity_query(p, strategy=args.query_from, base=base)
            try:
                handler.put(id=query, text=text, mode="import")
            except Exception as exc:
                failed += 1
                print(f"  fail  {rel}  — {exc}", file=sys.stderr)
                continue
            imported += 1
            print(f"  ok    {rel}  -> {query!r}")

        print(
            f"import-perplexity: kind={args.kind} imported={imported} "
            f"failed={failed}  [embedder={cfg.embedder}]"
        )
        if failed:
            sys.exit(1)
    finally:
        store.close()


def _derive_perplexity_query(
    path: Path,
    *,
    strategy: str,
    base: Path,
) -> str:
    """Pick the ``id=`` query for a report file.

    ``h1``:  first ``# Heading`` in the file, else fall back to filename.
    ``filename``: always the stem with hyphens turned into spaces.
    """
    if strategy == "filename":
        return _query_from_filename(path)
    # strategy == "h1"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                heading = stripped[2:].strip()
                if heading:
                    return heading
    except (OSError, UnicodeDecodeError):
        pass  # fall through to filename
    return _query_from_filename(path)


def _query_from_filename(path: Path) -> str:
    """Stem with underscores/hyphens normalized to spaces."""
    stem = path.stem
    # Normalize common separators to spaces; collapse repeats.
    for ch in ("-", "_"):
        stem = stem.replace(ch, " ")
    while "  " in stem:
        stem = stem.replace("  ", " ")
    return stem.strip()


def _resolve_dsn(override: str | None, *, cfg: Any = None) -> str:
    """Pick the database DSN: CLI override > config > env.

    `cfg` may be passed in by callers that already loaded it, to avoid
    re-reading env / .env multiple times in one CLI invocation.
    """
    if override:
        return override
    if cfg is None:
        from precis.config import load_config

        cfg = load_config()
    if cfg.database_url:
        return cfg.database_url
    print(
        "no database_url configured — set PRECIS_DATABASE_URL or pass --database-url",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
