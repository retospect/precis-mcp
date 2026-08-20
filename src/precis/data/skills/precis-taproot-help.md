---
id: precis-taproot-help
title: precis — the cross-paper claim-evidence graph (Taproot)
summary: claim hubs (finding tagged TAPROOT:claim) aggregate many papers as typed evidence edges; [fi<id>] is a living citation that resolves to the current best originator(s) — find a hub, read its evidence, cite it
applies-to: get/search (kind='finding', tags=['TAPROOT:claim'], view='evidence'); citing [fi<id>] in prose
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

**Citing a hub** — stay in this file. **Authoring, minting, or
merging one** → [[precis-taproot-mint-help]]. **Converting a draft's
legacy `[pc<id>]`/`[pa<id>]` cites into hub cites in bulk** →
[[precis-taproot-backfill-help]].

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
`establishes` (originator), `corroborates`, `contradicts`. A live
`contradicts` edge blocks the hub's nanopub mint until adjudicated
([[precis-nanopub-help]]) — attach deliberately, never as a softer
"partially disagrees". The originator (★) is **derived at read time**,
not stored — whichever supporter(s) the *other* supporters' citations
converge on (`src/precis/taproot/seniority.py::derive_evidence`, over
the held `cites` graph). No intra-supporter citation edge held → every
supporter stays `corroborates` (never guessed).

**A compound hub holds no direct evidence.** When a claim decomposes into
several atomic sub-claims (see [[precis-taproot-backfill-help]]'s
decomposition note), the bundling sentence gets its own hub — cite-able,
but attach-only-through-atoms: `link(...,
rel='establishes'|'corroborates'|'contradicts')` onto a compound
hub raises. Attach evidence to the atom hub the passage actually supports
instead — `get(id='fi<id>', view='links')` lists a compound's `conjunct-of`
atoms.

A compound's **trust** is derived, not absent: worst-of its atoms' own
trust states (`taproot/trust.py::_compound_trust`, status `hub-compound`)
— `get(id='fi<id>', view='evidence')` shows a trust label with no direct
edges underneath, the expected depth-1 rollup, not missing data.

**Edges are chunk-grounded.** An evidence edge names the *specific
passage* that supports the claim: supply a supporter's `source_handle`
(a `[pc<id>]` paper chunk) and the edge stores `pc<id>`-granular, so the
link graph — every reader on it (the finding's link table, the citation
tree) — resolves to that passage, not just the whole paper. Two distinct
passages of one paper become **two edges** ("the set of chunks that
support this point"). Omit `source_handle` and the edge falls back to a
coarse ref-level `pa<id>` — the whole paper, no passage — exactly what
makes a claim tree hard to walk. Always ground the edge when you know
the chunk. See `precis-fisheye-help`'s Claims group (`fisheye+1hop` on
prose that cites a hub) for the read-time render.

