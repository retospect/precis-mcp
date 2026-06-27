"""``precis add`` — ingest a paper into the v2 schema.

Three input modes (mutually exclusive):

* ``precis add FILE.pdf`` — local PDF; full Marker extraction.
* ``precis add --doi 10.x/y`` — metadata-only via CrossRef.
* ``precis add --arxiv 2401.12345`` — metadata-only via Semantic
  Scholar's ``arxiv:`` lookup.

Stdout (success): one line ``<cite_key>\\t<ref_id>\\t<status>``
where ``status`` is ``inserted`` or ``existed``. Designed to be
parseable for ``precis watch`` (B5).

Exit codes:
* 0 — paper ingested or already known.
* 2 — usage error (e.g. file missing, no DSN).
* 3 — pipeline error (lookup miss, marker failure).
* 4 — skipped: another host holds the advisory-lock claim
  on this PDF's content. See ADR 0016.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.ingest.add import (
    ArxivInput,
    DoiInput,
    PdfInput,
    PrecisAddInput,
    precis_add,
)
from precis.store import Store

log = logging.getLogger(__name__)


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis add`` subcommand on ``sub``."""
    p = sub.add_parser(
        "add",
        help="Ingest a paper (PDF, DOI, or arXiv ID) into the v2 schema.",
        description=(
            "Ingest a paper into the v2 schema. Idempotent: a re-run "
            "against any known identifier short-circuits with the "
            "existing ref_id."
        ),
    )
    p.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Path to a PDF file. Mutually exclusive with --doi / --arxiv.",
    )
    p.add_argument(
        "--doi",
        help="DOI to fetch from CrossRef (no PDF stored).",
    )
    p.add_argument(
        "--arxiv",
        help="arXiv ID to fetch from Semantic Scholar (no PDF stored).",
    )
    p.add_argument(
        "--as",
        dest="as_kind",
        choices=("paper", "cfp"),
        default="paper",
        help=(
            "Stored kind for a PDF ingest (default: paper). Use "
            "--as cfp to land a call-for-proposal / requirements "
            "document as a non-citable spec (same extraction pipeline, "
            "own reader namespace). Ignored for --doi / --arxiv."
        ),
    )
    p.add_argument(
        "--use-pdf2doi",
        action="store_true",
        help="Enable the pdf2doi fallback in the DOI extraction cascade.",
    )
    p.add_argument(
        "--crossref-mailto",
        default="",
        help="Email address to identify with CrossRef (polite-pool benefits).",
    )
    p.add_argument(
        "--s2-api-key",
        default="",
        help="Semantic Scholar API key (env: SEMANTIC_SCHOLAR_API_KEY).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def run(args: argparse.Namespace) -> None:
    """Top-level handler for ``precis add``."""
    input_obj = _resolve_input(args)
    dsn = resolve_dsn(args.database_url)

    store = Store.connect(dsn)
    try:
        result = precis_add(
            input_obj,
            store=store,
            use_pdf2doi=args.use_pdf2doi,
            crossref_mailto=args.crossref_mailto,
            s2_api_key=args.s2_api_key,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"add: {exc}", file=sys.stderr)
        sys.exit(3)
    finally:
        store.close()

    if result is None:
        # Another host holds the Postgres advisory-lock claim on this
        # PDF's pdf_sha256 (multi-host ingest). Surface it loudly —
        # for a one-shot operator-driven ingest, this is almost
        # certainly unintended.
        print(
            "add: skipped - another host holds the claim for this content",
            file=sys.stderr,
        )
        sys.exit(4)

    status = "inserted" if result.inserted else "existed"
    # Tab-separated for easy parsing by precis watch (B5).
    print(f"{result.cite_key}\t{result.ref_id}\t{status}")


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _resolve_input(args: argparse.Namespace) -> PrecisAddInput:
    """Pick exactly one of input / --doi / --arxiv. Exit with code 2
    on any combination that's ambiguous or empty."""
    sources = [bool(args.input), bool(args.doi), bool(args.arxiv)]
    if sum(sources) != 1:
        print(
            "add: provide exactly one of FILE.pdf, --doi, or --arxiv",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.input:
        if not args.input.is_file():
            print(f"add: PDF not found: {args.input}", file=sys.stderr)
            sys.exit(2)
        return PdfInput(
            pdf_path=args.input.resolve(),
            as_kind=getattr(args, "as_kind", "paper"),
        )
    if args.doi:
        return DoiInput(doi=args.doi)
    if args.arxiv:
        return ArxivInput(arxiv_id=args.arxiv)
    # unreachable; the count-check above guarantees one of the three
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["add_parser", "run"]
