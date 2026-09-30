---
id: precis-roadmap-help
title: precis — the roadmap quest body (rungs, capabilities, pathways)
summary: roadmap tick body — pathway/capability/rung vocabulary, the rung meta contract, the three roles (demand/supply/bridge), no-number-no-rung, root-only ticking, the capability ledger
answers:
  - what is a rung and what does meta.rung have to contain?
  - what does the demand role do vs supply vs bridge?
  - why was my rung write refused — "no number, no rung"?
  - why can't I tick a pathway or capability quest directly?
  - where does a capability's best-supply number live?
applies-to: get/put/edit/tag/link (kind='quest', quest_body='roadmap'; kind='todo', meta.rung)
tags: workflow
kinds: quest, todo
status: active
---

# precis-roadmap-help — the roadmap quest body

A **roadmap** quest (`meta.quest_body = "roadmap"`) grows a
capability/pathway/rung graph instead of proposing materials. Three node
types, no new kind:

- **root** — the one quest that ticks. Scans the capabilities serving it,
  picks one gap, acts, ends.
- **capability** (quest, `quest_body="roadmap"` + `meta.rubric_objectives`)
  — a measurable axis spine (e.g. positional accuracy, cycle time).
  Data, never ticked itself.
- **pathway** (quest, `quest_body="roadmap"`, no axes) — one bet toward
  the root. Data, never ticked itself.
- **rung** (todo, `meta.rung`) — a completable step: consumes capability
  values, produces capability values with citing evidence, serves one
  pathway and one capability.

Only the root runs the autonomous loop — a pathway or capability quest
carries the `roadmap` marker so its rungs render, but ticking it directly
is a guaranteed dry tick (it has no `rubric_objectives`/`demand`/`supply`
of its own to act on).

## The rung meta contract

```python
put(
    kind="todo",
    text="…",
    meta={
        "rung": {
            "pathway": "qu<id>",
            "consumes": [{"capability": "qu<id>", "key": "axis", "value": 1.0}],
            "produces": [
                {
                    "capability": "qu<id>",
                    "key": "axis",
                    "value": 2.0,
                    "evidence": ["fi<id>"],
                }
            ],
        }
    },
)
```

`meta.rung` accepts `pathway`, `consumes`, `produces`, and `benign`
(`"required"` only) — any other key is refused. `pathway` must be a
`qu<id>` handle. Every `consumes` / `produces` entry is `{capability:
"qu<id>", key: <non-empty string>, value: <number>}`; `produces`
additionally requires `evidence` — a non-empty list of well-formed
handles (`fi<id>`, `pa<id>`, …). A `produces` entry with empty or missing
`evidence` is refused whole-call — **no number, no rung**. `consumes`
entries carry no evidence; they are requirements, not claims.
`benign: "required"` is an override, never a default: it forces the
terminal-rung benign gate (below) onto a rung whose product is consumed
by another rung but still leaves the lab — the override only ever adds
the requirement, never removes it from a rung the graph already derives
as terminal.

```python
put(kind="todo", text="…", meta={"rung": {"pathway": "qu1", "produces": [
    {"capability": "qu2", "key": "accuracy_nm", "value": 3.0, "evidence": []}
]}})
# → refused: no number, no rung
```

`rung` is promotable on `tag()` too, so a rung's `produces` can be
updated on completion without a re-put.

## The three roles

One role per tick, chosen by scanning the axes of every capability
serving the root, lowest-in-the-chain first (the capability consumed by
the most other rungs' `consumes` — a chain-depth proxy):

- **bridge** — an axis with both a demand and a cited best supply that
  falls short of it. Compares the two and either mints a rung whose
  `produces` closes the gap, mints a new pathway quest (`STATUS:dormant`)
  when no existing pathway plausibly covers the shortfall, or writes a
  `dead-end` logbook entry when nothing plausible exists.
- **supply** — an axis with a demand set but no cited supply at all.
  Runs a literature search, mints finding hubs with quantified quotes,
  writes `meta.supply[key]` citing them.
- **demand** — an axis with no `meta.demand` entry yet. Reads the se
  part(s) the root or capability serves, computes what the part requires
  on that axis, writes `meta.demand[key]` plus a `decision` logbook entry.

Priority: bridge first (a cited shortfall blocks the chain above it),
then supply (a demand with nothing to compare against), then demand (an
axis with no number at all). A tick acts on exactly one axis, then ends —
no proposal menu, no `structure` schema, no Pareto frontier for this body.

## Minting a rung — `STATUS:open`, no `llm_tier`

A rung mints `STATUS:open` + `waiting-for:reto`, with `llm_tier`
**absent** — that absence, not the status, is what keeps it out of the
dispatch rotation. A human flips a rung into the doable rotation by
adding `llm_tier`; minting it is the spend, working it is a separate act.
A new pathway quest mints `STATUS:dormant`; a human activates it.

```python
edit(kind="todo", id=<rung-id>, meta={"llm_tier": "big"})  # opt a rung into the rotation
```

A rung whose `produces` appears in no other rung's `consumes` is
terminal (derived, never annotated). A terminal rung under a root that
has a capability naming "benign" gets that capability's demanded axes
folded into `consumes` as a requirement; an intermediate rung gets them
as an advisory only.

## Where `supply` lives

`meta.supply` on the **capability** quest, keyed by axis:

```python
edit(
    kind="quest",
    id=<capability-id>,
    meta={"supply": {"accuracy_nm": {"value": 2.1, "evidence": ["fi189542"]}}},
)
```

Every entry needs `evidence` — a non-empty list of handles — or the
write is refused. `meta.demand` is the mirror shape, keyed the same way,
each entry `{value, source, reason}` (`source` = `se:<slug>` / `td<id>` /
`qu<id>`, where the number came from). Neither lives on `measures` or
anywhere else — a rung's `done` `produces` value is what raises a
capability's *best supply* at read time, but the stored `meta.supply` is
the number a search result cited directly.

## The capability ledger

`view='tree'` on a roadmap root shows one row per capability × axis:
key, demanded, best supply (with its evidence), the rung closing it, and
a state — `unmet` / `partial` / `met` / `dead-end`. The same table is
pinned into the root's dossier as one regenerated chunk, rewritten from
the graph on every tick rather than appended.

```python
get(kind="quest", id=<root-id>, view="tree")  # capability ledger table
get(kind="quest", id=<root-id>, view="dossier")  # the pinned chunk, same table
```

## Fail signals

A rung minted with no evidence on its `produces` never happens — the
write is refused before the `todo` exists. Two signals mean the loop is
misbehaving rather than making progress:

- **A rung minted without a number** — a `produces` entry whose `value`
  is missing or unresolvable. Should be structurally impossible; if you
  see one, the guard was bypassed somewhere upstream of the write.
- **Deed count climbing while no ledger value changed** — only a ledger
  improvement (a capability's best supply getting better) stamps a
  `milestone`. A minted rung alone does not count as a deed and does not
  reset the dry-tick counter; three ticks that change neither the gap
  count nor a ledger value trip the existing cool-down + alert.

## See also

- [[precis-quest-help]] — quest verbs, logbook, dossier mechanics
- [[precis-quest-writing-help]] — when to mint a roadmap quest, the
  pathway/capability/rung vocabulary at write time
