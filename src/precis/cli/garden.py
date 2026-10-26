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
  transaction. Relink proposals apply through the consumer's reviewed
  door (gr507641), not this verb; other actions are still manual work.
* ``precis garden undo <merge_id|relink_id>`` — reverse an applied
  merge from the reversal record on the loser (``meta.garden_merge``),
  or delete the edge a relink wrote (``links.meta.garden_relink``,
  ids ``garden-relink:…``).
* ``precis garden restore <slug-or-id>`` — revive a superseded paper
  without reversing migrated content. This operator-only CLI resolves the
  original row, bypassing redirects; garden merges must use ``undo`` so
  their recorded link reversal stays intact.
* ``precis garden review [--per-day N] [--min-age-days N]`` — the
  sampled reviewer burn-down: a frontier-tier model verdict on the
  oldest open proposals, recorded as non-terminal ``garden-reviewer``
  rows (the proposal stays open for a human).
* ``precis garden stats`` — the per-detector reviewer agreement rate
  plus pending counts. Reporting only, no gate.

Non-merge proposals get ``precis garden reject <review_id>`` to retire
them from the queue without an apply.
"""

from __future__ import annotations

import argparse
import os
import re
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
            "Undo an applied merge or relink by its recorded id.",
            "apply_id",
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

    restore = gp.add_parser(
        "restore", help="Restore a superseded paper without moving content back."
    )
    restore.add_argument(
        "ref", metavar="slug-or-id", help="Cite key, pa handle or ref id."
    )
    restore.add_argument("--by", default=None, help="Recorded actor (default: $USER).")
    restore.add_argument("--dsn", default=None, help="Database URL override.")
    restore.add_argument("--reason", default=None, help="Reason for restoration.")
    restore.add_argument(
        "--dry-run", action="store_true", help="Validate without writes."
    )

    rev = gp.add_parser(
        "review",
        help="Sampled reviewer verdicts on the oldest open proposals.",
    )
    rev.add_argument("--per-day", type=int, default=20)
    rev.add_argument("--min-age-days", type=int, default=3)
    rev.add_argument(
        "--fable-per-day",
        type=int,
        default=5,
        help="How many of the day's sampled proposals the Fable "
        "re-review also sees (0 disables it).",
    )
    rev.add_argument("--dsn", default=None, help="Database URL override.")

    st = gp.add_parser(
        "stats",
        help="Reviewer agreement rate per detector (report only).",
    )
    st.add_argument("--dsn", default=None, help="Database URL override.")
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
    from precis.graph.garden.apply import undo_merge, undo_relink

    apply_id = args.apply_id
    if apply_id.startswith("garden-relink:"):
        counts = undo_relink(store, apply_id, actor=_actor(args))
        print(f"undid {apply_id}: {counts['links_deleted']} link(s) deleted")
        return
    counts = undo_merge(store, apply_id)
    print(
        f"undid {apply_id}: {counts['links_restored']} link(s) "
        f"restored, {counts['links_deleted']} merge-made deleted, "
        f"{counts['aliases_removed']} alias(es) removed"
    )


def _restore(args: argparse.Namespace, store: Store) -> None:
    from precis.store.revision_context import revision_context

    address = args.ref.strip()
    numeric = re.fullmatch(r"(?:pa)?([0-9]+)", address)
    scope = (
        "retired_at, meta.superseded_by and meta.dedup cleared; "
        "incoming supersedes edges removed. Identifiers, body/PDF, "
        "bibliography references and ordinary links are not moved back; "
        "audit history is preserved."
    )
    with (
        revision_context(
            args.reason,
            actor=_actor(args),
            trust_origin="human",
            trust_actor_type="operator",
        ),
        store.tx() as conn,
    ):
        # Resolve and lock the original row together: normal handle resolution
        # follows the redirect and would restore the winner instead.
        row = conn.execute(
            "SELECT r.ref_id, r.kind, r.retired_at, r.meta FROM refs r WHERE "
            + (
                "r.ref_id = %s"
                if numeric
                else "r.kind = 'paper' AND EXISTS ("
                "SELECT 1 FROM ref_identifiers i WHERE i.ref_id = r.ref_id "
                "AND i.id_kind = 'cite_key' AND i.id_value = %s)"
            )
            + " FOR UPDATE OF r",
            (int(numeric[1]) if numeric else address,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no paper ref for {address!r}")
        ref_id, kind, retired_at, meta = row
        if kind != "paper":
            raise BadInput(f"ref {ref_id} is {kind}, not paper")
        handle = f"pa{ref_id}"
        # Validate even on dry runs; the store repeats this guard for callers
        # that enter through other operator paths.
        if "garden_merge" in meta:
            record = meta["garden_merge"]
            merge_id = (
                record.get("merge_id", "<merge-id>")
                if isinstance(record, dict)
                else "<merge-id>"
            )
            raise BadInput(
                f"{handle} has a garden merge reversal record",
                next=f"precis garden undo {merge_id}",
            )
        if retired_at is None:
            print(f"no change: {handle} is already live.")
            return
        if meta.get("superseded_by") is None:
            raise BadInput(
                f"{handle} is retired but not superseded",
                next=f"edit(kind='paper', id='{handle}', "
                "args={'retire': False, 'reason': '<why>'})",
            )
        if args.dry_run:
            print(f"DRY RUN (no write) — would restore {handle}: {scope}")
            return
        store.restore_superseded_ref(ref_id, conn=conn)
        store.append_event(
            ref_id,
            source="garden.restore",
            event="restored_superseded",
            payload={
                "superseded_by": meta["superseded_by"],
                "reason": args.reason,
                "actor": _actor(args),
            },
            conn=conn,
        )
    print(f"restored {handle}: {scope}")


def _review(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.reviewer import review_sample

    rep = review_sample(
        store,
        per_day=args.per_day,
        min_age_days=args.min_age_days,
        fable_per_day=args.fable_per_day,
    )
    paused = " — paused (model window)" if rep.paused else ""
    print(
        f"reviewed {rep.sampled} proposal(s): {rep.approvals} approve, "
        f"{rep.rejections} reject, {rep.errors} error(s); "
        f"fable re-reviewed {rep.fable_sampled} "
        f"({rep.fable_errors} error(s)){paused}"
    )


def _stats(args: argparse.Namespace, store: Store) -> None:
    from precis.graph.garden.consumer import reject_reasons
    from precis.graph.garden.proposals import pending_proposals
    from precis.graph.garden.reviewer import agreement_stats

    pending: dict[str, int] = {}
    for p in pending_proposals(store, limit=1000):
        pending[p.detector] = pending.get(p.detector, 0) + 1
    stats = agreement_stats(store)
    reasons = reject_reasons(store)
    detectors = sorted(set(stats) | set(pending) | set(reasons))
    if not detectors:
        print("no garden proposals or reviewer rows")
        return
    for det in detectors:
        s = stats.get(det, {})
        rate = f"{s['rate']:.0%}" if s.get("sampled") else "—"
        frate = f"{s['fable_rate']:.0%}" if s.get("fable_sampled") else "—"
        print(
            f"{det}: pending={pending.get(det, 0)} "
            f"sampled={s.get('sampled', 0)} approval={rate} "
            f"fable={s.get('fable_sampled', 0)} opus-fable={frate}"
            + (
                " rejects="
                + ",".join(f"{k}:{v}" for k, v in sorted(reasons[det].items()))
                if det in reasons
                else ""
            )
        )


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.dsn))
    try:
        {
            "proposals": _proposals,
            "apply": _apply,
            "reject": _reject,
            "undo": _undo,
            "restore": _restore,
            "review": _review,
            "stats": _stats,
        }[args.garden_cmd](args, store)
    except (BadInput, NotFound) as exc:
        print(f"garden: {exc}", file=sys.stderr)
        sys.exit(2)


__all__ = ["add_parser", "run"]
