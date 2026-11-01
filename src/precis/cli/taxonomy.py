"""``precis taxonomy-bootstrap`` / ``precis taxonomy`` — campaign list + glossary mirror.

``taxonomy-bootstrap`` runs the procedure in
`docs/backlog/taxonomy-bootstrap.md` over the snapshot a campaign config
pins. The list is an output, not a table someone maintains, so this verb is
the whole interface: same campaign, same salt, same snapshot, same list.

``--stage census`` stops before the model pass, which is the only stage that
costs anything. ``--freeze`` is a separate flag because writing
``list.vN.yaml`` is an act with consequences — every binding document
afterwards cites that version.

``precis taxonomy import-glossary`` mirrors `docs/glossary.md` into
``repo-dev-glossary`` taxon + alias nodes (gr548266): idempotent, matching
``file_mirror.namespace`` + term; ``--sync`` soft-deletes nodes whose term
left the file; ``--dry-run`` counts without writing; ``--force`` bypasses
the blob-sha fast-path (`precis.taxonomy.glossary`). ``precis taxonomy
list --namespace <ns>`` prints the mirrored terms — the comparison set
``scripts/memory-lint`` uses for its coined-term check.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from precis.cli._common import connect_router_store, resolve_dsn
from precis.taxonomy import discovery
from precis.taxonomy import run as pipeline
from precis.taxonomy.config import CampaignConfig, load_campaign


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``taxonomy-bootstrap`` and ``taxonomy`` subparsers."""
    tax = sub.add_parser(
        "taxonomy",
        help="Term-taxonomy maintenance: mirror docs/glossary.md into nodes.",
    )
    tax_sub = tax.add_subparsers(dest="taxonomy_cmd", required=True)
    imp = tax_sub.add_parser(
        "import-glossary",
        help="Mirror a glossary file into repo-dev taxon + alias nodes.",
    )
    imp.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Glossary file (default: docs/glossary.md in this checkout).",
    )
    imp.add_argument(
        "--sync",
        action="store_true",
        help="Soft-delete namespace nodes whose term left the file.",
    )
    imp.add_argument(
        "--dry-run", action="store_true", help="Count changes; write nothing."
    )
    imp.add_argument(
        "--sha",
        default=None,
        help="Git blob sha to gate on (default: computed from the file). "
        "When it equals the sha stored after the last successful pass the "
        "import is a no-op.",
    )
    imp.add_argument(
        "--force",
        action="store_true",
        help="Ignore the stored blob sha and re-run the whole file.",
    )
    imp.add_argument(
        "--namespace",
        default="repo-dev-glossary",
        help="meta.file_mirror.namespace for mirrored nodes.",
    )
    imp.add_argument("--database-url", default=None, help="Postgres DSN override.")
    lst = tax_sub.add_parser(
        "list",
        help="Print the mirrored terms of a namespace, one per line.",
    )
    lst.add_argument(
        "--namespace",
        default="repo-dev-glossary",
        help="meta.file_mirror.namespace to list (default: repo-dev-glossary).",
    )
    lst.add_argument("--database-url", default=None, help="Postgres DSN override.")

    parser = sub.add_parser(
        "taxonomy-bootstrap",
        help="Generate a campaign's measurand list from corpus usage.",
    )
    parser.add_argument(
        "--campaign",
        default="norr-her-meta",
        help="Shipped campaign name (precis/data/taxonomy/campaigns/) or a "
        "path to a campaign YAML.",
    )
    parser.add_argument(
        "--stage",
        choices=["census", "all"],
        default="census",
        help="census: stage 1 only, deterministic and free (default). "
        "all: stages 1-4, which spends model calls on discovery.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Directory for stage dumps and the frozen list. Default: a "
        "'taxonomy' directory beside the snapshot file.",
    )
    parser.add_argument(
        "--salt",
        default=None,
        help="A/B split salt. Default: derived from the campaign name and "
        "snapshot sha, so the split is reproducible without remembering a "
        "value. Change it only to re-roll the split deliberately.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Scan only the first N snapshot rows (smoke runs).",
    )
    parser.add_argument(
        "--join-sides",
        default=None,
        help="Two comma-separated row values that must each clear the "
        "join threshold, e.g. 'mode:expt-electrochemical,mode:dft'.",
    )
    parser.add_argument(
        "--side-field",
        default="mode",
        help="Snapshot field the join sides are read from (default: mode).",
    )
    parser.add_argument(
        "--pack",
        type=int,
        default=1,
        help="Hubs per discovery call (default 1, the single-hub prompt the "
        "probes measured). Each claude -p call carries ~21k tokens of "
        "harness overhead against ~1k of prompt, so 4 cuts cost and wall "
        "per hub ~3.5x; the call timeout scales with it.",
    )
    parser.add_argument(
        "--call-timeout",
        type=float,
        default=None,
        help="Per-call wall clock in seconds. Default: the transport's own "
        "at --pack 1, 120 s per hub above that.",
    )
    parser.add_argument(
        "--placement",
        choices=("local", "cloud"),
        default=None,
        help="Pin discovery to local or cloud model rungs. 'local' fails a "
        "call instead of falling back to the cloud. Default: the tier's "
        "own chain.",
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="Write the next list.vN.yaml. Refuses if A/B stability is below "
        "the signed threshold, and never overwrites an existing version.",
    )
    return parser


