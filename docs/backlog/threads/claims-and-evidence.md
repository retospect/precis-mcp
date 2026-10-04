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

0. **Now (10-03).**
   - **Resume (21:45Z, round 3 live at 929107f3; round 4 marks 4508bcd4 + 7d60f008):** disputes gate verified on deployed code (zero-write run). Tier eval done → `disputes-judges-on-sonnet` (Haiku filed 1 known-false dispute; Sonnet and Opus none). Re-audit DONE 10-04 on prod 4181421c (Sonnet judge, deployed gate): removed 468 `disputes` links (460 pre-rule + 8 filed between the citation rule and the gate deploy) on 319 hubs, kept 9; re-promotion list empty (worker logs back to 08-17: no dispute ever reopened a claim); snapshots + hub list in scratch `adjudicate/`, report in claims-and-evidence-5. Extractor canary: qwen3-next-80b 3/3, glm 0/3; the strict small-tier extract timeout fix landed (7d60f008), so claims-and-evidence-8's full dry run on qwen waits only for 0186 and Reto's approval. claims-and-evidence-11 answers the fi189535 mint question.
   - **`disputes-judges-on-sonnet`** (high; waits on Reto's claims-and-evidence-5 decision 2). Move both disputes judges from Tier.MEDIUM to Tier.BIG; the evidence is in the item.
   - **Extraction truncation** (found by graph-memory-consumers, reviews/graph-memory-consumers.md §2). 147/275 prod `taproot:extract` replies were cut at 220 tokens and 114 silently became one atom.
     - Fixed: an explicit `canon._EXTRACT_MAX_TOKENS` = 1536 (graph-memory-consumers' replay of the 147: p99 752, max 924), and `_top_level_payload` refuses a cut-off reply by two arms: an object opened and never closed (catches a cut inside `assertions`, 23 prod replies) or `"claims"` named but not at top level. Strict → retryable `ExtractionUnavailable`; lenient → empty, never one atom, with a warning naming `origin=` (the chunk or ref).
     - Open (gripe gr464222; close it when done): re-extract the 114 one-atom findings plus the 30 extractions the router returned nothing for (lost outright; the cut-reply classification is in gr464222). Additive only: mint the missing atoms and composites beside the existing finding, never retire or rewrite it (orchestrator 10-03; costs cents, no Reto item; retiring would need one). Prod writes are back (14:40Z). Mapping: the calls carry `ref_id` NULL, and the minting path is `taproot/backfill.py::apply_chunk` on draft cite groups. Each capped request's PASSAGE (`composite-ab/prod_capped_requests.jsonl`) must be matched to its cite group, then the cascade re-run (dedup attaches to an existing hub with the same sentence). The extract call is the small tier (openai_compat, not `claude -p`), so the melchior runbook path (`docs/runbooks/prod-one-off-cli.md`) can run it. The lenient `extract_claim` callers (`chase.py` bridge, `hub_refine`) still read empty as final, which is the outage audit in item 4.
   - **report/definition types**: branch `report-definition-0183` @ 500cdbd65 (migration 0183) → the orchestrator's gate in round 2, behind 0182. Shared `review_like.proceedings_venue()` with a not-venue veto (prod corpus read); the accepting arm is frozen in `grounding.report_source_arms`. Abbreviated journal names ("J. Phys. Conf. Ser.") carry no venue word and are refused.
   - **False `disputes` root cause** (Reto, claims-and-evidence-4 answered 14:46Z). Done 10-03: kept fi211520 (published) and fi189521 (anchored), removed the 8 cross-sample `disputes` edges (rows with full meta in `scratch/adjudicate/ce4-edges-snapshot.txt`, re-addable), resolved al462068 and al461616, td462463 done. Diagnosis in review item claims-and-evidence-5: the verdict contract plus what the judge is handed (no setup comparison, `terminal` ignored, no claim source passage); model minor; skills and MCP not involved. Fixed (round 3): both judges state `claim_setup`/`passage_setup`/`same_setup` (+ `primary` in the strict judge) and read the claim's own establishing passage (`hub_refine._claim_source_passage`); a `disputes` link and its demotion are filed only when contradicts ∧ same_setup ∧ terminal/primary (`_disputes_allowed`, `StrictVerdict.disputes_ok`), otherwise memoed and logged "withheld"; the reground candidate path gets real neighbours. claims-and-evidence-6's W2–W4 + P1–P4 deployed in round 3 (setup fields, `llm_request_hash` on memos/log/link meta, `view='judgments'`, `remove_disputes` keyed on `link_id`). Tier eval and re-audit done (Resume above); open = `disputes-judges-on-sonnet` (claims-and-evidence-5 decision 2).
   - **fi263188 signed** (Reto 2026-10-03T19:37Z; claims-and-evidence-10). Trusty `RAyNWGyZ…OZy50`, publish row 146. Preflight's only blocker is the state: anchor (`precis nanopub anchor`), then publish; both are Reto's.
   - **Past-tense advisory on participial adjectives** (found in the proves/proof corpus pass, 10-03). fi263188's stored sentence ("… shows that no algorithm can decide … translated copies …") warns past-tense only on "translated", an adjective before a noun. Consider exempting a past participle that directly modifies a noun ("translated copies", "given set"); measure corpus flips first. Small.
   - **"Sole supporter, possibly derivative" is mostly noise** (Reto 10-03, traced on fi263188). `taproot/seniority.py::derive_evidence` derives originators only from `cites` edges among attached supporters, so any single-supporter hub gets the ⚠. That is 2,980 of the 3,550 supported hubs (~84%), 342 of them with an `establishes` edge. Fix after P1 stores the judge's `primary`/`terminal`: no warning when the sole passage was judged the source's own result, the ⚠ when it was judged a recitation or carries citation markers (the gr307372 risk), and a neutral "originality not checked" otherwise. Small; rank after the W2–W4 build. Trace in `review-queue/answered/claims-and-evidence-7.md`.
   - **Signed-grounding recurrence** (Reto via nanobuds-paper-29, 16:06Z; review item claims-and-evidence-9, filed with nanobuds-paper). fi189535's grounding froze 23 min before its TEM/STS `establishes` edges existed. The approve prefill (`precis_web/nanopub_render.py::_suggested_payload`) only offers already-attached chunks, and no sign gate checks depth or method coverage (`taproot/reword.py::_ungrounded_modes` is advisory, reword-only, with no acronyms). Proposed:
     - build 1: G1 freshness at sign (blocking with a confirm), G2 term coverage with in-paper acronym expansion, G3 pin coverage at draft edit and export, G4 the pin-within-grounding-papers rule, G5 skill lines including absence-claim discipline;
     - build 2: D1 hub_refine attaches method passages, D2 prefill ranks body passages first, D3 read-only sweep of signed hubs.
     **Approved (Reto 16:34Z): build 1, then build 2.** G1 blocks at sign (re-review, or confirm and go ahead); G2 and G3 warn until D3 measures them. **G1 + G5 landed 10-04** (`nanopub/freshness.py`, gate `grounding-stale`, `sign --accept-newer-evidence` / page checkbox, audit "signed over newer evidence" in `meta.reground_log`; `frozen_at` stamped in the grounding envelope by `nanopub_approve`, legacy rows fall back to `updated_at`; 0 reviewed rows in prod at landing, so nothing is blocked retroactively). **G2 landed 10-04** (`taproot/coverage.py` pure rule + `nanopub/term_coverage.py` suggestions; non-blocking preflight issue `term-coverage` on the approve and sign views, `nanopub check`/`preflight`, a printed warning on `sign`; prod rows 9 of 17 warn, 16 terms, fi189535 flags TEM and STS with pc209502 / pc209505, 209508, 209515; the full-corpus rate stays D3's job). **G3 landed 10-04** (`nanopub/pin_lint.py`: the sentence holding a `[fi<hub>>pc…]`/`[fi<hub>+pc…]` token against the pinned passage(s), `+` together with the hub's default passages, via G2's `analyse`; a `⚠ pin:` line on draft put/edit only for a new pin or a newly uncovered term, in `ExportResult.warnings` for every pin; a pin in a multi-cite sentence is judged on its own span only and bare years are exempt; measured on dr173020: 37 of 89 pins warn (54 before those two fixes); known noise left for D3: negated terms, PDF-extraction glue, range endpoints and locants read as numbers). **G4 landed 10-04** (same module: warns when a frozen-grounding hub's pin names a paper outside the grounding's papers; 0 of 10 frozen-hub pins in dr173020). Build 1 is complete. **D2 landed 10-04** (`precis_web/nanopub_render.py::_prefill_chunk_order` → `term_coverage.order_for_prefill`: for a body-required or method-naming claim the prefill orders caption, methods/results, body, abstract/front matter, then more claim terms carried first; reorder only, a frozen or parked payload still wins; prod 55 of 104 multi-passage hubs change first passage, fi189535 now opens on its TEM caption pc209502 then STS pc209509). **D3 landed 10-04** (`nanopub/grounding_sweep.py`, `precis nanopub sweep-grounding [--format md|json] [--state …]`, read-only: shallow grounding = every passage front matter or a definition sentence for a body-needing claim, short letters exempt; G2 gaps; better passages; summary per 100 hubs; prod, 17 frozen groundings: 1 shallow (fi211520, real), 9 with a G2 gap = 52.9 per 100 hubs of which 2 hubs and 4 of 16 terms are noise (negated/generic mode words, a chirality index read as a number, a superscript), fi189535 flagged on TEM/STS gaps, fi236369/fi236370 clean; **decided: G2 and G3 stay warnings**, 52.9 gap hubs per 100 with about 22% of them noise is too high to block on; `chunk_tier` now ignores a front-matter heading over more than 6 chunks of one paper, so a paper filed wholly under "ABSTRACT" is no longer front matter in D2/G2 either, and `rank_for_claim` ties prefer the grounding's own papers; the D2 first-passage count is unchanged at 55 of 104). **D1 landed 10-04** (`workers/hub_refine.py::_method_gap_arm` + `nanopub/method_gap.py`: after discovery, for each acronym/mode term the sentence names that no `establishes`/`corroborates` passage carries (numbers skipped in v1), search the evidence papers' chunks, rank by D2's `rank_for_claim`, judge at most 1 per term per pass (2 per term per claim version) and 4 per hub with the widen verifier and attach the ones that verify with `meta.widen.via = "method-gap"`; rejects and LLM failures go in the passage-grained `reground_seen` memo; the arm runs in a savepoint and not under reground; SI method passages are not searched in v1 (0 live evidence edges from SI refs in prod, 1 SI ref total) and wait on parent-aware source counting; no `REFINE_VERSION` bump, so existing hubs get it as they come due; **full-pass ceiling if every live hub were refined once, measured on prod 10-04: 3,539 hubs, 1,050 with an uncovered term, 1,050 verifier calls = $16.80 at $0.016/call (Haiku), $8.40 at $0.008 (Sonnet); 2 per term would have been 2,019 calls, $32.30, over the $25 line, so Reto set 1 per term**).
     - **Open question to answer first** (`review-queue/answered/claims-and-evidence-9.md`): how was fi189535's hub minted? Which reader, what it saw of the paper, and why the TEM caption and STS passages it read did not become evidence at mint. If the minting read is the natural source of the grounding, say what capturing it there would take. Answer in a new review item.
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
   - **Session advisory locks** (gr463966, routed here by Reto's organizer-pgbouncer-1 ruling; ingest-and-fetch yielded claim.py). `ingest/claim.py::Claim`, `workers/chunk_keywords.py` and `workers/anki_sync.py` (the audit missed anki_sync) hold `pg_try_advisory_lock` across pgbouncer transaction pooling, so lock and unlock land on different backends. The leaked lock at 14:5xZ: chunk_keywords' `_LOCK_KEY` (classid 3815272043 / objid 1600878336) on pooled backend pid 15730 (started 14:14Z). Backend 11267 from session-mcp's note is gone, so the leak recurs. Fixed (round 3): `store/advisory.py::try_xact_advisory_lock` (dedicated connection, explicit transaction, `SET LOCAL idle_in_transaction_session_timeout = 0`, `pg_try_advisory_xact_lock`), used by all three sites. Open after deploy: a lock leaked before the deploy stays until its backend is recycled, `pg_terminate_backend(<pid>)` runs, or Stage B's DISCARD ALL. Re-check `pg_locks` for `locktype='advisory'` with no matching worker pass. Then close gr463966. The pre-existing `tests/ingest/test_claim.py` integration tests skip in the dev container (backends multiplexed or no DSN); `Claim` is covered by `tests/test_store_advisory.py`.
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
