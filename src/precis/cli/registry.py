"""Command registry — which module owns each ``precis`` subcommand.

The dispatcher in :mod:`precis.cli.main` reads this table instead of
importing every subcommand module up front. A subcommand's module is
imported only when that subcommand is invoked (or its ``--help`` is
asked for), so an optional dependency of one subcommand cannot take
down ``precis serve`` or any other command. That outage happened once:
``tenacity`` sat behind the ``[paper]`` extra, a host without it failed
importing an unrelated subcommand, ``serve`` exited 1 and every node's
embedder died.

Each :class:`Command` carries its one-line ``help`` as a string so the
top-level ``precis --help`` lists every command without importing any
of them. ``tests/test_cli_lazy.py`` asserts each string still equals the
``help=`` the owning module passes to ``add_parser``, so the two cannot
drift apart silently.

Core commands are read from :data:`COMMANDS` directly, never through
install metadata: a long-lived container with stale entry points must
still be able to run ``serve``. Other distributions add commands
through the ``precis.cli`` entry-point group; each entry point resolves
to a sequence of :class:`Command`. A core name always wins a collision.

Adding a core subcommand: one :class:`Command` row here, plus the
module's ``add_parser(sub)`` and ``run(args)``.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points

log = logging.getLogger(__name__)

#: Entry-point group through which a plugin distribution adds subcommands.
CLI_PLUGIN_GROUP = "precis.cli"


@dataclass(frozen=True)
class Command:
    """One ``precis`` subcommand, resolvable without importing its module.

    ``register`` names the module function that adds the real subparser;
    a module whose registrar adds several commands (the ``jobs`` modules)
    is registered once however many of its commands are selected.
    ``run`` names the function the dispatcher calls with the parsed args.
    """

    name: str
    module: str
    help: str | None
    register: str = "add_parser"
    run: str = "run"


def _c(name: str, module: str, help: str | None, **kw: str) -> Command:
    return Command(name, f"precis.cli.{module}", help, **kw)


#: Top-level core subcommands, in ``precis --help`` order. ``serve`` and
#: ``jobs`` are built by :mod:`precis.cli.main` itself and are not listed.
COMMANDS: tuple[Command, ...] = (
    _c(
        "serve-embeddings",
        "serve_embeddings",
        "Run the HTTP embedding service (server side of the remote embedder).",
    ),
    _c(
        "anki-sync",
        "anki_sync",
        "Sync precis anki cards to AnkiWeb + read decay stats back.",
    ),
    _c("migrate", "migrate", "Apply pending DB migrations."),
    _c("schema-doc", "schema_doc", "Generate a Mermaid ER diagram of the DB schema."),
    _c("secret", "secret", "Manage the DB secrets vault."),
    _c("nanopub", "nanopub", "Mint, sign, anchor and audit claim nanopubs."),
    _c("pcb", "pcb", "PCB board import and catalog maintenance."),
    _c("settings", "settings", "Manage DB-resident settings (precis.settings)."),
    _c("users", "users", "Manage precis-web login accounts."),
    _c("db", "db", "Database baseline maintenance."),
    _c(
        "maintenance",
        "maintenance",
        "Daily housekeeping: refresh WATCH:* tags, purge tombstones, VACUUM.",
    ),
    _c(
        "enrich-openalex",
        "enrich_openalex",
        "Slurp free OpenAlex metadata (citations/topics/ORCID) onto papers.",
    ),
    _c(
        "fetch-openalex",
        "fetch_openalex",
        "OpenAlex Content download (paid): one DOI/stub, or --backfill.",
    ),
    _c(
        "fix-metadata",
        "fix_metadata",
        "Re-derive metadata for papers ingested with junk/empty titles.",
    ),
    _c(
        "migrate-refs",
        "migrate_refs",
        "Rewrite legacy kind:ref / ¶ references to universal handles.",
    ),
    _c(
        "reconcile-duplicates",
        "reconcile",
        "Merge duplicate paper refs (pdf_sha256 / doi-case / fuzzy title).",
    ),
    _c(
        "markup-backfill",
        "markup_backfill",
        "Re-queue front-matter-only Elsevier preview papers for re-fetch.",
    ),
    _c(
        "bodiless-heal",
        "bodiless_heal",
        "Judge and re-extract papers that hold a PDF but no body text.",
    ),
    _c(
        "title-backfill",
        "title_backfill",
        "Re-arm metadata enrichment for papers stuck at '(no title)'.",
    ),
    _c(
        "enrich-rearm",
        "enrich_rearm",
        "Re-arm metadata enrichment for named papers (refills new fields).",
    ),
    _c(
        "taxonomy-bootstrap",
        "taxonomy",
        "Generate a campaign's measurand list from corpus usage.",
    ),
    _c(
        "retire-draft-equations",
        "retire_draft_equations",
        "Convert legacy draft `equation` chunks to `$$…$$` paragraphs.",
    ),
    _c(
        "convert-draft-lists",
        "convert_draft_lists",
        "Convert markdown bullet paragraphs to ulist/item chunk trees.",
    ),
    _c(
        "resolve-metadata",
        "resolve_metadata",
        "Re-resolve needs-triage paper metadata (Crossref DOI / S2 title).",
    ),
    _c(
        "backfill-dois",
        "doi_backfill",
        "Recover a real DOI for DOI-less paper refs (S2 id / title match).",
    ),
    _c("podcast", "podcast", "Publish/list private audio-feed episodes."),
    _c("gripes", "gripe", "Dump all filed gripes for human triage."),
    _c(
        "memory",
        "memory",
        "Import / render harness memory as SPACE:repo-dev graph nodes.",
    ),
    _c("add", "add", "Ingest a paper (PDF, DOI, or arXiv ID) into the v2 schema."),
    _c(
        "ingest",
        "watch",
        "Watch a directory and auto-ingest PDFs into the v2 schema.",
    ),
    _c(
        "_watch_batch_ingest",
        "watch",
        argparse.SUPPRESS,
        register="add_batch_parser",
        run="run_batch",
    ),
    _c("worker", "worker", "Drive the derived-artifact queue (embeddings, summaries)."),
    _c("logs", "logs", "Read the centralised worker_logs table"),
    _c("stubs", "stubs", "List paper refs needing PDFs (with last fetcher attempt)."),
    _c(
        "stats",
        "stats",
        "Summarise finding-status counts, stub backlog, argument graph.",
    ),
    _c(
        "resolve",
        "resolve",
        "Substitute finding [pub_id] placeholders with cite_keys.",
    ),
    _c("draft", "draft", "Operate on draft documents (export, …)."),
    _c("paper", "paper", "Operate on paper refs (authors-resplit, …)."),
    _c("verify", "verify", "Mark a finding's citation chain as human-verified."),
    _c(
        "tools",
        "tools",
        "Run precis tools (get, search, put, edit, delete, tag, link)",
    ),
    _c("repl", "repl", "Interactive shell over the seven-verb tool surface."),
    _c("web", "web", "Run the precis web UI (FastAPI; requires the [web] extra)."),
    _c("cron", "cron", "Cron operations — tick the scheduled-task scanner."),
    _c("cast", "cast", "Compose/schedule the daily audio casts."),
    _c("quest", "quest", "Quest layer — strivings above the work."),
    _c(
        "classify",
        "classify",
        "Chunk/paper classifier cascades — targeted-scope drivers.",
    ),
    _c("llm", "llm", "LLM catalog — model choice as a queryable resource."),
    _c(
        "heartbeat",
        "heartbeat",
        "Report this host's load + CPU temp to host_heartbeat.",
    ),
    _c(
        "service",
        "service",
        "Live worker run control (service_config): prio / model / clear.",
    ),
    _c(
        "taproot",
        "taproot",
        "Taproot claim-hub authoring (mint hubs from citations).",
    ),
    _c(
        "taproot-migrate",
        "taproot_migrate",
        "Taproot atomic-claims migration runner — phase 0 (score/cohort) + "
        "phase 1 (dry-run decomposition) over EXISTING claim hubs. Both "
        "subcommands make zero claim-data writes.",
    ),
    _c(
        "email",
        "email",
        "Manage mailbox accounts for the email kind (IMAP read; v1).",
    ),
    _c("sim", "sim", "Drive external Pareto-sim repos (ingest/verify) as quests."),
    _c(
        "eval",
        "eval_cmd",
        "Execute one verb call, e.g. eval \"get(kind='skill', id='toc')\".",
    ),
)

#: ``precis jobs <job>`` subcommands. Several jobs share a module whose
#: ``add_parsers`` registers all of them at once.
JOB_COMMANDS: tuple[Command, ...] = (
    _c(
        "ingest",
        "ingest",
        "Pre-warm the store by ingesting every prose file under PRECIS_ROOT.",
        register="add_parsers",
        run="run_ingest",
    ),
    _c(
        "ingest-md",
        "ingest",
        "[DEPRECATED] alias for `ingest --kinds md`.",
        register="add_parsers",
        run="run_md",
    ),
    _c(
        "ingest-oracles",
        "ingest",
        "Seed the oracle kind from YAML wisdom files.",
        register="add_parsers",
        run="run_oracles",
    ),
    _c(
        "import-perplexity",
        "perplexity",
        "Bulk put(mode='import') a directory of Perplexity reports.",
    ),
    _c(
        "watch-patents",
        "patent",
        "Create a saved CQL patent watch (or delete one with --delete).",
        register="add_parsers",
        run="run_watch",
    ),
    _c(
        "list-patent-watches",
        "patent",
        "List every saved patent watch.",
        register="add_parsers",
        run="run_list",
    ),
    _c(
        "run-patent-watches",
        "patent",
        "Run a one-shot pass over all due patent watches.",
        register="add_parsers",
        run="run_runner",
    ),
    _c(
        "sweep-patent-fulltext",
        "patent",
        "Retry OPS description / claims endpoints for patents whose full "
        "text wasn't available at ingest time.",
        register="add_parsers",
        run="run_fulltext_sweep_cli",
    ),
    _c(
        "fetch-google-patents",
        "patent",
        "Fall-back full-text fetcher via patents.google.com. Picks patents "
        "tagged awaiting-fulltext or fulltext-unavailable (no gp-attempted "
        "tag), fetches the patents.google.com page, parses description + "
        "claims, and inserts blocks.",
        register="add_parsers",
        run="run_gp_fetch_cli",
    ),
    _c(
        "reingest-patents",
        "patent",
        "Force-reingest already-ingested patents so their claim blocks carry "
        "the patent_block markers the freedom-to-operate digest reads. "
        "Re-fetches OPS XML, re-parses, and swaps each ref's blocks in place "
        "(id/links/tags preserved). Operator backfill.",
        register="add_parsers",
        run="run_reingest_cli",
    ),
    _c(
        "check-provenance",
        "provenance",
        "Check a batch of DOIs against Crossref for retractions, expressions "
        "of concern, and corrections.",
        register="add_parsers",
    ),
    _c(
        "sync-retraction-watch",
        "provenance",
        "Pull the Retraction Watch dataset (CC-BY via Crossref) into the "
        "local provenance cache. Run monthly via cron.",
        register="add_parsers",
        run="run_sync",
    ),
    _c(
        "kill",
        "jobs_admin",
        "Request an operator force-kill of a running kind='job' ref.",
    ),
)

#: Names :mod:`precis.cli.main` builds itself; a plugin cannot take them.
RESERVED = frozenset({"serve", "jobs"})


def commands() -> tuple[Command, ...]:
    """Core commands, then every plugin's, in entry-point name order.

    A plugin entry point that fails to load, or does not resolve to a
    sequence of :class:`Command`, is skipped with a warning — one broken
    plugin must not take the CLI down.
    """
    out = list(COMMANDS)
    taken = {c.name for c in COMMANDS} | RESERVED
    for ep in sorted(entry_points(group=CLI_PLUGIN_GROUP), key=lambda e: e.name):
        try:
            loaded = ep.load()
        except Exception as exc:
            log.warning("precis.cli plugin %r failed to load: %s", ep.name, exc)
            continue
        if not isinstance(loaded, Sequence) or not all(
            isinstance(c, Command) for c in loaded
        ):
            log.warning(
                "precis.cli plugin %r is not a sequence of Command; skipped", ep.name
            )
            continue
        for cmd in loaded:
            if cmd.name in taken:
                log.warning(
                    "precis.cli plugin %r: command %r already taken; skipped",
                    ep.name,
                    cmd.name,
                )
                continue
            taken.add(cmd.name)
            out.append(cmd)
    return tuple(out)
