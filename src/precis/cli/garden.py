"""``precis garden`` — the graph gardener's human-facing surface.

The ``graph_garden`` job only ever *proposes*: ``reviews`` rows with
``verdict='proposed'`` and a JSON evidence payload (migration 0185;
``precis.graph.garden``). This command is where a human reads them and
acts:

* ``precis garden proposals`` — the pending-proposal read view: every
  garden proposal still awaiting a verdict, newest first, flagged
  ``stale`` when the target has drifted past the sha the proposal was
  written against.
* ``precis garden apply <review_id>`` — the explicit apply door.
  v1 supports ``action=merge`` proposals only: the loser's links
  repoint onto the winner, the loser retires as an alias, and the
  proposal flips to ``approved`` (the second bar) — all one
  transaction. Other actions are still manual work; the proposal
  carries the evidence either way.
* ``precis garden undo <merge_id>`` — reverse an applied merge from the
  reversal record on the loser (``meta.garden_merge``).

Non-merge proposals get ``precis garden reject <review_id>`` to retire
them from the queue without an apply.
"""

from __future__ import annotations

import argparse
import os
import sys

from precis.cli._common import resolve_dsn
from precis.errors import BadInput, NotFound
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``garden`` subparser on ``sub``."""
    p = sub.add_parser(
        "garden",
        help="Read/act on graph-gardener proposals (merge apply + undo).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gp = p.add_subparsers(dest="garden_cmd", required=True)

    prop = gp.add_parser("proposals", help="List pending graph-gardener proposals.")
    prop.add_argument(
        "--action",
        choices=["merge", "split", "relink", "prune", "tag"],
        default=None,
        help="Show only proposals of one action kind.",
    )
    prop.add_argument("--limit", type=int, default=200)
    prop.add_argument("--dsn", default=None, help="Database URL override.")

    for name, helptext, arg in (
        (
            "apply",
            "Apply an accepted proposal (v1: merge proposals only).",
            "review_id",
        ),
        (
            "reject",
            "Reject a pending proposal (it leaves the pending view).",
            "review_id",
        ),
        (
            "undo",
            "Undo an applied merge by its merge_id.",
            "merge_id",
        ),
    ):
        sp = gp.add_parser(name, help=helptext)
        sp.add_argument(arg, help=f"The {arg} to act on.")
        sp.add_argument(
            "--by",
            default=None,
            help="Actor recorded on the verdict/merge (default: $USER).",
        )
        sp.add_argument("--dsn", default=None, help="Database URL override.")
    return p


def _actor(args: argparse.Namespace) -> str:
    return args.by or os.environ.get("USER") or "human"


def _proposals(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.proposals import pending_proposals

    rows = pending_proposals(store, action=args.action, limit=args.limit)
    if not rows:
        print("no pending garden proposals")
        return
    for p in rows:
        stale = "" if p.current else "  [stale: target changed]"
        print(
            f"review {p.review_id}: {p.action} {p.target_kind}:{p.target_id}"
            f"{stale}\n    {p.title} — {p.rationale}"
        )
        for k, v in sorted(p.evidence.items()):
            print(f"    {k}: {v}")
        if p.payload:
            print(f"    payload: {p.payload}")


def _apply(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.apply import apply_merge
    from precis.graph.garden.proposals import pending_proposals

    review_id = int(args.review_id)
    prop = next(
        (p for p in pending_proposals(store) if p.review_id == review_id),
        None,
    )
    if prop is None:
        print(
            f"no pending garden proposal review_id={review_id}",
            file=sys.stderr,
        )
        sys.exit(2)
    if prop.action != "merge":
        print(
            f"review {review_id} is a {prop.action!r} proposal — the apply "
            "door handles merges only in v1; carry it out by hand or "
            f"`precis garden reject {review_id}`.",
            file=sys.stderr,
        )
        sys.exit(2)
    loser, winner = prop.payload.get("loser"), prop.payload.get("winner")
    if not isinstance(loser, int) or not isinstance(winner, int):
        print(
            f"review {review_id} has no loser/winner payload — refusing",
            file=sys.stderr,
        )
        sys.exit(2)
    rec = apply_merge(store, loser, winner, actor=_actor(args), review_id=review_id)
    print(
        f"merged ref {rec.loser} into {rec.winner} "
        f"({rec.merge_id}) — {len(rec.links)} edge(s) replayed; "
        f"`precis garden undo {rec.merge_id}` reverses"
    )


def _reject(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.proposals import resolve_proposal

    row = resolve_proposal(
        store, int(args.review_id), actor=_actor(args), verdict="rejected"
    )
    if row is None:
        print(
            f"no pending garden proposal review_id={args.review_id}",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"rejected proposal {args.review_id}")


def _undo(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.apply import undo_merge

    counts = undo_merge(store, args.merge_id)
    print(
        f"undid {args.merge_id}: {counts['links_restored']} link(s) "
        f"restored, {counts['links_deleted']} merge-made deleted, "
        f"{counts['aliases_removed']} alias(es) removed"
    )


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.dsn))
    try:
        {
            "proposals": _proposals,
            "apply": _apply,
            "reject": _reject,
            "undo": _undo,
        }[args.garden_cmd](args, store)
    except (BadInput, NotFound) as exc:
        print(f"garden: {exc}", file=sys.stderr)
        sys.exit(2)


__all__ = ["add_parser", "run"]
