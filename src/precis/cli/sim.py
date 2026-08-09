"""``precis sim ...`` — drive external Pareto-sim repos as quests.

Slice 1 (``docs/backlog/sim-harness.md``): a plain CLI tool, no
job_type/executor/dispatch/worker.

- ``list``   — read the registry, print each sim's slug/path/quest;
  an unreachable checkout is reported, not a crash.
- ``ingest`` — project a sim's manifest ``outputs:`` (findings +
  CSV) into ``PRECIS_ROOT/sim/<slug>/`` and drive the existing
  prose-ingest walker (``handler._ensure_ingested``, not the
  create-only ``put()``) so they land as searchable
  ``markdown``/``plaintext`` refs with the producing git SHA in
  ``meta``. Binary plots are skipped this slice.
- ``verify`` — for each ``verify:`` YAML entry flagged
  ``verified: false``, lit-search precis (read-only), an LLM judge
  returns ``{value_ok, citation_ref, note}``, then (non-dry) write
  back ``verified: true`` + ``source:`` and git-commit on a
  ``precis-verify/<date>`` branch, mint a ``material`` + ``citation``,
  and append a quest deed. ``--dry-run`` prints the records + exact
  YAML diff and writes nothing.

Business logic lives in :mod:`precis.sim.manifest`,
:mod:`precis.sim.registry`, :mod:`precis.sim.ingest`, and
:mod:`precis.sim.verify` so it's testable without argparse; this
module is just DSN/embedder/Hub bootstrap (mirrors
``cli/ingest.py:run_ingest``) plus arg wiring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``sim`` command and its subcommands on ``sub``."""
    parser = sub.add_parser(
        "sim", help="Drive external Pareto-sim repos (ingest/verify) as quests."
    )
    sim_sub = parser.add_subparsers(dest="sim_cmd", required=True)

    list_p = sim_sub.add_parser("list", help="List registered sims from the registry.")
    list_p.add_argument(
        "--registry",
        default=None,
        help=(
            "Path to the sim registry YAML (default $PRECIS_ROOT/sims.yaml, "
            "override with PRECIS_SIMS_REGISTRY)."
        ),
    )

    ingest_p = sim_sub.add_parser(
        "ingest",
        help="Project a sim's manifest outputs into PRECIS_ROOT and ingest them.",
    )
    ingest_p.add_argument("slug", help="Registry slug of the sim to ingest.")
    ingest_p.add_argument(
        "--registry", default=None, help="Path to the sim registry YAML."
    )
    ingest_p.add_argument("--database-url", default=None)
    ingest_p.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest every output even if its content hasn't changed.",
    )

    verify_p = sim_sub.add_parser(
        "verify",
        help="Lit-search verify low-confidence YAML entries + writeback + quest deed.",
    )
    verify_p.add_argument("slug", help="Registry slug of the sim to verify.")
    verify_p.add_argument("--registry", default=None)
    verify_p.add_argument("--database-url", default=None)
    verify_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print records + the exact YAML diff; write no files/git/precis.",
    )

    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis sim <subcommand>``."""
    if args.sim_cmd == "list":
        _run_list(args)
        return
    if args.sim_cmd == "ingest":
        _run_ingest(args)
        return
    if args.sim_cmd == "verify":
        _run_verify(args)
        return
    raise SystemExit(f"unknown sim subcommand: {args.sim_cmd!r}")


def _run_list(args: argparse.Namespace) -> None:
    from precis.sim.registry import load_registry, registry_path

    try:
        path = registry_path(override=getattr(args, "registry", None))
    except ValueError as exc:
        print(f"sim list: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        entries = load_registry(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"sim list: {exc}", file=sys.stderr)
        sys.exit(2)

    if not entries:
        print(f"sim list: no sims registered in {path}")
        return

    for slug, entry in sorted(entries.items()):
        reachable = entry.path.is_dir()
        flag = "" if reachable else "  [UNREACHABLE]"
        quest = entry.quest or "-"
        print(f"  {slug:<20} path={entry.path}  quest={quest}{flag}")


def _run_ingest(args: argparse.Namespace) -> None:
    from precis.sim.ingest import ingest_sim
    from precis.sim.manifest import load_manifest
    from precis.sim.registry import load_registry, registry_path

    try:
        reg_path = registry_path(override=getattr(args, "registry", None))
    except ValueError as exc:
        print(f"sim ingest: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        entries = load_registry(reg_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"sim ingest: {exc}", file=sys.stderr)
        sys.exit(2)

    entry = entries.get(args.slug)
    if entry is None:
        print(
            f"sim ingest: no sim {args.slug!r} in registry {reg_path}", file=sys.stderr
        )
        sys.exit(2)
    if not entry.path.is_dir():
        print(f"sim ingest: sim path unreachable: {entry.path}", file=sys.stderr)
        sys.exit(2)

    try:
        manifest = load_manifest(entry.resolved_manifest_path())
    except (FileNotFoundError, ValueError) as exc:
        print(f"sim ingest: {exc}", file=sys.stderr)
        sys.exit(2)

    from precis.config import load_config
    from precis.dispatch import Hub
    from precis.embedder import make_embedder
    from precis.store import Store

    cfg = load_config()
    root_str = cfg.root
    if not root_str:
        print("sim ingest: PRECIS_ROOT not set", file=sys.stderr)
        sys.exit(2)
    root = Path(root_str).resolve()

    dsn = resolve_dsn(getattr(args, "database_url", None), cfg=cfg)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        hub = Hub(store=store, embedder=embedder)
        outcome = ingest_sim(
            slug=args.slug,
            entry=entry,
            manifest=manifest,
            root=root,
            hub=hub,
            store=store,
            force=bool(args.force),
        )
    finally:
        store.close()

    for msg in outcome.messages:
        print(f"  {msg}")
    print(
        f"sim ingest {args.slug}: ingested={outcome.ingested}  "
        f"skipped={outcome.skipped}  failed={outcome.failed}"
    )
    if outcome.failed:
        sys.exit(1)


def _run_verify(args: argparse.Namespace) -> None:
    from precis.sim.manifest import load_manifest
    from precis.sim.registry import load_registry, registry_path
    from precis.sim.verify import (
        make_corpus_search_fn,
        make_llm_judge_fn,
        verify_sim,
    )

    try:
        reg_path = registry_path(override=getattr(args, "registry", None))
    except ValueError as exc:
        print(f"sim verify: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        entries = load_registry(reg_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"sim verify: {exc}", file=sys.stderr)
        sys.exit(2)

    entry = entries.get(args.slug)
    if entry is None:
        print(
            f"sim verify: no sim {args.slug!r} in registry {reg_path}", file=sys.stderr
        )
        sys.exit(2)
    if not entry.path.is_dir():
        print(f"sim verify: sim path unreachable: {entry.path}", file=sys.stderr)
        sys.exit(2)

    try:
        manifest = load_manifest(entry.resolved_manifest_path())
    except (FileNotFoundError, ValueError) as exc:
        print(f"sim verify: {exc}", file=sys.stderr)
        sys.exit(2)

    from precis.config import load_config
    from precis.dispatch import Hub
    from precis.embedder import make_embedder
    from precis.store import Store

    cfg = load_config()
    dsn = resolve_dsn(getattr(args, "database_url", None), cfg=cfg)
    dry_run = bool(args.dry_run)

    store = Store.connect(dsn)
    try:
        embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
        hub = Hub(store=store, embedder=embedder)
        outcome = verify_sim(
            slug=args.slug,
            entry=entry,
            manifest=manifest,
            search_fn=make_corpus_search_fn(store),
            judge_fn=make_llm_judge_fn(),
            dry_run=dry_run,
            store=None if dry_run else store,
            hub=None if dry_run else hub,
        )
    finally:
        store.close()

    for rec in outcome.records:
        mark = "OK " if rec.will_flip else "-- "
        cite = rec.citation_ref or "-"
        print(f"  {mark}[{rec.entry}] value_ok={rec.value_ok} cite={cite}")
        if rec.note:
            print(f"       note: {rec.note}")
    for file_diff in outcome.diffs:
        if file_diff.diff:
            print(f"\n--- writeback diff: {file_diff.rel_file} ---")
            print(file_diff.diff, end="")
    for msg in outcome.messages:
        print(f"  {msg}")
    tail = "" if outcome.branch is None else f"  branch={outcome.branch}"
    verb = "would verify" if dry_run else "verified"
    print(
        f"\nsim verify {args.slug}: flagged={outcome.flagged}  "
        f"{verb}={outcome.verified}{tail}"
    )


__all__ = ["add_parser", "run"]
