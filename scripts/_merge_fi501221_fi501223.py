"""Merge the duplicate claim hubs fi501221 and fi501223 (Q12 (d)).

Thin, reviewable wrapper over the merge door
(:func:`precis.taproot.hub.merge_hubs`): the loser collapses into the
winner — every live link repointed/deduped, live draft ``[fi<loser>]``
cites rewritten to the winner, the loser soft-deleted with a
``collapsed-into`` record edge. The winner's title/scope/pub_id are
untouched.

Default ``--winner 501223 --loser 501221`` — fi501223 carries the fuller
claim sentence ("insensitive to the density it is evaluated on …");
fi501221's ``corroborates`` edge survives the merge repoint. The audit
named no direction, so this default is builder judgment — Reto may
override: ``--winner``/``--loser`` flip it, and the dry run prints the
plan (check evidence counts before applying). Either side past
``'candidate'`` in ``nanopub_publish`` refuses the merge.

Dry run by default (prints the :class:`MergePlan`, writes nothing);
``--apply`` writes. Database URL from ``PRECIS_DATABASE_URL`` (see
``scripts/prod-precis``).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

_LOSER, _WINNER = 501221, 501223


def run(store: Any, *, winner_ref_id: int, loser_ref_id: int, apply: bool) -> str:
    """Merge ``loser_ref_id`` into ``winner_ref_id`` via ``merge_hubs``;
    returns the printed :class:`MergePlan`. ``apply=False`` plans only."""
    from precis.taproot.hub import merge_hubs

    plan = merge_hubs(
        store,
        loser_ref_id=loser_ref_id,
        winner_ref_id=winner_ref_id,
        dry_run=not apply,
    )
    out = str(plan)
    if not apply:
        out += "\ndry run — pass --apply to merge"
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="perform the merge")
    p.add_argument("--winner", type=int, default=_WINNER, metavar="REF_ID")
    p.add_argument("--loser", type=int, default=_LOSER, metavar="REF_ID")
    args = p.parse_args()

    dsn = os.environ.get("PRECIS_DATABASE_URL")
    if not dsn:
        sys.exit("set PRECIS_DATABASE_URL (scripts/prod-precis exports it)")
    from precis.store.store import Store

    store = Store.connect(dsn)
    try:
        print(
            run(
                store,
                winner_ref_id=args.winner,
                loser_ref_id=args.loser,
                apply=args.apply,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
