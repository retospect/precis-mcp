# claims and evidence — one identity per claim, every edge checked

**Status:** ends when every claim has one identity, every evidence edge is
checked against its source, contradictions are ruled on, and the hub corpus is
publishable (`backlog/claim-publication-nanopub-ots.md`). Triaged 2026-10-02
against the code: every item below was read and its named code checked; none
is fully shipped, six carry a confirmed bug, and the rest order along four
dependency chains.
**Last reviewed:** 2026-10-04
**Worktree:** `claims-and-evidence`
**Active:** yes — Reto 2026-10-01.

Created 2026-10-01 from the memory-graph pillar review. The taproot umbrella
(hub model, seniority, hub-refine) stays referenced by `knowledge-mesh.md`; the
defect and follow-on cluster below is owned here.

## Do next

0. **Now (10-03).**
   - **Resume (10-04 06:5xZ, prod e0b75bdc; nothing unlanded on the branch):** in prod: disputes re-audit (468 removed, 9 kept, claims-and-evidence-5), grounding builds 1+2 (below), the `human_rejected` mint-gate fix (7a23cb5e). Landed, waiting for the round-6 deploy: coverage noise cuts (78f2ae0c), reword `--after-ref-id` batches + `route_log` (2698cbcd), hub_refine pass budget (72a56df7: 600 s wall per pass via `PRECIS_TAPROOT_REFINE_PASS_WALL_S`, unstarted hubs handed back due; 12 verifies per hub per pass via `PRECIS_TAPROOT_REFINE_VERIFY_PER_HUB`; the 05:20–06:30Z pass that held the fetcher host's worker was 9 hubs, 231 Haiku `chase:verify` calls at 15.6 s, $2.81, nearly all from the widen arm). Next without Reto: (1) D1 dogfood: 14 hubs refined since 04:40Z, the read-only watch reports `method-gap` links and memo entries at 20; (2) after round 6 deploys, reword sweep dry run then `--apply` in ≤800 batches (below); (3) sole-supporter warning split, participial past-tense advisory, gr464222 re-extract. Waiting on Reto: claims-and-evidence-5 decision 2 (`disputes-judges-on-sonnet`), -8 (qwen extractor run, also waits on 0186), -12 (supersede fi211520/fi236297), -10 (anchor + publish fi263188).
   - **Round-4 dogfood (10-04 04:4xZ, prod 727728cc, deployed venv, read-only):** `nanopub check fi189535` → `term-coverage` TEM/STS with pc209502 first (as built); `sweep-grounding` = the pre-deploy measurement exactly (17 hubs, 1 shallow, 9 gap); the proves/copula lint holds. **Two defects found, fixed in the next sha:** (1) pre-existing since 08-15: the mint gate's `rejected-memo` arm (and the reword cohort) read `meta.taproot_rejected` as "a human rejected this claim", but hub_refine writes its per-source "does not support" judge memo under that key — every refined hub failed approve (prod: 1,124 findings carry it, 119 candidates, every entry the judge shape, no human writer exists); now `taproot.hub.human_rejected` ignores the judge memo; (2) a `term-coverage` suggestion was a glued reference list (fi263188); `term_coverage.is_bib_text` now drops such chunks from G2 suggestions, the sweep and D1 candidates. nanobuds-paper dogfooded G3 on dr173020 (37 warnings as measured; applied 22 method passages, suggestions right in 22 of 24 non-noise cases); its noise data cut three classes in `taproot/coverage.py`: an undefined written-out acronym ("density functional theory" covers DFT, 3+ letters), signed numbers (-0.85 covers a claimed 0.85; a claimed negative needs a minus), generic heads (calculations/measurements/analysis…) never coverage terms but still count for D2 ordering (`claim_terms(include_generic=True)`); dr173020 20 → 13 warnings, frozen gap hubs 9 → 8. Left: aliases (CNB vs NanoBud), a parenthetical cited to another hub inside a pin's span, uncited sibling-clause numbers blamed on the nearest pin. D1 at 07:00Z: 14 hubs refined since 04:40Z (none since the 05:20–06:30Z pass), 1 `method-gap` memo entry, 0 `method-gap` links, so the arm ran but has not yet attached anything; re-check `links.meta.widen.via='method-gap'` and `reground_seen` method-gap entries after round 6 deploys. The 2 `disputes` filed since the deploy (links 3022077 MOF-5 moisture, 3022083 UiO-66 pH) both carry `same_setup` and `terminal` with matching setups, and both read as real contradictions.
   - **Reword sweep is batch-only** (orchestrator round-5 review, 10-04). After 7a23cb5e the cohort is 2,457 hubs (381 newly admitted judge-memo hubs; top codes no-epistemic-mode 2,130, no-evidence-verb 1,905, over-long 566), ≈ $25–39 for one pass at Haiku MEDIUM rates (estimated: no `taproot:reword` row had ever reached `llm_call_log`). Run `precis taproot reword-sweep --limit 800 [--after-ref-id N]` per batch (≤ ~$13), taking N from the previous batch's stderr `last ref_id:`; `route_log` is bound since 2698cbcd, so spend is measurable after batch 1. Not run yet: dry run first, then `--apply`, after round 6 deploys (`--after-ref-id` is not in prod e0b75bdc).
   - **`disputes-judges-on-sonnet`** (high; waits on Reto's claims-and-evidence-5 decision 2). Move both disputes judges from Tier.MEDIUM to Tier.BIG; the evidence is in the item.
   - **Extraction truncation** (found by graph-memory-consumers, reviews/graph-memory-consumers.md §2). 147/275 prod `taproot:extract` replies were cut at 220 tokens and 114 silently became one atom.
     - Fixed: an explicit `canon._EXTRACT_MAX_TOKENS` = 1536 (graph-memory-consumers' replay of the 147: p99 752, max 924), and `_top_level_payload` refuses a cut-off reply by two arms: an object opened and never closed (catches a cut inside `assertions`, 23 prod replies) or `"claims"` named but not at top level. Strict → retryable `ExtractionUnavailable`; lenient → empty, never one atom, with a warning naming `origin=` (the chunk or ref).
     - Open (gripe gr464222; close it when done): re-extract the 114 one-atom findings plus the 30 extractions the router returned nothing for (lost outright; the cut-reply classification is in gr464222). Additive only: mint the missing atoms and composites beside the existing finding, never retire or rewrite it (orchestrator 10-03; costs cents, no Reto item; retiring would need one). Prod writes are back (14:40Z). Mapping: the calls carry `ref_id` NULL, and the minting path is `taproot/backfill.py::apply_chunk` on draft cite groups. Each capped request's PASSAGE (`composite-ab/prod_capped_requests.jsonl`) must be matched to its cite group, then the cascade re-run (dedup attaches to an existing hub with the same sentence). The extract call is the small tier (openai_compat, not `claude -p`), so the melchior runbook path (`docs/runbooks/prod-one-off-cli.md`) can run it. The lenient `extract_claim` callers (`chase.py` bridge, `hub_refine`) still read empty as final, which is the outage audit in item 4.
   - **report/definition types**: branch `report-definition-0183` @ 500cdbd65 (migration 0183) → the orchestrator's gate in round 2, behind 0182. Shared `review_like.proceedings_venue()` with a not-venue veto (prod corpus read); the accepting arm is frozen in `grounding.report_source_arms`. Abbreviated journal names ("J. Phys. Conf. Ser.") carry no venue word and are refused.
   - **False `disputes` root cause** (Reto, claims-and-evidence-4 answered 14:46Z). Done 10-03: kept fi211520 (published) and fi189521 (anchored), removed the 8 cross-sample `disputes` edges (rows with full meta in `scratch/adjudicate/ce4-edges-snapshot.txt`, re-addable), resolved al462068 and al461616, td462463 done. Diagnosis in review item claims-and-evidence-5: the verdict contract plus what the judge is handed (no setup comparison, `terminal` ignored, no claim source passage); model minor; skills and MCP not involved. Fixed (round 3): both judges state `claim_setup`/`passage_setup`/`same_setup` (+ `primary` in the strict judge) and read the claim's own establishing passage (`hub_refine._claim_source_passage`); a `disputes` link and its demotion are filed only when contradicts ∧ same_setup ∧ terminal/primary (`_disputes_allowed`, `StrictVerdict.disputes_ok`), otherwise memoed and logged "withheld"; the reground candidate path gets real neighbours. claims-and-evidence-6's W2–W4 + P1–P4 deployed in round 3 (setup fields, `llm_request_hash` on memos/log/link meta, `view='judgments'`, `remove_disputes` keyed on `link_id`). Tier eval and re-audit done (Resume above); open = `disputes-judges-on-sonnet` (claims-and-evidence-5 decision 2).
   - **fi263188 signed** (Reto 2026-10-03T19:37Z; claims-and-evidence-10). Trusty `RAyNWGyZ…OZy50`, publish row 146. Preflight's only blocker is the state: anchor (`precis nanopub anchor`), then publish; both are Reto's.
   - **Past-tense advisory on participial adjectives** (found in the proves/proof corpus pass, 10-03). fi263188's stored sentence ("… shows that no algorithm can decide … translated copies …") warns past-tense only on "translated", an adjective before a noun. Consider exempting a past participle that directly modifies a noun ("translated copies", "given set"); measure corpus flips first. Small.
   - **"Sole supporter, possibly derivative" is mostly noise** (Reto 10-03, traced on fi263188). `taproot/seniority.py::derive_evidence` derives originators only from `cites` edges among attached supporters, so any single-supporter hub gets the ⚠. That is 2,980 of the 3,550 supported hubs (~84%), 342 of them with an `establishes` edge. Fix after P1 stores the judge's `primary`/`terminal`: no warning when the sole passage was judged the source's own result, the ⚠ when it was judged a recitation or carries citation markers (the gr307372 risk), and a neutral "originality not checked" otherwise. Small; rank after the W2–W4 build. Trace in `review-queue/answered/claims-and-evidence-7.md`.
   - **Signed-grounding recurrence: builds 1 + 2 DONE 10-04** (claims-and-evidence-9 approved; landed 7c3ccff6, 555b82cf, 249aa4c6, e471bd23, 460d9a54, f51145c2 in round 4; summary in claims-and-evidence-11, mint question answered there). G1 `grounding-stale` blocks sign (confirm or reopen; it also lists system-attached edges, e.g. D1's, by design; legacy meaning: the 17 rows approved before 10-04 carry no `frozen_at`, all signed or later, so the `updated_at` fallback only matters if one flips back to reviewed, and then evidence linked before that flip is not listed; no approval event log exists to backfill from); G2 `term-coverage` and G3/G4 pin checks warn (decided: stay warnings, D3 measured 52.9 gap hubs per 100 with about 22% noise); D2 prefill ranks captions/methods first; D3 `precis nanopub sweep-grounding`; D1 hub_refine `method-gap` arm (caps 1/term/pass, 2/term/claim version, 4/hub; full-pass ceiling 1,050 calls ≈ $16.80 Haiku; the 1-per-term cap was set by this thread to stay under $25; verifier calls paid before a savepoint rollback are charged as unjudged attempts, a term with nothing to judge gets a per-claim-version `no-candidates` marker that only a claim edit clears, and `METHOD_GAP_CALLS_PER_PASS = 200` (~$3.20 Haiku) caps one whole pass). Open follow-ups: claims-and-evidence-12 (Reto: supersede fi211520 and fi236297); SI method passages wait on parent-aware source counting; G2/G3 noise classes (negated terms, extraction glue, locants/range endpoints, generic "analysis"/"measurements") for a later narrowing pass; a minting-time verify plus a `minted_by` stamp (claims-and-evidence-11) is unbuilt.
   - **Compound dry run + the "because" decision** (Reto 10-03, td345835 closed and re-scoped here; ranked after claims-and-evidence-6). Fresh `taproot-migrate` dry run on today's hubs, then a review item recommending conjunct-of split vs. one causal claim kept whole (`taproot-compound-migration.md`, Open). Phase 0 (10-03, free): 3,954 eligible, 1,794 likely-composite / 1,343 uncertain / 817 likely-atomic (`scratch/compound-dryrun/score-1003.json`); 136 claim sentences carry an explicit causal connective, 336 "via/through/mechanism". The extractor already folds mechanism clauses into the atom they explain (`canon._EXTRACT_PROMPT`, P2-13), so the dry run measures whether that holds. Canary 10-03: failed twice, then OK 11/11 after two fixes (round 3): the number gate splits a glued degree sign ("19°" read as invented vs "19 degrees"), and canary passages 7 and 9 name their subject ("The reaction"/"The catalyst" alone is a dangling referent that rule 1 rejects, so Haiku answered no-claim). Passage 2 still flips between 1 atom and 5 (one per angle, held as `nested`) in 1 of 3 runs. `dry-run --offset` added so one bulk run splits across processes: each call is ~45 s (`claude -p` Haiku, thinking on), so 3,954 serial is ~49 h, 6 slices ~8 h. These local calls write no `llm_call_log` rows (the documented haiku-lane blind spot still holds). The 6-slice run was refused by the session's permission check (10-03 16:0xZ); the command is in claims-and-evidence-8 for Reto. Reto 16:34Z: split with an `explains` relation (3–4 builds; design in `taproot-compound-migration.md`). **Build 1 is on branch `explains-0186` @ bf7b933a7 (migration 0186) → the orchestrator's gate**:
- contents: relation, extraction contract + prompt, `bad-explains` gate, `apply_extraction`/backfill/`apply_migrate` mint the edge, a transaction-visible cycle guard, canary passages 12–13;
- reviewed; 780 tests pass;
- live Haiku canary on its final prompt OK, 13/13 within bars (18:35Z).
Note: backfill is live, so once deployed, newly minted causal claims split. The bulk dry run follows the deploy. Local lane (answered in the item 16:41Z):
- `--tier small` (glm-4.7-flash, ~7 s and ~$0.0001 per call) must run on melchior; from the Mac it gets connection refused.
- glm needs a fresh canary: the August "small collapses" verdict was very likely the 220-token cap.
- Sonnet `--tier big` passed the canary 11/11.
- No tier's calls from `taproot-migrate` reach `llm_call_log`: it binds the meter but not `route_log`. The fix is in a coder worktree.
- Plan: deploy round 3 with build 1 and the log fix, run the glm canary on melchior, then the dry run there with `--tier small --escalate` plus a 100-hub Haiku sample.
   - **Citation rule** (Reto 10-03, landed 4d724fa3): skills + `_chase_llm._PROMPT_VERIFY` + `hub_refine._JUDGE_PROMPT`. Untested by any eval: after deploy, check the next hub_refine run files no cross-sample `disputes`. Deployed in round 2 (63301c5c, 13:49Z). At 13:53Z hub_refine had refined 0 hubs since the deploy, so the check is still open. Baseline from the 6 h before the deploy: 574 `chase:verify` calls and 11 `widen` disputes filed (`scratch/adjudicate/round2-dogfood.sql`).
     - Round-2 dogfood for the orchestrator (hub cite policy + hub_refine rules, 10-03 ~15:00Z, deployed venv on melchior, read-only):
       - hub_refine refined fi218687 (B₁₂N₁₂–C₅₀ nanobud vibrational frequencies) at 14:27:26Z, stamped `last_refined_version` "1". The version was not bumped on purpose; see "Old verdicts keep the old semantics".
       - Its deployed cite prints the originator alone (`azadi23`; the 3+ corroborators do not print).
       - fi218681, which has no originator, prints the earliest grounded verified primary `sharma18a` and leaves out the review `kharisov17`. Both follow `cite.resolve_hub_print`.
       - Script: `scratch/cite-standard/hub_print.py`.
     - Judge check, 14:45Z: hub_refine refined 8 hubs since the deploy, 99 `chase:verify` calls (none at the old 220 cap), 3 `widen` disputes, a rate similar to the baseline (11/574). Two of the three are wrong under the new rule: 948 → fi218681 uses a cited background sentence about nanobuds in general (C60) against a small-fullerene claim; 543 → fi218683 misreads the contrast (the judge invents "fullerene atoms"). 3211 → fi218681 is plausibly a real contradiction. The prompt change alone does not stop cross-setup disputes, which is input to the root-cause item above. No `taproot:extract` or reground-judge call since the deploy.
     - Round-2 dogfood, 13:53Z: the deployed cite rule prints the same keys as this tree on all 26 drafts with hub cites (`measure_all.py`, 0 changed). The deployed skill text and `_EXTRACT_MAX_TOKENS = 1536` are in the prod package. No `taproot:extract` calls had happened yet.
     - **Old verdicts keep the old semantics** (orchestrator round-2 review, finding 1). Decided: no `REFINE_VERSION` bump. A bump makes the 1,205 unsigned refined hubs due, but step 3 filters attached and memoed sources before the verifier, so it re-judges nothing. Cost would be up to ~$140, the spend of the current pass (14 days: chase:verify 9,342 calls $77.57, reground-judge 6,662 calls $61.97). Old-semantic verdicts are the ones dated before the round-2 deploy: 480 live `disputes` edges (430 `widen` on 289 hubs, 50 `reground` on 33 hubs) and 11,021 memo entries on 1,047 hubs. Next: re-judge the 480 `disputes` edges (~$4.5), retire the ones it no longer calls contradicting, and list the hubs their demotion reopened. Blocked 10-03: Tier.MEDIUM routes to `claude -p`, which is not logged in for the deploy user on melchior, and running the judge locally against prod was refused by the session's permission classifier. Reto 10-03: this session runs it (and the tier eval) after claims-and-evidence-6; if the permission check refuses, the exact command goes in the item and he decides. Dry-run script: `scratch/adjudicate/rejudge_disputes.py`. Re-judging the 10,583 memoed "no" verdicts under the new "partial" (passages combine) is ~$90, over threshold, so it is Reto's call. Sample 100 first to get the flip rate. Query: `~/.claude/projects/-Users-reto-precis-mcp/scratch/adjudicate/refine-version.sql`.
   - **Exported cite keys change on next export** (orchestrator round-2 review, finding 2). `cite.resolve_hub_print` replaces "every corroborator" with primary + ≤2 confirmations (else one unverified paper), with no marker and outside `PATTERNS_VERSION`. Measured 10-03 against prod while it still ran the old rule (`scratch/cite-standard/measure_all.py`, all-drafts.json): 12 of 26 drafts with hub cites change, all by dropping keys. dr42995 (Molecular Computing from Self-Assembling Nanoscale Cubes) 229 hubs, 840 → 593 keys; dr48057 (MOF for Electrodes) 11, 87 → 78; dr173020 (nanobuds review) 10, 103 → 82 (told nanobuds-paper); dr44369 (nanoscale transistor) 7, 37 → 19; dr43004 (Graphene, Nanoribbons, Nanotubes to Carbon D…) 3, 24 → 21; dr448178 (DFT Accuracy for Hydrogen Bonding) 3, 31 → 29; dr43029 (Graphene Transistor Structures) 2, 21 → 13; dr348633 (NO to NH3 findings) 2 hubs, same 620 keys; dr43006 (Waterbook) 13 → 12; dr198320 (Nanoscale Transistors: Molecular Devices) 4 → 1; dr259465 (Precis capability landscape) 13 → 12; dr344089 (Constraint-first multiscale design) 6 → 5.
   - **Web anchor** (Reto, claims-and-evidence-3 → option 1, 10-03): sha256 of the fetched content at every fetch (`cache_state.content_sha256`, migration 0184, no backfill), the passage's sha frozen at approve, "source changed since approval" on drift; web becomes an accepted report source. Built: branch `web-anchor-0184` @ be2b3b782 (stacked on `report-definition-0183`) → the orchestrator's gate after 0183. A byte-identical refresh keeps its chunks; sign tolerates a drifted web passage on its frozen quote + sha (papers/datasheets still refuse a vanished chunk). Side effect: a trafilatura upgrade does not re-extract a page whose bytes are unchanged. Open after deploy: hashing raw bytes may flag drift on every refresh of a dynamic page (ads, timestamps); count drift across refreshes vs. extracted-text changes before trusting the notice.
   - **Web as a hub evidence kind** (follow-up to the web anchor). `taproot/hub.py::EVIDENCE_SRC_KINDS` is `{paper, patent, edgar, datasheet}` (pinned by `test_kind_totality` and the 0180 relation-constraint seed), so a web page can be quoted in an approve payload but cannot be attached as a hub supporter, listed by `load_bundle` or offered in the approve prefill. Before widening it, measure what a web supporter does to backfill auto-attach (draft cites to web refs), seniority (no year) and the cite fallback (no cite_key). Then it is one relation-constraint migration plus the totality test.
   - **Prune draft-sentence check** (Reto, nanobuds-paper-22, round 3). The reground judge reads the citing draft sentence(s) and never prunes an edge that is the sole support for one of their clauses, or that a live `[fi…>pc…]` cite pins; it also re-judges against the current hub sentence. Then `slice_refine_eval` must pass. Test case: dr173020 (nanobuds review), `~/.claude/projects/-Users-reto-precis-mcp/nanobud-fidelity/prune-edges.tsv` → prune the 78 free edges, keep the 21 needed.
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
   approve gate; whether the verify/repair runs happened needs prod data.
   The support standard is Reto's citation rule (10-03): full text,
   passages combine, the pin is provenance, polarity a separate verdict.
   It is in the skills and both verifier prompts; the rubric's §5 is
   rewritten to it) ·
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
