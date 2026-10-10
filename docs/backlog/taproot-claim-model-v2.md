---
status: in-progress
pillar: memory-graph
---

# Taproot claim model v2

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Persist claim_type on hubs (v2)

_Grouped 2026-09-26; was `taproot-claim-type-v2`._

The extractor returns the claim sort (measurement / definition / capability /
mechanism / landscape); persist it into hub meta so hub_refine can prioritize
thin definitions/landscape claims for corroborators, lint can flag a
capability claim with no regime, and dedup can treat definitions specially.
Design pass first. Owner `src/precis/taproot/`.

Sizing evidence (td249196): a corpus-search probe over the claim hubs that no
rewriting pass could repair found that 70% were merely missing a link, but a
27% remainder (n=60) are of kinds no instrument can establish — design rules,
recited structural constants, order-of-magnitude estimates, notation
conventions, combinatorial theorems, software-capability statements. Agents
that had never seen the lint sorted them out unprompted, and they concentrate
in sentences carrying no measured quantity (57% linkable vs 83% for sentences
with one). That remainder is what a persisted `claim_type` is for.

Design hazard to answer before building: if a model assigns `claim_type` and
`claim_type` grants a lint exemption, the gate becomes something the model
configures. Keep the exemption table static and per-type
(`_ARTIFACT_LINT_EXEMPTIONS`), or gate the write.

Second field for the same extractor pass: **modality** (experimental /
computational / theoretical, plus whether the source performed the work or
reports another's). Orthogonal to `claim_type` — a DFT elastic-constant
result is measurement-sorted and computational-modality. Sizing and the two
failure modes: `taproot-claim-modality-axis.md`.

## A claim that hides its own method

_Grouped 2026-09-26; was `taproot-claim-modality-axis`, status idea._

Found while hand-adjudicating 30 grounding edges for
`llm-judge-reliability.md` (2026-08-25). Of 29 distinct claim hubs:

| | count |
|---|---|
| modality stated and accurate | 17 |
| **modality not stated** | **10** |
| **modality stated but wrong** | **2** |

Most claims already open with a method phrase — *"Raman spectroscopy
shows…"*, *"DFT-computed elastic constants…"*, *"Ab initio calculation
of full elastic tensors…"*, *"Two-probe measurements show…"*. So the
corpus's instinct is right and its enforcement is nil.

### The two failure modes

**Silent simulation.** `fi176612` — *"Reversible mechanical logic offers
a path toward the Landauer limit for switching energy"* — reads as
settled engineering. Its source is titled *Simulation of reversible
molecular mechanical logic gates and circuits*. Nothing in the sentence
tells a reader that no device was built. Same shape: `fi176659`,
`fi176660`, `fi177720`, `fi176594`.

**A confident wrong marker, which is worse than none.** `fi176677` opens
*"Powder X-ray diffraction shows…"*; the source's growth-kinetics work is
in-situ **energy-dispersive** XRD plus SANS, and PXRD appears in that
paper only for solving UiO-66 structures. `fi177518` says *"Two-probe
measurements"* of two values, one of which the source measured by van der
Pauw. A false marker reads as diligence.

### It is two axes, not one

1. **Method kind** — experimental / computational / theoretical /
   definitional.
2. **Provenance** — did *this source* perform the work, or is it
   reporting someone else's?

The second is already load-bearing in the grounding rubric — it is what
`FRONT_MATTER_ANCHOR` detects, and it drove the `NEEDS_SECOND_EDGE`
reading on `fi176620`'s 500 pW/K comparator (a real experiment, someone
else's, cited in this paper's intro). But it exists only as a *verdict
label on an edge*, never as a property of the claim, so a reader cannot
see "this is a review's summary of others' work" without chasing the
edge. `fi176677`'s source is a review; both axes are in play at once.

### Do not build a lint on it

Marker presence does **not** predict defect rate: unmarked claims ran
7/10 needing repair, marked claims 14/19 — statistically the same. This
is an axis for reader honesty and for `taproot-claim-type-v2`'s
prioritisation, **not** a defect signal. Anyone who assumes otherwise
will build a gate that fires on the wrong population (and this corpus has
already been burned twice by exactly that error — see
`ingest-strips-greek-glyphs.md` and §2 of
`grounding-verification-rubric.md`).

### Relation to `taproot-claim-type-v2`

Orthogonal, not duplicate. That item persists the claim *sort*
(measurement / definition / capability / mechanism / landscape). Modality
is a second field: a DFT elastic-constant result is measurement-sorted
**and** computational-modality. If the extractor is going to be taught to
return one, teach it both in the same pass — and inherit that item's
design hazard verbatim: **if a model assigns the field and the field
grants a lint exemption, the gate becomes something the model
configures.**

### Related

- `taproot-claim-type-v2.md` — the claim-sort axis; same extractor pass.
- `llm-judge-reliability.md` — where this was found; carries the counts.
- `grounding-verification-rubric.md` — `FRONT_MATTER_ANCHOR` and the
  hearsay-section rule are the provenance axis in edge form.

## Landscape claims need their own class (motivating case, 2026-10-09)

_Added 2026-10-09 from the review of fi449493; Reto agreed the direction.
Design decided and building 2026-10-10 (worktree `fizzy-toasting-pond`)._