**Patent evidence grounds in description text, not legal claims.** A
patent's claims section defines legal scope, not empirical support
(`docs/architecture/glossary.md`'s world-claim vs legal claim) —
`hub_refine`'s discovery leg drops legal-claim blocks before Verify, so
the automated corroborator search never surfaces one. A discovery-side
filter, not an attach-time guard: hand-attaching via `link()` still
means picking a description/abstract passage yourself.

**A prophetic patent example only ever corroborates.** When an evidence
edge's grounding chunk is a patent paragraph the `patent_example` axis
tagged `PATENT_EXAMPLE:prophetic` — present/future/modal tense, proposed
rather than performed (US patent convention) — `attach_evidence` appends
a fixed caveat to `meta.caveats`: `"prophetic example (proposed, not
performed) — corroborates at best"`. Injected at the single
evidence-edge choke point, never by the verify LLM, so every
prophetic-grounded edge carries it regardless of caller. A worked
example (past tense, performed) and any untagged patent chunk get no
caveat — a downgrade signal, never a hard exclusion.

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

**Atom vs compound — same rule, one level up.** When a claim decomposed
into a bundling **compound** hub over several atomic hubs (`conjunct-of`,
above), cite the atom when your sentence asserts just that one conjunct;
cite the compound only when your sentence genuinely restates the bundled
claim as a whole. `get(id='fi<id>', view='links')` lists a compound's
`conjunct-of` atoms if you need to pick among them.

**If a cited `[fi<id>]` errors "not a TAPROOT:claim finding":** the
finding either never was a hub, or was demoted to `TAPROOT:review` — a
2026-08-04 axis-pass race (fixed), but pre-fix casualties exist. Check
its tags (`get(id='fi<id>')`); if the sentence is meta-prose, de-cite
the draft down to the underlying `[pc<id>]`; if it passes
[[precis-taproot-mint-help]]'s "Claim admissibility" rubric, restore the
`TAPROOT:claim` tag
(`tag(kind='finding', id='fi<id>', add=['TAPROOT:claim'])`).

## Maturity — what's live vs dark

| | |
|---|---|
| Hub mint / evidence attach (`src/precis/taproot/hub.py`) — `put(kind='finding', supporters=…)`, `link(kind='finding', rel='establishes'\|'corroborates'\|'contradicts')`, and CLI `precis taproot mint` | live |
| Hub reword-in-place (`hub.py::refine_claim_sentence`) — `edit(kind='finding', title=…)` | live |
| Seniority derivation (originator/corroborator split) | live |
| Living-citation resolve + authorial pins (`precis resolve`) | live |
| Fisheye reference-ring Claims explosion | live |
| Claim→claim `refines` links — `link(kind='finding', rel='refines')` and CLI `precis taproot refine` | live (advisory-only, no evidence flow) |
| Whole-draft/section/chunk `[pc<id>]`→`[fi<id>]` backfill — `put(kind='job', job_type='taproot_backfill')` (serial, checkpointed, melchior `claude_inproc` lane) and CLI `precis taproot backfill` | live (on-demand; LLM runs on the cluster worker, never the MCP) |
| Atomic decomposition — `extract_claim` splits a span into atom hubs + an optional bundling **compound** hub, `conjunct-of`-linked via `taproot/hub.py::apply_extraction`; runs inside `taproot_backfill`, no separate door | live (compound hubs excluded from `hub_refine`'s due-set and `chase_trigger`'s embed/probe — those two touch atoms only) |
| Whole-paper `[pa<id>]` arm (stub-skip; default `[pa]`→`[pc]` re-ground; `params.ref_level`/`--ref-level` whole-paper promote) | live (slices 1+2; job + CLI) |
| Corpus-wide forward chase bridge (`PRECIS_TAPROOT_CHASE_ENABLED` — a `chase`-pass sub-feature, not its own service) | dark, default-OFF |
| Hub-refine pass (`workers/hub_refine.py`, `hub_refine` service) | dark, default-OFF |
| Chase-trigger pass (`workers/chase_trigger.py`, `chase_trigger` service) — marks a hub `TAPROOT_DUE` when a near paper/patent chunk lands, so hub-refine claims it promptly instead of waiting out its backstop | dark, default-OFF |
| `axis:taproot` `TAPROOT:claim`/`TAPROOT:review` classifier (`PRECIS_AXES_ENABLED`) | dark, default-OFF |

All dark rows default off — evidence stays sparse until turned on to
seed it. Everything with its own `service_config` service (`hub_refine`,
`chase_trigger`, every `axis:<id>`) flips live via `precis service prio`
/ `/categorizers`, no redeploy; the forward chase bridge is a
`chase`-pass-internal env flag, unaffected.

## See also

```python
get(kind="skill", id="precis-taproot-mint-help")  # author, mint, sharpen, merge a hub
get(
    kind="skill", id="precis-taproot-backfill-help"
)  # convert a draft's [pc]/[pa] cites in bulk
get(kind="skill", id="precis-fisheye-help")  # Claims explosion in the reference ring
get(
    kind="skill", id="precis-finding-help"
)  # finding lifecycle, chase, the evidence view
get(kind="skill", id="precis-citation-help")  # the inline [pc<id>] cite, write side
get(kind="skill", id="precis-draft-help")  # authoring prose that cites hubs
get(
    kind="skill", id="precis-nanopub-help"
)  # mint gates + claim-sentence grammar (authoring-scope) + publish pipeline
```
