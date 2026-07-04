"""eval-classifier — run the LLM classifier over a gold set and report
per-axis accuracy + confusion. Read-only (never writes the DB).

Grades TWO ways (ADR 0047 / gold_set/README):
  strict        prediction == the gold primary value
  accept-aware  prediction == primary OR in the axis's accept list

Handles both gold families:
  chunk axes (role, open-question)  -> gold_set_chunks.yaml
  ref   axes (domain, scale, ...)   -> gold_set.yaml

Usage:
  eval-classifier --axis role
  eval-classifier --axis role,open-question --limit 40
  eval-classifier --gold papers --axis domain,studytype

The LLM call is `_classify_one` (see `_llm.py`); pass --model to pick
the backend (default: the cheap local model used for classification).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Env-overridable so the eval can run on a cluster node against copied
# axis defs / gold sets (default: the in-repo layout).
AXES_DIR = Path(
    os.environ.get(
        "PRECIS_AXES_DIR", HERE.parent.parent / "src" / "precis" / "data" / "axes"
    )
)
GOLD_DIR = Path(os.environ.get("PRECIS_GOLD_DIR", HERE / "gold_set"))

# ---- axis definitions -------------------------------------------------


def load_axis(axis_id: str) -> dict:
    p = AXES_DIR / f"{axis_id}.yaml"
    if not p.exists():
        sys.exit(f"error: no axis definition at {p}")
    return yaml.safe_load(p.open())


# ---- gold sets --------------------------------------------------------


def load_gold(family: str) -> tuple[str, list[dict]]:
    """Return (item_kind, records). family in {'chunks','papers'}."""
    if family == "chunks":
        d = yaml.safe_load((GOLD_DIR / "gold_set_chunks.yaml").open())
        return "chunk", d["chunks"]
    if family == "papers":
        d = yaml.safe_load((GOLD_DIR / "gold_set.yaml").open())
        return "paper", d["papers"]
    sys.exit(f"error: unknown gold family {family!r} (want chunks|papers)")


def gold_value(rec: dict, axis_id: str) -> tuple[str | None, list[str]]:
    """(primary, accept[]) for this axis on this record, or (None, [])."""
    labels = rec.get("labels", {})
    if axis_id not in labels:
        return None, []
    primary = str(labels[axis_id])
    accept: list[str] = []
    if axis_id == "role":
        accept = [str(x) for x in labels.get("role_accept", [])]
    elif axis_id == "open-question":
        accept = [str(x) for x in labels.get("oq_accept", [])]
    else:  # ref axes: per-axis map under `accept`
        accept = [str(x) for x in labels.get("accept", {}).get(axis_id, [])]
    return primary, accept


# ---- prompt building --------------------------------------------------


def render_context(rec: dict, axis: dict) -> str:
    """Build the context block the axis declares it wants (chunk axes).

    Ref axes get title/journal/abstract; chunk axes get the packet named
    in the axis `context:` field (section_path/position/title/ref_tags/
    neighbor_gists_1).
    """
    lines: list[str] = []
    if axis.get("level") == "chunk":
        want = set(axis.get("context", []))
        if "title" in want and rec.get("title"):
            lines.append(f"Paper title: {rec['title']}")
        if "section_path" in want and rec.get("section_path"):
            lines.append(f"Section: {rec['section_path']}")
        if "position" in want and rec.get("position"):
            lines.append(f"Position in document: {rec['position']}")
        if "ref_tags" in want and rec.get("ref_tags"):
            lines.append(f"Paper tags: {', '.join(rec['ref_tags'])}")
        if "neighbor_gists_1" in want:
            if rec.get("prev_gist"):
                lines.append(f"Previous chunk (gist): {rec['prev_gist']}")
            if rec.get("next_gist"):
                lines.append(f"Next chunk (gist): {rec['next_gist']}")
        lines.append("")
        lines.append(f"CHUNK TEXT:\n{rec.get('text', '')}")
    else:  # ref/paper axis
        if rec.get("title"):
            lines.append(f"Title: {rec['title']}")
        if rec.get("journal"):
            lines.append(f"Journal: {rec['journal']}")
        if rec.get("year"):
            lines.append(f"Year: {rec['year']}")
        lines.append("")
        lines.append(f"ABSTRACT / LEADING TEXT:\n{rec.get('abstract', '')}")
    return "\n".join(lines)


def render_examples(axis: dict) -> str:
    """Optional few-shot block from the axis `examples:` list.

    Each example is {text, value, why?}. These are synthetic, illustrative
    snippets (NOT drawn from the gold set) — they teach the hard boundaries
    without leaking eval labels.
    """
    ex = axis.get("examples") or []
    if not ex:
        return ""
    lines = ["Worked examples (learn the boundaries):"]
    for e in ex:
        why = f"   # {e['why']}" if e.get("why") else ""
        lines.append(f'- "{e["text"]}" -> {{"value": "{e["value"]}"}}{why}')
    return "\n".join(lines) + "\n"


def build_prompt(rec: dict, axis: dict) -> str:
    ex = render_examples(axis)
    ex_block = f"\n{ex}\n" if ex else "\n"
    return f"{axis['prompt'].rstrip()}\n{ex_block}---\n{render_context(rec, axis)}\n"


# ---- scoring ----------------------------------------------------------


def score_axis(axis_id: str, results: list[dict]) -> dict:
    """results: [{primary, accept[], pred, ok}]. Returns metrics."""
    n = len(results)
    strict = sum(1 for r in results if r["pred"] == r["primary"])
    accept = sum(1 for r in results if r["pred"] in ([r["primary"]] + r["accept"]))
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        confusion[r["primary"]][r["pred"]] += 1
    errored = sum(1 for r in results if not r["ok"])
    return {
        "axis": axis_id,
        "n": n,
        "strict": strict,
        "accept": accept,
        "strict_pct": strict / n if n else 0.0,
        "accept_pct": accept / n if n else 0.0,
        "errored": errored,
        "confusion": confusion,
    }


def print_report(m: dict) -> None:
    print(f"\n=== axis: {m['axis']}  (n={m['n']}, llm-errors={m['errored']}) ===")
    print(f"  strict accuracy:       {m['strict']}/{m['n']} = {m['strict_pct']:.0%}")
    print(f"  accept-aware accuracy: {m['accept']}/{m['n']} = {m['accept_pct']:.0%}")
    gate = "PASS" if m["accept_pct"] >= 0.85 else "BELOW 85%"
    print(f"  gate (>=85% accept-aware): {gate}")
    print("  confusion (gold-primary -> predicted):")
    for gold_v in sorted(m["confusion"]):
        row = m["confusion"][gold_v]
        cells = ", ".join(f"{k}:{v}" for k, v in row.most_common())
        print(f"    {gold_v:16s} -> {cells}")


# ---- main -------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--axis", required=True, help="comma-separated axis id(s)")
    p.add_argument(
        "--gold",
        default="chunks",
        choices=["chunks", "papers"],
        help="which gold set (default: chunks)",
    )
    p.add_argument("--limit", type=int, default=0, help="cap items (0=all)")
    p.add_argument("--model", default=None, help="LLM backend (default: local)")
    p.add_argument("--concurrency", type=int, default=1, help="parallel LLM calls")
    args = p.parse_args()

    from _llm import classify_batch  # deferred: needs precis env

    _item_kind, records = load_gold(args.gold)
    if args.limit:
        records = records[: args.limit]

    all_metrics = []
    for axis_id in args.axis.split(","):
        axis = load_axis(axis_id)
        # only records that have a gold label for this axis
        items = []
        for rec in records:
            primary, accept = gold_value(rec, axis_id)
            if primary is None:
                continue
            items.append((rec, primary, accept))
        if not items:
            print(f"(no gold labels for axis {axis_id} in {args.gold})")
            continue
        prompts = [build_prompt(rec, axis) for rec, _, _ in items]
        print(
            f"classifying {len(prompts)} items for axis '{axis_id}' "
            f"(model={args.model or 'local-default'})...",
            file=sys.stderr,
        )
        preds = classify_batch(prompts, model=args.model, concurrency=args.concurrency)
        results = []
        for (rec, primary, accept), pred in zip(items, preds):
            results.append(
                {
                    "primary": primary,
                    "accept": accept,
                    "pred": (pred.get("value") if pred else None),
                    "ok": pred is not None and pred.get("value") is not None,
                }
            )
        m = score_axis(axis_id, results)
        print_report(m)
        all_metrics.append(m)

    # overall
    if all_metrics:
        tot = sum(m["n"] for m in all_metrics)
        sa = sum(m["accept"] for m in all_metrics)
        print(f"\n=== overall accept-aware: {sa}/{tot} = {sa / tot:.0%} ===")


if __name__ == "__main__":
    main()
