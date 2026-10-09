---
id: precis-fisheye-help
title: precis — the fisheye neighborhood render (focus + context) on every kind
summary: get(kind=<any>, id=…, extent='fisheye'|'fisheye+1hop'|'fisheye+2hop'[+recall]) — one node plus its surroundings, scaled by distance; the extent ladder, the spatial neighborhood (draft/plan sections), the cluster map (papers), the link neighborhood grouped by ring (memory, quest, taxon, concept, component, todo, finding, …). Every kind renders or says in one sentence why not.
answers:
  - how do I read a chunk along with the text around it, not just the chunk itself?
  - how do I see the neighbourhood of a quest, a memory, a taxon or a todo?
  - what's the difference between the fisheye neighborhood and fisheye+1hop?
  - what does extent= do on get, and is it the same as view='fisheye'?
  - which kinds have the fisheye ladder?
  - how is fisheye different from view='toc'?
  - how do I walk a memory's links and see its mirror filename?
  - why does extent='fisheye' on a measure or a web page raise Unsupported?
applies-to: get(kind=<any>, id=, extent=)
tags: addressing, verbs
status: active
---

# precis-fisheye-help — focus a node and get its neighborhood, not a bare chunk

A **fisheye** is a degree-of-interest render: focus one node and get it
**plus its surroundings**, scaled by distance — not a bare chunk floating
with no context, and not the whole document either. Pure assembly of
data that already exists (reading order, chunk gists, link edges) —
nothing is stored, nothing runs in the background.

Classifying refs into taxon nodes and walking their concept hierarchy:
`precis-classify-help`.

## One door: extent= on every kind's get
## Is extent= the same as view='fisheye'?

```python
get(kind="memory", id="me4641", extent="fisheye+1hop")  # the note + its links by ring
get(kind="quest", id="qu202467", extent="fisheye+1hop")  # serves / served-by under Roadmap
get(kind="taxon", id="tn88", extent="fisheye+1hop")  # specialises / has-instance under Taxonomy
get(kind="paper", id="mao18", extent="fisheye")  # the cluster map; pc<id> rows to drill
get(id="pc13234", extent="fisheye")  # a chunk, bare handle: the split within its cluster
get(kind="draft", id="dc41", extent="fisheye+1hop")  # a section + reading order + reference ring
get(kind="skill", id="precis-get-help", extent="verbatim")  # a skill: body, no ring
```

`extent=` selects the rung; `view='fisheye'` (and the other rung labels
on `view=`) means the same thing — the ladder shipped on that door
first. Passing both with different values is a `BadInput`. A kind with
no graph node answers `Unsupported` with the reason in one sentence,
never a silent fall-back to the lone chunk.

## The extent ladder — how much to render

Each rung **strictly contains** the previous one:

| `extent=` | Shows |
|---|---|
| `kwd` | one-line bookmark (a section: under its ancestor path) |
| `summary` | the node's gloss (summary → keywords → first line) — alone |
| `verbatim` | the node's full text — alone |
| `fisheye` | verbatim center **+ the spatial neighborhood** |
| `fisheye+1hop` | `fisheye` **+ the ring**: what it points at / what points at it, one edge out |
| `fisheye+2hop` | `fisheye+1hop` **+ the second hop as counts** (not on draft/plan) |

Any rung takes a **`+recall` suffix** (`fisheye+1hop+recall`; bare
`+recall` means that one): the k=8 nearest refs of the same kind and
`finding` by embedding, each with a gist line and a similarity score —
what is *about* the same thing but was never linked. A recall line is
a lead to check, not a connection. Not on draft/plan sections. On a
memory, recall stays inside the focus's own `SPACE:` value (a
`repo-dev` memory never recalls a `research` one).

The first three rungs render the node **alone**. Surroundings appear
only at `fisheye` and up.

## Which kinds have the ladder — every kind, by family

