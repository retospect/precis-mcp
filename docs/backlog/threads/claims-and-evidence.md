# claims and evidence — one identity per claim, every edge checked

**Status:** ends when every claim has one identity, every evidence edge is
checked against its source, contradictions are ruled on, and the hub corpus is
publishable (`backlog/claim-publication-nanopub-ots.md`). Triaged 2026-10-02
against the code: every item below was read and its named code checked; none
is fully shipped, six carry a confirmed bug, and the rest order along four
dependency chains.
**Last reviewed:** 2026-10-02
**Worktree:** `claims-and-evidence`
**Active:** yes — Reto 2026-10-01.

Created 2026-10-01 from the memory-graph pillar review. The taproot umbrella
(hub model, seniority, hub-refine) stays referenced by `knowledge-mesh.md`; the
defect and follow-on cluster below is owned here.

## Do next

0. **Round 2 queue (10-02 ~22:40Z).**
   - Committed here, unlanded: the scope edit door, the anchored/published refusal (orchestrator: land early in round 2), and the cite-standard fallback (079e07f16 + d9a287074). The fallback waits on the orchestrator's sample check (reviews/claims-and-evidence.md §3); after that, send nanobuds-paper the dr173020 (nanobuds review) before/after key set.
   - In flight: the report/definition types + web anchor, with migration 0183 going to the orchestrator's gate.
1. **Scope chain** — (edit door shipped 10-02: `edit(kind='finding',
   meta={'scope': …})`; `refine_claim_sentence` now refuses an anchored/published
   hub — `HubFrozenError`; the notation sweep skips + reports it) ·
   `scope-key-vocabulary-registry` (two hardcoded key sets,
   `sentence_lint.SCOPE_KEYS` and `canon._SCOPE_KEYS`) — **blocked on
   knowledge-mesh's `term-taxonomy` v1.5 `axis` start node**: Reto ruled
   2026-09-30 the registry reads the axis taxon nodes, not a second list →
   `aida-uri-ignores-scope` (2 duplicate pairs, prod data) → the scope
   backfill inside `nanopub-corpus-remediation`.
2. **Adjudication** — `disputes-adjudication-workflow`, which absorbs
   `taproot-adjudicate-reopened-claims` (premise half-stale: the widening arm
   now files non-blocking `disputes`, but a demotion still reopens reviewed
   hubs) · `contradicts-conflates-evidence-and-prose-misuse` (residue = the
   `misused-by` relation only) · `claim-conflict-search` items 4–5 (slice 1
   shipped dark; nothing reads its output at approve).
3. **Doors** — `taproot-merge-mcp-surface` (web door + agent `view='merge-plan'` shipped; open = corpus-wide banded candidate scan, low priority) ·
   `taproot-cite-time-attach-or-mint` · `taproot-directed-claim-minting` · `nanopub-supersede-door` ·
   `preprint-to-published-cite-upgrade`.
4. **Evidence quality** — `evidence-edge-verification` (rubric labels +
   approve gate; whether the verify/repair runs happened needs prod data) ·
   `pa-arm-locate-should-capture-a-verbatim-quote` ·
   `taproot-sole-supporter-coverage` (half shipped; open = name a candidate
   originator) · `taproot-numeral-audit` · `taproot-backfill-defects`
   (open: [pc] silent drop, fragment continuation clauses, the 8-finding
   demotion triage, the extract_claim outage audit in `chase.py` and the
   `hub_refine` paths) ·
   `taproot-claim-quality` (§a mostly superseded by the hearsay gate) ·
   hub_refine prune stage stays disabled until the judge re-judges against
   the CURRENT hub sentence and skips an edge that is the sole support for a
   clause of a citing draft sentence (dr173020: 100 votes = 53 bad · 25
   redundant · 21 needed; review item `nanobuds-paper-21`, awaiting Reto) ·
   `taproot-inbound-grounding` · `computed-pathways-cannot-be-cited-as-
   claim-evidence` (open: magnitude re-check, re-dispatch of a `ready`
   pathway, nanopub visibility, web attach form).
5. **Composite + publication, last** — `taproot-compound-migration` (L;
   blocked on `reground.py`'s embedding-ranking TODO; blocks
   `claim-publication-nanopub-ots`) → `claim-publication-nanopub-ots` ·
   `retire-fi-go-nanopub` (open: `[np<id>]` grammar + migration sweep) ·
   `nanopub-corpus-remediation` (step 5: `identity.py` hashes the sentence
   without `_normalize_number_text`). Publishing before identity and
   evidence settle publishes the defects.
6. `taproot-claim-model-v2` — persisted `claim_type`; design-heavy, no
   dependents yet.

## Horizon

- (none)

## Parked

All `backlog/<slug>.md`; each unparks when the triage promotes it.

- **taproot-self-plagiarism** · **claim-query-rescan-watermark** ·
  **notation-detector-gaps** · **taproot-reground** · **finding-chase** ·
  **finding-edit-dry-run-preview** · **classifier-cite-gap-analysis** ·
  **nightly-fixer-for-drifted-cites** — the remaining claims-cluster items.
- **Auto-Ⓐ abstract verify** — when full text is unobtainable but the held
  abstract is present, a MEDIUM-tier pass sets `abstract` machine-earned
  (`by='verify:abstract'`), gated behind the acquiring-arm give-up; owner
  `src/precis/taproot/trust.py` + `workers/chase.py`. Was
  `trust-taxonomy-followons`.
- **"Declared-unobtainable sources" exporter section** — calm end-matter list of
  abstract/vouched claims, kept out of the "Unverified claims" problem list;
  owner export `docx.py`/`latex.py`. Was `trust-taxonomy-followons`.
- **`claim_trust_bulk` batch meta fetch** — one `fetch_refs_by_ids` per
  unverified lifecycle finding today; only if it shows up in a profile.
- Paper/figure items filed with this cluster: **figure-permission-request-flow**
  · **figure-kind-slices** ·
  **paper-annotation-critique** — unpark when a draft needs them.

## No action needed

- (none)

## Seam

- `finding-stable-identity` was filed with this cluster by name only: it is
  about pcb/se DRC findings (`pcb_drc_findings` has no stable key), not claim
  hubs. Owner is the pcb thread, not this one.

- `knowledge-mesh.md` keeps the taproot umbrella (hub model, seniority,
  hub-refine); this thread owns the defect and follow-on items. A fix that
  changes the hub schema is knowledge-mesh's call.
- `ingest-and-fetch.md` owns extraction fidelity; a claim quote that fails
  `evidence-edge-verification` because the source text is corrupt (the glyph
  pair) is theirs, not a claims defect.
- `draft-authoring.md` owns the draft side of cite-time attach; the doors above
  own the claim side.
