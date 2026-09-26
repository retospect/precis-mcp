---
status: draft
title: Claim publication — signed nanopubs + OpenTimestamps anchoring, minted locally, pushed at publication time
model: opus
blocked-by: taproot-compound-migration
---

# Claim publication — nanopubs + OpenTimestamps

Built state (slices 1–5, registry mirror): `src/precis/nanopub/__init__.py`
package docstring — present-state home, not this file. This file is the
open-work residue.

Blocked on `taproot-compound-migration`, but per hub, not globally: a decomposed,
reviewed hub can publish while the rest of the graph is still compound.

## Patent / book (ISBN) grounding

Mint's grounding gate hard-requires a DOI per passage
(`nanopub/gates.py`: "no DOI — provenance content is DOI + quote + snip
(patent grounding is an open item)"). Same gap, two source types — design
together, one gate:

- **Patents.** The nanopub ecosystem is DOI-centric; no standard
  identifier fits. Likely a local scheme.
- **Books** (the Callister class). fi19981's best mid-span corroboration
  is Callister, *Fundamentals of Materials Science and Engineering* 3e
  (2008, Wiley, ISBN 978-0-470-12537-3) — "over one billion transistors …
  doubles about every 18 months" — no DOI, so the passage can't ride the
  artifact even though edge, quote, snip and pdf sha all check out.
  `urn:isbn:` (a registered URN namespace) in the provenance graph where
  DOI would go; edition matters — the sha pins the copy, the ISBN names
  the edition.

Gate shape for both: accept exactly one of doi | isbn | patent-no per
passage — quote verbatim-containment, snip uniqueness, pdf-sha pin, and
the hearsay checks are already identifier-agnostic. `_suggested_payload`
should emit an `isbn`/`patent-no` field when the ref has one and no DOI;
the approve form passes it through.

Until either lands: DOI-less passages stay out of minted payloads — keep
the corroborates edge internally, note the passage in the hub for the
human reader.

## Revocation and correction policy

Two unmade policy calls:

- **Allowlist revocation.** An identity leaving the allowlist after its
  claims were cited can't unpublish them; the remedy is a supersede or
  qualification nanopub recording the changed basis, but whether/when
  that fires is a judgement call, not decided.
- **Correction vs retraction vs invalidation.** `supersedes` /
  `invalidates` / `retracts` are all live vocabulary in the corpus; which
  applies when the *wording* was wrong vs the *claim* was wrong vs the
  *evidence* was misread is undecided.

## First registry POST + introduction nanopub

`nanopub/registry.py` is built and triple-gated (`interactive=True` +
`--live` + clean preflight); the first real POST is Reto's call, still
untaken. Two things want to exist first:

- **Introduction nanopub** — **built** (`src/precis/nanopub/intro.py`,
  `precis nanopub intro`): signs + (`--live`)
  publishes the key→ORCID declaration, records the trusty URI in the
  vault. The `approvesOf` path from an existing agent is still open
  (deferrable — rates agents, not claims); the out-of-band ORCID
  back-link (adding the trusty URI under 'Websites & social links') is
  still Reto's pending step, same as the first real registry POST above.
- **Fingerprint page — POSTPONED INDEFINITELY (Reto, 2026-08-29).** No
  self-hosted external-facing trust serving: the independent binding a
  reader has is the published introduction nanopub (registry + mirrors —
  "the net") plus the ORCID record's back-link, both on infrastructure
  we don't operate. `https://precis.retostamm.com/id/precis` stays a
  name we own, not a service we must keep up for verification.

## Mirror pull

`nanopub/mirror.py` is built; `PRECIS_MIRROR_ENABLED` is now ON
cluster-wide (deploy `precis_worker_nanopub_mirror`), so the daily delta
sync runs. The initial ~87k-nanopub backfill is a separate one-time
manual door, still untaken: `precis nanopub mirror sync --live --all`
(`/nanopubs.json` returns the full code list in one flat array, no
paging — probed 2026-08-15).

## Three deferred publish-time gates

`nanopub/preflight.py`'s trust gate checks (signer, key) against an
open-window allowlist row today; three refinements are named-deferred in
its own docstring:

1. **Allowlist-as-published-artifact.** Version, sign, OTS-anchor the
   allowlist itself; each published claim records the version that gated
   it, so "only allowlisted signers were trusted" becomes third-party
   verifiable.
2. **Validity-window-vs-signature-time.** Check the window in force
   *when the signature was made*, not at preflight time; needs a
   trustworthy time source (the OTS anchor), so the two features are
   coupled.
3. **Inbound key-strength/DER/SPKI gauntlet.** Parse the DER, require
   valid SPKI, check modulus size, reject unknown algorithms — against
   *external* keys (our own already sign at 2048 minimum, 4096
   preferred, enforced at mint).

## Outbound retraction

Inbound detection is built (`ingest/provenance.py`: Crossref,
`refs.retraction_status`, `retracted-by`/`corrected-by`/
`concern-raised-by` links). Missing: the outbound half — emitting a
retraction/qualification nanopub for a published edge grounded in a
source that's since been retracted.

## Negative-results pathway

`workers/hub_refine.py` already computes verified non-support
(`meta.citation_misses`); publishing grounded negative checks
(`cito:disagreesWith`) has no artifact path yet. Plausibly higher value
than positives — the computation is already paid for, and almost nobody
in the ecosystem publishes this.

## Query-time section filter

The primary-source hearsay gate's mint-side check (`nanopub/gates.py`,
`section_path` matched against references/related-work/background
patterns) is plain SQL on a stored column. The *query-time* half —
filtering a search/hunt by `section_path` — doesn't exist: the column is
stored, shown in `view='toc'`, never filterable in search (migration
0118 dropped its dead index). Until built, directed-mint evidence hunts
stay TOC-based (skills already phrase it that way).