| Family | Kinds | `fisheye` shows | `fisheye+1hop` adds |
|---|---|---|---|
| Tree | `draft`, `plan` — **section handles** `dc<id>` / `pe<id>` / `¶…` | the reading-order span under the ancestor heading | the reference ring (Cited / Cross-refs / Notes / Claims) |
| Document | `paper`, `patent`, `datasheet`, `cfp`, `edgar` — whole handle or chunk handle | whole: the cluster map; chunk: the split within its cluster | the link neighborhood of the ref |
| Link | everything else with a handle — `memory`, `finding`, `quest`, `todo`, `taxon`, `concept`, `component`, `structure`, `gripe`, `folder`, `job`, … | the card (title → gist → body) | the link neighborhood by ring |
| Skill | `skill` (`sk:<slug>`) | the verbatim body; `kwd` a bookmark | nothing — a file has no corpus position |

`Unsupported`, with the reason: a **whole draft or plan** (the eye
focuses one section — `view='toc'` lists the `dc<id>` handles); a
**measure** (a row of a run, not a node — fish-eye its paper or quest);
**`web`** and the other query/URL-addressed providers (`calc`, `math`,
`wikipedia`, `youtube`, …: no stored ref, no handle); **`python`**,
**`md`** (file-backed, no refs row); **`tag`** (a vocabulary row).

## The spatial neighborhood (the `fisheye` rung on a section)

For a tree kind (`draft`/`plan`), `fisheye` renders a **graduated,
forward-biased span over reading-order neighbours** centered on the
focused section:

- **±5** neighbours render **full** (verbatim)
- **±10** render as a **summary** line
- **±15** render as a **keyword** (`kwd`) bookmark
- backward reach is **half** the forward reach (you've passed what's
  behind, you're heading into what's ahead)

The whole span renders under the node's **ancestor branch** so the focus
never floats free of its heading.

## The reference ring (`fisheye+1hop` on a section)

Where the spatial fisheye walks *reading order*, `fisheye+1hop` adds
what the section *points at*, one edge out:

- **Cited** — papers / datasheets / patents the section cites
- **Cross-refs** — other draft/plan chunks it links (`[dc<id>]`)
- **Notes** — memories/findings **linked to** the section (inbound
  `related-to`, `see-also`, `cites`, …)
- **Claims** — a `[fi<id>]` (or `[pub_id]`) cite naming a live claim hub
  explodes into its evidence: the claim, its derived `establishes`
  originator(s) (★, with the grounding chunk when known), a one-line
  corroborator/contradictor count, and its advisory `refines` neighbours
  (`↰ refined by fi<id>` — a sharper claim exists; `↳ refines fi<id>` —
  what this one sharpens). An authorial pin (`[fi<id>>pa5]` /
  `[fi<id>+pa5]`) marks the pinned paper 📌 and notes a divergence from
  the derived originator. A hub nobody has chased yet shows the claim
  with `(no evidence derived yet)`. A composite hub's `conjunct-of`
  atoms are not in the ring — `get(id='fi<id>', view='links')`.

Edges only, both directions, each group capped with a visible
`+N more — focus to expand` line. A memory merely *about* the section
but never linked is a `search` hit, not a hop.

## The link neighborhood — one heading per ring, one line per edge

On a link or document kind, `fisheye+1hop` lists every ref linked to the
focus, either direction, under one heading per ring group, each
`(group, label)` block capped at 8 with a `… +N more` line. Each line
reads from the focus's side: an edge it is the source of keeps its name
(`serves: qu12`), an edge pointing at it reads as the inverse
(`served-by: qu7`, `contradicted-by: pa3`), and an inbound edge with no
inverse reads `<-establishes: pa5` ("pa5 establishes this"). A neighbour
carrying `AUDIT:ungrounded-number` is flagged on its line. A memory with
a mirrored file reads `me4641 (worker_busy_vs_starved_diagnosis.md)`.

