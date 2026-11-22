---
id: precis-taproot-hub-edit-help
family: evidence
title: precis — attach evidence, reword, sharpen, or merge an existing Taproot claim hub
summary: attach evidence to an existing claim hub, reword it in place, sharpen it with a refines link, or merge duplicate hubs — for minting a new hub from a sourced claim see precis-taproot-mint-help
answers:
  - how do I attach more evidence to an existing claim hub?
  - how do I merge two claim hubs that say the same thing?
  - how do I reword a claim hub without minting a duplicate?
  - how do I sharpen a claim without losing the original wording?
  - what's the difference between rewording, refining, and merging a hub?
  - how do I retire a claim hub that is already reviewed, signed or published?
applies-to: link/edit(kind='finding') hub-editing doors; precis taproot refine (CLI equivalent)
status: active
tags: workflow, design
kinds: finding
scope: write
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
generic finding-link door. Two more refusals, here and at mint
(`supporters=`): the role is only `establishes`|`corroborates` —
`primary` is not a role, omit it (the default is `corroborates`) — and
evidence attaches to ATOM hubs, so a composite (a hub with `conjunct-of`
atoms) refuses the edge; attach to the atom the passage supports.

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
so existing `[<pub_id>]` cites keep resolving. Ref-level evidence edges are
untouched; chunk-addressed users cause an atomic refusal until retargeting is
supported. Candidate/reviewed claims edit in place; unsigned approval reopens
and the response lists consumers flagged for re-review. Do not retitle a
frozen or signed hub — a signed (anchored, published) nanopub freezes the
wording and the retitle refuses: see `precis-nanopub-help`'s correction boundary.
Rejects a non-hub finding. `dry_run=True` previews the retitle —
old/new sentence and the `pub_id` change — and writes nothing; drop
`dry_run` to apply. If the new wording's `pub_id` already belongs to a
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

## Rescope a hub

A hub's `scope` (`material`, `method`, …) is part of its identity — the
`pub_id` and the `(sentence, scope)` dedup key hash it — so a retitle
that fixes the wording but leaves a stale scope term behind leaves the
hub unfindable by a later mint of the corrected claim (which then forks a
duplicate). Fix the scope through the same door:

```python
edit(kind="finding", id="fi42", meta={"scope": {"material": "C60", "method": "cage-cage"}})
edit(kind="finding", id="fi42", meta={"scope": {}})        # clear it
edit(kind="finding", id="fi42", title="…reworded…", meta={"scope": {...}})
edit(kind="finding", id="fi42", meta={"scope": {...}}, dry_run=True)   # preview
```

`scope` replaces the entire scope and maps strings to strings. Supply
`title=` to include writer attribution in `meta=` (see
`precis-finding-help`). Same effects as a retitle: the
`pub_id` is re-derived and the **old one kept as an alias**, a `pub_id`
that collides with a *different* live hub raises (duplicate-hub signal —
see "Merge duplicate hubs"), and a non-hub finding is rejected. Scope lint
(free-text values, keys outside the controlled set) is advisory, as at
mint: it shows in the response, never blocks. `dry_run=True` previews old
-> new scope and old -> new `pub_id` and writes nothing.

**Not this door for a materially sharper/narrower claim** — that's a new
mint + `refines` link, below, not a retitle.

## Claim type

Every hub carries a sort, `measurement | calculation | definition |
capability | mechanism | landscape`, set by the extractor at mint (or
the backfill classifier) and shown on `get(kind='finding',
view='evidence')` as `claim type: <type> (llm|human)`.
`edit(kind="finding", meta={"claim_type": …})` is **refused**: this
door cannot tell a human from an agent, so reclassifying is a human
act — the type form on `/claim/<head>`, or `precis taproot classify
--hub fi<N> --set <type> --apply`. A human choice is never overwritten
by the LLM paths.

`calculation` (a result or prediction obtained from a computation —
DFT, NEGF, MD, a named force field or theoretical model, method and
model kept in scope) shares measurement policy: per-edge verified,
widened, disputes-eligible, publishable. Sort by how the result was
obtained, even when the same quantity could be measured.

`landscape` (a background sentence about a whole class of systems, the
parent specific claims `refines` from) behaves differently:

- Dedups on the sentence alone: a later mint of the same sentence under
  another scope converges onto it instead of forking.
- Never widened: no refine / inbound-ground / chase arms add evidence.
- Never a `disputes` counterparty: it cannot contradict a specific result.
- Verified by consensus, not per edge: the evidence view shows
  `consensus (landscape): N independent sources, pass|fail` (floor 3).
- Not publishable: `nanopub` approve refuses it. Cite it as `[fi<id>]`.

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
out = put(kind="finding", title="…sharper wording…", args={"scope": {}, "supporters": […]})
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

## Merge duplicate hubs

