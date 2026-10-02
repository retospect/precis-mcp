"""``precis taxonomy-bootstrap`` — generate a campaign's measurand list.

Runs the procedure in `docs/backlog/taxonomy-bootstrap.md` over the snapshot a
campaign config pins. The list is an output, not a table someone maintains, so
this verb is the whole interface: same campaign, same salt, same snapshot, same
list.

``--stage census`` stops before the model pass, which is the only stage that
costs anything. ``--freeze`` is a separate flag because writing
``list.vN.yaml`` is an act with consequences — every binding document
afterwards cites that version.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from precis.taxonomy import discovery
from precis.taxonomy import run as pipeline
from precis.taxonomy.config import CampaignConfig, load_campaign


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``taxonomy-bootstrap`` subparser on ``sub``."""
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
    """Execute ``precis taxonomy-bootstrap``."""
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
