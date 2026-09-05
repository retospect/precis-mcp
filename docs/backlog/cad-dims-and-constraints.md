---
status: draft
title: cad dims + constraints — named dimensions, one-sided bounds, refuse-the-impossible
prio: high
model: opus
---

# cad: named dimensions + declarative constraints

Reto, 2026-09-05: "at this level there are already impossible
considerations (ie if a is 20cm and b is 15cm, they can not be the same
length). Also, we can have one sided restrictions (longer than 10cm is a
valid open ended thing)."

## The shape

```
dim a = 200            # mm, exact
dim b = 150
dim c >= 100           # one-sided, open-ended — valid and useful
dim c <= 500           # bounds accumulate (intersection)
constrain a = b        # ✖ refused at put — impossible by declaration
```

Internal model: **one interval per equality class**. Union-find merges
dims constrained equal; each class holds the intersection of every bound
asserted on its members (`= v` is the degenerate `[v, v]`; `>= v` is
`[v, ∞)`). Empty intersection = contradiction, **refused at parse, before
any geometry exists** — the same loud posture as a bad mate (decision:
refuse, not advisory — Reto's framing is that impossibility should be
caught, and the kernel never carries known-false declarations).

## Slices

1. **v1 (kernel, no migration) — SHIP FIRST**: `dim`/`constrain` lines,
   interval accumulation, class merge, contradiction refusal with the
   members and their bounds named; round-trip via meta
   (`dims` = `{name: [lo|null, hi|null]}`, `constraints` = pairs);
   rendered in the tree's declarations block. Dims are declarative
   metadata in v1 — configs do not reference them yet.
2. **v2 — parametrized configs**: `box:w{a}d{b}h10` resolves dims into
   geometry (needs exact values or a chosen representative in-range).
   This is real parametrics; own slice, own decisions.
3. **Cross-dim inequalities** (`constrain a <= b`) are a difference-
   constraint graph (Bellman-Ford over the inequality graph), not
   union-find — deferred until a consumer needs them.

## Consumers (why this is foundational)

- **Print-in-place clearances** (`cad-print-in-place.md`): process rules
  are one-sided bounds ("FDM pin clearance >= 0.3") — this engine checks
  those.
- **`estimate` kind / margin-budget-tree**: intervals that narrow as a
  design firms up are the same object.
- **se `demands_relation` / tier-2 `view='fits'`**: tolerance relations
  between named measures — same interval algebra, shared vocabulary.
- **Port types with dimensional payload** (eventual): `type:shaft-d5`
  as a checked `d = 5` rather than a string match.

Units: mm throughout (kernel convention) — 20 cm is authored as 200.