def probe_verdict(result: pipeline.RunResult, config: CampaignConfig) -> str:
    """The n≈100 pass line: stability against the unit-key ceiling.

    ``Thresholds.min_probe_ratio`` is the stated criterion; this prints it
    next to the number so a probe's summary carries its own verdict rather
    than a reader comparing against a bar they have to look up.
    """
    bar = config.thresholds.min_probe_ratio
    verdict = "PASS" if result.stability_ratio >= bar else "FAIL"
    return (
        f"probe criterion {verdict}: stability {result.stability:.3f} is "
        f"{result.stability_ratio:.2f} of the unit-key ceiling "
        f"{result.unit_ceiling:.3f} (signed minimum {bar:g}); the full run "
        f"is judged by min_stability {config.thresholds.min_stability:g}"
    )


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taxonomy`` / ``precis taxonomy-bootstrap``."""
    if args.cmd == "taxonomy":
        _run_taxonomy(args)
        return
    config = load_campaign(args.campaign)
    out = (
        Path(args.out).expanduser()
        if args.out
        else config.snapshot_path.parent / "taxonomy"
    )

    if args.stage == "census":
        _rows, mentions, digest = pipeline.run_census(config, limit=args.limit)
        kinds: dict[str, int] = {}
        with_unit = 0
        for mention in mentions:
            kinds[mention.kind] = kinds.get(mention.kind, 0) + 1
            if mention.raw_unit:
                with_unit += 1
        print(f"campaign        {config.campaign}")
        print(
            f"snapshot        {config.snapshot.row_count} rows, "
            f"sha {config.snapshot.sha256[:12]}, "
            f"pulled {config.snapshot.pulled_at}"
        )
        print(f"mentions        {len(mentions)}  digest {digest[:16]}")
        for kind in sorted(kinds):
            print(f"  {kind:<22}{kinds[kind]}")
        print(f"  with a unit           {with_unit}")
        return

    salt = args.salt or f"{config.campaign}:{config.snapshot.sha256[:16]}"
    join_sides: tuple[str, str] | None = None
    if args.join_sides:
        parts = tuple(p.strip() for p in args.join_sides.split(",") if p.strip())
        if len(parts) != 2:
            raise SystemExit("--join-sides needs exactly two comma-separated values")
        join_sides = (parts[0], parts[1])

    # Stream every call record to disk as it lands: a run killed at hour
    # nine of eleven keeps its metering. write_stage_outputs rewrites the
    # same file from the result afterwards (identical content, one place).
    out.mkdir(parents=True, exist_ok=True)
    responses_path = out / "responses.jsonl"
    responses_path.write_text("", encoding="utf-8")

    def append_response(record: discovery.CallRecord) -> None:
        with responses_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record.to_json(), sort_keys=True, ensure_ascii=False)
            )
            handle.write("\n")

    if args.pack < 1:
        raise SystemExit("--pack must be at least 1")
    timeout_s = (
        args.call_timeout
        if args.call_timeout is not None
        else discovery.call_timeout_s(args.pack)
    )
    # No database work of its own, but the router reads live routing
    # overrides through the bound store.
    connect_router_store(None)
    result = pipeline.run_pipeline(
        config,
        discovery.router_client(timeout_s=timeout_s, placement=args.placement),
        salt=salt,
        limit=args.limit,
        join_sides=join_sides,
        side_field=args.side_field,
        on_call=append_response,
        pack=args.pack,
    )
    paths = pipeline.write_stage_outputs(result, out)
    print(result.summary())
    print(probe_verdict(result, config))
    print(f"stage dumps     {paths[0].parent}")
    if args.freeze:
        written = pipeline.freeze_run(result, config, out)
        print(f"frozen list     {written}")
    else:
        print("frozen list     — not written (pass --freeze)")


def _run_taxonomy(args: argparse.Namespace) -> None:
    """Execute ``precis taxonomy import-glossary`` / ``precis taxonomy list``."""
    from precis.config import load_config
    from precis.store import Store
    from precis.taxonomy import glossary

    if args.taxonomy_cmd == "list":
        store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
        try:
            for term in glossary.list_mirrored_terms(store, args.namespace):
                print(term)
        finally:
            store.close()
        return

    path = (
        Path(args.file)
        if args.file
        else Path(__file__).resolve().parents[3] / "docs" / "glossary.md"
    )
    if not path.is_file():
        raise SystemExit(f"precis taxonomy import-glossary: no file at {path}")
    store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
    try:
        report = glossary.import_glossary(
            store,
            path,
            sync=args.sync,
            dry_run=args.dry_run,
            blob_sha=args.sha or glossary.glossary_blob_sha(path),
            force=args.force,
            namespace=args.namespace,
        )
    finally:
        store.close()
    print(report.summary())
