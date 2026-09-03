---
id: precis-taproot-help
title: precis — the cross-paper claim-evidence graph (Taproot)
summary: claim hubs (finding tagged TAPROOT:claim) aggregate many papers as typed evidence edges; [fi<id>] is a living citation that resolves to the current best originator(s) — find a hub, read its evidence, cite it
answers:
  - how do I search for an existing claim hub before minting a new one?
  - what does a bare [fi<id>] cite resolve to?
  - what's the evidence model behind a claim hub — how is it graded?
  - which taproot features are live vs still dark?
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
`contradicts` edge touching the hub blocks its nanopub mint, from ANY
counterpart ref kind and either direction — post-split (`docs/backlog/
disputes-edge-nonblocking-disagreement.md`), source-kind no longer
filters the gate, since `contradicts` is now adjudication-derived only
(Part 2, not built) and is itself the warrant for blocking. A milder,
non-blocking disagreement — an unadjudicated hunch, a review critique, a
possible scope mismatch — files `disputes` instead, which never fires
the gate regardless of source kind ([[precis-nanopub-help]] has the
scope). The originator (★) is **derived at read time**,
not stored — whichever supporter(s) the *other* supporters' citations
converge on (`src/precis/taproot/seniority.py::derive_evidence`, over
the held `cites` graph). No intra-supporter citation edge held → every
supporter stays `corroborates` (never guessed).

**Every hub hunts its own opposition.** The `conflict_search` worker
pass (dark until enabled) sweeps live claim hubs: LLM-negated
paraphrases of the claim are ANN-searched over paper/patent/hub
passages ("X has no effect" sits far from "X enhances" in embedding
space), the hits are verify-budgeted by `paper_rank` with a reserved
floor for low-prestige sources, and a confirmed opposing passage is
filed as a `disputes` edge (`meta.via='conflict_search'`). The hub's
`meta.conflict_search = {version, at, candidates_checked,
disputes_filed}` is the coverage ledger — "no known conflict as of
`at`, method `version`" is a checkable statement, not silence; a
missing or stale-version ledger means the hub was never swept by the
current method.

**A compound hub holds no direct evidence.** When a claim decomposes into
several atomic sub-claims, the bundling sentence gets its own hub — cite-able,
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

Evidence beyond what's agent-minted stays sparse for now — the
forward-chase passes that auto-discover corroborators run dark by
default; hub mint, evidence attach, reword-in-place, seniority
derivation, living-citation resolve, and the fisheye Claims ring are
all live today. See [[precis-taproot-mint-help]] for the write
contracts and [[precis-taproot-backfill-help]] for bulk `[pc]`/`[pa]`
conversion.

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
