---
status: draft
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

_Added 2026-10-09 from the review of fi449493; Reto agreed the direction._

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

The extractor's `landscape` sort is exactly this class. Keeping such
hubs is right: review papers are made of them, and they are the parent
nodes specific claims hang off (`refines`). But they must behave
differently once `claim_type` is persisted:

1. **Generic scope, sentence-only dedup.** A landscape hub's scope is
   the class the sentence is about, never the citing draft's subject.
   Conflict search for this type matches on sentence alone, so a
   draft-specific mint folds into the existing parent instead of forking.
2. **Consensus verifier, not per-edge sign-off.** The empirical
   question ("does this chunk affirm or contradict") does not apply.
   The check is: three or more independent sources across distinct
   systems state it, and ideally one review states it in this form.
   Pass or fail at hub level; no human judgement per edge.
3. **No widen.** Landscape sentences are embedding attractors. The
   method-gap and inbound-grounding arms skip this type, or route the
   candidate chunk to a specific child hub.
4. **Never a disputes counterparty.** `claims-and-evidence.md` already
   logged a judge disputing a small-fullerene claim with a background
   sentence about nanobuds in general. A landscape hub states the
   common case and cannot contradict a specific result.
5. **Probably no nanopub.** Signing a consensus sentence attributes
   nothing; these hubs are citable inside the mesh and may not need the
   publish path. Decide at design time rather than exempt silently.

The design hazard above stands: the per-type verifier and widen table is
static, and reclassification is a human door, so the model cannot relax
its own gate by calling a claim `landscape`.
