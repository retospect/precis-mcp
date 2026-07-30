"""``precis taproot mint`` — mint claim hubs from a cite-seeded JSON spec.

    precis taproot mint --spec spec.json
    precis taproot mint --json '[{"sentence": "...", "scope": {}, \
"supporters": [{"paper": "pa5", "role": "corroborates"}]}]'
    precis taproot mint --spec spec.json --dry-run
    precis taproot mint --spec spec.json --format json

A thin CLI skin over :func:`precis.taproot.authoring.seed_claim_hub`, itself
a helper over the existing taproot write door (:mod:`precis.taproot.hub`) —
for a human/backfill to mint a claim hub from a draft's already-existing
paper-chunk citations, not a second write path.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from precis.cli._common import resolve_dsn


def add_parser(subparsers: Any) -> None:
    """Register the ``taproot`` subcommand group (currently just ``mint``)."""
    p = subparsers.add_parser(
        "taproot", help="Taproot claim-hub authoring (mint hubs from citations)."
    )
    tsub = p.add_subparsers(dest="taproot_cmd", required=True)

    m = tsub.add_parser(
        "mint",
        help="Mint/attach claim hubs from a JSON spec of cited claims.",
    )
    spec_group = m.add_mutually_exclusive_group(required=True)
    spec_group.add_argument("--spec", help="Path to a JSON spec file.")
    spec_group.add_argument(
        "--json", dest="json_spec", help="Inline JSON spec (same shape as --spec)."
    )
    m.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve papers + print what WOULD be minted/attached; write nothing.",
    )
    m.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    m.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent).",
    )
    m.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")


def _load_spec(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.loads(args.json_spec)
    if not isinstance(raw, list):
        print("taproot mint: spec must be a JSON array of claims", file=sys.stderr)
        sys.exit(2)
    return raw


def _dry_run_result(store: Any, entry: dict[str, Any]) -> dict[str, Any]:
    from precis.identity import make_pub_id, make_taproot_hub_paper_id
    from precis.taproot.authoring import (
        _evidence_edge_exists,
        find_hub_by_pub_id,
        resolve_paper_ref_id,
    )
    from precis.taproot.hub import _DEFAULT_ROLE

    sentence = entry.get("sentence", "")
    scope = entry.get("scope") or {}
    supporters = entry.get("supporters", [])

    pub_id = make_pub_id(make_taproot_hub_paper_id(sentence, scope))
    hub_ref_id = find_hub_by_pub_id(store, pub_id)

    attached = 0
    already = 0
    for supporter in supporters:
        # Resolution only -- raises BadInput on an unresolvable (or
        # non-paper/patent) paper, same as the real (write) path would, so
        # a dry-run catches a bad spec before anything is minted.
        paper_ref_id = resolve_paper_ref_id(store, supporter.get("paper"))
        role = supporter.get("role") or _DEFAULT_ROLE
        # A brand-new hub has no existing evidence at all -- every
        # supporter would be a fresh attach. For a pre-existing hub, check
        # the actual (paper, hub, role) edge -- reporting every supporter
        # as "already" just because the hub pre-exists was inaccurate for
        # a genuinely new supporter on an old hub.
        if hub_ref_id is not None and _evidence_edge_exists(
            store, paper_ref_id=paper_ref_id, hub_ref_id=hub_ref_id, role=role
        ):
            already += 1
        else:
            attached += 1

    return {
        "pub_id": pub_id,
        "hub_ref_id": hub_ref_id,
        "attached": attached,
        "already": already,
        "sentence": sentence,
        "dry_run": True,
    }


def _preflight_resolve_supporters(store: Any, claims: list[dict[str, Any]]) -> None:
    """Resolve every supporter's ``paper`` across every claim, read-only.

    Runs BEFORE any write so a bad spec anywhere in the batch (an
    unresolvable handle, or one that resolves to a non-paper/patent ref —
    :func:`~precis.taproot.authoring.resolve_paper_ref_id` now rejects
    those) fails closed: nothing gets partially minted to prod for the
    claims that precede the bad one.

    Raises:
        BadInput: same as :func:`~precis.taproot.authoring.resolve_paper_ref_id`
            (or a missing ``'paper'`` field, mirroring
            :func:`~precis.taproot.authoring.seed_claim_hub`'s own check).
    """
    from precis.errors import BadInput
    from precis.taproot.authoring import resolve_paper_ref_id

    for entry in claims:
        for supporter in entry.get("supporters", []):
            paper = supporter.get("paper")
            if paper is None:
                raise BadInput("supporter missing required 'paper' field")
            resolve_paper_ref_id(store, paper)


def _print_results(results: list[dict[str, Any]], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(results, indent=2))
        return

    for r in results:
        snippet = (r.get("sentence") or "").strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        suffix = "  [DRY-RUN]" if r.get("dry_run") else ""
        collapsed = r.get("collapsed") or []
        collapsed_note = f", {len(collapsed)} collapsed" if collapsed else ""
        print(
            f"{r['pub_id']}  {snippet}  "
            f"(+{r['attached']} evidence, {r['already']} already"
            f"{collapsed_note}){suffix}"
        )


def _run_mint(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import seed_claim_hub

    claims = _load_spec(args)
    store = Store.connect(resolve_dsn(args.database_url))
    results: list[dict[str, Any]] = []

    try:
        if not args.dry_run:
            # Pre-flight: read-only resolution of every supporter, before
            # any claim's hub gets minted -- a bad claim N never leaves
            # claims 0..N-1 written while hiding that partial write.
            _preflight_resolve_supporters(store, claims)

        for entry in claims:
            sentence = entry.get("sentence", "")
            if args.dry_run:
                results.append(_dry_run_result(store, entry))
                continue
            scope = entry.get("scope") or {}
            supporters = entry.get("supporters", [])
            out = seed_claim_hub(
                store,
                sentence=sentence,
                scope=scope,
                supporters=supporters,
                set_by=args.set_by,
            )
            out["sentence"] = sentence
            results.append(out)
    except BadInput as exc:
        print(f"taproot mint: error: {exc.cause}", file=sys.stderr)
        if results:
            # A write can still fail mid-batch for some other reason after
            # pre-flight passed (e.g. a concurrent delete) -- never hide a
            # partial run; show what already committed before exiting.
            print(
                "taproot mint: already-committed before the failure:",
                file=sys.stderr,
            )
            _print_results(results, args.format)
        sys.exit(1)
    finally:
        store.close()

    _print_results(results, args.format)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot <taproot_cmd>``."""
    if args.taproot_cmd == "mint":
        _run_mint(args)
    else:
        print(f"taproot: unknown subcommand {args.taproot_cmd!r}", file=sys.stderr)
        sys.exit(2)


__all__ = ["add_parser", "run"]
