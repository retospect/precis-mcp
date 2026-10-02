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

1. **Confirmed small bugs** (each S, verified on the 10-02 tree) — wrong
   output today, cheapest first:
   - `claim-publication-nanopub-ots` § latent bug: `nanopub/evidence.py`'s
     source filter is `("paper", "patent")` while `attach_evidence` also
     accepts `edgar`/`datasheet` — such evidence silently drops from a bundle.
     Reto ruled 10-02 (`claims-and-evidence-1`): both publishable, edgar
     cites its SEC accession. Edgar widening in flight; the datasheet
     identifier waits on `claims-and-evidence-2` (a datasheet ref stores no
     URL). The contradicts half was already fixed (D1 `live_contradicts`).
2. **Scope chain** — `scope-key-vocabulary-registry` (two hardcoded key sets,
   `sentence_lint.SCOPE_KEYS` and `canon._SCOPE_KEYS`) →
   `taproot-hub-scope-no-edit-door` (`edit(kind='finding')` takes no
   `scope=`; open question: does a scope edit re-derive `pub_id`?) →
   `aida-uri-ignores-scope` (2 duplicate pairs, prod data) → the scope
   backfill inside `nanopub-corpus-remediation`.
3. **Adjudication** — `disputes-adjudication-workflow`, which absorbs
   `taproot-adjudicate-reopened-claims` (premise half-stale: the widening arm
   now files non-blocking `disputes`, but a demotion still reopens reviewed
   hubs) · `contradicts-conflates-evidence-and-prose-misuse` (residue = the
   `misused-by` relation only) · `claim-conflict-search` items 4–5 (slice 1
   shipped dark; nothing reads its output at approve).
4. **Doors** — `taproot-merge-mcp-surface` (web door shipped; no MCP verb) ·
   `taproot-cite-time-attach-or-mint` · `taproot-directed-claim-minting` · `nanopub-supersede-door` ·
   `preprint-to-published-cite-upgrade`.
5. **Evidence quality** — `evidence-edge-verification` (rubric labels +
   approve gate; whether the verify/repair runs happened needs prod data) ·
   `pa-arm-locate-should-capture-a-verbatim-quote` ·
   `taproot-sole-supporter-coverage` (half shipped; open = name a candidate
   originator) · `taproot-numeral-audit` · `taproot-backfill-defects`
   (open: [pc] silent drop, fragment continuation clauses, the 8-finding
   demotion triage, the extract_claim outage audit in `chase.py` and the
   `hub_refine` paths) ·
   `taproot-claim-quality` (§a mostly superseded by the hearsay gate) ·
   `taproot-inbound-grounding` · `computed-pathways-cannot-be-cited-as-
   claim-evidence` (open: magnitude re-check, re-dispatch of a `ready`
   pathway, nanopub visibility, web attach form).
6. **Compound + publication, last** — `taproot-compound-migration` (L;
   blocked on `reground.py`'s embedding-ranking TODO; blocks
   `claim-publication-nanopub-ots`) → `claim-publication-nanopub-ots` ·
   `retire-fi-go-nanopub` (open: `[np<id>]` grammar + migration sweep) ·
   `nanopub-corpus-remediation` (step 5: `identity.py` hashes the sentence
   without `_normalize_number_text`). Publishing before identity and
   evidence settle publishes the defects.
7. `taproot-claim-model-v2` — persisted `claim_type`; design-heavy, no
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
