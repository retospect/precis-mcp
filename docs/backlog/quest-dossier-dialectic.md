---
status: draft
title: quest dossier as dialectic — hypothesis findings, refuted lifecycle, crosslinked log
prio: normal
---

# Quest dossier as dialectic

## Motivation / why

The qu164903 dossier is 286 chunks of per-tick chronological accretion —
barrier numbers and `[st…]` ids, near-zero finding cites, zero
chunk-to-chunk references, literature as unfetched `pa` stubs. Root causes
are structural (audited 2026-08-26):

- The tick (`quest/tick.py`) is a single tool-less LLM call; it can only
  cite handles `build_tick_prompt` serializes — and the prompt advertises
  `[fi…]` while **the quest layer never mints, reads, or serves a finding**
  (zero production hits). Gaps, logbook, and servers sections strip handles
  that already exist (`Gap.handle`, `ql` ids, per-server refs).
- `_MAX_DETAIL_PAPERS = 6` serves the first six papers in serves-graph
  (insertion) order — not relevance — and only those are citable.
- `dossier.rewrite_dossier` retires every unpinned chunk each tick, so no
  narrative chunk keeps a `dc` id; nothing can crosslink *into* the dossier.

Target (Reto, 2026-08-26): **dossier = planning + thinking document**
(hypothesis / support / counterargument / experiment, densely linked, a
frontier model can argue through it); **log = lab notebook** (what
happened), crosslinked *from* the dossier constantly.

## Decisions

**Hypotheses are findings.** `put(kind='finding', hypothesis=True,
motivation=, testable_by=, motivated_by=)` already exists
(`handlers/_finding_hypothesis.py`) with exactly the right gates (no
evidence by type; ≥2 motivators across ≥2 papers; discriminating experiment
required). Quests mint their conjectures as hypothesis findings and serve
them — `[fi…]` becomes reachable, and the chase worker accumulates
literature evidence for/against *between* ticks for free.

**Refuted lifecycle (decided).** New closed-vocab `STATUS:refuted` on
findings. Rejection *converts*: mint the negative ruling as an established
finding (the hard-won "X does not gate Y" is premium knowledge), link the
hypothesis `superseded-by`/`retracts` → ruling, stamp it refuted.
Search **default-excludes refuted** (cheap via `store/_tag_filter.py`
prefilter), reachable explicitly with `status='refuted'` — this is the
do-not-repropose ledger, typed. UI: living-cite rendering means **every
cite site everywhere turns red automatically** (`precis_web/linkify.py`
claim-sigil path: filled ◆ live, hollow ◇ pending, red = refuted, hover
"refuted — see ruling"); red banner + superseding ruling on `/claim/fi…`
and the smartdraft rail. Semantics: red = *the claim as stated is dead*,
not "this document is wrong".

**Dossier shape.** Live hypotheses only, one section each: the `[fi…]` hub
(statement/motivation/testable_by live on the ref, not restated) — support
handles with one why-clause each — counterargument as a steelman with its
own handles — the discriminating experiment linked `tests → [fi…]` with
**pre-registered branch predictions** (BEP estimate from
`estimate-kind-ms-chemistry-workup.md`). Settling collapses the section to
one linked sentence. Tick's job becomes dialectic maintenance, enforced
structurally by the cite-diff gate in `dossier-present-tense-refinement.md`
(the keystone: stable `dc` ids via anchored in-place edits — also what lets
the prompt serve a *neighborhood* projection instead of the whole history,
fixing the 1.4 MB ref bloat and the crosslink gap with one mechanism).

**Tick diet fixes (small, `quest/tick.py`).** Emit `Gap.handle` in the gaps
section; print `ql` handles in `_logbook_tail` (the log becomes citable —
"belief in [fi…] weakened when the replicate diverged [ql…]"); per-server
handles in `_servers_summary`. Replace the `[:_MAX_DETAIL_PAPERS]` slice
with relevance ranking against open gaps + live hypotheses (existing
embedding machinery), filling a *token* budget; prefer papers with held
bodies; prune/demote serves for settled questions. Known companion defect:
`dossier-paper-handles-emitted-bare-not-bracketed.md`.

