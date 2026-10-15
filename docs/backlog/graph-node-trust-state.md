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
**do not disturb R14**. Slice 1 is built in compatibility mode (migrations
0214/0215); see its entry below for what remains.
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
BUILT in compatibility mode (tree `mesh-trust`, 2026-10-15; migrations
0214/0215). Strict mode and the remaining writer closure are open (below).**

Reto's physical-scope ruling, 2026-10-05 (~19:50Z as relayed): **refs and
links only**. No chunk columns or backfill; deferred domain tables are not
trust-filtered. The as-built contract lives in the 0214 header and
`precis.store.revision_context`; in short:

- `refs`/`links` carry `trust_origin`, `trust_confidence` and
  `trust_write_version` (constant defaults `unknown/unknown/0`, CHECKs added
  NOT VALID, validated by 0215 in its own transaction). Triggers derive the
  stamp from the write context (`precis.trust_origin`, `.trust_actor_type`,
  `.trust_act`). Raw trust values, NULL, invalid origins and a bare
  considered value are refused.
- A converted writer stamps `<origin>/hunch` v1 on create and on every
  semantic change, including a live-body change through the parent-ref chunk
  guard (both owners on a move). Derived `ord < 0` chunks and appended
  timeline entries are excluded: comments, job events, summaries and
  results, run and quest logs, review notes, figure/diagram turns and
  tag-overflow storage (`precis_trust_subject_chunk`). An
  unconverted writer writes v0 and is refused on v1 content. Retire/restore
  and bookkeeping meta keep the stamp.
- Conversion is per kind: only refs of the converted kinds
  (`precis_trust_kind_converted`, pinned equal to the handlers that set
  `trust_origin`) and links with a converted end take a stamp or an act. A
  door that also writes a paper leaves the paper v0, so the paper writers
  (enrichers, ingest, `edit(kind='paper')`) are not refused. Converting a
  kind is a migration that replaces that list.
- Acts (consider, demote, correct-origin, legacy-import, carry, plus the
  implicit create/revise) land in `revisions.trust_acts`, in order, under
  the row lock. A stale `expected_sha` is refused, a same-op replay records
  nothing, and a changed payload conflicts. The sealer keeps rows that carry
  acts and drops phantom create acts from upserts. Content shas exclude the
  stamp, so existing review certificates are untouched.
