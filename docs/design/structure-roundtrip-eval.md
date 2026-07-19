# Structure round-trip eval — a co-improvement harness

**Status:** v1 (invariant tier). **Cadence:** run ~monthly as models and the
structure tool improve. **Isolation:** DB-free — the whole loop runs in memory
via `apply_ops`; never touches Postgres or prod.

## What it measures — and why round-trip

We build atomic structures (the `structure` kind, ADR 0043) and we want two
things to work well, *together*, and to keep improving:

1. **extract-description** — turn a structure we made into faithful prose (for
   cards, search, briefs). *structure → text.*
2. **build-from-text** — an agent builds a structure from a literature hint.
   *text → structure.*

So the eval is a **round trip**, cycle-consistency style:

```
S  ──describe(model)──▶  D  ──build(model)──▶  S'
(ground truth)          (prose)             (rebuilt)
                                               │
                        invariants match? ─────▶ score ∈ [0,1]
```

The source structure **S is its own answer key** — no hand-authored gold set, no
LLM judge. This tracks both the **model** (comprehension / tool use) and the
**tool** (are the ops + skill doc expressive enough) on one axis, so a monthly
run shows whether a new model, or a sharper skill doc, moved the number.

## The rule that makes comparison tractable

**Never compare coordinates or paths — compare the canonical form of the
result.** An atom reachable "down 2, over 3" in several equivalent ways is *one*
structure; a prose round trip rebuilds at canonical positions, not the source's
exact floats. So a coordinate/RMSD comparison would wrongly fail correct
answers. We compare a **fingerprint of representation-invariant properties**
(`src/precis/structure/invariants.py`), all derived from geometry via the
existing `probe.py` neighbor tools:

- **composition** (element multiset)
- **per-layer composition**, bottom→top — captures a dopant's *layer*
- **n_fixed** — the constraint preserved
- **adsorbate site classes** (top / bridge / hollow, by surface coordination)
- **coordination-number histogram** — a permutation/translation-invariant graph
  shape (catches gross topology differences composition can't)
- **min interatomic distance** — a validity floor (atomic overlap caps the score)

This is the **cheap tier**: two equivalent representations *always* fingerprint
identically, so it has **no false-negatives** from relabeling/reordering — which
is exactly the multiplicity problem. Its weakness is false-*positives* (two
different structures, same invariants); we close that by adding invariants or
graduating tiers.

## The anti-cheat

The description **must be natural-language prose — no coordinates, no JSON, no
code**. Otherwise a model "describes" by dumping the ops payload and "builds" by
copying it back (trivially cycle-consistent, learned nothing). Prose forces a
genuine compress→reconstruct — which is the real product skill.

## Scoring

`compare(fp(S), fp(S'))` → a weighted match fraction → `bucket_to_ordinal`
(1..5), the same mapping the other eval axes use. Weights (v1):
composition .30 · layers .25 · adsorbate-sites .20 · n_fixed .15 ·
coordination .10; a sub-floor `min_dist` in S' caps the score at 0.5 (an
overlapping rebuild is invalid regardless of the rest). We also record **cost +
turns** per round trip — "speed" is a first-class axis alongside fidelity.

## Configs

- **same-model round trip** (default) — one composed number per model: the tier
  ranking signal.
- **fixed-describer split** — pin a strong describer, vary only the builder, to
  isolate *build-from-text* when a model scores low and you want to know which
  half failed.

## Source of S — "databases of models"

1. **Parametric generator** (v1, fully isolated) — emit valid fcc(111) slabs via
   the same `slab` op path, with random element / size / a top-layer dopant.
   Infinite supply, known-good by construction, zero external dependency.
2. **Our corpus** — real stored `structure` scenes (realistic distribution).
3. **Public DBs** — Materials Project / COD / PubChem (realistic, adds a fetch).

Start at (1); (2)/(3) widen the distribution later.

## The eventual upgrade (seam, not built)

Exact structural identity — pymatgen **`StructureMatcher`** (+ spglib
standardized cell) for periodic slabs, RDKit **canonical SMILES / InChI** for
molecules — matches up to lattice / translation / rotation / permutation within
tolerance. Battle-tested library work, not research; the one real cost is
tolerance calibration on slabs-with-vacuum. The `Fingerprint` type is the seam:
swap the comparator, keep the loop. Orthogonal to this is the **catpath barrier
oracle** ("is the built surface a *good catalyst*", not "the same structure") —
a separate, expensive confirmation, deliberately **not** folded in here.

## Known limitations (v1)

- **Single-trial variance.** Models are stochastic, so one trip per (model,
  structure) is noisy — a run saw deepseek at 0.17 / 0.83 / 0.33 across three
  passes (mostly intermittent malformed builds). The metric and tool are stable;
  the *sampling* is not. For a trustworthy monthly number, run **K trials per
  (model, structure)** and average (K≈3–5). v1 reports a single pass; read the
  faults column, not the third decimal.
- **False-positives**, per the fingerprint tier — two different structures with
  the same invariants score as a match. Close with more invariants or the
  `StructureMatcher` upgrade.

## What a run finds (co-improvement, not just a score)

The harness surfaces *why* a trip fails, which feeds tool + doc improvements —
already: the `slab` op crashed on `fix_layers:null` (fixed → clean OpError) and
deepseek reads `fix_layers` as a list of indices, not a count (fixed → a
self-correcting error + a sharper skill-doc line). A build fault keeps the
offending payload so a monthly run is debuggable without a re-run.

## Layout

| Piece | Path |
|---|---|
| Comparator (invariant fingerprint + `compare`) | `src/precis/structure/invariants.py` |
| Harness (generator → describe→build → score) | `scripts/llm_eval/roundtrip.py` |
| Month-over-month trend log | `scripts/llm_eval/ROUNDTRIP_RESULTS.md` |
| Shared dispatch (budget-safe retry, JSON parse) | `scripts/llm_eval/_dispatch.py` |