**Simulation step deep-links.** Register plugin handle codes
(`handle_registry.PLUGIN_GROUP`, currently unused by every plugin) and mint
stable step ids as (structure × network edge label, e.g. `NH2_H→NH3`) —
identity already unique in `precis_pathway/analysis.py`, just not
addressable. Evidence edges per
`computed-pathways-cannot-be-cited-as-claim-evidence.md`. Add a
microkinetics digest view (Eyring rates, steady-state coverages, per-step
residence times over the network — scipy+networkx, no new deps) so the
dossier can argue "the slow step is [step], τ ≈ …, sites occupied".

**Lit search for the tick.** Options: (a) two-round tick — model emits
queries, code executes + fetches, same tick re-prompts with results;
(b) per-quest **agentic tick** (claude_inproc, `plan_tick.py` as template,
restricted toolset: search/get/more/estimate) — Reto is game for a
quest-specific tick for qu164903. Natural split: coordinator tick stays the
cheap heartbeat; the frontier-review escalation (`quest/cascade.py`) runs
agentic at Tier.FRONTIER and does the deep workup. Either way the fetch
backlog (standing blocker in the dossier) must be expedited for
high-ranking stubs.

**Argumentation guidance.** Chemist's-creed section in `_PROMPT_TEMPLATE`
(parallel to the explorer's creed): every barrier claim names its mechanism
class (electronic/geometric/kinetic); every mechanism claim cites a
descriptor `[es…]`, computed observable, or paper — else it is flagged
hypothesis and paired with its discriminating experiment. New weave-review
lenses (`quest/weave_review.py::_LENS_BRIEFS`): mechanism, crosslink
(bare numbers → handles, `[pa]` → living `[fi hub]`), kinetics.

## qu164903 ref audit (agent drain, 2026-08-26) — the empirical case

The 1.4 MB default render = **85% logbook** (4,652 entries, ~50%
near-duplicate restatements; one triple restated ~15× in a day) + **15%
untyped links**. Frontier: **0 converged Pareto points**, 192 provisional
rows × 14 metrics nearly all `⚠untrusted`, trust boilerplate repeated
verbatim ~180×. The campaign's ~30 genuine mechanistic rulings (d¹⁰-gate
refutation, stability/barrier decoupling, colocated-recipe
irreproducibility…) exist **only as prose — zero `fi` handles in the entire
1.4 MB**; observed cost: closed branches re-proposed as "never tested" days
after closure. The 3 real open questions are restated 6–8× each. Six human
interventions (fabrication call-out, resets, PBC audit) are the
highest-information entries. Literature: 852 served papers, ~30 actually
used in reasoning, every link untyped `related-to`, and the serves set is
polluted (off-topic medical papers serve a Pd-catalysis quest) — supports
the relevance-ranked serving + pruning decision above.

**Root causes, not cleanup (Reto, 2026-08-26).** The 1.4 MB is mostly a
*render* bug, not a data problem: the logbook and dossier are separate
surfaces (web: `/refs/quest/<id>/logbook`, smartdraft), but
`handlers/quest.py::get` default-view inlines **every** logbook entry by
design ("the logbook shows by default"). Fix: default render = digest
(striving + gaps-style health line + dossier narrative + trusted frontier +
logbook *tail* with count and a `view='logbook'` pointer — `view='log'`
stays the generic ref-events ledger) — the logbook itself
stays append-only forever; it's the lab notebook and the provenance record,
never compacted or destroyed. Likewise the ~180× trust boilerplate: each
distinct rejection/untrust reason ("adsorbate detached", "wrong binding
site", reset-epoch N) becomes one addressable ref (the TrustEvent node
below), stated once; affected measurements link `invalidated-by → [te…]`
and rows render a 2-char flag + handle — same state-once-link-everywhere
pattern as the refuted-hypothesis red sigil.