Prevention first: a put that is the same as, a component of, or a
broader/narrower form of an existing hub is refused at mint time with
options attach (`dedup='attach:fi<N>'`), refine (the reword above — not
offered once the hub is past `candidate`) and force (`dedup='force'`); see
`precis-taproot-mint-help` "Search before you mint". Duplicates that got
through are merged as follows.

Pick the survivor (better wording / more evidence). `id` is the duplicate
that retires, `merge_into` the survivor:

```python
# 1. preview: the plan, nothing written
edit(
    kind="finding",
    id="fi<dup>",
    args={"merge_into": "fi<survivor>", "reason": "same claim, minted twice"},
    dry_run=True,
)
# 2. apply: the same call without dry_run
edit(
    kind="finding",
    id="fi<dup>",
    args={"merge_into": "fi<survivor>", "reason": "same claim, minted twice"},
)
```

The plan lists the edges repointed to the survivor, the edges dropped
because the survivor already holds them, self-loops dropped, and the draft
chunks whose `[fi<dup>]` cite becomes `[fi<survivor>]`. Applying does
exactly that in one transaction, retires the dup and records `fi<dup>
--refines--> fi<survivor>`. Dropped links are deleted for good; the
survivor's title, scope and `pub_id` are untouched. `reason` is required
and lands on both hubs' event log. One pair per call, no other edit kwarg
alongside; a re-run answers `already merged` and writes nothing.

Refusals:

- **Either hub past `candidate`** (reviewed, signed, published, rejected):
  no merge is possible, for anyone: its identity is frozen, so this door
  and the operator CLI (`precis taproot merge`) both refuse. Supersede
  instead, and the hub past `candidate` is always the one kept, whichever
  of `id` / `merge_into` you named; the refusal prints the call:
  `delete(kind='finding', id='fi<candidate-state dup>',
  superseded_by='fi<past-candidate hub>', reason='<why they are the same
  claim>')`. It retires the dup, records the redirect and moves the dup's
  other links to the kept hub (see `precis-delete-help`). Add
  `dry_run=True` to preview. If both hubs are past `candidate`, the
  refusal prints the retirement procedure call for the dup instead (next
  section). Do not repoint links and delete by hand.
- **A link on the dup that the survivor may not hold** (a `disputes`
  link when the survivor is a landscape claim, an `explains` link that
  would close a cycle): both modes refuse, name the link as it stands
  and give the `link(..., mode='remove')` call that removes it. Run that
  call, then retry.
- **A draft chunk cites the dup and cannot be rewritten**: the refusal
  lists the citing chunks; repoint those cites in the draft, then retry.
- **Either hub is a hypothesis**: a conjecture is never merged. Withdraw
  it with `precis-nanopub-help` "Record a rejection"; promotion is the
  reviewer's approve on its claim page.
- **`id` or `merge_into` is not a claim hub**, or both name the same hub.

The plan without the edit door (it also shows a blocked pair instead of
refusing):

```python
get(kind="finding", id="fi<survivor>", view="merge-plan", args={"loser": "fi<dup>"})
```

## Retire a hub past candidate

The plain `delete(kind='finding', ...)` refuses a hub past `candidate`
(reviewed, signed, anchored, published, or any terminal publish row): its
identity is frozen. It retires only through the retirement procedure, an
explicit flag on the same door:

```python
# a duplicate: superseded by another finding hub
delete(kind="finding", id="fi<N>", superseded_by="fi<M>",
       reason="same claim as fi<M>", args={"retire_published": True})
# withdrawn, no successor: retracted
delete(kind="finding", id="fi<N>", reason="the source retracted the value",
       args={"retire_published": True})
```

`reason` is required and `superseded_by` must name a live finding. Add
`dry_run=True` to preview. One transaction:

- The hub's publish row becomes `superseded` (successor named) or
  `retracted` (none) and records the reason and successor. The row is
  kept. A `publishing` row refuses until the registry POST resolves; a hub
  whose rows are all terminal keeps their state and gets the retirement
  recorded.
- Links move to the successor and draft `[fi<N>]` cites become
  `[fi<M>]`. Without a successor a live draft cite refuses: remove it
  first.
- `get(kind='finding', id=<N>)` reads a tombstone line, `retired:
  superseded by fi<M> — <reason>` or `retracted: <reason>`; a superseded
  hub's `fi<N>` handle redirects to `fi<M>`.
- A hub that was signed, anchored or published gets one owner todo
  (`ask-user`) listing the artifact's identifiers: a retraction or erratum
  is owed outside precis. The procedure does not withdraw the artifact.

The flag on a hub still at `candidate` refuses: use the plain door.

## See also

- [[precis-taproot-mint-help]] — mint a new hub from a sourced claim
- [[precis-taproot-help]] — what a hub is; citing [fi<id>]