- Converted producers: the verb path for memory, finding, concept, taxon,
  todo and gripe (`Handler.trust_origin = 'generated'`; `link` runs on the
  source kind). The web dispatch ingress is `human` for those kinds and
  keeps the kind's own producer otherwise. Worker/CLI doors converted with
  `trust_door`: the taproot hub reword/evidence/dispute/merge doors,
  `hub_refine._reverify_pinned_edges`, verify/repair-evidence, the
  grounding backfill, garden apply/undo, `migrate_links`, paper hygiene,
  dedup and orphan-stub reconcile, the chase snapshot, `set_intent` and the
  maintenance purge (a purged parent nulls its children's `parent_id`). A
  door handed an open transaction sends its context into it and restores
  the caller's on return. CLI commands that write converted kinds outside
  the verb path run as a producer from the registry (`Command.trust`):
  `precis memory` (generated) and `precis migrate-refs` (computed).
- Surfaces: `args={'trust': …}` on put/edit, `edit(mode='trust')`, the
  `trust:` line on `get` (stamp + sha), `origin · confidence` on `/graph`
  nodes and edges (the old `trust=` chip now reads "finding tier") and
  `precis trust report|show|consider|demote|correct-origin|legacy-import|strict`.
  `precis-edit-help` § "Mark a node considered".

#### Slice 1 — what remains before the strict boundary

1. **Writer closure.** `precis trust report` lists v0 rows written since
   0214, by ref kind / relation and set_by. Each entry is a writer still to
   convert:
   - every kind without `trust_origin`, converted as one unit with all of
     its writers. Papers next: the paper verb, the enrichers (OpenAlex,
     Crossref, ORCID cross-check, resolve/fix-metadata) and the stub-to-full
     body upgrade in `ingest/db_writer.py`;
   - get/search acquisitions (`_cache_ops`, `_cache_base`, `_good_search`,
     paper/provider acquire);
   - workers outside the verb path (glossary, dispatch/executors, alerts,
     agentlog);
   - raw ingest (`ingest/db_writer.py`, `pres.py`) and `_draft_ops` /
     draftimport;
   - DB functions (`precis_measure_taxon`, `file_gripe_readonly`, seed
     functions) and plugin ref/link producers;
   - handlers called directly rather than through the dispatcher
     (quest compute and catalyst seed, `precis_web/design_turn.py`);
   - the web routes that write without dispatch: nanopub, claim, and the
     papers merge/delete routes. They get the doors' default producers;
     typed human text there is not yet labelled human.

   Insert-only writers of converted kinds (`file_gripe_readonly`,
   `precis_measure_taxon`, worker-filed gripes/todos, quest rulings) are
   fine in compat mode and refused only under strict. A
   `handlers/structure.py` link note (`merge_meta`) is refused only on a
   structure→converted link first stamped elsewhere; unverified.
2. **Carry on copy.** `migrate_links` copies links as new `computed/hunch`
   rows, so a considered link loses its stamp across a paper merge. A
   per-row `carry` act is the fix.
3. **Capability.** The GUCs are transport. Human origin and the operator
   acts are gated in Python only (MCP refuses `human`, `correct-origin` and
   `legacy-import`). An authenticated ingress or DB-role capability still
   has to be frozen; slice 2's Reto-only human review depends on it.
4. **Measured gate.** Run the 0214/0215 dry-run/apply on a representative
   throwaway copy. Measure the DDL/VALIDATE lock time, per-write trigger
   overhead (the BEFORE guard on every refs UPDATE, the chunk guards) and
   ledger growth (one `revisions` row per converted create). Then freeze the
   lock budget for `precis_trust_activate_strict`. None of this was measured
   here; only the per-branch test DB ran.
5. **Certificate scope.** 0185's content sha still covers timeline
   chunks. A comment therefore stales a consider act's `expected_sha` but
   does not demote the stamp. Slices 2/3 decide which sha a certificate
   binds.
6. **Strict activation.** The coordinator runs `precis trust strict` after
   items 1–5 and exercises straddling transactions, nested functions, COPY
   and old-binary refusal. The switch takes SHARE ROW EXCLUSIVE on
   refs/links/chunks under `lock_timeout`; a timeout leaves compat in force
   (tested).

A static review of the built slice (2026-10-15) found context-less
writers, paper doors and the 0214 lock order; those are fixed. Reto ruled
2026-10-15 ~12:40Z that builders may /qland and the release owner deploys;
never /qgo (migration). Strict mode stays off until items 1–6.

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

## Explicitly NOT in scope

- Bypassing the populated-table migration gate, independent review per
  slice, or coordinator scheduling; R14 stays untouched.
- No backfill, guessed origin, service change or model run. No production
  trust assignments; legacy rows stay unknown/unknown until a recorded act.
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

Slice 1 (built): refs/links plus their revision audit and parent-ref body
guards; no chunk/domain-table columns or natural-key association ledger. Later slices: ingress/ingest, taxonomy, concept/memory/finding handlers,
dream/LLM/computed producers and derived projections; reviews/revisions
target hashes; universal rows/search/working sets/eye traversal; draft/
quest evidence consumers and gardener/curation exception queues.
Refresh owning docstrings and user skills with each shipping slice;
slice 1 refreshed `precis-edit-help`.

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
slice 1 is built as migrations 0214/0215 (refs/links only, compatibility
mode); its remaining closure, capability, measurement and strict-switch
items are listed under slice 1 above. Slice 2 must separately gate the
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
