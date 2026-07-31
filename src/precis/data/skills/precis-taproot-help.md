---
id: precis-taproot-help
title: precis — the cross-paper claim-evidence graph (Taproot)
summary: claim hubs (finding tagged TAPROOT:claim) aggregate many papers as typed evidence edges; [fi<id>] is a living citation that resolves to the current best originator(s)
applies-to: get/search (kind='finding', tags=['TAPROOT:claim'], view='evidence'); citing [fi<id>] in prose; precis taproot mint / refine
status: active
---

# precis-taproot-help — one claim, many papers, one citable hub

**Taproot** is the cross-paper evidence graph: instead of fifty papers
asserting the same fact as fifty disconnected citations, they converge
on one **claim hub** — a `finding` tagged `TAPROOT:claim`
(`STATUS:canonical`), the canonical node for that world-claim, citable
as `[fi<id>]`.

**`fi<id>` vs `pub_id`.** `fi<id>` (kind+serial, same family as
`pc`/`dc`/`me`) is the handle you write when citing a hub. `pub_id`
(a 6-char base32 content hash, e.g. `tbx2hd`) is the internal
mint-time dedup key — identical claim text always hashes to the same
`pub_id`, so concurrent mints of the same claim converge on one hub.
Both resolve to the same hub; `[fi<id>]` is the one to author.

## Find a claim hub to cite
## Search for existing claim hubs before minting a new one

```python
search(kind="finding", tags=["TAPROOT:claim"])  # every claim hub
get(id="fi42", view="evidence")  # originators / corroborators / contradicts
```

A hub surfaces in the **default** `finding` search — no `status=`
needed; the default cohort unions hubs in by their `TAPROOT:claim` tag
alongside `established` chase findings.

## The evidence model — typed, graded, cross-paper

Papers attach to a hub as one of three typed edges (ADR 0073):
`establishes` (originator), `corroborates`, `contradicts`. The
originator (★) is **derived at read time**, not stored — it's whichever
supporter(s) the *other* supporters' citations converge on
(`src/precis/taproot/seniority.py::derive_evidence`, over the held
`cites` graph). No intra-supporter citation edge held → every supporter
stays `corroborates` (never guessed).

