---
status: draft
title: "dr42995 taproot conversion — the remainder is 706 sourceless quantities, and that is a worker job, not more agent waves"
---

# What is actually left to convert

Companion to `dr42995-grounding-audit-results.md` (which audits what is *already*
grounded). This item covers the inverse: what remains ungrounded, and why the
obvious plan for it is wrong.

## The population, measured

Of dr42995's ~6,858 `paragraph`/`item` chunks:

| class | count | status |
|---|---|---|
| already carry `[fi` | 754 | done |
| uncited, name a source ("et al.") | 20 | **wave 3 leg A** |
| uncited, carry a bare quantity | 700 | the real remainder |
| authorial prose (design, plan-of-record, transitions) | ~5,400 | legitimately needs no citation |
| `chunk_kind='table'` | 251 | **unblocked 2026-08-27** — see §The table blocker |

Of the 20 attributed-uncited, only **12 are groundable claims**. Six are the
draft's own embedded `Review finding (…)` / `Open question [TQ-…]` annotations,
and two are authorial. See §The meta-annotation trap.

### The measurement itself was wrong once — do not repeat it

An earlier pass reported **675** attributed-uncited chunks. The regex
`\[@?[a-z]+[0-9]{4}` matched `[dc1516244]` — `dc` satisfies `[a-z]+`, `1516`
satisfies `[0-9]{4}` — so it was counting the draft **citing itself** via internal
chunk cross-references. The true figure is 20, a 34× overcount that made the
appendix leg look 92 chunks short when it was complete.

**Rule: never regex-match bracket citations without excluding
`[<2-letter-code><digits>]` handles.**

### And filter `retired_at IS NULL`

`chunks.retired_at` marks superseded chunks. They stay in the table but are
excluded from reading order and export, so **the MCP draft door never shows
them** — but raw SQL does. dr42995 carries **135 retired prose chunks**, 21 of
them in the quantitative set (hence 706 → 700).

This only bites when targets are handed to an agent as SQL-derived chunk ids,
which is exactly how wave 3 leg A was briefed: 2 of its 12 targets
(`dc1507445`, `dc1507894`) turned out to be retired, with live successors
(`dc2928113`, `dc1516370`) *already cited*. Work that looked undone was done.
Any batch pass driven off SQL must filter `retired_at IS NULL`; one driven off
the MCP reading order gets it for free.

## The remainder is far smaller than it looks — measured, not estimated

The 700 are the hard class: the number is stated bare, with **no source named**,
so grounding one means *finding* a source rather than matching a stated one.
That framing led to an early estimate of "100+ agent-waves, a batch job." **Wave
3 leg B measured it instead, and the estimate was wrong.**

Leg B swept all 500 chunks of ord 6500–6999 and triaged the 93 that carry a
quantity:

| class | count |
|---|---|
| authorial design spec | ~42 |
| derived / arithmetic | ~15 |
| already cited before the leg | 14 |
| empirical, searched, **no held source** | ~15 |
| **empirical, newly grounded** | **5** |
| off-domain / tool output | 2 |

So **~6% of uncited quantity-bearing chunks convert.** Roughly 60% never needed
a citation at all — they are the authors' own design parameters and their own
arithmetic. Extrapolated across the 700, the whole remaining draft is worth on
the order of **40 new hubs**, not thousands.

The cost driver is *reading*, not minting: a leg must read ~500 chunks to find
~5 groundable ones, and both legs run so far used well under their 10-hub cap.
That makes the remainder roughly **12–15 more sweep legs** — tractable as agent
waves after all. A worker lane would still be cheaper per chunk, but it is no
longer a prerequisite.

**The triage census is the real deliverable of each leg**, more than the mint
count. It is what converts "700 unconverted chunks" from an alarming backlog
into a known, mostly-benign population.

Leg C (ord 1000–1499) independently reproduced the shape: ~25–30 authorial,
~25–30 derived, ~8–10 off-domain, ~15 meta-annotations, 14 tables — 4 grounded.
Two legs, two buckets, the same distribution.

## A cite is not proof of grounding

Wave 3 leg C surfaced a defect that undermines every census above, filed as
gripe **265228**: deleting a claim hub leaves dangling `[fi<id>]` cites in draft
prose. Corpus-wide, live chunks only: **7 dangling cites, 3 documents, 6 dead
hubs** — three of them in dr42995, and two created as recently as 2026-08-27,
so this is ongoing rather than a historical scar.