---

# Absorbed 2026-09-26

## Nanopub MCP surface gaps (from the nanobud campaign, 2026-08-17)

_Grouped 2026-09-26; was `nanopub-mcp-surface-gaps`, status draft._

The 124-hub nanobud campaign stress-tested the agent surface. Hub
authoring via MCP is complete and behaved well: `put(supporters=)`
mint/convergence, `link(rel=)` chunk-granular idempotent evidence
attach (no 502 double-write hazard, unlike the web `evidence/add`
door), `edit(title=)` reword with dup detection, `view='nanopub'`/
`view='evidence'` reads. What was missing:

### 0. Mirror/publish status read view (measured; merged from
### nanopub-status-read-kind, 2026-08-21)

62h of local session mining found **1,063** Bash `psql`/`scripts/prod-psql`
calls concentrated in nanopub-mint verification workflows polling
`nanopub_mirror` / `nanopub_publish` — tables with no precis-kind read path.
Agents aren't routing around MCP friction; the surface has a hole. Add a read
view (a `view=` on the nanopub-adjacent kind, or a thin `nanopub-status` kind)
covering the polled questions: mirror/publish state per claim, counts by
status, recent failures. Test: the mint-verification recipe (see the
`precis.nanopub` package docstring) completes via MCP reads only. Pattern to
institutionalize: measured detour → kind/view, not new verbs (general form:
`mcp-aggregate-surface-gaps.md`).

### 2. MCP approve door — Reto's policy call, not a default

Approve itself stayed web/human-only by design; the campaign ran it
via user-authorized curl to the web door, paced. If that pattern
recurs, a feature-flagged MCP approve (allowlist/authorization-token
gated, server-side pacing) would remove the curl scaffolding — but it
moves a line that is currently deliberately drawn. Decide before
building. Sign/signoff/anchor/publish stay human-only regardless.

### 3. Minor

- Paper soft-delete is web-only (`POST /papers/<id>/delete`); no MCP
  equivalent (campaign needed it for dup/un-import cleanup).
- Session-MCP process wedged ~30 min mid-campaign (cost the tier-2
  agent a timeout loop) — reliability bug, separate from features;
  gripe when reproduced.

## A provenance field nothing derives

