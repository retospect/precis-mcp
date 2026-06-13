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
  attempted >24h ago and still pending).
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
        "on its next pass).",
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
        rows = store.stub_backlog(limit=args.limit, awaiting=args.awaiting)
    finally:
        store.close()

    if not rows:
        print(
            "stubs: no stub paper refs found "
            "(every paper has either a pdf_sha256 or no external identifier)",
            file=sys.stderr,
        )
        return
    print(serialize(rows, format=resolve_format(args), schema=_SCHEMA))


__all__ = ["add_parser", "run"]
