---
id: precis-argument-help
title: precis — build a defensible argument as a reusable lemma/inference graph
summary: the reasoning shadow beside a draft — state lemmas, chain inferences, keep it out of the published prose
answers:
  - how do I state a lemma or chain an inference without it leaking into the published draft?
  - what's the operator vocabulary for meta.rule in an argument chain?
  - what happens to downstream conclusions if I retract a lemma?
  - when should I NOT use the argument graph?
applies-to: put / get / link / tag (kind='memory', kind='finding')
status: active
---

# precis-argument-help — the reasoning shadow beside a draft

While writing, you cite sources and draw conclusions from them. The
argument graph externalises *that reasoning* as small, individually
defensible steps — so it survives the turn, is reusable across drafts
and personas, and self-invalidates when a source it rests on is
retracted.

## What the argument graph is (and is not)

A shadow layer of small, reusable steps beside a draft: **lemmas**
(claims) and **inferences** (reasoning steps that combine lemmas into a
new one). It is:

- **NOT published.** Lemmas and inferences never `\cite{}` — same rule
  as `finding`. The draft's reader-facing citation is always the
  primary source (`[pc893]`); the argument graph is the writer's-aide
  layer behind it.
- **NOT a proof checker.** precis does not verify that a conclusion
  follows from its premises — you *assert* the logical step
  (`meta.rule`), precis *records and audits* it (stores the edges,
  ripples a retraction, surfaces inherited caveats). Validity is your
  judgment call, every time.
- **Sparse and opt-in.** A grounded lemma comes free from a `finding`
  you already made. Mint an inference node only at a genuinely
  contestable reasoning step — not one per sentence. If you're minting
  a lemma for every clause, that's a smell (mirrors the `finding`
  spin-breaker discipline — see "When NOT to use it" below).

## State a lemma

Two kinds of lemma, one routing test: **does it pin to a single corpus
source?**

- **Yes → it's a `finding`, not a `kind:lemma` memory.**
  `put(kind='finding')` already refuses a sourceless claim ("If this is
  your own synthesis with no single source, it is NOT a finding —
  record a memory instead"); the same test routes the other way — a
  claim with one clean source belongs in `finding`, not here. See
  `precis-finding-help`.
- **No (derived / composite / sourceless judgment) → `memory` tagged
  `kind:lemma`.**

```python
put(
    kind="memory",
    text="pc893 (Nature, unretracted) claims the reaction proceeds via a "
    "three-electron pathway.",
    tags=["kind:lemma"],
    link="pc893",
    rel="cites",
)

put(
    kind="memory",
    text="pc999 claims the same intermediate is observed under N2 ambient.",
    tags=["kind:lemma"],
    link="pc999",
    rel="cites",
)
```

`rel='cites'` records *what this lemma rests on* — the "cited paper" the
retraction ripple and the `view='argument'` stale-premise flag both
check. No `TRUST:` tag: trust is the *absence* of a `retracts` /
`raises-concern-about` edge on the cited source, plus any inherited
caveats (see "Caveats" below) — never an asserted ordinal.

## Chain an inference

Create the `kind:inference` node, attach each premise with
`derived-from` (the inference *was produced from* its premises — reused,
not a new relation), set `meta.rule` + `meta.warrant`, then `entails`
the conclusion:

```python
# 1. the inference node — rule + warrant go straight on put()
infer = put(
    kind="memory",
    text="From X (pc893) and Y (pc999), the shared intermediate "
    "implies a common three-electron mechanism.",
    tags=["kind:inference"],
    rule="and-intro",
    warrant="both lemmas hold under the same ambient (N2), so the "
    "shared-intermediate claim composes cleanly",
)
# → memory id=501

# 2. attach the premises (each lemma "derived-into" the inference)
link(kind="memory", id=501, target="me<lemma-A-id>", rel="derived-from")
link(kind="memory", id=501, target="me<lemma-B-id>", rel="derived-from")

# 3. the conclusion — a fresh, reusable lemma
concl = put(
    kind="memory",
    text="The reaction proceeds via a common three-electron "
    "mechanism under N2 ambient.",
    tags=["kind:lemma"],
)
# → memory id=502
link(kind="memory", id=501, target="me502", rel="entails")
```

Read it back as: *"L_A `derived-into` I, L_B `derived-into` I, I `entails`
Z"* — "from A and B, infer Z." No single premise alone claims to entail
Z; the inference node carries the joint step. `meta.rule` and
`meta.warrant` can also be set later via
`edit(kind='memory', id=501, rule=..., warrant=...)` — no body rewrite
required.

## The operator vocabulary (`meta.rule`)

Free text — precis doesn't validate it — but these are the scannable
defaults, so prefer one when it fits:

| `rule` | Reads as |
|---|---|
| `modus-ponens` | If P then Q; P holds; therefore Q. |
| `and-intro` | P holds, Q holds; therefore P ∧ Q. |
| `or-elim` | Either P or Q leads to R; therefore R. |
| `abduction` | R is observed; P would best explain R; therefore (tentatively) P. |
| `statistical` | The pattern holds across N cases; therefore it generalises (with the usual caveats). |
| `analogy` | P holds in a structurally similar system; therefore (tentatively) P holds here. |
| `generalisation` | P holds in every case examined; therefore P holds in general. |

## Read the argument

```python
get(kind="memory", id=501, view="argument")
```

Renders the proof tree begat-style (like `finding`'s claim → begat chain):
the inference's premises (recursing into any premise that is itself a
lemma produced by an *earlier* inference), the rule + warrant, the
conclusion, and two graph-only flags — no text reading, ADR 0054 §8:

- **stale-premise** — a premise cites a source that now carries a
  `retracts` / `raises-concern-about` edge. The `STALE:retracted-premise`
  tag on the inference itself (set automatically the moment the
  retraction edge is written — see "Retraction ripple" below) is the
  authoritative, always-current signal; this view is the backstop for
  arguments assembled *after* the retraction, before the tag catches up.
- **inherited-caveat** — a premise (or something it was built on) carries
  an unaddressed caveat via `qualified-by`, marked *"inherited — confirm
  still addressed."*

Call `view='argument'` on a `kind:lemma` id too — it shows the
inference(s) that entail it (the "what produced this claim?" direction),
recursed the same way.

## Recursion

A conclusion lemma is just another `kind:lemma` memory — it can serve as
a premise for the *next* inference exactly like a grounded `finding`
premise does. This is how the graph deepens without any new machinery:
mint I2 with `derived-from` → the earlier conclusion (plus any other new
premises), `entails` → a new lemma, and so on. There is no depth limit
other than the view's recursion guard (a defensive cap, not a real
ceiling).

