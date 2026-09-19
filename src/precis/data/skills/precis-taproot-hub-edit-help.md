---
id: precis-taproot-hub-edit-help
title: precis — attach evidence, reword, sharpen, or merge an existing Taproot claim hub
summary: attach evidence to an existing claim hub, reword it in place, sharpen it with a refines link, or merge duplicate hubs — for minting a new hub from a sourced claim see precis-taproot-mint-help
answers:
  - how do I attach more evidence to an existing claim hub?
  - how do I merge two claim hubs that say the same thing?
  - how do I reword a claim hub without minting a duplicate?
  - how do I sharpen a claim without losing the original wording?
  - what's the difference between rewording, refining, and merging a hub?
applies-to: link/edit(kind='finding') hub-editing doors; precis taproot refine (CLI equivalent)
status: active
tags: workflow, design
kinds: finding
---

# precis-taproot-hub-edit-help — attach evidence, reword, sharpen, or merge a claim hub

See [[precis-taproot-help]] for what a claim hub is, `fi<id>` vs
`pub_id`, and how citing `[fi<id>]` resolves. For minting a brand-new
hub from a sourced claim, see [[precis-taproot-mint-help]].

## Attach evidence to an existing hub

To add a supporter to a hub that already exists (not at mint time),
`link(kind='finding', ...)` is the write door — no CLI equivalent, this
is MCP-only:

```python
link(kind="finding", id="fi42", rel="corroborates", target="pc293")
```

`rel` ∈ `establishes` / `corroborates` / `contradicts`; `target` is the
supporting paper/chunk handle — `pc<id>` grounds the edge at that
passage, `pa<id>` lands it ref-level. Don't hand-file `rel='contradicts'`
here — it's adjudication-derived (Part 2, not built) and a live one
blocks the hub's nanopub mint; a source that disagrees with the claim
is a `link(rel='disputes')` between the two claim hubs (see
[[precis-taproot-mint-help]]'s dispute-filing note), not an
evidence-role attach on this one. `rel` is a conservative write-time
label only — the originator/corroborator split is **derived** at read
time (`get(id='fi42', view='evidence')`), same as every other door here.
`id` must resolve to a live `TAPROOT:claim` hub (`fi<id>`, a pub_id, or a
bare ref_id); anything else, or `mode='remove'`, falls through to the
generic finding-link door.

## Reword a hub in place

Same claim, better wording — a rubric fix (dangling referent, meta-prose,
empty intensifier, TeX→UTF-8 notation) rewords the hub, it doesn't mint a
new one:

```python
edit(
    kind="finding",
    id="fi42",
    title="Hybridization of fullerenes with 2D materials has been pursued "
    "across graphene, g-C₃N₄, TMDs, h-BN, and black phosphorus.",
)
```

Retitles the hub in place: `refs.title` updates (full length, never
truncated), the `finding_body`
chunk is DELETE+INSERT re-emitted (embedding/summary cascade re-runs —
this is also the chunk hub dedup retrieves over, so the reword is
picked up automatically), stale card variants (`ord < 0`) drop, and a new
content-derived `pub_id` is added — the **old** one is kept as an alias,
so existing `[<pub_id>]` cites keep resolving. Evidence edges are
untouched. Rejects a non-hub finding and `dry_run` (no preview; the
write is direct). If the new wording's `pub_id` already belongs to a
*different* live ref, that's a duplicate-hub signal — the call raises
naming that ref rather than silently fusing it; see "Merge duplicate
hubs" below.

**A reword surfaces downstream.** Every draft cite records the hub's
`pub_id` at the moment its prose was written, so retitling marks each
citing passage as **drifted**: it shows in that draft's
`view='hygiene'` quoting both statements, and it **blocks the draft's
export** until the passage is re-checked and rewritten. The old
`pub_id` keeps resolving throughout — nothing breaks, it becomes
visible. Reword freely; just expect the citing prose to need a look,
and prefer one deliberate reword over several cosmetic ones.

**Not this door for a materially sharper/narrower claim** — that's a new
mint + `refines` link, below, not a retitle.

## Sharpen, refine, or merge a claim hub

Three different operations on an existing hub:

- **Same claim, better wording** → reword in place, above.
- **Materially sharper/narrower claim** → mint a new hub and link it
  `refines` the original, below. Both wordings stay independently citable,
  and the fisheye Claims ring shows the next editor that a sharper version
  exists.
- **Duplicate hubs** (two hubs converged separately on the same claim) →
  merge, below.

```python
# 1. mint the sharper claim (its own hub / fi<id>)
out = put(kind="finding", title="…sharper wording…", scope={}, supporters=[…])
# 2. link sharper --refines--> original
link(kind="finding", id=f"fi{out['hub_ref_id']}", rel="refines", target="fi<original>")
```

`id`/`target` each accept an `fi<id>` handle, a pub_id, or a bare
ref_id; both must resolve to live `TAPROOT:claim` hubs. The link is
**directed** (sharper → coarser), **advisory-only** (no evidence flows —
each hub keeps its own paper→hub edges), and **idempotent**. The Claims
ring then shows `↰ refined by fi<sharper>` on the original and `↳
refines fi<original>` on the sharper one.

CLI: `precis taproot refine --from fi<sharper> --to fi<original>`
(`--dry-run` to preview).

### Merge duplicate hubs

The merge door is a CLI verb, human-run: `precis taproot merge --loser fi<dup>
--winner fi<survivor>` (`--dry-run` prints the plan: edges repointed,
redundant edges dropped, the loser retired). It refuses a loser past
`candidate`. From the agent surface there is no merge verb — the
`pub_id`-collision raise from a reword attempt above is the handoff:
name the pair in a todo for the operator, or do it by hand. Pick the
survivor (better wording / more evidence), then:

```python
# 1. repoint every citing draft chunk from the dup to the survivor
edit(
    kind="draft",
    id="dc1652005",
    mode="find-replace",
    find="[fi<dup>]",
    text="[fi<survivor>]",
)
# 2. move evidence unique to the dup onto the survivor
link(kind="finding", id="fi<survivor>", rel="corroborates", target="pc<chunk>")
# 3. retire the dup
delete(kind="finding", id="fi<dup>")
```

Repeat step 1 for every draft chunk citing `[fi<dup>]` (`search(kind='draft',
q='[fi<dup>]')`) and step 2 for every evidence edge the dup holds that the
survivor doesn't; delete last — a draft still citing the dup would 404 once
it's gone.

## See also

- [[precis-taproot-mint-help]] — mint a new hub from a sourced claim
- [[precis-taproot-help]] — what a hub is; citing [fi<id>]