_Grouped 2026-09-26; was `nanopub-model-provenance-is-forgeable`, status draft._

The nanopub artifact already has a place for model attribution.
`src/precis/nanopub/assemble.py:223-224` emits one `precis:llmModel` triple per
entry of `MintInput.software["llm_models"]` into the **pubinfo graph**, which is
inside the signed bytes and covered by the OTS anchor. No format change and no
migration are needed to populate it.

Nothing populates it from reality. There are exactly two callers:

| caller | value |
|---|---|
| `src/precis/cli/nanopub.py:332` | `llm_models=args.llm_model` — a hand-typed CLI flag |
| `src/precis_web/routes/nanopub.py:261` | `llm_models=[]` — **the web door emits nothing** |

So the field is an honor-system assertion, and the door most likely to be used
in practice asserts nothing at all.

### Why this is worse than leaving it empty

A hand-typed model id inside a **signed, timestamped** artifact is a false
witness waiting to happen. `--llm-model claude-opus-5` makes the artifact attest,
under signature, that opus produced a claim that `z-ai/glm-4.7-flash` actually
wrote, and no part of the system can contradict it. A missing field is an honest
gap; a forgeable one invites a reader to trust something no one verified.

Either derive it or remove it. Do not leave it typed.

### What is actually true today, measured 2026-08-20

The taproot claim path, as routed in prod (`app_settings` `llm.chain.*`):

| stage | tier | model in prod |
|---|---|---|
| `taproot:extract` — **the claim sentence** | SMALL | `z-ai/glm-4.7-flash` |
| `taproot:dedup` | MEDIUM | `claude-haiku-4-5-20251001` (earlier: `z-ai/glm-4.7`, 1,409 calls) |
| `merge_confirm` | BIG | `claude-sonnet-5`, and only when confidence is low |
| `_verify_support_with_caveats` — **does this passage support this claim** | MEDIUM | `claude-haiku-4-5-20251001` |

**FRONTIER (`claude-opus-5`) is never called anywhere in the taproot path.** The
`nursery`/`structural`/`deep_review` tiers — including the opus rung — operate on
the **todo tree**, not on findings, so "an opus reviewed this claim" is not a
thing the system can do today regardless of what an operator runs.

Caveat on the numbers: only 150 `taproot:extract` calls exist all-time against
~1,244 hubs, so the logged pipeline cannot account for every hub. Many were
likely minted through the agent/MCP direct-mint path, which tags nothing as
`taproot`. Do not state that every hub was glm-minted — it is not established.

### Where the model must be recorded

Not on the artifact — on the **claim and the edge**, at write time, with the
artifact rendering it. Derive-never-duplicate: the DB row is the source, the
pubinfo triple is the projection. You cannot run "show me every claim whose
evidence was only ever haiku-verified" over signed RDF blobs, and that query is
the entire point.

Nothing is recorded today: `refs.set_by` is NULL for all 126 hubs in the
dr173020 cohort, `refs.meta` holds only `{"scope": …, "source": "taproot"}`, and
evidence edges carry `support` / `support_reason` / `caveats` / `source_handle`
with no model. `llm_call_log` has `tier`/`model`/`source`/`ts` and reaches back
to 2026-07-14 unpruned, but joining it to a claim is inference by timestamp, not
provenance.

Record per step, because the stages ran on different tiers and the highest-stakes
judgment is per-edge:

- extract → model, tier, prompt/rubric version (no rubric version constant exists
  yet — that gap is already tracked);
- dedup/placement verdict → model, tier, confidence;
- **each evidence edge's support verdict** → model + tier, alongside the `support`
  and `caveats` already stored there. Smallest change, highest value;
- human acts → who and when, typed *differently* from machine acts. PROV-O
  distinguishes `prov:SoftwareAgent` from `prov:Person`; the artifact currently
  cannot express that a human read a passage.

Use the exact versioned id (`claude-haiku-4-5-20251001`), never a family name —
"Claude" is not machine-comparable and ages badly.

### Also worth emitting: the verdict itself

