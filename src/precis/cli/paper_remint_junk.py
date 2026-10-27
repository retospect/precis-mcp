"""``precis paper-remint-junk`` — re-mint papers stuck on a junk cite_key.

gr521515: a paper that ingested with empty/garbage metadata keeps the junk
``cite_key`` minted then (``anon13a``, ``user*``, ``the*``) even after a
metadata repair gives it a real byline/year. This sweep lists every live
paper whose displayed key is junk-shaped
(:func:`precis.identity.is_junk_cite_key`) but whose stored authors+year
now mint a non-junk key via :meth:`Store.suggest_cite_key` — one row per
candidate: ref handle, old key → suggested key, title.

Junk-keyed papers that are NOT candidates are reported too, as skipped
with a reason: a byline with no person author at all (corporate/
collective only) and a record with no publication year — minting either
produces a bad key (a corporate token as surname, or the ``00``
placeholder digits the junk key already carries; gr521515 dry-run:
``group00``, ``neuralynx12``, ``boys00``). Those need metadata repair,
not a re-mint.

Dry-run by default: prints the list and writes nothing. ``--apply``
rekeys each row through :func:`precis.store.paper_rekey.rekey_paper` —
the displaced key stays resolvable as a ``cite_key_alias`` redirect, the
stored PDF moves to the new key's shard, and a ``cite_key_changed``
ref_event records the reason. A suggested key already claimed by a live
ref, by an alias redirect, or by an earlier row in this same run lands
on ``suggest_cite_key``'s letter disambiguation (the dry-run names the
exact keys the apply attempts), never a stolen key. A row that still
fails at apply time — e.g. a key claimed between plan and write — is
reported and skipped, never fatal to the rest of the sweep.
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import resolve_dsn
from precis.errors import BadInput, NotFound
from precis.identity import _first_person_surname, is_junk_cite_key

_CANDIDATES_SQL = """
    SELECT DISTINCT ON (r.ref_id)
           r.ref_id, ri.id_value, r.title, r.authors, r.year
      FROM refs r
      JOIN ref_identifiers ri
        ON ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
     WHERE r.kind = 'paper' AND r.retired_at IS NULL
     ORDER BY r.ref_id, ri.created_at DESC
"""


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``paper-remint-junk`` subparser on ``sub``."""
    p = sub.add_parser(
        "paper-remint-junk",
        help="Re-mint papers whose cite_key is a junk mint (anon*/user*/the*).",
        description=(
            "List live papers whose displayed cite_key is junk-shaped but "
            "whose metadata now yields a real key. Dry-run by default; "
            "pass --apply to rekey each."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the re-mints. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N candidate papers (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def _candidates(
    store,
) -> tuple[list[tuple[int, str, str, str]], list[tuple[int, str, str, str]]]:
    """Remint rows plus ``(ref_id, old_key, reason, title)`` skips.

    A row is skipped — reported, never minted — when its byline has no
    person author (corporate/collective only: the mint would hand a
    group token the surname slot) or its record has no year (the key
    would carry the ``00`` placeholder — the same digits the junk mint
    stamped). Both need metadata repair first, not a re-mint.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(_CANDIDATES_SQL).fetchall()
    remint: list[tuple[int, str, str, str]] = []
    skipped: list[tuple[int, str, str, str]] = []
    claimed: set[str] = set()  # keys this run already plans to take
    for ref_id, key, title, authors, year in rows:
        old = str(key or "")
        if not is_junk_cite_key(old):
            continue
        if not _first_person_surname(authors):
            skipped.append(
                (
                    int(ref_id),
                    old,
                    "no person author in byline (corporate/collective only)",
                    str(title or ""),
                )
            )
            continue
        if year is None:
            skipped.append(
                (
                    int(ref_id),
                    old,
                    "no publication year in the record",
                    str(title or ""),
                )
            )
            continue
        suggested = store.suggest_cite_key(
            authors, year, exclude_ref_id=int(ref_id), extra_taken=claimed
        )
        if not suggested or is_junk_cite_key(suggested) or suggested == old:
            continue
        claimed.add(suggested)
        remint.append((int(ref_id), old, suggested, str(title or "")))
    return remint, skipped


def run(args: argparse.Namespace) -> None:
    """Execute ``precis paper-remint-junk``."""
    from precis.config import load_config
    from precis.runtime import build_runtime
    from precis.store.paper_rekey import AUTO_REMINT_REASON, rekey_paper

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "paper-remint-junk: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    mode = "APPLY" if args.apply else "DRY-RUN"
    rows, no_mint = _candidates(store)
    if args.limit is not None:
        rows = rows[: args.limit]
    print(
        f"paper-remint-junk [{mode}]: {len(rows)} candidate(s), {len(no_mint)} skipped",
        file=sys.stderr,
    )
    rekeyed = 0
    skipped: list[str] = []
    for ref_id, old, new, title in rows:
        line = f"pa{ref_id}  {old} -> {new}  {title[:80]}"
        if args.apply:
            try:
                receipt = rekey_paper(
                    store,
                    ref_id,
                    new,
                    reason=AUTO_REMINT_REASON,
                    source="paper-remint-junk",
                )
            except (BadInput, NotFound) as exc:
                skipped.append(f"pa{ref_id} {old} -> {new}: {exc.cause}")
                line = f"{line}\n    skipped: {exc.cause}"
            else:
                rekeyed += 1
                line = f"{line}\n    {receipt}"
        print(line)
    if no_mint:
        print("skipped (needs metadata repair, not a re-mint):")
        for ref_id, old, reason, title in no_mint:
            print(f"pa{ref_id}  {old}  {reason}  {title[:80]}")
    if args.apply:
        print(
            f"paper-remint-junk [APPLY]: rekeyed {rekeyed}, skipped {len(skipped)}",
            file=sys.stderr,
        )
        for s in skipped:
            print(f"  skipped {s}", file=sys.stderr)
    elif rows:
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "run"]
