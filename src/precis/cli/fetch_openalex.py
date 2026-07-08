"""``precis fetch-openalex`` — OpenAlex Content pull (single or backlog sweep).

Rescue for OA papers stuck behind a publisher anti-bot wall — MDPI's Akamai,
Wiley/science.org's Cloudflare — that every free fetch leg 403s on. OpenAlex
caches the full text and serves it from ``content.openalex.org`` (**not** the
publisher), so this downloads the PDF straight into the watch inbox, where
``precis watch`` ingests it like any other drop.

Paid (~$0.01/file); needs ``PRECIS_OPENALEX_CONTENT_KEY`` (free to obtain at
https://openalex.org/users, then fund a balance). This is the deliberate
operator path — the automatic cascade leg is separately opt-in
(``PRECIS_OPENALEX_CONTENT_AUTO``) so the routine worker can't silently spend.

Usage::

    precis fetch-openalex 10.3390/chemosensors11090486     # one DOI
    precis fetch-openalex 53423                            # one stub ref_id
    precis fetch-openalex --backfill --limit 2000 --max-usd 25

**Backfill** is the "try the whole backlog once, last resort" sweep: it walks
paper stubs that have a DOI, **already failed every free leg** (a prior
``fetcher:%`` event, no ``fetch_ok``), and haven't been OpenAlex-tried yet, and
runs the paid content leg on each. It only *spends* when OpenAlex actually has
a cached PDF (≈16% of the backlog in practice; the rest cost nothing but the
free metadata call). Bounded by ``--limit`` and a hard ``--max-usd`` cap, and
resumable — each attempt writes a ``fetcher:openalex_content`` event so a
re-run skips what it already tried. Run it on the host that owns the watch
inbox (melchior).
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
    FetchOutcome,
    StubRef,
    _openalex_content_key,
    _stub_filename,
    _try_openalex_content,
)

# Default hard spend ceiling for a backfill sweep — a backstop, not a target.
_DEFAULT_MAX_USD = 25.0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "fetch-openalex",
        help="OpenAlex Content download (paid): one DOI/stub, or --backfill.",
        description=(
            "Pull full text from OpenAlex's content cache (content.openalex.org "
            "— bypasses the publisher's anti-bot wall) into the watch inbox. "
            "Paid ~$0.01/file; needs PRECIS_OPENALEX_CONTENT_KEY."
        ),
    )
    p.add_argument(
        "target",
        nargs="?",
        help="A DOI (10.xxxx/…) or a stub ref_id. Omit with --backfill.",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help="Sweep the free-exhausted stub backlog (DOI + a prior failed free "
        "fetch, not yet OpenAlex-tried). Last-resort bulk fetch.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="Max stubs to consider in --backfill mode (default 2000).",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=_DEFAULT_MAX_USD,
        help=f"Hard spend cap for a --backfill sweep (default ${_DEFAULT_MAX_USD:.0f}).",
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


_STUB_IDS = """
    (SELECT min(id_value) FROM ref_identifiers
      WHERE ref_id = %(rid)s AND id_kind = 'doi'),
    (SELECT min(id_value) FROM ref_identifiers
      WHERE ref_id = %(rid)s AND id_kind = 'arxiv'),
    (SELECT min(id_value) FROM ref_identifiers
      WHERE ref_id = %(rid)s AND id_kind = 's2'),
    (SELECT min(id_value) FROM ref_identifiers
      WHERE ref_id = %(rid)s AND id_kind = 'cite_key')
