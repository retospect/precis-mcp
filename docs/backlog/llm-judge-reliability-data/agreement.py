"""Agreement analysis for the grounding-verifier reliability slice.

Reads roster.psv + replicate_*.jsonl (same directory) and prints a
markdown report: test-retest agreement across the three opus replicates
(Fleiss' kappa, per-label one-vs-rest kappa, pairwise Cohen's kappa) and
cross-rung agreement of the sonnet replicate against the opus modal
verdict. Stdlib only; run with `uv run agreement.py`.
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).parent
OPUS = ["replicate_a_opus", "replicate_b_opus", "replicate_c_opus"]
SONNET = "replicate_d_sonnet"

# Downstream, dispositions matter only through the repair lane they fire.
ACTION = {
    "NONE": "benign",
    "SCOPE_DRIFT": "benign",
    "CLAIM_DEFECT": "claim-edit",
    "WRONG_CHUNK": "edge-repair",
    "FRONT_MATTER_ANCHOR": "edge-repair",
    "WRONG_SOURCE": "edge-repair",
    "NEEDS_SECOND_EDGE": "edge-repair",
    "UNDECIDED": "undecided",
}


def load(name: str) -> dict[int, dict]:
    rows = {}
    path = HERE / f"{name}.jsonl"
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[int(row["link_id"])] = row
    return rows


def fleiss_kappa(ratings: list[list[str]]) -> float:
    """ratings: one inner list of rater labels per item (equal length)."""
    cats = sorted({c for item in ratings for c in item})
    n = len(ratings[0])
    counts = [Counter(item) for item in ratings]
    p_i = [(sum(c[k] ** 2 for k in cats) - n) / (n * (n - 1)) for c in counts]
    p_bar = sum(p_i) / len(ratings)
    p_j = [sum(c[k] for c in counts) / (len(ratings) * n) for k in cats]
    p_e = sum(p**2 for p in p_j)
    if p_e == 1.0:
        return 1.0
    return (p_bar - p_e) / (1 - p_e)


def cohen_kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def per_label_kappa(ratings: list[list[str]]) -> dict[str, tuple[float, int]]:
    """One-vs-rest Fleiss kappa per label + how many items any rater gave it."""
    out = {}
    for lab in sorted({c for item in ratings for c in item}):
        binary = [[("Y" if c == lab else "N") for c in item] for item in ratings]
        used = sum(1 for item in ratings if lab in item)
        out[lab] = (fleiss_kappa(binary), used)
    return out


def modal(item: list[str]) -> str:
    c = Counter(item)
    top, top_n = c.most_common(1)[0]
    if top_n == 1:  # 3-way split: no mode
        return "SPLIT"
    return top


def main() -> None:
    roster = []
    with (HERE / "roster.psv").open(encoding="utf-8") as fh:
        header = fh.readline().strip().split("|")
        for line in fh:
            roster.append(dict(zip(header, line.strip().split("|"))))
    link_ids = [int(r["link_id"]) for r in roster]

    reps = {name: load(name) for name in [*OPUS, SONNET]}
    for name, rows in reps.items():
        missing = [l for l in link_ids if l not in rows]
        if missing:
            print(f"⚠ {name}: missing {len(missing)} rows: {missing}")

    common = [l for l in link_ids if all(l in reps[n] for n in reps)]
    print(
        f"# Agreement report — {len(common)}/{len(link_ids)} edges scored by all 4 replicates\n"
    )

    for field in ("disposition", "passage_verdict"):
        opus_ratings = [[reps[n][l][field] for n in OPUS] for l in common]
        print(f"## {field} — test-retest (3× opus)\n")
        unanimous = sum(1 for item in opus_ratings if len(set(item)) == 1)
        splits = sum(1 for item in opus_ratings if len(set(item)) == 3)
        print(f"- unanimous: {unanimous}/{len(common)}; 3-way splits: {splits}")
        print(f"- Fleiss' kappa: **{fleiss_kappa(opus_ratings):.2f}**")
        for x, y in combinations(range(3), 2):
            k = cohen_kappa([r[x] for r in opus_ratings], [r[y] for r in opus_ratings])
            print(f"- Cohen's kappa {OPUS[x][-6]}×{OPUS[y][-6]}: {k:.2f}")
        print("\n| label | one-vs-rest kappa | items with label |")
        print("|---|---|---|")
        for lab, (k, used) in per_label_kappa(opus_ratings).items():
            print(f"| {lab} | {k:.2f} | {used} |")
        print()

        sonnet = [reps[SONNET][l][field] for l in common]
        modes = [modal(item) for item in opus_ratings]
        decided = [(m, s) for m, s in zip(modes, sonnet) if m != "SPLIT"]
        k = cohen_kappa([m for m, _ in decided], [s for _, s in decided])
        agree = sum(m == s for m, s in decided)
        print(f"## {field} — cross-rung (sonnet vs opus modal)\n")
        print(
            f"- raw agreement: {agree}/{len(decided)} (excl. {len(common) - len(decided)} opus 3-way splits)"
        )
        print(f"- Cohen's kappa: **{k:.2f}**")
        dis = Counter((m, s) for m, s in decided if m != s)
        for (m, s), n in dis.most_common():
            print(f"  - opus={m} → sonnet={s}: {n}")
        print()

    # Collapsed to the repair lane actually fired.
    def act(row: dict) -> str:
        return ACTION.get(row["disposition"], "other")

    opus_ratings = [[act(reps[n][l]) for n in OPUS] for l in common]
    print(
        "## repair lane (benign / claim-edit / edge-repair / undecided) — the decision that matters\n"
    )
    unanimous = sum(1 for item in opus_ratings if len(set(item)) == 1)
    print(f"- test-retest unanimous: {unanimous}/{len(common)}")
    print(f"- test-retest Fleiss' kappa: **{fleiss_kappa(opus_ratings):.2f}**")
    sonnet = [act(reps[SONNET][l]) for l in common]
    modes = [modal(item) for item in opus_ratings]
    decided = [(m, s) for m, s in zip(modes, sonnet) if m != "SPLIT"]
    k = cohen_kappa([m for m, _ in decided], [s for _, s in decided])
    print(f"- cross-rung Cohen's kappa (sonnet vs opus modal): **{k:.2f}**")
    print("\n| stratum | edges | opus unanimous |")
    print("|---|---|---|")
    for stratum in ("exemplar", "random"):
        ids = [
            int(r["link_id"])
            for r in roster
            if r["stratum"] == stratum and int(r["link_id"]) in common
        ]
        una = sum(1 for l in ids if len({act(reps[n][l]) for n in OPUS}) == 1)
        print(f"| {stratum} | {len(ids)} | {una} |")


if __name__ == "__main__":
    main()