| Ring | Relations | Worked example |
|---|---|---|
| **Claim graph** | `establishes`, `corroborates`, `contradicts`, `refines`, `conjunct-of`, `disputes`, `motivated-by` | `get(kind='finding', id='fi42', extent='fisheye+1hop')` → `Claim graph:` · `<-establishes: pa5 — …` · `<-refines: fi77 — …` (fi77 refines this); a claim hub leads with its trust posture (`precis-finding-help`) |
| **Roadmap** | `serves`, `served-by` | `get(kind='quest', id='qu12', extent='fisheye+1hop')` → `Roadmap:` · `serves: qu3 — Grow the mesh` · `served-by: qu40 — …` · `served-by: pa88 — …`; a todo that `serves` a quest shows the same block |
| **Taxonomy** | `specialises`, `generalises`, `instance-of`, `has-instance`, `quantifies`, `quantified-by` | `get(kind='taxon', id='tn88', extent='fisheye+1hop')` → `Taxonomy:` · `specialises: tn2 — Faradaic efficiency` · `has-instance: me5 — FE at −0.5 V` |
| **Concepts** | `has-prerequisite`, `prerequisite-of`, `analogy-of`, `contrasts-with` | `get(kind='concept', id='cn9', extent='fisheye+1hop')` → `Concepts:` · `has-prerequisite: cn4 — Maxwell counting` |
| **Parts** | `contains`, `part-of`, `made-of`, `used-in` | `get(kind='component', id='cp3', extent='fisheye+1hop')` → `Parts:` · `contains: cp7 — Bipolar plate`; on `cp7` the same row reads `part-of: cp3`; a memory section `part-of` its file reads `contains:` on the file's eye |
| **Argument** | `entails`, `entailed-by`, `qualifies`, `qualified-by`, `derived-from`, `derived-into` | `get(kind='memory', id='me12', extent='fisheye+1hop')` → `Argument:` · `derived-from: me3 — …` (the proof tree itself: `view='argument'`, `precis-memory-help`) |
| **Notes & links** | `related-to`, `see-also`, `supports`, `cites`, `generalises`, `corrects` | `get(kind='paper', id='pa5', extent='fisheye+1hop')` → `Notes & links:` · `related-to: me77 — Read this for the floppy modes`; a finding that `supports` a quest reads `supported-by:` on the quest |

A relation outside these groups (`parent`, `draft-of`, `touched`, …) is
structural and not in the ring; `view='links'` lists everything.

## The second hop (`fisheye+2hop`)

What the ring's refs link to, excluding the focus and the ring itself,
as one count line per kind and relation: `12 paper via cites` reads
"twelve papers that the ring's refs cite". Counts keep a hub with
hundreds of second-hop edges inside one response. To list one group,
repeat the call with `q='<kind>:<label>'`:

```python
get(kind="finding", id="fi42", extent="fisheye+2hop")  # counts
get(kind="finding", id="fi42", extent="fisheye+2hop", q="paper:cites")  # that group, ≤40 refs
```

`more()` does not expand a group; it only pages a body that was too long.

## Read the same neighborhood in a browser

`/eye/<handle>?extent=<rung>` renders the same text the `get` returns,
every handle in it a link to its own focus page, the ladder as a row of
links; `/eye/` takes a handle or a query. A draft section also has the
three-pane reader, `/smartdraft/<draft-slug>?focus=dc<id>`.

## Don't confuse `fisheye` with `view='toc'`

`view='toc'` (`precis-toc-help`) is a **separate, recursive drill-down**
render for long documents — you pick a range, it re-clusters, you drill
again; its `kinds:` line is the authoritative list of where it works.
`fisheye` is the opposite move: you've already picked one node and want
its immediate surroundings rendered around it.

## See also

- [[precis-draft-help]] — draft chunk addressing, editing
- [[precis-toc-help]] — the recursive drill-down TOC render
- [[precis-get-help]] — the get verb generally
- [[precis-paper-help]] — paper chunk handles (pc<id>), citation export
- [[precis-relations]] — link relation vocabulary (cites, see-also, …)
- [[precis-taproot-help]] — the Claims group's claim hubs, evidence edges
- [[precis-memory-help]] — the memory walk, view='argument'
