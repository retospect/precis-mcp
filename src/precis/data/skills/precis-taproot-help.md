---
id: precis-taproot-help
title: precis — the cross-paper claim-evidence graph (Taproot)
summary: claim hubs (finding tagged TAPROOT:claim) aggregate many papers as typed evidence edges; [pub_id] is a living citation that resolves to the current best originator(s)
applies-to: get/search (kind='finding', tags=['TAPROOT:claim'], view='evidence'); citing [pub_id] in prose; precis taproot mint
status: active
---

# precis-taproot-help — one claim, many papers, one citable hub

**Taproot** is the cross-paper evidence graph: instead of fifty papers
asserting the same fact as fifty disconnected citations, they converge
on one **claim hub** — a `finding` tagged `TAPROOT:claim`
(`STATUS:canonical`), the canonical node for that world-claim, with its
own citable `pub_id`.

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
stays `corroborates` (never guessed). Each edge carries a grounding
chunk pointer (`source_handle`) once the chase populates one. See
`precis-fisheye-help`'s Claims group (`fisheye+1hop` on prose that
cites a hub) for the read-time render of this same evidence.

## Cite a claim hub — the living citation
## What does a bare [pub_id] cite resolve to?

A bare `[<pub_id>]` resolves, at `precis resolve` and in the fisheye
reference ring, to the hub's **current** derived `establishes`
originator(s) — falling back to corroborators, then in-flight — freshly
re-derived on every run (ADR 0074). A later-discovered originator or a
hub merge improves the `.bib` output on the next `resolve`; no re-cite.

Pin it when you know better than the derivation:

```text
[<pub_id>>pa5,pc293]   # replace — cite exactly these handles
[<pub_id>+pa5]         # supplement — derived originators plus these
```

A `pc<id>` (paper-chunk) handle pins a passage but resolves to its
parent paper's cite_key. A **replace** pin that diverges from the
current derivation prints a stderr advisory; `--strict-pins` promotes
that to a CI-gate exit 3. A **supplement** pin never fires the
advisory (it's purely additive).

**One paper chunk can ground more than one claim hub.** A chunk that
asserts two distinct claims can supply evidence to two different hubs
— so a given `[pc<id>]` handle doesn't map to a single `[pub_id]`. Pick
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
pub_id — not the chunk); `role` defaults `corroborates`;
`source_handle` records the grounding `[pc<id>]` you'd otherwise cite
inline. It mints the hub (or converges onto an existing one for
identical claim content) and attaches each supporter's evidence edge,
idempotently — a re-run of the same spec attaches nothing twice. Cite
the resulting `[<pub_id>]` in your prose afterward.

## Maturity — what's live vs dark

| | |
|---|---|
| Hub mint / evidence attach (`src/precis/taproot/hub.py`) | live |
| Seniority derivation (originator/corroborator split) | live |
| Living-citation resolve + authorial pins (`precis resolve`) | live |
| Fisheye reference-ring Claims explosion | live |
| Corpus-wide forward chase bridge (`PRECIS_TAPROOT_CHASE_ENABLED`) | dark, default-OFF |
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
