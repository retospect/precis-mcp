---
status: draft
title: Graph node and edge trust state — origin, grounding, review and honest confidence
pillar: memory-graph
prio: high
---

# Graph node and edge trust state

## Motivation / why

Reto, 2026-10-05: findings are specific and have a trust axis; taxon,
concept, memory, dream output and their links are too loose. Approved
response: “This is good proposal. make it so.” The three independent
fields and rules below are decided. Human authorship is not authority:
a quick human note defaults to **hunch**, while **considered** is an
explicit act.

This change extends accountability across the existing mesh; it does not
replace findings with a weaker claim kind or invent another trust score.
The current [finding trust axis and trust ladder](../glossary.md#coined-terms)
retain their meaning. This document proposes storage/rollout slices, not
a new adjudication policy.

**Reto rulings recorded, 2026-10-05:** inventory and independent planning
review are complete; the orchestrator confirms all five policy decisions
below and **slice 1 is unblocked**. Independent Codex review is required
per slice. Keep this multi-slice item `status: draft` for owner-led gated
work, not automatic fixer pickup. Target R15 or a later approved gate;
**do not disturb R14**. This branch currently contains documentation only.
All schema work requires a NEW forward-only migration and the gated path
under [thresholds](../conventions/thresholds.md). The policy approval does
not bypass review of the concrete populated-table migration/defaults.

## In scope

### Existing owners and vocabulary

- [term-taxonomy](term-taxonomy.md): refs remain mesh nodes; flat tags
  remain the cheap membership index, not a replacement node graph.
  Keep `specialises/generalises`, not a new `is-a` synonym. The
  proposed edge-axis taxon and existing `instance-of/has-instance`
  vocabulary remain owned there.
- [knowledge-mesh](knowledge-mesh.md): use the existing eye ladder,
  lede-first node rows and stable handles; no new traversal verb.
  Measures keep their `tier`, conditions and scientific provenance.
- [graph-gardener](graph-gardener.md): proposal/review/apply boundaries
  and human protection still apply; merge/split never auto-apply.
- [local-mesh-upkeep §2](local-mesh-upkeep.md#2-review-ledger--one-reviewed-by-stamp-for-chunks-refs-and-links):
  reuse the **existing** `reviews` ledger and SHA-current review
  semantics, plus §2b's `revisions` stable-head history. Migration 0185
  already shipped these; the backlog still calls them a proposal.
- [curation-gate](curation-gate.md): use actor/run identity, `agentlog`
  and `touched` for write sets; this item owns universal origin, not
  another run-review loop. Human-content protection is independent of
  whether human content is well grounded.
- [taproot](../../src/precis/taproot/__init__.py): `motivated-by` and
  `disputes` carry no support; `establishes/corroborates` are evidence
  roles; citation-trust labels and the nanopub publish ladder remain
  separate. An existing role name alone is not proof of verification.

### Production inventory — read-only, 2026-10-05

Queries used `scripts/prod-psql --ro` (`agent_ro`, transaction-local
timeouts only). Observations at 14:25–14:30 UTC, not a single frozen
snapshot. No bodies, credentials, model calls or production writes.
Code checked at main `4b7d64594fbf9ebd5bce5a31b3baf7239ed4b413`.
Native Python search/symbol reads led discovery; served root `/app`
differs from this worktree, so local source decides code claims.

Counts include retired refs/chunks unless “live” is stated. NULL/unknown
provenance below measures the persisted actor column, **not** a guessed
new origin category. No requested node has `meta.origin`,
`meta.grounding`, `meta.review` or `human_verified_by` populated.

| Ref kind | All / live refs | NULL or blank/unknown set_by | Stored chunks / NULL set_by | Refs with any reviews / current approval |
|---|---:|---:|---:|---:|
| taxon | 108 / 108 | 108 | 108 / 108 | 0 / 0 |
| concept | 44 / 17 | 44 | 44 / 44 | 0 / 0 |
| memory | 14,120 / 11,824 | 14,120 | 11,492 / 11,492 | 0 / 0 |
| finding | 4,248 / 4,210 | 4,248 | 4,604 / 4,604 | 143 / 143 |

Broader rollout size: **465,727 refs, 458,863 NULL set_by**;
**4,160,385 chunks, 423,197 NULL set_by**. This corrects the blanket
“NULL on insert” premise: general `insert_ref` omits `set_by`, but
`ingest/db_writer.py` explicitly stamps refs and chunks (default
`system`), and some card paths stamp `agent`. Non-NULL actor is still
not an origin/grounding/review classification.

All **241,275 links** have non-NULL actor strings: `system` 156,824,
`agent` 84,446, `user` 5. These broad labels cannot safely distinguish
extraction, computation and generation. The five `user` rows are not
automatically “considered” or verified. No universal origin column exists.
361 links already use `meta.origin` for unrelated workflow labels
(`draft-backfill` 359, `directed-mint` 1, `manual-audit` 1):
do not repurpose that key.

All persisted link relations are inventoried below. NULL/blank/unknown
`set_by` is **0 in every row**. “Generic” means `agent/system/dream`;
“pin” means a non-NULL chunk endpoint, not a proof of support; “finding”
means a finding endpoint, not necessarily a canonical verified claim.
“Review” counts links with any ledger row, not inferred human approval.

| Relation | Rows | Generic actor | Chunk pin | Finding endpoint | Source-handle key | Verified-by key | Links with reviews |
|---|---:|---:|---:|---:|---:|---:|---:|
| authored | 154173 | 154173 | 0 | 0 | 0 | 0 | 0 |
| related-to | 56305 | 56305 | 4301 | 104 | 0 | 0 | 0 |
| cites | 17036 | 17036 | 6098 | 4160 | 0 | 0 | 0 |
| corroborates | 5367 | 5363 | 4999 | 5367 | 5364 | 2490 | 2490 |
| serves | 3955 | 3955 | 0 | 34 | 0 | 0 | 0 |
| supports | 970 | 970 | 0 | 963 | 0 | 0 | 0 |
| derived-from | 818 | 818 | 162 | 738 | 0 | 0 | 0 |
| establishes | 504 | 503 | 496 | 504 | 504 | 357 | 357 |
| supersedes | 366 | 366 | 0 | 0 | 0 | 0 | 0 |
| fixes | 277 | 277 | 0 | 0 | 0 | 0 | 0 |
| touched | 219 | 219 | 219 | 0 | 0 | 0 | 0 |
| motivated-by | 185 | 185 | 101 | 185 | 102 | 0 | 0 |
| quantifies | 170 | 170 | 170 | 0 | 0 | 0 | 0 |
| part-of | 138 | 138 | 0 | 0 | 0 | 0 | 0 |
| specialises | 110 | 110 | 0 | 0 | 0 | 0 | 0 |
| represents | 107 | 107 | 0 | 0 | 0 | 0 | 0 |
| conjunct-of | 104 | 104 | 0 | 104 | 0 | 0 | 0 |
| blocked-by | 104 | 104 | 0 | 0 | 0 | 0 | 0 |
| awaits-evidence | 84 | 84 | 0 | 84 | 0 | 0 | 0 |
| draft-of | 68 | 68 | 0 | 0 | 0 | 0 | 0 |
| raises-concern-about | 54 | 54 | 4 | 46 | 0 | 0 | 0 |
| refines | 36 | 36 | 0 | 26 | 0 | 0 | 0 |
| disputes | 28 | 28 | 0 | 27 | 20 | 0 | 0 |
| dossier-of | 23 | 23 | 0 | 0 | 0 | 0 | 0 |
| supported-by | 16 | 16 | 0 | 0 | 0 | 0 | 0 |
| realized-by | 8 | 8 | 0 | 0 | 0 | 0 | 0 |
| depicts | 7 | 7 | 7 | 0 | 0 | 0 | 0 |
| cited-in | 7 | 7 | 0 | 7 | 0 | 0 | 0 |
| see-also | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| has-requirement | 3 | 3 | 0 | 0 | 0 | 0 | 0 |
| corrects | 3 | 3 | 0 | 0 | 0 | 0 | 0 |
| superseded-by | 3 | 3 | 0 | 0 | 0 | 0 | 0 |
| tests | 3 | 3 | 0 | 3 | 0 | 0 | 0 |
| blocks | 3 | 3 | 0 | 0 | 0 | 0 | 0 |
| made-of | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| qualifies | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| derived-into | 2 | 2 | 0 | 0 | 0 | 0 | 0 |
| concern-raised-by | 2 | 2 | 0 | 2 | 0 | 0 | 0 |
| contradicts | 2 | 2 | 2 | 0 | 0 | 0 | 0 |
| plan-of | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| made-by | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| figure-of | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| copy-of | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| contrasts-with | 1 | 1 | 0 | 0 | 0 | 0 | 0 |
| has-figure | 1 | 1 | 1 | 0 | 0 | 0 | 0 |
| cited-by | 1 | 1 | 0 | 1 | 0 | 0 | 0 |

**Structural support check.** Physical rows in `specialises` (110),
`part-of` (138), `represents` (107), `realized-by` (8),
`made-of` (2), `qualifies` (2): **367 total; 0/367** have a resolvable
chunk pin, finding endpoint, or resolvable `pc/pk/fb` source handle;
0 have `verified_by` or review rows. Their only meta keys observed:
`part-of.role` (4), `realized-by.se_binding` (8). These are a conservative,
explicitly enumerated structural family, not a claim that every relation
is structural or every body lacks a citation. `generalises` is a
registered inverse with no physical rows; `is-a` and `measured-by` are
not registered relation slugs here. No physical `instance-of` rows were
observed. A future measured-by relation must follow term-taxonomy's
registry process, not be silently invented here.

**Dream and LLM writes today (code checked, not executed):**

| Producer / seam | What it writes and marks | Observed coverage / missing contract |
|---|---|---|
| `workers/dream_agent.py`, `data/prompts/dream-prompt.md` | Agentic MCP memory writes; prompt asks for `DREAM:speculative`, source handles and a link to the tick's agentlog. Agentlog source is `dream`; linked search/source nodes are traceability, not proof the inference follows. | 10,959 DREAM-tagged memories, all NULL set_by, no ref review rows. 1,999 dream agentlogs; 3,061 memories have 3,078 related-to edges to them. Counts overlap; a tag or missing run edge cannot identify all generation. |
| Dream acquisition / stub creation | `DREAM:acquire`; stub path may set `refs.meta.set_by`. The object can be a real source requested by a dream, not generated source text. | 4,872 DREAM-tagged papers, all NULL set_by and no ref reviews; never backfill their origin from the DREAM tag. |
| `handlers/memory.py` | `rule/warrant` inference metadata; public meta allowlist only `hook`; consolidation writes `DREAM:consolidated`, supersedes and moved links. | No universal origin/grounding/review fields. 396 structural-digest and 19 deep-digest tagged refs; 19 memory refs carry deep-review cost/date. Reviewer output is itself generated, not automatically reviewed. |
| `taproot/hub.py::mint_hub` | Finding `meta.source=taproot`, body, hub tags and identity; role links use actor stamps. | 3,714 finding refs with source=taproot; source is a pipeline label, not one of the new origins. |
| `workers/hub_refine.py::_verified_stamp` | Link `verified_by/at/verified_claim_sha`, support/caveats/source_handle; legacy trigger mirrors to reviews. | 2,847 link approvals currently match target SHA; 143 current finding approvals. Still not universal node/edge trust; confirm checker identity/version before projecting a new state. |
| `workers/llm_summarize.py` | Derived `chunk_summaries` with model/config metadata, not new human-authored facts. | Model metadata belongs to the derived artifact; it cannot upgrade source-node review/grounding by inheritance. No derived-artifact origin coverage count was requested or inferred. |
| `taxonomy/subjects.py` and taxonomy bootstrap | Computed `specialises` proposals; model-generated taxonomy candidates/frozen list provenance; glossary worker emits concepts with `source_glossary_version`. | 44 concepts carry that version, 77 taxa have `legacy_source`; neither proves actor origin or approved definition/use. |

**Reuse the ledger, but do not trust its missing fields.** Inventory has
181 approved/53 rejected chunk review rows, 2,847 approved link rows,
143 approved ref rows, and 115 approved/6 rejected measure rows.
Only 32 chunks, 2,847 links, 143 findings and 115 measures have an
approved row matching the current SHA (not a newest-verdict projection).
2,731 link approvals and all 143 ref approvals have NULL model; actors
include `hub-refine`, `flow`, `cites`, etc. NULL model therefore does
**not** mean human, despite the old schema comment. Explicit actor identity
and recorded act determine human review; incomplete legacy identity stays
unknown/unreviewed in the new projection until reconciled. Existing
approvals/verdicts remain intact as history.

Evidence: credential-free SQL and aggregate result files
`.scratch/trust-prod-{schema,inventory,markers,structural}*` in the task
worktree; SHA-256 manifest delivered with the ready artifact. One initial
read-only query failed on an ambiguous column, was qualified and rerun;
tables above use the successful receipt. No completeness claim about
unindexed external writers or hidden body citations.

### Decided trust contract — Reto, 2026-10-05

Every graph node and edge has three **independent** trust dimensions,
plus a separate confidence field for **all** origins:

| Field | Values | Meaning and required evidence |
|---|---|---|
| origin | human, extracted, computed, generated; unknown for legacy | Who/how it was made. Generated includes dreams and LLM inference. Writer actor and optional model/version/run are separate identities, not a quality score. Never SQL NULL; do not repurpose workflow `meta.origin`. |
| grounding | none, motivated, defined, sourced, verified | A provoking artifact is not evidence. **Defined** is a human-reviewed, considered definition, distinct from a pinned supporting passage/finding (**sourced**) or checker-confirmed source fidelity (**verified**). Defined is not a rung below sourced that a numeric comparison may admit as evidence. |
| review | unreviewed, machine-reviewed, human-reviewed | Who looked, independently of source fidelity; current review records who + UTC when, actor type, model/version where applicable and target/dependency SHA. A stronger model may machine-review; **only Reto** may mark human-reviewed. Rejection remains a separate ledger verdict. |
| confidence | hunch, considered, unknown | Separate from origin for humans and machines. Quick human notes default to hunch; considered requires an explicit recorded act. Legacy confidence is unknown, never SQL NULL. This is deliberate judgment, not calibrated accuracy. |

Reuse `reviews` for reviewed acts and `revisions` for explicit promotion,
demotion and correction history; no second competing reviews table.
An approved review is usable only when it matches the target and relevant
grounding versions. A proposal row is not a machine approval. Stored
`verified_by` strings are migration evidence, not a new authority.
Do not infer considered from human authorship, model size, deterministic
execution or a numeric probability. Separate confidence allows a hunch
to mature without rewriting origin.

1. **Admission and default reads.** Hide unknown, unreviewed generated and
   hunch material by default from the start of slice 3; no prolonged
   staging exemption. A generated term with a definition, at least one
   sourced use and a current approved review may be default-visible while
   **keeping origin=generated**, subject to the same explicit considered
   act and current-certificate checks as other admitted content. A sourced
   use pins an addressable passage/finding; co-occurrence or a naked URL
   is insufficient. Keep `proposed/systematic` taxonomy maturity separate.
   Headers always state the hidden count and the **one explicit switch**
   to include hidden content; included content is visibly marked. Human
   queue/review views can list hidden items. Hiding deletes or demotes
   nothing in storage.
2. **Support and shared exclusion.** Only verified evidence edges carry
   support. Trusted endpoints never upgrade a loose edge. A single shared,
   typed evidence-eligibility predicate excludes **definition-only**
   grounding in taproot support counting, the nanopub publish gate and
   draft `cite-findings-only` checking, alongside their existing hypothesis
   exclusions and stricter evidence rules. No copied per-caller filters.
   `refines` is in the evidential relation class for this gate but remains
   advisory: the new classification does not make it carry support.
3. **Defined structural navigation.** Class relations as structural
   (e.g. existing `specialises`/`generalises`, `part-of`, and registered
   equivalents of is-a, measured-by or synonym-of) versus evidential
   (`establishes`, `corroborates`, `contradicts`, `refines`). Preserve
   existing slugs, inverses and non-support roles; examples do not authorize
   new relation names. A defined node/edge supports default navigation
   over **structural relations only**. It requires a current human review
   by Reto and confidence=considered, and renders **“definition (who, UTC
   date)”**, never as a sourced claim. A loose structural proposal remains
   generated/unreviewed; never relabel historical origin to repair it.
   At link time refuse an evidential edge whose only grounding is a
   definition, naming this rule in the error. Definitions later backed by
   a finding acquire a **separate sourced/verified grounding** with its own
   pins and acts; do not upgrade/overwrite the definition in place.
4. **Review by exception.** Queue generated/hunch nodes used by a draft,
   a quest, or **at least 5 other nodes**, plus nodes whose grounding
   changed and all their actual consumers (“review everywhere it is used”).
   Count distinct dependent nodes, not duplicate edges. A stronger model
   may machine-review; human-reviewed is reserved to Reto. Sample **50
   nodes per origin per month**, including unknown as a separate legacy
   stratum, and report an error rate per origin. Dependency threshold,
   sample size and cadence are configuration, not fixed constants. A
   smaller population is reported with its actual denominator, never
   padded by duplicate samples. No model run is authorized by this spec.
5. **Recorded transitions.** Trust only ratchets up by a recorded act;
   demotion is explicit and recorded. Changed source content invalidates
   the old certificate for reads, queues affected consumers and records
   an invalidation/demotion event atomically with the change or its
   reconciliation. Preserve prior acts, addresses, definition groundings
   and revisions. Filtering is not a storage demotion.

Grounding is typed support, not one total ordering: a node can retain a
`defined` grounding and acquire a separate sourced/verified grounding.
Evidence consumers examine the evidential grounding and its current
certificate, not the presence or absence of a definition on the same node.
A definition-only path can never launder support into a visible claim.

### Ordered slices and acceptance gates

**0. Inventory and design review — complete.** Read-only tables and write
seams above; independent Codex planning review PASS at
`3d6552e4a337605d25a1df80af4d9230fe30dcd3`. Reto's five decisions below
supersede the initial inventory/decision hold. This is not a runtime ship.

**1. Non-NULL origin AND confidence on every refs/links write; legacy unknown.
GATED SCHEMA; unblocked for the gated path, R15 or when ready.**

Reto's physical-scope ruling, 2026-10-05 (~19:50Z as relayed): **refs and
links only**. No chunk columns or backfill; no demonstrated measure consumer
needs new origin/confidence fields in this slice. Deferred domain tables
are not trust-filtered. The five policies and slices 2–4 remain unchanged;
this is a smaller first storage slice, not universal graph enforcement.

- Stamp all writers of `refs` and `links`, including raw-SQL ingest,
  bulk/COPY, workers, nested SQL functions and plugin-to-mesh materializers.
  A deferred table's writer enters scope when it mints or changes a ref or
  link. No parent/endpoint origin or confidence inheritance.
- Extend the transaction-scoped write context with origin, confidence and
  attributable explicit acts. Quick human notes use human+hunch; generated
  hypotheses use generated+hunch even when requested by a human. Considered
  requires an explicit recorded act; never infer human from token owner.
- Dedicated constrained columns and the existing ref/link revision ledger;
  no new natural-key target registry, plugin schema or per-association
  ledger. Body edits affect the owning ref's content version without
  stamping 4.16M chunk rows. Preserve old actor evidence and workflow meta.
- Legacy origin AND confidence are unknown, without guessing. Additive
  compatibility defaults also mark unconverted writes honestly unknown;
  measure and close this bounded window through per-writer conversion.
  Strict guards then refuse missing context. A cluster-wide writer fence
  is not required by the proposed rollout; exact bounded-lock acceptance
  and compatible rollback still gate implementation.
- Acceptance: complete in-scope writer coverage, direct-SQL/NULL/invalid/
  bare-considered refusal, pooled transaction isolation, recorded acts,
  content-neutral sealer retention, unknown legacy/rerun preservation and
  mixed-version cutover. Detailed cases and deferred scope are below.
- Independent Codex review, focused canonical tests/types/Ruff, throwaway
  migration dry-run/apply plus scheduled integration gate before shipping.
  No R14 integration, deploy or full suite outside coordinator scheduling.

Concrete slice-1 gate contract: [storage, writers and cutover](#slice-1-concrete-populated-table-gate-contract).

**2. Shared evidence-exclusion predicate and defined grounding.
GATED IF SCHEMA.**

- Extend existing target/SHA registry and `reviews/revisions` to the
  supported graph entities, typed source pins and source-revision
  dependencies; pin trust-metadata hash behavior so approvals do not
  invalidate themselves. Reto-only human review must be authenticated,
  not a caller-provided model=NULL or actor string. Machine review remains
  distinct and cannot self-assert human identity.
- Class structural/evidential relations once, resolving inverses centrally.
  Store defined as a separately addressable grounding act with its human
  reviewer, considered act and UTC date. Later sourced/verified grounding
  adds its own source/certificate without replacing that definition.
- Implement **one shared evidence-eligibility predicate** used by taproot
  support counting, nanopub preflight/publish and draft cite-findings-only.
  Definition-only material is excluded by type, as hypotheses are excluded
  by predicate; all existing stronger evidence/verification rules remain.
  Structural visibility never implies scientific support. `refines`
  remains advisory despite membership in the evidential gate class.
- Link-time validation rejects an evidential edge grounded only in a
  definition before persistence, with a named rule and actionable error.
  Preserve ordinary structural links, endpoint/cycle guards and existing
  non-support relations. Human hunch cannot mint authoritative defined
  grounding. Render defined as “definition (who, UTC date)”.
- Include the lighter term/concept admit act (definition + at least one
  pinned sourced use + current approved review), without replacing the
  taxonomy proposed/systematic lifecycle or finding/adjudication gate.
  Admission preserves generated origin and requires explicit considered
  confidence for default visibility.
- Required regression tests: **each of the three evidence consumers**
  rejects a definition-only node/edge via the shared predicate; link-time
  definition-only evidential create is refused with no persisted edge.
  Cover inverse relation spelling and attempted use as either endpoint,
  a defined structural navigation edge, human hunch/refused human identity,
  and separate later sourced/verified grounding without in-place upgrade.
  Existing eligible evidence remains eligible, refines still carries no
  support, stale target/source review fails, and every act survives retries.
- Independent Codex review before this slice lands; no model run required.

**3. Default read/traversal filtering with hidden counts.**

Apply one shared visibility policy to search, snippets/cards, +1hop/+2hop
and graph walks; use slice 2's evidence predicate for evidence consumers,
not a looser structural navigation rule. From rollout, default reads hide
unknown, generated-unreviewed and hunch content. Admitted generated terms
retain origin=generated and may be visible; human-reviewed considered
**defined** groundings enable structural navigation only. No long staging
period and no storage deletion/demotion to implement hiding.

Every response header gives the unique hidden count and the one explicit
include-hidden switch; opt-in results visibly name trust state. Human
queue/review views can list hidden items. Count overlapping reasons once;
apply policy before final ranking/pagination and prevent hidden intermediate
nodes from laundering support. Bounded ANN counts must say “hidden in this
candidate set” rather than claim a global total. Do not leak unauthorized
bodies via counts/snippets. Direct-handle reads of hidden targets direct the
caller to the same explicit switch/review access; no new verb.

Acceptance mixes unknown, hunch, unreviewed generated, admitted generated,
current sourced/verified and defined structural fixtures. Verify counts,
zero-hidden headers, the single opt-in switch, stable pagination, review
access and state labels, with storage unchanged. Tests distinguish
structural navigation from evidence eligibility. Independent Codex review.

**4. Review queue and sampled audit; no always-on review sweep.**

Queue generated/hunch nodes used by any draft or quest, or at least 5
other nodes; grounding changes queue affected nodes and all actual uses.
All thresholds are configuration with the decided defaults. Deduplicate by
target+dependency snapshot so one change queues each consumer once. Reuse
gardener's proposal discipline and upkeep's ledger/auto-apply protections;
Reto alone marks human-reviewed, while stronger models may machine-review.

Sample 50 nodes per origin per month (configurable size/cadence), unknown
as a separate stratum; report per-origin errors/sample size, sample rule,
confidence interval and stale/unevaluable counts. Human-adjudicated checks
are correctness evidence; model agreement is labelled agreement. Record
actual denominators for small populations and abstentions. Make the error
rubric/version inspectable; this spec does not authorize paid audit runs.

Tests cover draft use, quest use, 4 versus 5 distinct dependents, duplicate
edges not inflating dependency, unused hunch not flooding the queue,
changed grounding reaching all consumers, configurable thresholds/cadence,
low-degree sampling, small strata and actor authority. Independent Codex
review before shipping; operational model/service holds remain in force.

### Slice 1: concrete populated-table gate contract

**Narrowed review proposal, not migration SQL.** Reto's 2026-10-05 physical
ruling supersedes the previous 69-core/ten-SE-table proposal. Source checked
in this task worktree at `2c6bd5c8354f569f88c7cbefd98898b132667f9d`;
only documentation changes here. Native Python search/symbol lookup located
`best_measure`; local owning code confirms the consumer and ref body hash.
The served index is discovery evidence, not proof of this branch or deployed
implementation. The original 14:25–14:30 UTC inventory remains historical;
no new production audit, schema allocation, backfill or runtime work.

#### Storage boundary and column shape

**Exactly two stamped tables: `refs` and `links`.** Every kind using these
tables is in scope, even a CAD/PCB/plugin-owned ref. Domain tables themselves
are excluded. `revisions` changes only to record acts for its existing
numeric ref/link targets; it is an audit ledger, not another stamped node.
No `chunks`, `measures`, association or plugin columns in this slice.

| Column on refs and links only | Type / permitted values | Compatibility default | Strict-write contract |
|---|---|---|---|
| `trust_origin` | non-NULL text; human, extracted, computed, generated, unknown | unknown | Known producer for ordinary new assertions; unknown only for recorded legacy/import/carry exceptions. |
| `trust_confidence` | non-NULL text; hunch, considered, unknown | unknown | Typed writer supplies hunch unless an explicit considered act is attached; legacy may remain unknown. |
| `trust_write_version` | non-NULL small integer; 0 or 1 | 0 | 0 means unstamped legacy/compatibility content and requires unknown/unknown. 1 means this write passed the contract, not a trust rank. |

Use NOT NULL and finite-vocabulary/version-pair CHECK constraints. Keep the
constant defaults for mixed-version compatibility; strict DB guards refuse
an ordinary write that falls back to version 0, even while these defaults
remain. Defaults are not authority to bypass guards. Reject caller-supplied
NULL, invalid enums and considered without a matching act in both modes.
Unknown origin plus considered is permitted on legacy content only after an
explicit act: consideration does not reconstruct origin. `set_by`, workflow
`meta.origin`, scientific `tier`/`trusted` and existing review meanings stay
separate. No origin-only indexes, grounding/review columns or read filtering
in slice 1; slice 3 owns measured read-index decisions.

**Why no chunk columns:** memories, findings, taxa and concepts are refs;
chunks hold their bodies. Existing `precis_ref_sha_of` combines ref content
with `precis_body_sha`, which hashes live `ord >= 0` body text. That gives
one content-versioned trust subject for each requested node. Existing
`precis_chunks_body_revision` only covers registered kinds and INSERT/DELETE:
it is a reuse seam, **not proof of complete enforcement**. The implementation
must cover semantic body changes, retirement/moves and direct SQL for every
in-scope ref, including kinds currently outside `covered_meta`, through a
parent-ref act/invalidation guard. No chunk provenance column, scan/backfill
of 4.16M rows, or body rewrite is needed. Mutation-time guards on chunks are
allowed solely to protect the owning ref, not to stamp chunk records.
Derived cards/keywords/summaries cannot silently change the ref's origin or
be presented as independently considered passages; independent passage/card
trust is deferred. A ref stamp certifies only that ref's declared content
projection, not every derivative attached to it. New trust-version coverage
must not retroactively change existing review certificates; use a separate
explicit-act projection if the current review hash omits semantic fields.

**Why no measures columns:** `best_measure` and quest `view='measures'`
already use the newest current measure review and expose `review_state`.
Those are real mesh consumers, but neither currently needs new origin/hunch
columns to keep that contract. No specific new measure consumer requirement
was established for slice 1. Keep existing measure review/SI behavior;
`mx` is not silently treated as a trusted ref or included in new filtering.
A measured result materialized as a finding/ref or link is stamped there.

| Deferred storage / projection | Slice-1 disposition and mint boundary |
|---|---|
| Chunks as independent passage/card targets; chunk summaries and tag/membership rows | **Deferred, not trust-filtered; revisit only with a concrete consumer.** Bodies participate in the owning ref's trust-version guard above; no independent inherited chunk/tag trust. |
| Measures, compatibility value views, property/category definitions | **Deferred, not trust-filtered; revisit only with a concrete consumer.** `precis_measure_taxon` creates taxon refs and links: those writes are enforced at mint, not the measure row. |
| PCB, structure, design, checklist, parts, CAD tables | **Deferred, not trust-filtered; revisit only with a concrete consumer.** No guards on domain-only writes, stage/swap or nested objects. Lazy part refs, CAD refs, generated findings and graph links still pass mesh guards. |
| Source-binary tables, citation/identity tables | **Deferred, not trust-filtered; revisit only with a concrete consumer.** No stamps on binaries, identifiers/bylines/citations. Acquisition/import ref and link creation is in scope. |
| Nanopub artifact/mirror/proof tables | **Deferred, not trust-filtered; revisit only with a concrete consumer.** Claim refs and materialized evidence links remain in scope. Slice 2's nanopub evidence-consumer predicate tests are unchanged. |
| All SE plugin tables and other plugin-only domain tables/selectors | **Deferred, not trust-filtered; revisit only with a concrete consumer.** No plugin migration or enforcement dependency. Any plugin-created ref/link uses the core contract. |
| Embedded associations, external file/version keys and derived relations not stored as links | **Deferred, not trust-filtered; revisit only with a concrete consumer.** No per-association/natural-key ledger. They do not convey reviewed evidence by inheritance; a persisted link gets its own stamp. |

Operational/authentication/cache-index/lease tables remain implementation
state, not reviewed assertions. Their producers must stamp any resulting
ref/link. This limited scope does **not** prove trust enforcement over every
possible graph projection or domain object. Deferred objects are neither
deleted nor promoted; later readers must not label them considered/sourced
by copying a nearby ref's state. Reto's five policies and slices 2–4 retain
their intended behavior; no deferred domain-table rollout is smuggled into
slice 1 to anticipate those later consumers.

#### Reuse revisions for explicit ref/link acts

Current revision targets are already numeric ref/link IDs, one row per
target per transaction. Retain that identity and unique target/xact index;
no target-kind expansion, `target_key`, digest index or association adapters
are needed now. Extend `revisions` with a non-NULL ordered `trust_acts`
JSONB array (legacy default empty) and a `trust-recorded` event for a
trust-only transaction. Preserve actor/model/reason, DB UTC time, prior
state, ordinary content events and existing SHA interpretation. Audit rows
survive deletion of their targets. No second review ledger.

Each act records operation ID, target and expected content version, action
(create, carry, revise, consider, correct-origin, demote, legacy-import),
before/after origin and confidence, authenticated actor/type, model/run
when applicable, reason and DB UTC time. A protected append/aggregate path
validates shape and keeps the first prior state while appending same-xact
acts in order. Under the target lock, reject stale versions; same operation
ID/payload returns its receipt, a changed payload conflicts. Later
transactions cannot rewrite earlier acts; direct arbitrary ledger DML is
not an ingress. DB guards and deferred validation require the matching act
before committing a version-1 stamp or considered state. Rollback/no-op
upserts cannot manufacture successful acts.

Considered is an explicit command/flag payload, including on create; it
requires a reason and expected content version. It never follows merely
from human authorship, a deterministic calculation, a stronger model or
review completion. Legacy origin may remain unknown after consideration.
No side effect marks human-reviewed, sourced, defined, verified or published.
Substantive replacement records its producer and new confidence intent;
considered cannot survive solely because the ref ID did. Assertion-preserving
carry names its predecessor; operational-only changes and true no-op upserts
preserve the state. Guards classify semantic fields and body operations
explicitly; an unknown new field is not a bookkeeping exemption.

**Preserve the reviewed sealer correction:** `precis_revisions_seal()` must
retain every revision with nonempty trust acts even if edit/undo makes its
final content SHA equal its initial SHA. Coordinate sealing and deferred
validation; empty content-neutral edits retain existing no-op behavior.
Only a future NEW migration may replace sealed 0185 functions.

**Prior TRUST-S1-KEY-1 remains closed at contract level.** Same-root PASS
at `2c6bd5c8354f569f88c7cbefd98898b132667f9d` approved full identity off-index,
bounded nonunique digest lookup, fresh post-lock full equality and
collision-safe ordered acts. The exact design remains in that revision's
spec/history, with long-key/equivalent-key/forced-collision/concurrency
acceptance. It is **deferred entirely**, not replaced with an unsafe index:
refs/links have bounded numeric identities. Do not add or test natural-key
machinery in slice 1. Any future concrete non-ref consumer must reuse or
explicitly re-review that contract; no SQL or concurrency proof was claimed.

#### Writer coverage and transaction boundaries

The closure is **every writer of refs or links**, including indirect body
changes to a ref's declared semantic content. It is not every writer of
all physical tables. Extend `revision_context`/`PrecisPool`, preserving
transaction-local actor/context isolation. Existing write-verb wrapping and
pool-checkout-only application do not cover get/search side effects,
already-acquired connections or autocommit; enforce on the actual write
transaction, with direct-SQL DB guards as the common boundary.

| In-scope write family | Required conversion / evidence |
|---|---|
| Ref/link primitives and raw ingest | `_refs_ops`, `_links_ops`, raw ref SQL in `ingest/db_writer.py` and `pres.py`, raw links in `_draft_ops`/draftimport. No exempt bulk/COPY or caller-supplied `conn=`. |
| Body-as-ref mutations | `_chunks_ops`, ingest replacement, worker/body writers and raw chunk SQL must record/invalidate the owning ref's semantic state. Guard INSERT/DELETE plus semantic UPDATE/move/retire effects, including both old/new owners; derived-only bookkeeping has an explicit exclusion. |
| Read-side acquisitions and caches | `_cache_ops`, `_cache_base`, `_good_search`, paper/provider acquire paths: stamp the ref/link at creation even under get/search. Extracted content and generated conclusions use distinct production contexts. |
| Workers/jobs/agents | Dream, chase, glossary, dispatch/executors, alerts, agentlog and any other output using core Store or raw ref/link SQL. Nonsemantic counters/queue updates preserve state; no model/worker execution is authorized by this inventory. |
| Domain/plugin-to-mesh materialization | `_pcb_ops.ensure_part_ref`, domain ref writers in CAD/structure/material/component/rxn, SE/bio/chem ref mutations, publication/evidence materializers. Only ref/link effects enter enforcement; domain-only stage/swap, se save_tree and catalogue rows do not. |
| DB functions/triggers and imports | `precis_measure_taxon`/nested legacy-value functions in 0188, `file_gripe_readonly`, seed/operator import functions and any plugin trigger minting refs/links. Context propagates into nested statements; change existing functions only in a NEW migration. |
| Ingress and connection modes | MCP/CLI/web/bots carry authenticated producer context down to the writer. Handle COPY/executemany/direct psycopg, nested override, savepoint, retry and new transactions on reused connections; reject an unsupported autocommit mutation rather than silently lose context. |

Static candidate destinations were previously inventoried; narrow that
manifest to `refs`/`links`, classify semantic versus bookkeeping mutations,
and reconcile at the chosen implementation assembly. Candidates are not
executed coverage. The implementation gate must include all registered
core/plugin producers touching those tables, dynamic SQL and nested DB
functions, plus body mutation guards. A new ref/link writer without a
converted adapter/test fails closure; a new domain-only table does not.
No full-schema/table registry or installed-plugin domain audit is required.
Raw SQL cannot bypass provenance by writing valid enum strings. A custom
GUC or ContextVar is transport, not authenticated identity; freeze trusted
ingress/DB-role capabilities before implementation. Shared MCP credentials
do not prove a human authored the payload. Ordinary typed writer confidence
defaults to hunch; DB compatibility defaults remain unknown, never human.

#### Legacy backfill, cutover and rollback limits

**Smallest proposed cutover: additive expansion, rolling per-writer
conversion, then an atomic strict boundary for mesh writes. No planned
cluster-wide writer fence or service maintenance window.** Brief bounded
DDL/guard locks are still necessary; actual lock protocol/budgets and
mixed-version races require the populated-table gate. If measurements
cannot meet those budgets, stop and request a targeted mesh-write pause;
do not silently expand to cluster-wide downtime. Read filtering still
starts immediately in slice 3; this write-compatibility window does not
change that policy.

1. **Prepare on a representative throwaway DB.** Reconcile the two-table
   writer closure at the exact compatible assembly. Measure ref/link DDL,
   constraint validation, body-guard and ledger costs. Freeze abort/retry
   lock budgets and the strict-switch transaction protocol before SQL.
   No optional-plugin migration ordering is needed for domain tables;
   plugin ref/link producers do need compatible core-context integration.
2. **Expand with honest defaults.** Add the three constant-default columns
   to refs and links and audit support to revisions through future NEW
   forward-only migrations. All existing rows, including retired rows,
   logically read unknown/unknown/version 0. Verify metadata-only default
   behavior and constraints on the throwaway copy; do not assume every
   ALTER is cheap. No chunk or domain-table backfill, origin guessing or
   corpus body rewrite. Any necessary physical reconciliation is bounded,
   keyset-resumable and conditional on version 0; never reset newer acts.
3. **Compatibility mode and per-writer conversion.** Old writers may
   create unknown/unknown/version-0 refs/links or semantically change still
   unstamped rows; these writes are counted separately from pre-expansion
   legacy rows, with a bounded rollout deadline. Converted writers produce
   version 1 and acts. An unconverted writer cannot semantically overwrite
   a version-1 assertion or its body: refuse with an actionable context
   error, never preserve stale considered state or silently demote it.
   Bookkeeping/no-op updates preserve existing stamps. Considered/invalid
   bypass guards apply from expansion, not only after cutover. Measure
   compatibility activity by writer/path where attributable; unattributed
   SQL remains an explicit unresolved writer, never evidence of closure.
4. **Strict boundary without stopping unrelated writes.** Once all
   in-scope writers are converted and exercised, coordinator activates the
   strict guards atomically. The chosen DB switch must serialize with
   guarded ref/link/body-write transactions, so a transaction admitted in
   compatibility mode either finishes before the boundary or is refused/
   retried under strict rules; none may commit a late version-0 assertion.
   Lock waiting is bounded and a failed switch leaves compatibility mode
   intact. Version heartbeats, MAX(id) or a quiet sample alone do not prove
   this race closed. Domain-only writers and readers need no global fence.
   Retain harmless compatibility defaults behind strict guards; any old
   writer missing context now fails visibly, including nested function,
   COPY and body paths. Exact lock/race proof is an implementation gate,
   not claimed by this document.
5. **After cutover.** Ordinary new assertions require known origin and
   hunch or explicit considered. Restricted, reasoned legacy/import/carry
   acts may keep origin unknown. Completion reconciles ref/link version
   counts and acts, separately reporting compatibility-window unknowns;
   those rows are not guessed into known provenance. Existing unknowns
   remain unknown until a recorded act, with later slice-3 visibility.

**Rollback limits:** before strict mode, a legacy binary may write only
under compatibility rules and cannot edit already stamped semantic content;
that limitation is visible, not a promise of unrestricted old-binary
rollback. After strict mode, roll back only to a tested context-capable
binary. Missing-context writers fail even though constant defaults remain.
Do not disable guards, reopen compatibility, change sealed migrations or
remove audit to make an old binary work. Failed additive stages resume by
recorded stage/key; failed strict activation aborts atomically. Forward
repair and compatible application rollback need throwaway rehearsal;
backup restore remains a separate disaster-recovery decision. No service,
SQL, production backfill or maintenance action is authorized by this spec.

#### Slice-1 acceptance for the populated-table gate

Required future receipts; **none of these DB/runtime tests executed here**:

- **Scope/closure:** exactly refs/links stamped, revisions audit-only; no
  chunk/domain/plugin columns, backfill or domain-only write enforcement.
  Every direct, bulk, indirect/raw-SQL and installed-plugin ref/link writer
  is reconciled at the assembly SHA. Deferred-table-to-ref/link minting
  is guarded; domain-only operation still works without trust context.
- **Values/identity:** quick human note human+hunch; generated output under
  human request generated+hunch; quotation extracted, calculation computed.
  Missing context after strict cutover, NULL/invalid values, enum-only
  considered, forged human/legacy capability and arbitrary ledger DML fail.
- **Body/ref boundary:** all requested kinds including uncovered-meta kinds;
  body replace/move/retire and raw SQL cannot leave a stale considered ref.
  Both owners checked on move; content hashes exclude trust metadata;
  existing review certificates not rewritten. Derived card/keyword updates
  do not relabel the source ref or gain independent considered status.
- **Transactions/acts:** no-op identity preserved; passed connection,
  autocommit handling, pooled reuse, nested rollback/restore and retries
  isolated. Ref/link numeric-target same-xact acts retain order and first
  prior state; concurrent operation replay is idempotent or conflicts on
  changed payload, never loses an act. Stale expected version is refused.
- **Considered/sealer:** explicit create/legacy consideration with reason;
  demotion recorded; origin can stay unknown. Edit → consider/demote → undo
  retaining initial content SHA still commits every trust act. Empty
  content-neutral edits retain old no-op behavior. No review/grounding/
  publication promotion. No natural-key/digest-index work in this slice.
- **Compatibility/cutover:** untouched legacy and old-writer new rows read
  unknown/unknown, distinguished in receipts. Old writer may change only
  version-0 semantic content; converted writes get acts; attempts to alter
  version-1 content/body fail without context. Exercise transactions that
  straddle the strict switch, nested functions, COPY, lock-budget abort,
  converted-writer rollback and old-binary refusal after cutover. No late
  compatibility commit; unrelated domain writes continue.
- **Backfill/limits:** ref/link counts/IDs/content and old actors preserved,
  retired rows included, no inference from DREAM/set_by/model NULL. Batches
  resume without resetting version 1. Measure locks/WAL/runtime and ledger
  overhead on a representative throwaway copy; no production audit rerun
  or corpus mutation as substitute proof.

Independent contract and implementation reviews, focused canonical
`scripts/test`, scoped types/Ruff, throwaway migration dry-run/apply and
coordinator-scheduled integrated gate remain required before shipping.
Slice 2's shared predicate and three-consumer/link-refusal tests are
unchanged. R14 and unrelated holds remain untouched.

#### Remaining implementation gates after the scope ruling

Physical scope is **settled: refs/links only**, no plugin-table migration
order. Do not re-ask the five policy questions. Remaining concrete gates:

1. Approve exact core DDL/constraints, numeric ref/link ordered-act guard
   and sealer/parent-body trigger plan against the chosen assembly. No
   natural-key design approval or full-schema provenance map is needed.
2. Freeze the additive typed provenance/considered payload on existing
   put/edit paths, authenticated ingress/DB-role capability and receipt/
   error shape. Explicit expected content projection must cover semantic
   changes without rewriting existing review SHA semantics.
3. Name the first compatible assembly and complete in-scope writer closure;
   approve measured lock/batch budgets, compatibility deadline/counters,
   strict-switch serialization and tested rollback limits. A targeted
   mesh-write pause is a contingency only if staging rejects online bounds.

## Explicitly NOT in scope

- Bypassing the populated-table migration gate, independent review per
  slice, or coordinator scheduling; R14 stays untouched.
- This branch makes no schema/product writes, migrations, backfills,
  service changes or model runs. No production trust assignments.
- No new source of scientific truth, trust number, graph engine, kind,
  verb, replacement review ledger or blanket corpus-wide model review.
- No automatic human-authority promotion, generated-origin laundering,
  auto-merge/split, or bypass of taproot publication/adjudication holds.
- No guess about historical origin from set_by, DREAM tags, model NULL,
  provenance URLs, provider name or agentlog absence.

## Acceptance criteria

The ordered slice criteria above are the implementation gates. For this
planning delivery: successful read-only inventory receipts with exact
counts and definitions; per-relation table; actual producer/write-seam
evidence; decision-preserving spec plus the five recorded Reto rulings;
all required owners cross-linked, glossary and thread updated; independent
Codex document review and exact pushed SHA ready artifact. No runtime
change is claimed by this item's review.

Before release of the eventual implementation: every in-scope mesh
writer enforces origin and confidence, legacy stays honestly unknown, source/current-review
proof controls read eligibility, default/opt-in counts are accurate, loose
links do not transmit trust, admission works independently of finding
gates, and review/invalidation/error-rate receipts cover downstream uses.
Each schema slice gets migration dry-run/apply on a throwaway DB and
scheduled integration gates; nothing here authorizes the populated-table
migration merely because the final design was approved.

## Target + blast radius

Slice 1: refs/links plus their revision audit and parent-ref body guards;
no chunk/domain-table columns or natural-key association ledger. Later slices: ingress/ingest, taxonomy, concept/memory/finding handlers,
dream/LLM/computed producers and derived projections; reviews/revisions
target hashes; universal rows/search/working sets/eye traversal; draft/
quest evidence consumers and gardener/curation exception queues.
Refresh owning docstrings and user skills with each shipping slice;
this documentation-only branch adds no runtime declarations.

## Open questions / decisions log

**Reto via orchestrator, 2026-10-05 — all five policy questions resolved:**

1. **Confidence:** separate `hunch | considered | unknown` field for all
   origins. Legacy unknown; considered is an explicit recorded act.
2. **Generated admission:** YES, a generated term with a definition,
   sourced use and current review can be default-visible while keeping
   origin=generated. Admission does not rewrite authorship.
3. **Definitional exception:** YES. Add `defined` grounding, human-reviewed
   and considered, for structural navigation only. A shared typed predicate
   excludes definition-only material in taproot support, nanopub publication
   and cite-findings-only; link time refuses definition-only evidential
   edges. Mark “definition (who, UTC date)”. Add later sourced/verified
   grounding separately, never upgrade the definition in place. Tests for
   evidence consumers and link refusal are mandatory.
4. **Cutover:** hide liberally from the start, without a long staging
   period; headers always give hidden counts and the one include switch,
   and human queue/review views can list hidden items. No storage deletion
   or demotion as a side effect of filtering.
5. **Review operation:** stronger models may machine-review; only Reto
   marks human-reviewed. Queue draft/quest-used nodes or nodes used by at
   least 5 others; audit 50 nodes per origin per month with per-origin
   error rates. Thresholds are configuration, not constants.

**Execution order:** slice 1 origin+confidence write-time non-NULL and
legacy unknown → slice 2 shared evidence exclusion + defined grounding
→ slice 3 default reads/counts → slice 4 review queue/audit. Independent
Codex review per slice. **Slice 1 unblocked**, migration through the gated
path for R15 or when ready; do not disturb R14. Earlier planning-only
holds and unresolved-policy wording are superseded by this decision.

**Remaining implementation gate, not a request to revisit these rulings:**
the [slice-1 concrete contract](#slice-1-concrete-populated-table-gate-contract)
now proposes exact physical targets, columns/defaults/constraints, writer and
ref/link writer closure, considered acts, and bounded rolling cutover. Reto
resolved physical scope on 2026-10-05: refs/links only; deferred domain
tables are not trust-filtered. The three remaining implementation gates
require concrete review before migration SQL is added. No migration number is reserved in this documentation update. Slice 2 must separately gate the
multi-grounding storage representation. Model runs and production schema
application remain outside this owner task's authorization.

**Physical scope ruling, 2026-10-05 (~19:50Z as relayed):** narrow slice 1
to refs and links. Chunk trust can be carried at ref level; no new measure
consumer requires extra columns now. All listed domain/plugin/association
storage is deferred, not trust-filtered, revisited only with a concrete
consumer. Enforce every in-scope writer, including raw SQL and deferred-
table-to-mesh minting. Replace the proposed cluster-wide fence with bounded
additive compatibility and per-writer conversion, subject to the measured
strict-switch gate. Five policies and slices 2–4 are unchanged.
