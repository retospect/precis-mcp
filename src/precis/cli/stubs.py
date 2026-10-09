"""``precis stubs`` — list paper refs needing PDFs (with last fetch attempt).

The chase worker creates stub paper refs (DOI / arXiv / S2 id
registered; ``pdf_sha256 IS NULL``) when a finding's chain reaches
a paper the corpus doesn't have. This command surfaces the
backlog: each row names the cite_key, the most useful identifier,
the last attempt the fetcher worker made (if any), and a one-line
status.

Read flow: queries ``refs WHERE pdf_sha256 IS NULL AND kind='paper'``
joined with the **latest** ``ref_events`` row per ref where
``source LIKE 'fetcher:%'``. TOON-table by default; rich table on
a TTY; JSON for downstream tooling.

Typical use:

* ``precis stubs`` — full backlog, newest stubs first.
* ``precis stubs --limit 100``
* ``precis stubs --awaiting`` — only stubs never attempted (or
  attempted >24h ago and still pending); excludes the cooled no-OA
  bucket.
* ``precis stubs --manual`` — the manual-retrieval list: the cooled
  no-OA bucket (several passes, every source said no open-access copy)
  as DOI, title, year and cite count, newest-requested first, so a
  human can go buy them (gr453859). ``--no-oa`` is the same flag.
* ``precis stubs --format json`` — for piping into a workflow.

Sibling commands:

* ``precis worker --only fetch`` — drive the fetcher cascade
  against the same backlog.
* ``scripts/doilist`` — the operator-facing DOI-list fetcher (file-
  driven, pre-existing; complementary).
"""

from __future__ import annotations

import argparse
import sys

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.format import serialize
from precis.store import Store

# Column order pinned here so TOON / JSON / table all see the same
# shape. Adding a column lands in one place.
_SCHEMA: list[str] = [
    "ref_id",
    "cite_key",
    "identifier",
    "last_attempt",
    "last_source",
    "last_event",
    "state",
]

#: ``--manual``'s shape: what a human needs to buy or retrieve a paper
#: by hand (``Store.manual_retrieval_list``).
_MANUAL_SCHEMA: list[str] = [
    "doi",
    "title",
    "year",
    "cites",
    "requested",
    "passes",
    "cite_key",
    "ref_id",
]


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stubs",
        help="List paper refs needing PDFs (with last fetcher attempt).",
        description=(
            "Surface the stub backlog: paper refs whose PDF hasn't "
            "landed yet, with the latest fetcher attempt summarised. "
            "Drive a fetch pass via ``precis worker --only fetch``."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to return (default 50).",
    )
    p.add_argument(
        "--awaiting",
        action="store_true",
        help="Show only stubs never attempted or attempted >24h ago "
        "and still pending (i.e. the queue the fetcher would target "
        "on its next pass). Excludes the cooled no-OA bucket.",
    )
    p.add_argument(
        "--manual",
        "--no-oa",
        dest="manual",
        action="store_true",
        help="The manual-retrieval list: stubs the fetcher has cooled on "
        "(several passes, every source said no open-access copy), as "
        "DOI, title, year and cite count, newest-requested first. "
        "This is the list to retrieve by hand or buy.",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    add_format_argument(p)


def run(args: argparse.Namespace) -> None:
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        if args.manual:
            rows = store.manual_retrieval_list(limit=args.limit)
            total = store.stub_backlog_count(no_oa=True)
        else:
            rows = store.stub_backlog(limit=args.limit, awaiting=args.awaiting)
    finally:
        store.close()

    if not rows and args.manual:
        print("stubs: no stub has exhausted its no-OA passes", file=sys.stderr)
        return
    if not rows:
        print(
            "stubs: no stub paper refs found "
            "(every paper has either a pdf_sha256 or no external identifier)",
            file=sys.stderr,
        )
        return
    if args.manual:
        print(
            f"stubs: {total} cooled no-OA stub{'s' if total != 1 else ''} "
            f"need manual retrieval (showing {len(rows)})",
            file=sys.stderr,
        )
        print(serialize(rows, format=resolve_format(args), schema=_MANUAL_SCHEMA))
        return
    print(serialize(rows, format=resolve_format(args), schema=_SCHEMA))


__all__ = ["add_parser", "run"]