**Trust is per-step, and it is re-earned, never edited (Reto, 2026-08-26:
"untrusted is too coarse; it comes from catpath").** catpath already
produces per-edge NEB barriers with per-attempt geometry checks
(`autocatpath/neb.py` — detachment, wrong binding site) and persists
per-edge results + warnings (`precis_pathway/persist.py`); the coarseness
is precis's collapse to one per-candidate verdict
(`quest/frontier.py::Candidate.flags['barrier_trusted']`), where one bad
edge poisons all 14 row metrics. Fix rides the step-handle design: trust
lives on per-step **Measurements**. A failed check untrusts *that edge*;
other edges' barriers stay arguable. Nuance the headline needs: the
candidate's rate-limiting barrier is only trusted when every edge that
could plausibly exceed the current max is trusted (an untrusted edge can
hide the true rate-limiting step) — render as "max over trusted edges,
incomplete" rather than all-or-nothing. Restoration = supersession, three
escalating lanes, each minting a new Measurement with `supersedes` (the
TrustEvent stays on the old one — provenance kept): (1) re-run just the
failing edge's NEB; (2) `autocatpath/neb.py::rescore_band` — single-point
re-scoring of the saved band with a higher-tier calculator (built for
mixed-precision; doubles as the verification lane); (3) replicate under
the current engine epoch after resets.

The catpath half is handed off separately: structured per-(edge, check)
trust records (verdict/severity/evidence, passes included, schema-versioned)
emitted alongside the unchanged warning strings, plus endpoint-aware
detachment semantics (product desorption ≠ failure) — prompt at catpath repo
`HANDOFF-structured-trust-records.md`. Its Deliverable-1 schema is the
contract; the precis per-step-Measurement consumer here **waits for that
schema to land** rather than guessing at it.

**Salvage strategy: full fresh start (Reto, 2026-08-26 — wiping the lab
notebook too is fine; "we learned things").** The learnings survive as the
minted rulings-as-findings plus the substrate fixes themselves (the
PBC-ground-rules block, validated site heights → site-symbolic ops, the
trust model). Once the new substrate lands, start the campaign clean:
mint the ~30 rulings first (so the fresh quest's do-not-repropose set and
served findings are populated from day one), then a fresh dossier, fresh
logbook, pruned+retyped serves, re-measurement under the trusted engine.
Superseded framing below kept for reference: The
campaign's numbers are epistemically compromised regardless (3 resets, 0
converged Pareto points, most barriers untrusted), so in-place compaction
of 4,652 entries buys little. When the new substrate lands: mint the ~30
rulings as findings with their evidence handles (the one migration worth
doing by hand), re-found the dossier in dialectic form (live hypotheses
only), prune + retype the serves graph, and let re-measurement under the
trusted engine repopulate the frontier. Same quest id — links and logbook
continuity matter; it's the dossier and the serving set that restart, not
the quest.

