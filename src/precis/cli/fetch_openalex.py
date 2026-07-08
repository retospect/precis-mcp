"""``precis fetch-openalex <doi|ref_id>`` — one-shot OpenAlex Content pull.

Manual rescue for an OA paper stuck behind a publisher anti-bot wall — MDPI's
Akamai, Wiley/science.org's Cloudflare — that every free fetch leg 403s on.
OpenAlex caches the full text and serves it from ``content.openalex.org``
(**not** the publisher), so this downloads the PDF straight into the watch
inbox, where ``precis watch`` ingests it like any other drop.

Paid (~$0.01/file); needs ``PRECIS_OPENALEX_CONTENT_KEY`` (free to obtain at
https://openalex.org/users, then fund a balance). This is the deliberate,
one-at-a-time path — the automatic cascade leg is opt-in
(``PRECIS_OPENALEX_CONTENT_AUTO``) so a big backlog can't silently spend.

Usage::

    precis fetch-openalex 10.3390/chemosensors11090486     # by DOI
    precis fetch-openalex 53423                            # by stub ref_id

By ref_id we also drop an acquisition sidecar so ingest folds the PDF into
*that* stub (keeping its title/DOI) instead of minting a duplicate.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.ingest.fetch_sidecar import write_sidecar
from precis.store import Store
from precis.workers.fetch_oa import (
    StubRef,
    _openalex_content_key,
    _stub_filename,
    _try_openalex_content,
)


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "fetch-openalex",
        help="One-shot OpenAlex Content download (paid) for one DOI / stub.",
        description=(
            "Pull a paper's full text from OpenAlex's content cache "
            "(content.openalex.org — bypasses the publisher's anti-bot wall) "
            "into the watch inbox. Paid ~$0.01/file; needs "
            "PRECIS_OPENALEX_CONTENT_KEY."
        ),
    )
    p.add_argument(
        "target",
        help="A DOI (10.xxxx/…) or a stub ref_id to fetch.",
    )
    p.add_argument(
        "--into",
        default=None,
        help="Inbox dir to download into (default PRECIS_WATCH_INBOX).",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="OpenAlex Content API key (default PRECIS_OPENALEX_CONTENT_KEY).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def _stub_for_ref(store: Store, ref_id: int) -> StubRef:
    """Load the identifiers of an existing stub ref into a StubRef."""
    sql = """
        SELECT
          (SELECT min(id_value) FROM ref_identifiers
            WHERE ref_id = %s AND id_kind = 'doi'),
          (SELECT min(id_value) FROM ref_identifiers
            WHERE ref_id = %s AND id_kind = 'arxiv'),
          (SELECT min(id_value) FROM ref_identifiers
            WHERE ref_id = %s AND id_kind = 's2'),
          (SELECT min(id_value) FROM ref_identifiers
            WHERE ref_id = %s AND id_kind = 'cite_key')
    """
    with store.pool.connection() as conn:
        row = conn.execute(sql, (ref_id, ref_id, ref_id, ref_id)).fetchone()
    if row is None:
        raise SystemExit(f"fetch-openalex: no ref {ref_id}")
    return StubRef(
        ref_id=ref_id, doi=row[0], arxiv=row[1], s2_id=row[2], cite_key=row[3]
    )


def run(args: argparse.Namespace) -> None:
    api_key = args.api_key or _openalex_content_key()
    if not api_key:
        raise SystemExit(
            "fetch-openalex: no API key — set PRECIS_OPENALEX_CONTENT_KEY or "
            "pass --api-key (get one free at https://openalex.org/users)."
        )
    inbox = args.into or os.environ.get("PRECIS_WATCH_INBOX", "").strip()
    if not inbox:
        raise SystemExit(
            "fetch-openalex: no inbox — pass --into or set PRECIS_WATCH_INBOX "
            "(the dir `precis watch` scans)."
        )
    inbox_dir = Path(inbox)

    target = args.target.strip()
    store: Store | None = None
    try:
        if target.startswith("10.") and "/" in target:
            stub = StubRef(ref_id=0, doi=target, arxiv=None, s2_id=None, cite_key=None)
        elif target.isdigit():
            store = Store.connect(resolve_dsn(args.database_url))
            stub = _stub_for_ref(store, int(target))
            if not stub.doi:
                raise SystemExit(
                    f"fetch-openalex: ref {target} has no DOI to resolve via OpenAlex."
                )
        else:
            raise SystemExit(
                f"fetch-openalex: '{target}' is neither a DOI (10.xxxx/…) nor "
                "a numeric ref_id."
            )

        outcome = _try_openalex_content(
            stub,
            inbox_dir=inbox_dir,
            api_key=api_key,
            email=os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip(),
        )
    finally:
        if store is not None:
            store.close()

    if outcome is None:  # pragma: no cover — guarded above
        raise SystemExit("fetch-openalex: nothing attempted (no DOI/key).")

    if outcome.event == "fetch_ok":
        # Fold into the stub in place (sidecar keyed on the download name), so
        # ingest keeps this ref's title/DOI instead of re-deriving identity.
        if stub.ref_id:
            write_sidecar(
                inbox_dir / (_stub_filename(stub) + ".pdf"),
                ref_id=stub.ref_id,
                identifiers={
                    "doi": stub.doi or "",
                    "arxiv": stub.arxiv or "",
                    "s2": stub.s2_id or "",
                    "cite_key": stub.cite_key or "",
                },
                source="fetcher:openalex_content",
            )
        size = outcome.payload.get("size_bytes")
        print(
            f"fetch-openalex: OK — {size} bytes → {outcome.payload.get('filename')} "
            f"(${outcome.cost_usd:.2f}); `precis watch` will ingest it.",
            file=sys.stderr,
        )
        return

    detail = outcome.payload.get("error") or outcome.payload.get("cached") or ""
    print(f"fetch-openalex: {outcome.event} — {detail}", file=sys.stderr)
    raise SystemExit(1)


__all__ = ["add_parser", "run"]