The reason it matters *here*: a conversion pass treats a chunk that already
carries a cite as converted and skips it. So a deleted hub silently reverts
grounded prose to ungrounded **while still looking grounded**, and every census
in this document counts it as done. `dc1507432` is the worked example — it cites
dead `fi176919`, while its sibling `dc1507242` carries live `fi177523` for the
same σ = 40 S/cm figure.

Any future sweep should therefore validate existing cites, not just count them.
The detection query lives in the gripe.

Densest 500-ord buckets (tight number+unit regex, uncited only):

```
6500 → 77   1000 → 73   0 → 55   7000 → 51   1500 → 49
2000 → 47   7500 → 46   3000 → 43   500 → 42   6000 → 34
```

`section_path` is **empty** on this draft and heading `meta` carries no `level`,
so sections are addressable only by **ord range**, never by name. Any batch pass
must bucket by ord.

## The triage is the deliverable, not the mint count

A pass that grounds 8 claims honestly and reports 40 as unfindable is a success;
one that grounds 40 by stretching hubs is damage. Every chunk sorts into:

- **empirical** — a measured value about the world → ground it
- **authorial design spec** — a number the authors *chose* (tolerance, protocol
  setting, budget) → no citation, correctly
- **derived / theorem** — computed from other draft values, or a mathematical
  fact ("the cube is the only Platonic solid that tessellates 3D space")
- **off-domain** — economics, package counts, SMT throughput. This chemistry
  corpus can never ground these; report, don't chase.

Wave 2 established the precedent by correctly leaving the DNA sequence-design
filters, optical hardware choices, and the C1–C8 risk narrative uncited.

## The meta-annotation trap

dr42995 contains its own review annotations as ordinary body chunks:

> `Review finding (GAPS) [MINOR]: Microfluidic DNA circuit claim is uncited. Add
> reference (e.g. Karzbrun et al. 2014 or Kim et al. 2006).`

These are **addressed to the author** — a TODO list, not claims. They name
sources and lack `[fi`, so every retrieval heuristic and every conversion agent
scores them as prime targets. Grounding one produces a hub asserting that a gap
exists. Six sit in the 20-chunk attributed set alone (dc1507266, dc1509405,
dc1509410, dc1509962, dc1511412, dc1512300).

A batch pass **must** exclude chunks opening with `Review finding` or
`Open question [TQ-`. Their *content* is still valuable — it is a list of gaps
the author already knows about — but as input to acquisition, not to minting.

## The table blocker was a stale flag — fixed 2026-08-27

The 251 table chunks were written off as unreachable: `meta.flag =
'needs-table-review'` with no `meta.table` grid, so every structured edit door
refused while `get()` rendered the LaTeX from `text` perfectly. 407 such chunks
across 24 refs corpus-wide.

The divergence was one-sided by construction. The **read** path
(`table_data.py::table_payload`) already falls back to `parse_latex_table`; the
**write** path read `meta.table` and nothing else. And the flags are stale —
they were written by an older parser. Measured against today's parser on all
251: **244 recover (97%)**; the 7 that don't are float wrappers whose `tabular`
landed in a different chunk, plus two exotic column specs (`>{\scriptsize}`,
nested `\multicolumn` spanning a header).

The fix is the DRY one: `handlers/draft.py::_edit_table` now recovers through
`table_payload` — the *same* function the read path uses — and clears the stale
flag once a grid is persisted. No migration, no backfill, and it covers all 407
corpus-wide, not just this draft's 251.

**This is still not a licence to hand-reconstruct `table={header,rows}`.** The
recovery is a deterministic re-parse of the chunk's own stored text; typing a
grid by hand is 251 chances to mangle live append-only content. The distinction
is the whole point, and two agents were right to refuse the hand version.

Note the recovered cells stay **strings** — raw LaTeX carries no type
information, and coercing `"2"` to `2` would silently retype identifiers.

## Prerequisite for any batch lane

`wants=` / `provenance=` acquisition mode is unreachable over MCP — the verb
signature in `tools/core.py::put` *is* the schema and omits them, and
`workers/planner_prompt.py` actively teaches the broken call. The fix shipped in
`52b680d1` but **is not deployed**, so until a `/go` lands it, any worker pass
must file acquisition stubs via a plain `put(kind='paper')`.