## The publication boundary

Lemmas and inferences **never** `\cite{}` — the draft's reader-facing
citation stays the primary source. To make the reasoning trail reachable
from the prose without leaking it into the export, point a draft chunk
at the inference with `see-also` (a writer's-aide pointer, not a
citation):

```python
link(kind="draft", id="dc123", target="me501", rel="see-also")
```

## When NOT to use it

- **First-time claims, opinions, rhetoric.** Those are prose — write
  them into the draft directly. A lemma is for a claim you'll *reuse* or
  whose *provenance/validity* matters enough to track.
- **A single-source empirical claim.** That's a `finding` (see above),
  not a `kind:lemma` memory.
- **Over-producing lemmas.** One inference node per genuinely
  contestable step, not per sentence — mirrors the `finding` spin-breaker
  guidance (`precis-finding-help`). If the argument graph for a short
  draft has dozens of nodes, you're probably mining connective tissue
  that belongs in prose.

## Retraction ripple (what happens automatically)

When a `retracts` or `raises-concern-about` edge is written against a
paper (by the provenance write-through, or by hand — see
`precis-relations`), precis walks the argument graph *from that paper*
and tags every inference resting on it — directly or transitively
through a chain of prior conclusions — `STALE:retracted-premise`. This
is **system-set**: `tag(add=['STALE:...'])` / `tag(remove=[...])` from
the agent path is refused — the tag is derived and recomputed on every
retraction-edge add *or* remove, so it always matches current
reachability. Read it, don't set it:

```python
search(kind="memory", tags=["STALE:retracted-premise"])
```

For the corpus-wide sweep (every stale inference + every inference
carrying an unaddressed caveat + every open lemma-vs-lemma
`contradicts`), see `precis stats --argument` (a CLI report, exhaustive
by SQL construction — not an LLM scan).

## Caveats

A caveat is the *negative/limiting* complement to `finding.scope` — not
"holds under N2 ambient" (positive setup) but "only validated for n <
100" or "assumes the linear regime" (where the claim *breaks* or is
*unproven*). Distinct fields; don't cram a caveat into `scope`.

**State a caveat.**

```python
put(
    kind="memory",
    text="The three-electron pathway was only validated for n < 100 "
    "cycles — long-run behaviour is untested.",
    tags=["kind:caveat"],
    link="me<lemma-or-finding-id>",
    rel="qualifies",
)
```

**What propagates.** `view='argument'` surfaces every caveat a
conclusion *inherited* through its premise chain, marked "inherited —
confirm still addressed." You confirm or neutralise it in the
inference's `meta.warrant` prose (record the judgment there — the graph
doesn't have an "addressed" toggle in v1). precis never auto-decides
whether a caveat still bites downstream — it only refuses to let you
forget it's there (ADR 0054 §7/§8).

## See also

```python
get(kind="skill", id="precis-finding-help")  # grounded (single-source) lemma
get(kind="skill", id="precis-citation-help")  # verified quote + verifier confidence
get(
    kind="skill", id="precis-relations"
)  # entails/qualifies + the retracts/raises-concern-about pair
get(kind="skill", id="precis-provenance-help")  # retraction/correction/concern checks
get(
    kind="skill", id="precis-memory-help"
)  # kind:lemma/kind:inference sub-kinds, meta.rule/warrant
```
