# claims and evidence — one identity per claim, every edge checked

**Status:** ends when every claim has one identity, every evidence edge is
checked against its source, contradictions are ruled on, and the hub corpus is
publishable (`backlog/claim-publication-nanopub-ots.md`). Today the cluster of
~35 taproot/finding/nanopub items has never been triaged as a set: Do-next 1 is
the one confirmed bug, Do-next 2 is the triage that ranks the rest, and
everything under 2 is provisional until it runs.
**Last reviewed:** 2026-10-01
**Worktree:** `claims-and-evidence`
**Active:** yes — Reto 2026-10-01.

Created 2026-10-01 from the memory-graph pillar review. The taproot umbrella
(hub model, seniority, hub-refine) stays referenced by `knowledge-mesh.md`; the
defect and follow-on cluster below is owned here.

## Do next

1. **backlog/computed-pathways-cannot-be-cited-as-claim-evidence.md** — the
   bug half. A pathway evidence edge is written, but
   `taproot/seniority.py::_EVIDENCE_SRC_KINDS` filters it out of
   `derive_evidence`, so the claim page shows no originators; read the item's
   "Still open" section for the narrower `repair_evidence.py` filter and the
   nanopub-bundle gap. Silent wrong output, hence first.
2. **Triage pass over the rest**, five lines; within a line the order is
   provisional until the triage says which items still reproduce:
   - **Identity / model** — `backlog/taproot-claim-model-v2.md` ·
     `aida-uri-ignores-scope` · `scope-key-vocabulary-registry` ·
     `taproot-hub-scope-no-edit-door` · `finding-stable-identity` ·
     `taproot-compound-migration` ·
     `compound-hub-posture-ignores-conjunct-evidence`. Upstream of the other
     four: an edge cannot be checked or published against a claim whose
     identity moves.
   - **Contradiction / adjudication** — `disputes-adjudication-workflow` ·
     `taproot-adjudicate-reopened-claims` ·
     `contradicts-conflates-evidence-and-prose-misuse` ·
     `claim-conflict-search`.
   - **Mint / attach doors** — `taproot-cite-time-attach-or-mint` ·
     `taproot-merge-mcp-surface` ·
     `direct-mint-apply-rerolls-the-reviewed-sentence` ·
     `taproot-directed-claim-minting` · `nanopub-supersede-door` ·
     `preprint-to-published-cite-upgrade`.
   - **Evidence quality** — `evidence-edge-verification` ·
     `pa-arm-locate-should-capture-a-verbatim-quote` ·
     `taproot-sole-supporter-coverage` · `taproot-numeral-audit` ·
     `taproot-backfill-defects` · `taproot-claim-quality` ·
     `taproot-inbound-grounding` · gr458941 (fisheye Claims group: originator
     never derived — same read path as Do-next 1).
   - **Publication** — `claim-publication-nanopub-ots` ·
     `retire-fi-go-nanopub` · `nanopub-corpus-remediation` ·
     `approve-prefill-blank-doi`. Last: publishing a corpus whose identity and
     evidence are unsettled publishes the defects.

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
  · **figure-kind-slices** · **per-user-library-link** ·
  **paper-annotation-critique** — unpark when a draft needs them.

## No action needed

- (none)

## Seam

- `knowledge-mesh.md` keeps the taproot umbrella (hub model, seniority,
  hub-refine); this thread owns the defect and follow-on items. A fix that
  changes the hub schema is knowledge-mesh's call.
- `ingest-and-fetch.md` owns extraction fidelity; a claim quote that fails
  `evidence-edge-verification` because the source text is corrupt (the glyph
  pair) is theirs, not a claims defect.
- `draft-authoring.md` owns the draft side of cite-time attach; the doors above
  own the claim side.
