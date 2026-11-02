"""``precis pathway`` — pathway-kind maintenance, plugin side.

Registered through the ``precis.cli`` entry-point group (the plugin seam
``precis.cli.registry.commands()`` walks): core never imports this
package, so the command cannot live under ``src/precis/cli`` —
``tests/test_plugin_import_boundary.py`` walks core's AST for exactly
that.

``reingest-structures <pathway>`` repairs one pathway's intermediate
``structure`` refs from the per-state extxyz still on its succeeded
``autocatpath_seed`` jobs (``meta.structures``, keyed by the full state
name — the record that survived gr549179's slug collision), then
re-stamps ``meta.structure_refs``. Only the legacy-slug collision
groups are touched: the member keeping the legacy slug is re-saved
onto the shared ref its stamped mapping already names (by ref id —
its slug is whatever the original ingest minted), each other member
mints once under its new suffixed slug, and every non-colliding state
keeps its stamped ref untouched — older pathways' refs sit under a
different ``pw-`` prefix than the recomputed one, so a blanket
slug-recompute would mint a duplicate per state. Dry-run by default;
``--apply`` writes.

An operator one-shot for pathways ingested before the injective slug —
nothing calls it on a cadence.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from precis.cli._common import resolve_dsn
from precis.cli.registry import Command

#: The ``precis.cli`` entry point resolves to this sequence — the plugin
#: analogue of a ``COMMANDS`` row. ``help`` must byte-match ``add_parser``'s
#: ``help=`` below or the lazy/eager help trees diverge.
COMMANDS: tuple[Command, ...] = (
    Command(
        "pathway",
        "precis_pathway.cli",
        "Pathway-kind maintenance: re-ingest intermediate structures.",
    ),
)


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``pathway`` subcommand (``reingest-structures``)."""
    p = sub.add_parser(
        "pathway",
        help="Pathway-kind maintenance: re-ingest intermediate structures.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    psub = p.add_subparsers(dest="pathway_cmd", required=True)
    ri = psub.add_parser(
        "reingest-structures",
        help="Rewrite a pathway's intermediate structure refs from its seed jobs.",
        description=(
            "Re-collect each state's relaxed extxyz from the pathway's "
            "succeeded autocatpath_seed jobs (meta.structures), repair the "
            "legacy-slug collision groups' structure refs (gr549179) and "
            "re-stamp meta.structure_refs. Non-colliding states keep their "
            "stamped refs untouched. Dry-run by default; --apply writes."
        ),
    )
    ri.add_argument("pathway", help="Pathway ref id or slug.")
    ri.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-ingest. Without this flag the command is a dry-run.",
    )
    ri.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )
    return p


def run(args: argparse.Namespace) -> None:
    """Dispatch ``precis pathway <cmd>``."""
    if args.pathway_cmd == "reingest-structures":
        _reingest_structures(args)


def _reingest_structures(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.runtime import build_runtime

    from . import ingest as pw_ingest

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "pathway: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        raise SystemExit(2)

    ident: int | str = int(args.pathway) if args.pathway.isdigit() else args.pathway
    ref = store.get_ref(kind="pathway", id=ident)
    if ref is None:
        print(f"pathway: no live pathway ref {args.pathway!r}", file=sys.stderr)
        raise SystemExit(2)

    structures, seed_key = pw_ingest.seed_geometry(store, int(ref.id))
    if not structures:
        print(
            f"pathway {ref.slug or ref.id}: no succeeded seed job carries "
            "meta.structures — nothing to re-ingest from (a monolith-era "
            "pathway keeps no per-state geometry; re-run it instead)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    meta = dict(ref.meta or {})
    key = str(meta.get("content_key") or "") or seed_key
    collisions = pw_ingest.colliding_states(structures)
    prior = {str(s): int(r) for s, r in (meta.get("structure_refs") or {}).items()}
    shared = {rid for rid, n in Counter(prior.values()).items() if n > 1}
    plan = pw_ingest.reingest_plan(key, structures, prior)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(
        f"pathway reingest-structures [{mode}]: {ref.slug or ref.id} "
        f"(ref {ref.id}), {len(structures)} state(s)",
        file=sys.stderr,
    )
    for state in structures:
        act = plan.get(state)
        if act is None:
            continue
        cur = prior.get(state)
        if act.action == "keep":
            action = f"leave st{act.ref_id} untouched (already distinct)"
            target = ""
        elif act.action == "repair":
            action = f"re-save onto st{act.ref_id} (shared) in place"
            target = ""
        else:
            existing = store.get_ref(kind="structure", id=str(act.slug))
            action = (
                f"replace st{existing.id} in place"
                if existing is not None
                else "mint a new structure ref"
            )
            target = f"{act.slug} — "
        if cur is None:
            now = "currently unmapped"
        elif cur in shared:
            now = f"currently st{cur} (shared)"
        else:
            now = f"currently st{cur}"
        print(f"  {state}: {target}{action}; {now} [legacy-slug collision]")
    untouched = len(structures) - len(plan)
    print(f"  {untouched} non-colliding state(s) keep their stamped refs untouched")
    if collisions:
        print(
            "  legacy-slug collisions: "
            + "; ".join(
                f"{base} <- {', '.join(names)}" for base, names in collisions.items()
            ),
            file=sys.stderr,
        )
    if dry_run:
        print("Re-run with --apply to write.", file=sys.stderr)
        return

    res = pw_ingest.reingest_pathway_structures(store, ref)
    skipped = (
        f"; skipped (geometry unreadable, prior entry kept): {', '.join(res.skipped)}"
        if res.skipped
        else ""
    )
    print(
        f"pathway reingest-structures [APPLY] done: wrote {res.written} of "
        f"{res.input_states} state(s); meta.structure_refs now maps "
        f"{len(res.structure_refs)}{skipped}.",
        file=sys.stderr,
    )


__all__ = ["COMMANDS", "add_parser", "run"]