A review draft (dc2445855, carbon nanobuds) minted fi449493, "The
electrical and optical properties of carbon nanomaterials are
conventionally tuned by chemical or electrochemical doping and by charge
transfer from adsorbed molecules", scope `material=Carbon nanobuds`.
fi192855 already said the same thing scoped to `carbon nanomaterials`
with 15 verified supporters. The mint-time conflict check did not fire
(gripe filed 2026-10-09, linked from fi192855), because the dedup key
hashes scope and the draft's subject had leaked into the scope of a
sentence that names the generic class. The widen arm then attached 14
topic-adjacent chunks (COF electrocatalysts, porous carbons, H₂O₂
production, single-atom catalysts). The reviewer faced 17 edges, none
about nanobuds, for a sentence no single quote can confirm or refute.
Resolved by hand: cite repointed, far-field edges dropped, hubs merged.

Keeping such hubs is right: review papers are made of them, and they are
the parent nodes specific claims hang off (`refines`). But they must
behave differently, which needs the sort persisted.

### Design (decided 2026-10-10)

**Finding of the code read:** no claim sort exists anywhere today. The
extractor prompt (`canon._EXTRACT_PROMPT`) never asks for one, the
`CanonicalClaim` dataclass has only `sentence` + `scope`, and nothing in
`refs.meta`, tags or links carries it. `hub_refine.claim_depth_policy` is
a regex stand-in (quantity/mechanism → body-required) recomputed per use.

**Owner module:** `src/precis/taproot/claim_type.py` — the closed type
set, the static per-type policy table, the SQL predicates derived from
it, the identity helper, the one write door, the LLM classifier.

1. **Field.** `refs.meta.claim_type` ∈ {measurement, definition,
   capability, mechanism, landscape} or absent; `refs.meta.claim_type_by`
   ∈ {llm, human}. Jsonb, no migration (same as `artifact_type`, `scope`,
   `tagline`). **Never part of the `(sentence, scope)` pub_id key** — a
   type in the key would fork the same sentence into two hubs.
2. **Who writes it.** The extractor returns `type` per claim (parsed by
   `canon._parse_claim_item`, unknown values dropped to absent);
   `hub.mint_hub` persists it with `claim_type_by='llm'`. A backfill pass
   (`precis taproot classify`, dry-run default, idempotent: skips hubs
   that have a type) classifies existing hubs with a MEDIUM-tier call.
   **Reclassification is a human door** following the tagline pattern:
   the web form on `/claim/<head>` and `precis taproot classify --hub
   fiN --set <type>` write `claim_type_by='human'`; the LLM paths never
   overwrite a human value. The MCP `edit(kind='finding', meta=...)`
   door rejects `claim_type` and names the two doors — it cannot tell a
   human from an agent.
3. **Policy table is static code**, keyed by type, read by every
   consumer; the hazard above is answered by (1) the table being code and
   (2) the write being gated. Only `landscape` deviates from the default:
   - *dedup_sentence_only*: a landscape hub also registers
     `ref_identifiers(id_kind='taproot_sentence', id_value=sha256(normalized
     sentence))`. `mint_hub` looks that identifier up for **every** mint
     after the pub_id miss, so any draft-specific mint of a sentence an
     existing landscape parent already states converges onto the parent
     instead of forking. Registering a sentence another live hub already
     holds reports that hub as a merge candidate instead of failing.
   - *widen=False*: excluded from `hub_refine`'s due-set, from
     `inbound_ground._near_hubs`, and from `chase_trigger`'s embedding
     refresh and `_near_claims` DUE-marking (a landscape sentence is an
     embedding attractor; its 14 far-field edges came from exactly these
     arms). Setting the type to landscape also pops a pending TAPROOT_DUE.
   - *sweep_conflicts=False, disputes_counterparty=False*: not swept by
     `conflict_search`, not a finding counterparty there, never the `src`
     of a `disputes` edge (`hub.link_claims` refuses; the placement-time
     hub↔hub disputes link is skipped when either side is landscape). A
     landscape hub states the common case and cannot contradict a
     specific result.
   - *verifier='consensus'*: no per-edge sign-off. The hub-level check is
     independent supporters ≥ 3 (the existing union-find over authors,
     `_finding_evidence._independent_supporter_counts`), rendered as a
     `consensus` line on the evidence view and the claim page. Pass/fail,
     advisory, computed at read time.
   - *publishable=False*: `nanopub.mint.approve` refuses a landscape hub
     with a message naming the reclassification door. Decided, not
     silently exempted: a signed consensus sentence attributes nothing;
     these hubs are citable inside the mesh (`[fi<id>]`) and that is all
     they need today.
4. **Not in this build.** Per-type lint exemptions (the
   `_ARTIFACT_LINT_EXEMPTIONS` axis stays keyed on artifact_type only);
   `claim_depth_policy` keeps its regex until the type is populated
   corpus-wide; modality (section above) is not asked for in the same
   extractor pass yet — one new field per prompt change, measured first.

**Slices.** (1) field + extractor + mint + sentence identity + human
doors + classify CLI + edit-door refusal; (2) the policy consumers: widen
exclusions, conflict/disputes exclusions, consensus line, approve
refusal. Both ship together from the building worktree; the only prod
follow-up is running `precis taproot classify --apply` once (≈1.5k
MEDIUM-tier calls) and reclassifying fi192855 to landscape by hand.
