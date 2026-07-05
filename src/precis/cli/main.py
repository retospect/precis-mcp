"""Top-level CLI entry point and dispatcher.

Hosts :func:`main` (the ``precis`` console script entry point) and
:func:`_build_parser` (the argparse tree). Each subcommand's
parser registration and implementation live in a sibling module:

- :mod:`precis.cli.migrate`   — ``precis migrate``
- :mod:`precis.cli.maintenance` — ``precis maintenance run`` (nightly cron)
- :mod:`precis.cli.gripe`     — ``precis gripes`` (human-only triage dump)
- :mod:`precis.cli.ingest`    — ``precis jobs ingest{,-md,-oracles}``
- :mod:`precis.cli.perplexity`— ``precis jobs import-perplexity``
- :mod:`precis.cli.patent`    — ``precis jobs {watch,list,run}-patent-watches``

Keeping the dispatch table in one place is the cost; the benefit is
that each subcommand owns a single file you can read without
hunting through a 1 100-line monolith.
"""

from __future__ import annotations

import argparse
import logging
import sys

from precis.cli import (
    add,
    cron,
    db,
    draft,
    fix_metadata,
    gripe,
    heartbeat,
    ingest,
    logs,
    maintenance,
    migrate,
    migrate_refs,
    patent,
    perplexity,
    provenance,
    reconcile,
    repl,
    resolve,
    resolve_metadata,
    retire_draft_equations,
    schema_doc,
    serve_embeddings,
    stats,
    stubs,
    tools,
    verify,
    watch,
    web,
    worker,
)

log = logging.getLogger(__name__)


def main() -> None:
    """``precis`` console-script entry point.

    Parses argv, configures root logging, and dispatches to the
    owning subcommand module. ``serve`` is special-cased inline
    because it's the only subcommand with no arguments of its own
    and no database touch.
    """
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

    if args.cmd == "serve-embeddings":
        serve_embeddings.run(args)
        return

    if args.cmd == "migrate":
        migrate.run(args)
        return

    if args.cmd == "schema-doc":
        schema_doc.run(args)
        return

    if args.cmd == "db":
        db.run(args)
        return

    if args.cmd == "maintenance":
        maintenance.run(args)
        return

    if args.cmd == "fix-metadata":
        fix_metadata.run(args)
        return

    if args.cmd == "migrate-refs":
        migrate_refs.run(args)
        return

    if args.cmd == "reconcile-duplicates":
        reconcile.run(args)
        return

    if args.cmd == "retire-draft-equations":
        retire_draft_equations.run(args)
        return

    if args.cmd == "resolve-metadata":
        resolve_metadata.run(args)
        return

    if args.cmd == "gripes":
        gripe.run(args)
        return

    if args.cmd == "add":
        add.run(args)
        return

    if args.cmd == "watch":
        watch.run(args)
        return

    if args.cmd == "_watch_batch_ingest":
        watch.run_batch(args)
        return

    if args.cmd == "worker":
        worker.run(args)
        return

    if args.cmd == "logs":
        logs.run(args)
        return

    if args.cmd == "stubs":
        stubs.run(args)
        return

    if args.cmd == "stats":
        stats.run(args)
        return

    if args.cmd == "resolve":
        resolve.run(args)
        return

    if args.cmd == "draft":
        draft.run(args)
        return

    if args.cmd == "verify":
        verify.run(args)
        return

    if args.cmd == "jobs":
        _dispatch_job(args)
        return

    if args.cmd == "tools":
        tools.run(args)
        return

    if args.cmd == "repl":
        repl.run(args)
        return

    if args.cmd == "web":
        web.run(args)
        return

    if args.cmd == "heartbeat":
        heartbeat.run(args)
        return

    if args.cmd == "cron":
        cron.run(args)
        return

    parser.error(f"unknown command: {args.cmd!r}")


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse tree.

    Each subcommand module contributes its own subparser(s) via the
    ``add_parser`` / ``add_parsers`` hook. This function stays the
    single source of truth for the top-level command list; the
    implementations live elsewhere.
    """
    parser = argparse.ArgumentParser(
        prog="precis",
        description="precis-mcp v2 - paper, document, state, and tool access.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run the MCP server (stdio).")
    serve_embeddings.add_parser(sub)

    migrate.add_parser(sub)
    schema_doc.add_parser(sub)
    db.add_parser(sub)
    maintenance.add_parser(sub)
    fix_metadata.add_parser(sub)
    migrate_refs.add_parser(sub)
    reconcile.add_parser(sub)
    retire_draft_equations.add_parser(sub)
    resolve_metadata.add_parser(sub)
    gripe.add_parser(sub)
    add.add_parser(sub)
    watch.add_parser(sub)
    watch.add_batch_parser(sub)
    worker.add_parser(sub)
    logs.add_parser(sub)
    stubs.add_parser(sub)
    stats.add_parser(sub)
    resolve.add_parser(sub)
    draft.add_parser(sub)
    verify.add_parser(sub)
    tools.add_parser(sub)
    repl.add_parser(sub)
    web.add_parser(sub)
    cron.add_parser(sub)
    heartbeat.add_parser(sub)

    jobs = sub.add_parser("jobs", help="Run a one-shot maintenance job.")
    jobs_sub = jobs.add_subparsers(dest="job", required=True)

    ingest.add_parsers(jobs_sub)
    perplexity.add_parser(jobs_sub)
    patent.add_parsers(jobs_sub)
    provenance.add_parsers(jobs_sub)

    return parser


# ---------------------------------------------------------------------------
# Job dispatch
# ---------------------------------------------------------------------------


#: Jobs subcommand → (module, callable-attr-name). The runner looks
#: up the module attribute lazily so adding a new job is one line
#: here plus one handler in the owning module.
_JOB_DISPATCH: dict[str, tuple[object, str]] = {
    "ingest": (ingest, "run_ingest"),
    "ingest-md": (ingest, "run_md"),
    "ingest-oracles": (ingest, "run_oracles"),
    "import-perplexity": (perplexity, "run"),
    "watch-patents": (patent, "run_watch"),
    "list-patent-watches": (patent, "run_list"),
    "run-patent-watches": (patent, "run_runner"),
    "sweep-patent-fulltext": (patent, "run_fulltext_sweep_cli"),
    "fetch-google-patents": (patent, "run_gp_fetch_cli"),
    "check-provenance": (provenance, "run"),
    "sync-retraction-watch": (provenance, "run_sync"),
}


def _dispatch_job(args: argparse.Namespace) -> None:
    """Route ``precis jobs <job>`` to the owning module's runner."""
    entry = _JOB_DISPATCH.get(args.job)
    if entry is None:
        print(f"jobs: unknown subcommand {args.job!r}", file=sys.stderr)
        sys.exit(2)
    module, fn_name = entry
    getattr(module, fn_name)(args)


if __name__ == "__main__":
    main()
