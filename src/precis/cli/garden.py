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

from precis.cli._common import bind_process_store, resolve_dsn
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
        "audit history is preserved. cite_keys the merge re-homed to the "
        "survivor move back onto the restored ref, and the loser's own "
        "cite_key_alias redirects move back as aliases — survivor-owned "
        "keys stay put."
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
        # follows the redirect and would restore the winner instead. A merged
        # ref's cite_key no longer sits on the tombstone — the gr524022 fold
        # re-homes it as cite_key_alias on the survivor and stamps the moved
        # sets on the loser (meta.rehomed_cite_keys /
        # meta.rehomed_cite_key_aliases) — so the marker, not any alias on
        # the survivor, pins which loser owned the key. A live ref
        # that holds the key outright only matches when no retired ref
        # claims it (the marker wins — it is the merge's own record).
        if numeric:
            rows = conn.execute(
                "SELECT r.ref_id, r.kind, r.retired_at, r.meta "
                "FROM refs r WHERE r.ref_id = %s FOR UPDATE OF r",
                (int(numeric[1]),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.ref_id, r.kind, r.retired_at, r.meta, "
                "COALESCE(r.meta->'rehomed_cite_keys' ? %s, false) "
                "AS keyed "
                "FROM refs r WHERE "
                "r.kind = 'paper' AND ("
                "  (r.retired_at IS NOT NULL "
                "   AND (r.meta->'rehomed_cite_keys' ? %s "
                "        OR r.meta->'rehomed_cite_key_aliases' ? %s)) "
                "  OR (EXISTS (SELECT 1 FROM ref_identifiers i "
                "              WHERE i.ref_id = r.ref_id "
                "                AND i.id_kind = 'cite_key' "
                "                AND i.id_value = %s) "
                "      AND NOT EXISTS (SELECT 1 FROM refs l "
                "                      WHERE l.retired_at IS NOT NULL "
                "                        AND (l.meta->'rehomed_cite_keys' "
                "                             ? %s "
                "                             OR l.meta->"
                "'rehomed_cite_key_aliases' ? %s)))) "
                "ORDER BY keyed DESC, r.retired_at DESC, r.ref_id "
                "FOR UPDATE OF r",
                (address, address, address, address, address, address),
            ).fetchall()
        if len(rows) > 1 and bool(rows[1][4]) == bool(rows[0][4]):
            # Ambiguous only among same-rank claims: a rehomed_cite_keys
            # match outranks an alias-only one — the key's true owner is
            # the loser it was taken from, not a later fold the redirect
            # moved through — so a unique top rank wins outright.
            tied = [r for r in rows if bool(r[4]) == bool(rows[0][4])]
            ids = ", ".join(f"pa{r[0]}" for r in tied)
            raise BadInput(
                f"{address!r} resolves to {len(tied)} merged papers — "
                f"address one by id: {ids}",
                next=[f"precis garden restore pa{r[0]}" for r in tied],
            )
        row = rows[0] if rows else None
        if row is None and not numeric:
            # Pre-marker merges (folded before the rehomed_cite_keys
            # record existed) leave no claim, so the query above finds
            # nothing — and a live-alias hit below would read "already
            # live" while the loser stays retired. Fall back to the old
            # resolution: chase the cite_key_alias to its holder, then
            # the holder's supersedes edge back to the loser. A survivor
            # that absorbed several losers can't be pinned by key alone
            # — refuse and name each candidate's explicit id. One marker
            # still vetoes the chase even here: a value recorded as a
            # loser's adopted_displaced_key is provably the SURVIVOR's
            # own pre-merge key, never any loser's — the PK puts that
            # alias on the survivor the marker names.
            rows = conn.execute(
                "SELECT r.ref_id, r.kind, r.retired_at, r.meta "
                "FROM refs r WHERE r.kind = 'paper' "
                "AND EXISTS (SELECT 1 FROM ref_identifiers i "
                "            JOIN links l ON l.src_ref_id = i.ref_id "
                "              AND l.relation = 'supersedes' "
                "            WHERE l.dst_ref_id = r.ref_id "
                "              AND i.id_kind = 'cite_key_alias' "
                "              AND i.id_value = %s) "
                "AND NOT EXISTS (SELECT 1 FROM refs l "
                "               WHERE l.retired_at IS NOT NULL "
                "                 AND l.meta->>'adopted_displaced_key' "
                "                     = %s) "
                "FOR UPDATE OF r",
                (address, address),
            ).fetchall()
            if len(rows) > 1:
                ids = ", ".join(f"pa{r[0]}" for r in rows)
                raise BadInput(
                    f"{address!r} resolves to {len(rows)} merged papers — "
                    f"address one by id: {ids}",
                    next=[f"precis garden restore pa{r[0]}" for r in rows],
                )
            row = rows[0] if rows else None
        if row is None:
            # No retired ref claims the key — but it may still resolve to a
            # LIVE ref through a cite_key_alias (the survivor's own displaced
            # key, or an adopted key). Say that plainly instead of NotFound.
            live = store.get_ref(kind="paper", id=address)
            if live is not None:
                print(f"no change: pa{live.id} is already live.")
                return
            raise NotFound(f"no paper ref for {address!r}")
        ref_id, kind, retired_at, meta = row[0], row[1], row[2], row[3]
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
        notes: list[str] = []
        if args.dry_run:
            # Run the real restore inside a savepoint that always rolls
            # back — a dry run surfaces the same refusals (and notes)
            # the real run would raise instead of promising a restore
            # that then refuses.
            with conn.transaction(force_rollback=True):
                store.restore_superseded_ref(ref_id, conn=conn, notes=notes)
            print(f"DRY RUN (no write) — would restore {handle}: {scope}")
            for note in notes:
                print(f"  note: {note}")
            return
        store.restore_superseded_ref(ref_id, conn=conn, notes=notes)
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
    for note in notes:
        print(f"  note: {note}")


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
    bind_process_store(store)
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
