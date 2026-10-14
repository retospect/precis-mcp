"""Build the ``categorise`` lane's frozen eval set from a discovery run dump.

Gold = the frontier run's folded measurand keys on the same hubs
(``local-mesh-upkeep.md`` §1 row 1): each item replays the exact
:func:`precis.taxonomy.discovery.build_prompt` for one hub sentence, and
its gold lists, per mention index, the key the frontier's answer folded
to. A candidate is scored by folded-key agreement — the same statistic
``compare_runs.py`` reports, so "agreement with the frontier" and the
frontier's own test-retest (the auto-apply bar's leg) are commensurable.

No DB: inputs are the snapshot jsonl (hub sentences), a run dump's
``mentions.jsonl`` and ``discovered.jsonl``, and the campaign config (for
the prompt and the alias fold). Mentions the frontier declined
(``skip_reason``, no discovered row) carry no gold — they stay in the
prompt but out of the denominator.

    uv run python scripts/llm_eval/build_mesh_categorise_gold.py \
        --run-dir …/taxonomy-packed300-v2 --snapshot …/hubs.v2.jsonl \
        --n 120 --holdout 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUT = "scripts/llm_eval/gold_set/local/mesh_categorise_v1.jsonl"
DEFAULT_HOLDOUT = "scripts/llm_eval/gold_set/local/mesh_categorise_holdout_v1.jsonl"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def build_items(
    run_dir: Path,
    snapshot_path: Path,
    campaign: str,
) -> tuple[list[Any], dict[str, Any]]:
    """Assemble per-hub items; returns ``(items, ctx)`` for the set header."""
    from precis.mesh_eval.sets import MeshItem
    from precis.taxonomy.config import load_campaign
    from precis.taxonomy.discovery import build_prompt
    from precis.taxonomy.normalise import alias_key
    from precis.taxonomy.types import Anchor, Mention

    config = load_campaign(campaign)
    text_field = config.snapshot.text_field
    ref_field = config.snapshot.ref_field
    aliases = config.measurand_aliases

    sentences: dict[int, str] = {}
    for row in _load_jsonl(snapshot_path):
        ref_id = int(row[ref_field])
        sentences[ref_id] = str(row.get(text_field) or "")

    def _mention(raw: dict[str, Any]) -> Mention:
        a = raw["anchor"]
        return Mention(
            anchor=Anchor(
                source_ref_id=int(a["ref_id"]),
                start=int(a["start"]),
                end=int(a["end"]),
                source_kind=a.get("source_kind", "ref_text"),
                scheme=int(a.get("scheme", 1)),
            ),
            kind=raw["kind"],
            literal=raw["literal"],
            raw_unit=raw.get("raw_unit"),
            context=raw.get("context", ""),
            marker=raw.get("marker"),
        )

    # mentions.jsonl is census order; keep only the kinds discovery saw
    # (run.py's mentions_by_ref defaults to "value").
    mentions_by_ref: dict[int, list[tuple[Mention, dict[str, Any]]]] = {}
    for raw in _load_jsonl(run_dir / "mentions.jsonl"):
        if raw.get("kind") != "value":
            continue
        m = _mention(raw)
        mentions_by_ref.setdefault(m.anchor.source_ref_id, []).append((m, raw))

    # anchor -> frontier measurand
    gold_by_anchor: dict[tuple[int, int, int], str] = {}
    halves: dict[int, str] = {}
    for raw in _load_jsonl(run_dir / "discovered.jsonl"):
        a = raw["mention"]["anchor"]
        gold_by_anchor[(a["ref_id"], a["start"], a["end"])] = raw["measurand"]
        halves[a["ref_id"]] = raw.get("half") or ""

    items: list[MeshItem] = []
    for ref_id, pairs in sorted(mentions_by_ref.items()):
        sentence = sentences.get(ref_id)
        if not sentence:
            continue
        mentions = [m for m, _raw in pairs]
        gold_mentions = []
        for i, m in enumerate(mentions):
            key = (ref_id, m.anchor.start, m.anchor.end)
            measurand = gold_by_anchor.get(key)
            if measurand is None:
                continue
            gold_mentions.append(
                {
                    "index": i,
                    "anchor": m.anchor.render(),
                    "gold_key": alias_key(measurand, aliases),
                    "gold_measurand": measurand,
                }
            )
        if not gold_mentions:
            continue
        items.append(
            MeshItem(
                item_id=f"cat-{ref_id}",
                lane="categorise",
                prompt=build_prompt(sentence, mentions, config),
                gold={"mentions": gold_mentions},
                meta={
                    "ref_id": ref_id,
                    "sentence": sentence,
                    "half": halves.get(ref_id),
                    "mentions": [raw for _m, raw in pairs],
                },
            )
        )
    ctx = {
        "campaign": campaign,
        "aliases": {
            "species": {k: list(v) for k, v in aliases.species.items()},
            "phrases": {k: list(v) for k, v in aliases.phrases.items()},
        },
    }
    return items, ctx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Discovery run dump dir (mentions.jsonl + discovered.jsonl).",
    )
    ap.add_argument(
        "--snapshot",
        required=True,
        type=Path,
        help="Snapshot jsonl the run was built on (hub sentences).",
    )
    ap.add_argument("--campaign", default="norr-her-meta")
    ap.add_argument("--n", type=int, default=120, help="Graded items.")
    ap.add_argument(
        "--holdout", type=int, default=20, help="Reserved human-holdout items."
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--frontier-retest",
        type=float,
        default=None,
        help="Frontier test-retest reference to record (from "
        "`precis mesh retest` on the two frontier runs).",
    )
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--holdout-out", default=DEFAULT_HOLDOUT)
    args = ap.parse_args(argv)

    items, ctx = build_items(args.run_dir, args.snapshot, args.campaign)

    import random

    rng = random.Random(args.seed)
    rng.shuffle(items)
    from precis.mesh_eval.sets import dump_set, split_holdout

    graded, holdout = split_holdout(
        items[: args.n + args.holdout], args.holdout, seed=args.seed
    )

    snap_sha = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()[:16]
    built_at = datetime.now(UTC).isoformat()
    meta = {
        "lane": "categorise",
        "version": "categorise-v1",
        "built_at": built_at,
        "source": f"{args.run_dir} over {args.snapshot.name} (sha {snap_sha})",
        "seed": args.seed,
        "scorer_ctx": ctx,
        "reference": (
            {"frontier_retest": args.frontier_retest}
            if args.frontier_retest is not None
            else {}
        ),
    }
    out = dump_set(graded, meta, args.out)
    print(f"wrote {len(graded)} graded items -> {out}", file=sys.stderr)
    if holdout:
        hout = dump_set(
            holdout, {**meta, "holdout_of": str(args.out)}, args.holdout_out
        )
        print(f"wrote {len(holdout)} holdout items -> {hout}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