"""


def _stub_for_ref(store: Store, ref_id: int) -> StubRef:
    with store.pool.connection() as conn:
        row = conn.execute(f"SELECT {_STUB_IDS}", {"rid": ref_id}).fetchone()
    if row is None:
        raise SystemExit(f"fetch-openalex: no ref {ref_id}")
    return StubRef(
        ref_id=ref_id, doi=row[0], arxiv=row[1], s2_id=row[2], cite_key=row[3]
    )


def _backfill_batch(store: Store, *, limit: int) -> list[StubRef]:
    """Free-exhausted paper stubs not yet tried via OpenAlex, newest first.

    "Free-exhausted" = has a DOI, has at least one prior ``fetcher:%`` event
    (so the free cascade ran) and no ``fetch_ok`` (none of it worked), and no
    ``fetcher:openalex_content`` event yet (so a re-run resumes, never re-pays).
    """
    sql = """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi')      AS doi,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'arxiv')    AS arxiv,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 's2')       AS s2,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.pdf_sha256 IS NULL
           AND r.deleted_at IS NULL
           AND EXISTS (SELECT 1 FROM ref_identifiers ri
                        WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi')
           AND EXISTS (SELECT 1 FROM ref_events e
                        WHERE e.ref_id = r.ref_id AND e.source LIKE 'fetcher:%%')
           AND NOT EXISTS (SELECT 1 FROM ref_events e
                            WHERE e.ref_id = r.ref_id AND e.event = 'fetch_ok')
           AND NOT EXISTS (SELECT 1 FROM ref_events e
                            WHERE e.ref_id = r.ref_id
                              AND e.source = 'fetcher:openalex_content')
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [
        StubRef(ref_id=int(r[0]), doi=r[1], arxiv=r[2], s2_id=r[3], cite_key=r[4])
        for r in rows
    ]


def _process(
    store: Store | None,
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
    email: str,
    record_event: bool,
) -> FetchOutcome | None:
    """Run the OpenAlex content leg on one stub; sidecar + event on success."""
    outcome = _try_openalex_content(
        stub, inbox_dir=inbox_dir, api_key=api_key, email=email
    )
    if outcome is None:
        return None
    if outcome.event == "fetch_ok" and stub.ref_id:
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
    # Record the attempt so the sweep is resumable + the spend is audited.
    if record_event and store is not None and stub.ref_id:
        store.append_event(
            stub.ref_id,
            source="fetcher:openalex_content",
            event=outcome.event,
            payload=outcome.payload,
            duration_ms=outcome.duration_ms,
            cost_usd=outcome.cost_usd,
        )
    return outcome


def _run_backfill(
    store: Store,
    *,
    inbox_dir: Path,
    api_key: str,
    email: str,
    limit: int,
    max_usd: float,
) -> None:
    stubs = _backfill_batch(store, limit=limit)
    if not stubs:
        print(
            "fetch-openalex: backlog is empty (nothing free-exhausted).",
            file=sys.stderr,
        )
        return
    print(
        f"fetch-openalex: sweeping {len(stubs)} free-exhausted stubs "
        f"(cap ${max_usd:.2f})…",
        file=sys.stderr,
    )
    spent = 0.0
    fetched = no_content = errors = 0
    for stub in stubs:
        if spent >= max_usd:
            print(
                f"fetch-openalex: hit ${max_usd:.2f} cap — stopping.", file=sys.stderr
            )
            break
        try:
            outcome = _process(
                store,
                stub,
                inbox_dir=inbox_dir,
                api_key=api_key,
                email=email,
                record_event=True,
            )
        except Exception as exc:
            errors += 1
            print(f"fetch-openalex: ref {stub.ref_id} error: {exc}", file=sys.stderr)
            continue
        if outcome is None:
            continue
        if outcome.event == "fetch_ok":
            fetched += 1
            spent += outcome.cost_usd or 0.0
            print(
                f"fetch-openalex: ✓ ref {stub.ref_id} {stub.doi} "
                f"({outcome.payload.get('size_bytes')} bytes)",
                file=sys.stderr,
            )
        elif outcome.event == "no_oa_version":
            no_content += 1
        else:
            errors += 1
    print(
        f"fetch-openalex: done — {fetched} fetched (~${spent:.2f}), "
        f"{no_content} no-content, {errors} errors, of {len(stubs)} tried. "
        f"`precis watch` ingests the {fetched} PDFs.",
        file=sys.stderr,
    )


def _run_single(
    store: Store | None,
    target: str,
    *,
    inbox_dir: Path,
    api_key: str,
    email: str,
) -> None:
    if target.startswith("10.") and "/" in target:
        stub = StubRef(ref_id=0, doi=target, arxiv=None, s2_id=None, cite_key=None)
    elif target.isdigit():
        assert store is not None
        stub = _stub_for_ref(store, int(target))
        if not stub.doi:
            raise SystemExit(
                f"fetch-openalex: ref {target} has no DOI to resolve via OpenAlex."
            )
    else:
        raise SystemExit(
            f"fetch-openalex: '{target}' is neither a DOI (10.xxxx/…) nor a "
            "numeric ref_id."
        )

    outcome = _process(
        store,
        stub,
        inbox_dir=inbox_dir,
        api_key=api_key,
        email=email,
        record_event=bool(stub.ref_id),
    )
    if outcome is None:
        raise SystemExit("fetch-openalex: nothing attempted (no DOI/key).")
    if outcome.event == "fetch_ok":
        print(
            f"fetch-openalex: OK — {outcome.payload.get('size_bytes')} bytes → "
            f"{outcome.payload.get('filename')} (${outcome.cost_usd:.2f}); "
            "`precis watch` will ingest it.",
            file=sys.stderr,
        )
        return
    detail = outcome.payload.get("error") or outcome.payload.get("cached") or ""
    print(f"fetch-openalex: {outcome.event} — {detail}", file=sys.stderr)
    raise SystemExit(1)


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
    email = os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()

    if not args.backfill and not args.target:
        raise SystemExit("fetch-openalex: pass a DOI / ref_id, or use --backfill.")

    # Backfill and a numeric single target both need the DB; a bare DOI doesn't.
    need_db = args.backfill or (args.target and args.target.strip().isdigit())
    store = Store.connect(resolve_dsn(args.database_url)) if need_db else None
    try:
        if args.backfill:
            assert store is not None
            _run_backfill(
                store,
                inbox_dir=inbox_dir,
                api_key=api_key,
                email=email,
                limit=args.limit,
                max_usd=args.max_usd,
            )
        else:
            _run_single(
                store,
                args.target.strip(),
                inbox_dir=inbox_dir,
                api_key=api_key,
                email=email,
            )
    finally:
        if store is not None:
            store.close()


__all__ = ["add_parser", "run"]
