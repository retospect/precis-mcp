"""``precis resolve-metadata`` — PDF-free re-resolution of the triage backlog.

Bucket B of the duplicate/metadata plan
(``docs/design/duplicate-paper-handling.md``): resolve canonical metadata
for ``needs-triage`` papers from what we already hold — Crossref by the
stored DOI, or a Semantic Scholar title search that recovers a DOI for the
id-less ones. Auto-applies only the high-confidence verdicts; the review
and discard lanes are printed for a human to action.

Dry-run by default; ``--apply`` writes the ``auto`` verdicts. Network-bound
(Crossref / S2), so run it on a node with outbound access.
"""

from __future__ import annotations

import argparse
import os
import sys

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``resolve-metadata`` subparser on ``sub``."""
    p = sub.add_parser(
        "resolve-metadata",
        help="Re-resolve needs-triage paper metadata (Crossref DOI / S2 title).",
        description=(
            "Resolve canonical metadata for needs-triage papers from the "
            "stored DOI (Crossref) or a Semantic Scholar title search "
            "(recovers a DOI for id-less papers). Auto-applies only "
            "high-confidence hits; prints the review + discard lanes. "
            "Dry-run by default; --apply to write."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write the auto verdicts. Without this the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N triage papers (default: all).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-lookup wall-clock cap in seconds (default: 20).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Politeness pause between network lookups, seconds (default: 0.5).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis resolve-metadata``."""
    from precis.config import load_config
    from precis.ingest.metadata_resolve import resolve_triage
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "resolve-metadata: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    apply = args.apply
    mode = "APPLY" if apply else "DRY-RUN"
    mailto = os.environ.get("ACATOME_CROSSREF_MAILTO", "")
    s2_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    print(f"resolve-metadata [{mode}]: limit={args.limit}", file=sys.stderr)

    if not s2_key:
        print(
            "resolve-metadata: SEMANTIC_SCHOLAR_API_KEY unset — S2 title search "
            "will be heavily rate-limited (slow). Set it for the title track.",
            file=sys.stderr,
        )
    results = resolve_triage(
        store,
        apply=apply,
        limit=args.limit,
        mailto=mailto,
        s2_api_key=s2_key,
        call_timeout=args.timeout,
        delay=args.delay,
    )

    # Group by verdict; print the actionable lanes (review / discard) in full.
    buckets: dict[str, list] = {"auto": [], "review": [], "discard": [], "miss": []}
    for r in results:
        buckets.setdefault(r.verdict, []).append(r)
    for lane in ("auto", "review", "discard"):
        for r in buckets.get(lane, []):
            print(r.line())

    verb = "applied" if apply else "would apply"
    print(
        f"\nresolve-metadata [{mode}] done over {len(results)} paper(s): "
        f"{verb} {len(buckets['auto'])} auto, "
        f"{len(buckets['review'])} for review, "
        f"{len(buckets['discard'])} discard-candidates, "
        f"{len(buckets['miss'])} unresolved.",
        file=sys.stderr,
    )
    if not apply and buckets["auto"]:
        print("Re-run with --apply to write the auto verdicts.", file=sys.stderr)


__all__ = ["add_parser", "run"]