Compaction target for the *default render* ≈100 KB shape (informational —
mostly falls out of the digest fix + structure-table dedup): settled-rulings
register (verbatim,
with evidence handles — these become the minted findings) · dated chronicle
(~15 paragraphs; human interventions verbatim; **which reset nulls which
era's numbers**) · current-state header · one dedup'd structure table
(collapse frontier+leaderboard; 2-char trust flags) · links pruned to the
~30 load-bearing papers, retyped `supports`/`cites`. Raw logbook survives
as archival `view='logbook'`; the default render becomes the compacted form.

Graph note beyond the decisions above: **Measurement must be first-class**,
separate from Structure — values get re-measured, diverge across replicates,
and are nulled by engine resets, so it needs `supersedes` /
`invalidates`(TrustEvent→Measurement, fan-out 309 at the Aug-23 reset) and a
reset-epoch stamp. `duplicate-of` edges for PBC translation twins.

## Mechanism — `dialectic_ops` (Reto-approved 2026-08-29, in build)

Tick 4 post-reset showed prompt-side structure cannot survive: the
dossier-format contract *bans* block markdown (chunks render it literally),
so any `###`-skeleton flattens on the first whole-rewrite by design. The fix
is the ledger's own proven move — take the structure OUT of the rewritable
narrative:

- **Storage** (mirrors the pinned ledger, migration-free): one
  `meta.pinned='dialectic'` container chunk per dossier; one child block per
  live hypothesis (`meta.pinned='dialectic-hyp'`, `meta.hypothesis=<finding
  ref id>` — statement/motivation/testable_by live on the finding, never
  restated); entries as block children (`meta.pinned='dialectic-entry'`,
  `meta.role=support|counter|experiment`). `read_narrative` /
  `rewrite_dossier` already exclude any truthy `meta.pinned` — the model
  structurally cannot flatten what it never rewrites.
- **Ops** (new tick payload key, sibling of `ledger_ops`): `open`,
  `support`/`counter` (one why-clause + evidence handles; near-dup-guarded),
  `experiment` (upserted in place — ONE discriminating experiment per block,
  with `predicts` pre-registered branch predictions), `settle` (collapses
  the render to one linked sentence; entries kept as history). Ops address
  blocks by **fi handle** — real stable ids, no ledger-style
  quote-the-exact-text ambiguity.
- **Edges minted at apply time**: each `support`/`counter` entry's inline
  handles become real `links` rows, evidence `supports`/`contradicts` →
  hypothesis finding (existing relation vocab, idempotent on the unique
  tuple) — the dialectic is a queryable graph, not a document shaped like
  one. `tests` edges for experiment entries are DEFERRED to the
  simulation-step deep-link slice above (needs the `tests`/`tested-by`
  relation migration).
- **Narrative shrinks to synthesis** — the prompt shows rendered blocks
  read-only (like the ledger) with upsert discipline: maintain via
  `dialectic_ops`, do not restate hypothesis content in `dossier_text`.

Composes with (does not replace) `dossier-present-tense-refinement.md`:
that keystone makes the residual prose refinable; this makes the hypothesis
network un-flattenable. Render half (web view for blocks) is a second
slice.

## Open

- Dialectic mutation survivors (advisory pass on the 19573e15 ship — ride the
  next quest-touching ship): assert `outcome.dialectic_applied == 0` on an
  op-free tick (tick.py `dialectic_applied = 0` init); in the non-dict-op
  test, put a VALID op after the garbage one (kills `continue`→`break`);
  a two-block `_load_dialectic_blocks` test where the first block has garbage
  meta.hypothesis (kills the loader's `continue`→`break`); a narrative-gate
  test where dialectic_applied is the ONLY progress signal (tick.py
  progress_evidence `or`). Cap-value mutants (16→17, 8→…) are accepted
  unkillable. Per [[mutate_diff_false_survivor]]: re-verify each by applying
  the mutation before chasing.
- Dialectic follow-ups (pre-ship review, 2026-08-29): (a) `_render_dialectic`
  is 2-queries-per-block (`get_ref` + `tags_for`) where `_render_ledger` is
  pure in-memory — batch when block counts grow; (b) `_resolve_hypothesis_id`
  accepts ANY live finding, not just ones serving this quest — decide whether
  to scope-check (strictness could break legit cross-quest cites); per-tick
  op cap (16) and per-entry edge cap (8) are in.
- Budgeted QE "electronic autopsy" job on frontier leaders (PDOS → d-band
  center, Löwdin charges, magmoms; `autocatpath/dft.py` already builds the
  calculator and seeds spins) — turns orbital/spin claims into citable
  measurements. Demand-driven off the frontier only.
