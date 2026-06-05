"""``precis verify <pub-id>`` — stamp ``human_verified_at`` on a finding.

The chase worker establishes a finding's citation chain
deterministically (regex + S2 references). That's enough to flip
``STATUS:established`` and let ``precis resolve`` substitute the
primary cite_key — but the chain hasn't been read by a human.

``precis verify`` is the opt-in layer for authors who want
manuscripts gated on actual human review. Run it on each finding
whose chain you've eyeballed; ``precis resolve --strict-verified``
then refuses to substitute anything that hasn't been verified.

Typical workflow:

* ``get(kind='finding', id='ab12c3')`` — read the chain.
* ``precis verify ab12c3 --note 'walked the cite chain manually'``
* ``precis resolve manuscript.tex --strict-verified --format latex``

Idempotent — re-running refreshes the timestamp and overwrites the
note. ``--clear`` removes the verification (used when a downstream
event invalidates the prior review).
"""

from __future__ import annotations

import argparse
import os
import sys

from precis.cli._common import resolve_dsn
from precis.errors import NotFound
from precis.store import Store


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "verify",
        help="Mark a finding's citation chain as human-verified.",
        description=(
            "Stamp ``human_verified_at`` on a finding's ref so "
            "``precis resolve --strict-verified`` will substitute "
            "the primary cite_key. Idempotent."
        ),
    )
    p.add_argument(
        "pub_id",
        help="The finding's pub_id (the placeholder you'd write in "
        "your draft, e.g. ``ab12c3``). Numeric ref_ids also accepted.",
    )
    p.add_argument(
        "--by",
        default=None,
        help="Verifier identity (default: $USER, falls back to 'human').",
    )
    p.add_argument(
        "--note",
        default=None,
        help="Optional free-text note describing the verification.",
    )
    p.add_argument(
        "--clear",
        action="store_true",
        help="Remove the verification stamp rather than setting it. "
        "Use when a downstream event invalidates a prior review.",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def run(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        ref_id = _resolve_finding_ref_id(store, args.pub_id)
        if args.clear:
            store.clear_human_verified(ref_id)
            print(f"verify: cleared verification on finding ref_id={ref_id}")
            return
        by = args.by or os.environ.get("USER") or "human"
        store.set_human_verified(ref_id, by=by, note=args.note)
        suffix = f" — {args.note}" if args.note else ""
        print(f"verify: ref_id={ref_id} verified by {by!r}{suffix}")
    except NotFound as exc:
        print(f"verify: {exc.cause}", file=sys.stderr)
        sys.exit(2)
    finally:
        store.close()


def _resolve_finding_ref_id(store: Store, raw: str) -> int:
    """Resolve a ``pub_id`` (or bare numeric ref_id) to its finding ref_id.

    The CLI accepts both shapes so an operator with the
    placeholder text from a draft can paste it verbatim, while
    scripts can pass the numeric id they already have.
    """
    raw = raw.strip()
    if not raw:
        raise NotFound("verify: pub_id is required")
    if raw.isdigit():
        ref_id = int(raw)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs "
                "WHERE ref_id = %s AND kind = 'finding' AND deleted_at IS NULL",
                (ref_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"no live finding with ref_id={ref_id}")
        return int(row[0])

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id "
            "FROM ref_identifiers ri "
            "JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = 'pub_id' AND ri.id_value = %s "
            "  AND r.kind = 'finding' AND r.deleted_at IS NULL",
            (raw,),
        ).fetchone()
    if row is None:
        raise NotFound(f"no finding with pub_id={raw!r}")
    return int(row[0])


__all__ = ["add_parser", "run"]