**Edges are chunk-grounded.** An evidence edge names the *specific
passage* that supports the claim: supply a supporter's `source_handle`
(a `[pc<id>]` paper chunk) and the edge is stored `pc<id>`-granular, so
the link graph — and every reader on it (the finding's link table, the
citation tree) — resolves to that passage, not just the whole paper.
Two distinct passages of one paper become **two edges** ("the set of
chunks that support this point"). Omit `source_handle` and the edge
falls back to a coarse ref-level `pa<id>` — the whole paper, no passage
— which is exactly what makes a claim tree hard to walk. Always ground
the edge when you know the chunk. See `precis-fisheye-help`'s Claims
group (`fisheye+1hop` on prose that cites a hub) for the read-time
render of this same evidence.

## Cite a claim hub — the living citation
## What does a bare [fi<id>] cite resolve to?

A bare `[fi<id>]` resolves, in a draft, the fisheye reference ring,
and the draft export, to the hub's **current** derived `establishes`
originator(s) — falling back to corroborators, then in-flight — freshly
re-derived on every render (ADR 0074). A later-discovered originator or
a hub merge improves the cite on the next render; no re-cite.
`precis resolve` (the standalone `.tex`/`.md` CLI, not draft export)
still keys on the content-hash `[<pub_id>]` form instead — same
resolution, different token.

Pin it when you know better than the derivation:

```text
[fi<id>>pa5,pc293]   # replace — cite exactly these handles
[fi<id>+pa5]         # supplement — derived originators plus these
```

A `pc<id>` (paper-chunk) handle pins a passage but resolves to its
parent paper's cite_key. A **replace** pin that diverges from the
current derivation prints a stderr advisory; `--strict-pins` promotes
that to a CI-gate exit 3. A **supplement** pin never fires the
advisory (it's purely additive).

**One paper chunk can ground more than one claim hub.** A chunk that
asserts two distinct claims can supply evidence to two different hubs
— so a given `[pc<id>]` handle doesn't map to a single `[fi<id>]`. Pick
the hub for the specific claim your sentence makes, not just "the hub
near this chunk."

## Turn a draft's [pc<id>] cites into a hub cite
## Mint a claim hub from a claim I've already sourced

Hubs are paper-sourced and system/tooling-minted, **never**
agent-`put`-created — `put(kind='finding', ...)` always makes a
chase-target finding, never a hub (a draft's own novel assertion stays
draft-local, never enters the shared claim graph). Mint one with the
`precis taproot mint` CLI:

```bash
precis taproot mint --spec spec.json
precis taproot mint --dry-run --spec spec.json  # resolve + report, write nothing
```

`spec.json` is a JSON array of `{sentence, scope, supporters}` — one
entry per claim, each supporter a `{paper, role, source_handle}`:
`paper` is the supporting paper (its `pa<id>` handle, cite_key, or
pub_id); `role` defaults `corroborates`; **`source_handle` is the
grounding `[pc<id>]` paper chunk and you should always supply it** — it
lands on the edge as `src_chunk_id`, so the edge cites the passage
(`pc<id>`), not just the paper (`pa<id>`). List the same paper's
different supporting passages as separate supporters (same `paper`,
different `source_handle`) to attach the whole set. Omit it only when
you genuinely can't name the chunk; the edge then stays coarse
ref-level. It mints the hub (or converges onto an existing one for
identical claim content, via the content-hash `pub_id`) and attaches
each supporter's evidence edge, idempotently — a re-run of the same
spec attaches nothing twice (the dedup key includes the grounding
chunk, so re-running never duplicates a passage). Cite the resulting
`[fi<id>]` in your prose afterward.

## Sharpen a claim — link a reworded version (don't merge)

When you have a sharper/reworded version of an existing claim, **mint it
as its own hub, then link it** — don't try to edit or merge the original.
Both wordings stay independently citable, and the fisheye Claims ring
shows the next editor that a sharper version exists.

```bash
# 1. mint the sharper claim (its own hub / fi<id>)
precis taproot mint --json '[{"sentence":"…sharper wording…","scope":{},"supporters":[…]}]'
# 2. link sharper --refines--> original
precis taproot refine --from fi<sharper> --to fi<original>   # --dry-run to preview
```

`--from`/`--to` each accept an `fi<id>` handle, a pub_id, or a bare
ref_id; both must resolve to live `TAPROOT:claim` hubs. The link is
**directed** (sharper → coarser), **advisory-only** (no evidence flows —
each hub keeps its own paper→hub edges), and **idempotent**. In the
Claims ring the original then shows `↰ refined by fi<sharper>` and the
sharper one shows `↳ refines fi<original>`.

## Maturity — what's live vs dark

| | |
|---|---|
| Hub mint / evidence attach (`src/precis/taproot/hub.py`) | live |
| Seniority derivation (originator/corroborator split) | live |
| Living-citation resolve + authorial pins (`precis resolve`) | live |
| Fisheye reference-ring Claims explosion | live |
| Claim→claim `refines` links (`precis taproot refine`) | live (advisory-only, no evidence flow) |
| Corpus-wide forward chase bridge (`PRECIS_TAPROOT_CHASE_ENABLED`) | dark, default-OFF |
| Hub-refine pass (`workers/hub_refine.py`, `PRECIS_TAPROOT_REFINE_ENABLED`) | dark, default-OFF |
| `axis:taproot` `TAPROOT:claim`/`TAPROOT:review` classifier (`PRECIS_AXES_ENABLED`) | dark, default-OFF |

Both dark flags default off — evidence stays sparse until a corpus run
is turned on to seed it.

## See also

```python
get(kind="skill", id="precis-fisheye-help")  # Claims explosion in the reference ring
get(
    kind="skill", id="precis-finding-help"
)  # finding lifecycle, chase, the evidence view
get(kind="skill", id="precis-citation-help")  # the inline [pc<id>] cite, write side
get(kind="skill", id="precis-draft-help")  # authoring prose that cites hubs
```