`support` and `caveats` are stored on the edge and **deliberately dropped at the
publish boundary**. The artifact therefore tells a reader "derived from DOI X,
role `corroborates`, here is the quote" but never "and our check judged support
qualified, with these caveats." The caveats are precisely what a reader needs to
weigh the claim. The "universal anchors only" rule that (correctly) strips chunk
and ref ids does not apply to a model id or a caveat — both are universal.

### Do not backfill

The existing hubs have no recorded model. A forensic `llm_call_log` join by
timestamp is inference, and baking an inferred model into a signed artifact is
the same species of error as "correcting" a claim to match a corrupt passage.
Mark them `unattributed` and move on.

### The payoff is a gate, not a disclosure

Once the model is on the edge, this becomes writable: *refuse to sign a claim
whose evidence was never verified above MEDIUM.* Today that rule cannot be
expressed because the data does not exist. Provenance is what turns a preference
into an enforceable gate — that, not transparency, is the reason to build it.

## Two evidence kinds can be attached but never published

_Grouped 2026-09-26; was `nanopub-bundle-drops-edgar-datasheet-evidence`, status draft._

`taproot/hub.py::attach_evidence` accepts any source in
`EVIDENCE_SRC_KINDS = {paper, patent, edgar, datasheet}`. The nanopub read
path narrows further, without saying so: `nanopub/evidence.py::load_bundle`'s
`_source` helper returns `None` for any ref whose kind is not `("paper",
"patent")`, and a `None` source is skipped by both the supporter loop and the
`contradicts` loop.

So for an `edgar`- or `datasheet`-sourced edge:

- **A supporter vanishes from the minted artifact.** A claim grounded solely
  in a datasheet would mint a nanopub whose source list is empty, with no
  error — the gate that checks for evidence reads the same emptied bundle.
- **A `contradicts` edge does not block.** `gates.py::check_contradicts`
  iterates `bundle.contradicts`, so a dispute filed from an SEC filing or a
  datasheet is silently unenforced. (Hub- and finding-sourced disputes are
  also absent, but *deliberately* — see
  `disputes-edge-nonblocking-disagreement.md`. This one is not deliberate.)

### Blast radius today: zero

Measured read-only against prod 2026-08-20 — every evidence edge into a
`TAPROOT:claim` hub, by source kind:

| source kind | relation | rows |
|---|---|---|
| paper | corroborates | 1321 |
| paper | establishes | 161 |
| paper | contradicts | 1 |
| finding | contradicts | 1 |

No `edgar`, `datasheet` or `patent` evidence edges exist. The defect is purely
latent — which is exactly why it should be fixed before someone attaches the
first one and trusts the result. Note the corpus also has **zero patent
evidence edges** despite `patent-evidence-parity.md`; `patent` at least
survives `_source`, so that is a separate, non-silent gap.

### The fix, and the question inside it

Mechanically: derive `_source`'s kind tuple from `EVIDENCE_SRC_KINDS` instead
of restating it, so the write door and the read door cannot drift again. That
is a one-line change plus a test asserting the two sets are equal.

But it needs a decision first, because the narrowing may have been intentional
and merely undocumented: **is an SEC filing or a datasheet an admissible
citation in a published nanopub?** A datasheet has no DOI, no authors and no
retraction channel, so the citation model the nanopub emits (`doi`, `year`,
`pdf_sha256`) degrades. Two coherent answers:

1. **Yes, publishable** — widen `_source`, and decide what identifier stands
   in for a DOI in the emitted citation.
2. **No, internal-only** — then `attach_evidence` should *refuse* these kinds
   for hubs on a publication path, rather than accepting a write that is
   silently discarded downstream. Narrow `EVIDENCE_SRC_KINDS`, or gate at
   approve with an explicit violation naming the unpublishable source.

Either way the two doors must agree. Today they disagree in the direction that
loses evidence without telling anyone, which is the worst of the three
options.

Found 2026-08-20 while correcting the `contradicts` gate-scope claim in
`precis-nanopub-help`; the double filter was the surprise.
