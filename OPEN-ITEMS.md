# precis-mcp — Open Items

Durable backlog. Only **open / blocked / deferred** work lives here; done
items are removed (history is `git log`). Cross-cutting code-quality work is
tracked separately in `docs/improvement-plan.md` (same delete-on-ship rule).

> **Convention** — Status: `open`/`blocked`/`deferred` · Severity:
> `critical`/`feature`/`polish` · Owner: where the fix lives · Test: the
> regression that pins it.

---

## ✨ Trust-taxonomy follow-ons (5-state Ⓐ/✍ shipped)

Status: open · Severity: feature/polish · Owner: `src/precis/taproot/trust.py`, `src/precis/workers/chase.py`, exporters · Test: below.

The 5-state trust ladder (`clean` ‹ `abstract` Ⓐ ‹ `vouched` ✍ ‹ `unverified` ⚠ ‹ `unsupported` ‼) + a paper-level `meta.unacquirable_override` set from the paper Meta tab (read through by `taproot.trust`) shipped. Two deferred pieces:

- **Auto-Ⓐ: abstract-grounded verification (the real capability).** Today `abstract` is *human-asserted* only (author picks "Abstract backs it" on the Meta tab). The follow-on is a chase/verify pass that, when the full text is unobtainable but the **held abstract** is present, runs an LLM judgment ("does this abstract unambiguously support the claim?") and sets `abstract` **without** a human — same state, earned by machine. Needs: the abstract-present check (S2/Crossref abstracts already stored), a MEDIUM-tier `is_corroborating`-style prompt scoped to the abstract, and a write path that stamps the override with `by='verify:abstract'`. Gate it behind the acquiring-arm give-up so it only fires once OA fetch is exhausted.
- **Calm end-matter section in exporters (polish).** `abstract`/`vouched` claims currently get a calm inline body mark (`[abstract-only: …]` / `[author-vouched: …]`) + the `export_override` audit event, but are deliberately **excluded** from the "Unverified claims" problem list (they're not problems). Add a separate, calm end-matter section ("Declared-unobtainable sources") listing them for transparency — `docx.py`/`latex.py` + `_trust_marks.unverified_claims_entries` sibling. Currently they have an inline mark with no end-matter entry.
- **Perf (polish, reviewer-flagged):** `claim_trust`'s paper-level read-through adds one `fetch_refs_by_ids([frontier_id])` per *unverified lifecycle* finding in the `claim_trust_bulk` loop (`trust.py`). Bounded (only unverified, only lifecycle), but the smartdraft reader could render many at once — batch the frontier-paper meta fetch across the bulk call if it shows up.

---

## 🔧 axis:taproot demotes freshly-minted claim hubs (root-caused 2026-08-04) + fleet remediation

Status: open (fleet remediation pending) · Severity: bug · Owner: `src/precis/workers/axis_pass.py::_claim_ref` · Test: `run_axis_pass(axis_id='taproot')` over a live `TAPROOT:claim` hub must skip it, not reclassify.

Root cause of the nanobuds-copy "non-hub `[fi]` cites": NOT the backfill (placement validates via `_is_claim_hub` and the evidence edges landed). The `axis:taproot` pass (enabled fleet-wide since 2026-07-30) claims every `finding` lacking its `TAPROOTCASCADE` marker — including hubs `mint_hub` just created — and its `replace_prefix=True` write swaps `TAPROOT:claim`→`TAPROOT:review` on a SMALL-model "review" verdict, 23–72 s after mint. Any future mint (backfill or `chase` bridge) races the same window until fixed.

- **Fleet remediation (fix shipped 04628e8c):** finder query (links with hub-role relations into findings now lacking `TAPROOT:claim`) returned 12 rows on 2026-08-04. Nanobuds four are handled (fi189520/189540/191261 de-cited from `dr173020`; fi191180 is an uncited orphan duplicate of retired fi191261 — leave demoted). Remaining **8 in the DNA-origami/nanozyme domain** (fi176420, fi176447, fi176451, fi176820, fi176871, fi177633, fi178235, fi178237) need per-claim triage: restore `TAPROOT:claim` where the rubric passes (fi176451's mechanism claim clearly does), leave demoted + de-cite where meta-prose — check which draft cites them first.
- **Secondary defect** (distinct, filed as gripe 191953): `backfill.py::apply_chunk` rewrites prose to an `[fi]` cite even when `attach_evidence` raised for an "attach" placement with a pre-populated `plan.hub_ref_id`.
- **v2 follow-on: persist `claim_type`** — extractor returns the claim sort (measurement/definition/capability/mechanism/landscape) into hub meta so `hub_refine` can prioritize thin definitions/landscape claims for corroborators, lint can flag a capability claim with no regime, and dedup can treat definitions specially. Design pass first.
- **Grounding-depth policy (from Reto's fi189527 question):** abstract-only grounding is fine for definition/existence claims; measurement/mechanism claims want a body-passage corroborator too. Fold into the `hub_refine` dark pass design (search inside the paper for the meat passage, attach as second chunk).

---

## 🔧 MCP `put(kind='job')` can't set `parent_id` → ad-hoc job submit unreachable; taproot backfill recipe wrong

Status: open · Severity: feature · Owner: `src/precis/tools/core.py::put` + skills `precis-taproot-help`/`precis-job-help` · Test: an MCP-surface test that `put(kind='job', parent_id=<todo>, job_type=…)` succeeds.

- **Discovered dogfooding `taproot_backfill` (ship 6dbe5e94).** `JobHandler.put` accepts `parent_id` (handlers/job.py:146), but the MCP tool `core.py::put()` **never forwards it** — `parent_id` isn't in the generated tool schema. So the ad-hoc `put(kind='job', parent_id=<todo>, …)` path that `precis-job-help` documents is **uncallable through the MCP**; every ad-hoc job submit errors "requires parent_id." The only MCP-reachable launch is the canonical `put(kind='todo', meta={'executor','job_type','params'})` + dispatch-worker mint.
- **My `precis-taproot-help` backfill recipe is consequently wrong** — it shows a bare `put(kind='job', job_type='taproot_backfill', params=…)` that fails. Fix it to the canonical todo+meta form (verified working: todo `188904` → job `188905`).
- **Fix:** (a) forward `parent_id` in `core.py::put()` (one-line add to the signature + payload) so ad-hoc submit works and derived-compute jobs can parent directly on their subject ref (e.g. the draft) per ADR 0044; (b) correct the taproot + job-help recipes. `parent_id` also wants a companion for the polymorphic build-subject case (a draft/structure ref, not only a todo).

---

## 🔧 `plan_tick` backlog starves the single melchior `claude_inproc` worker (SPOF throughput)

Status: open · Severity: polish (verify not chronic) · Owner: `src/precis/workers/executors/claude_inproc.py` + planner dispatch · Test: n/a (ops observation).

- **Observed 2026-08-03 during the taproot dogfood:** 102 jobs `STATUS:queued`, ~100 of them `plan_tick` (planner-coroutine ticks from todos 186940–187042), draining a few per pass on the one melchior agent worker (the `agent_worker_inproc_topology` SPOF). An ad-hoc `taproot_backfill` job (188905) sat unclaimed for 20+ min simply queued behind the pile.
- Likely aggravated by the agent-worker restart at 14:19 UTC (deploy bounce) letting the queue accumulate during the gap — but a 100-deep planner-tick queue on a single sequential worker is a standing throughput/SPOF risk (and possibly a planner-runaway signal — that many active `meta.llm_tier` todos ticking). Related: `spend_limit_parks_todos`, planner guardrails.
- **To decide:** is this transient post-deploy catch-up (drains on its own) or chronic (needs a second agent-worker / plan_tick rate-limit / claim-priority for ad-hoc jobs)? A cheap draining-vs-growing sample answers it. Cluster-ops mis-diagnosed this as `service_config`-disabled — it is NOT (the pass `default_profiles=_AGT`, default-on; registry.py:83).

---

## Residuals (2026-08-05 — paper-editing + taproot-chasing agent-fix bundle)

Two pieces of that session's bundle collided at ship-time with sibling work
that merged to `main` first (trust-surfaces `70dfd3bc`/`897b77cf`,
clickable-grounding `b7e22467`, smartdraft bulk derivation `af06f8e2`). Both
were **deferred out of the ship** rather than force-merged onto correctness-
sensitive taproot/claim code; redo each against the new `main`, combining with
the sibling feature it overlaps.

- **td48769 finding arm — dry_run preview deferred** · Status: open · Severity:
  feature · Owner: `src/precis/handlers/finding.py::FindingHandler.edit`. The
  paper/cfp/datasheet dry_run-preview arms shipped; the **finding** arm was
  reverted because `finding.edit` was reworked by the trust/retitle ship into a
  three-op surface (`pick_candidate` / `title=` retitle-hub door /
  `unacquirable_note=` override) that **deliberately rejects** `dry_run` with a
  documented "no faithful preview" rationale. Reconciling needs a design call:
  give `pick_candidate` a real no-write preview (it rewrites links + flips
  status — the preview my dropped code computed via the validated
  `picked_link`/`other_links` is faithful) while `title=` keeps rejecting (a
  retitle has no preview) and `unacquirable_note=` either previews its meta
  patch or keeps rejecting. Test scaffold existed (dropped
  `test_finding_edit_dry_run_previews_pick_and_does_not_write`).

- **Taproot evidence-edge read-side multi-handle (`source_handles`) deferred** ·
  Status: open · Severity: polish · Owner: `src/precis/taproot/seniority.py`
  (`derive_evidence` **and** the new `derive_evidence_bulk`) + `utils/refeye.py`
  + `precis_web/claim_render.py` + `templates/claim/view.html.j2`. This is the
  read-side half of the existing "Evidence edge records one grounding pointer"
  item: the write side (`f899551d`) already lands two `links` rows for a paper
  grounding one claim via two passages; my read-side fix (widen `EvidenceEdge`
  to a `source_handles` list so both surface) collided with the sibling's
  clickable-grounding render (single `source_handle` → `/c/<handle>` anchor) and
  its **bulk** derivation path (`derive_evidence_bulk`, smartdraft's hot path).
  Redo: widen to `source_handles` in **both** the single-hub and bulk derivation
  functions, and render **each** handle in the list clickable (combine with the
  sibling's `source_is_chunk` parse), not just the first. The conn= threading
  half of that cleanup shipped fine.

---

## 🔧 provenance flag — make `corrected` / `expression_of_concern` *do* something downstream

Status: deferred · Severity: feature · Owner: `src/precis/runtime/search.py` + citation-grounding filter · Test: n/a yet (design phase).

- The fetch-time provenance gate now **populates** `retraction_status` for all three statuses (`retracted` skips the chase; `corrected` / `expression_of_concern` stamp a reader-banner flag and proceed — shipped this session). But the soft flag is still *display-only*: nothing consumes `corrected` / `expression_of_concern` in ranking or citation grounding.
- **Deferred consumer work:** wire the column into search + citation surfaces with severity-appropriate action — `retracted` = hard action (downrank/exclude, block as a citation anchor), `corrected` / `expression_of_concern` = soft flag (annotate, mild downrank). Same column, different severity; pairs with the `ROLE3:own` citation-grounding filter.

---

## Residuals (2026-08-04 — morning-audio outage investigation)

Chased from "fix the morning report / docker-hub unlock". The combined morning
episode ships in code (`148403c4`) but produced **no audio Aug 3–4**; root-caused
to a §L regression **plus** an independent, more serious spark worker deadlock.

- **§L seed omits the tts-role passes — cast_audio FIXED, class still open** ·
  Status: open (class) · Severity: bug · Owner:
  `deploy/roles/precis_worker/tasks/main.yml` §L seed. §L retired
  `PRECIS_*_ENABLED` as the live gate; pass enablement is now a `service_config`
  prio row, seeded at deploy from a **hardcoded 4-service list**
  (`llm_summarize`/`classify`/`llm_reconcile`/`job_claude_docker`).
  `cast_audio`/`briefing_audio` — whose enable flags live in the *tts role's*
  `tts.env`, invisible to this loop — got no row and silently went dark on spark
  when §L deployed (~Aug 2). **Fixed:** live `precis service prio spark
  cast_audio 5` + a shipped seed entry gated on `precis_capabilities.tts_render`
  (`3eec86d0`). `briefing_audio` intentionally left OFF — the standalone
  news-only episode is retired, and §L's default-off *is* the retirement, so the
  `tts.env` `PRECIS_BRIEFING_AUDIO_ENABLED=0` flip is now moot. **Generalization
  still open:** drive the seed from the registry's `enable_env` set (×
  advertised capability), not a hand-maintained list, so no future role-gated
  pass regresses the same way.

- **autocatpath fan-out: a terminally failed seed never escalates** · Status:
  open · Severity: bug (latent stall class). A seed job that exhausts its
  attempt cap leaves its seed todo's `child_job_succeeded` auto_check
  permanently unsatisfied, so `T_agg` never becomes dispatchable and no
  `autocatpath_aggregate` job ever exists — the ADR 0064 §C retry lane
  (`quest/compute.py::harvest_measures`, which watches explore/aggregate jobs)
  sees nothing, and the candidate waits forever with no retry, no gripe, no
  rule-out. Noticed while fixing gr191615 (which covers only counters spent by
  the retired explore-era failure mode). Do: decide the escalation signal for a
  dead seed subtree — e.g. treat "all seed todos terminal but not all done" as
  a failed barrier eval feeding the §C ladder, or a nursery detector on aged
  undispatchable `T_agg` trees.

- **spark autocatpath_seed durable submit/poll migration (wedge + completion RESOLVED)**
  · Status: only the durable end-state remains · Severity:
  critical → low · gripe **191351**. **Wedge FIXED (7497c30d, deployed +
  live-verified):** spark's `system` worker was running `autocatpath_seed`'s MACE
  compute *in-process* via the blocking `ssh_node` dispatch (`job_ssh_node` is a
  `_SYS` pass; jobs pinned to spark via `target_node`), and loading MACE/CUDA
  (torch 2.13+cu130) in the long-lived worker deadlocked (main thread in
  `libcuda.so`), the 2h lease shielding it from reclaim, starving every system
  pass incl. `cast_audio` into a SIGKILL loop. `_dispatch` now runs the compute
  out-of-process (`runner.run_seed_partial_subprocess`, killable, bounded by
  `resources.wall_seconds`). Verified on spark post-deploy: worker stable (same
  PID 35+ min, main thread in `ppoll` not CUDA), a killed seed child recorded a
  clean failure without wedging, passes rotating. **Seed completion CONFIRMED (2026-08-04): worker stable 22h-plus; ref 190130 (00:31 UTC) COMPLETED in-subprocess (seed=0 mace:medium, 16 states, meta.partial populated).**
  The SIGTERM'd seeds seen since (rc=-15) trace to worker restarts from
  concurrent sibling-deploy bounces (`signal 15 received` at ~20:35 UTC →
  restart), which the out-of-process subprocess path survives cleanly — no
  wedge, the fix working as designed. Remaining (low): port the still-blocking
  `_dispatch` to the ssh_node `submit`/`poll` protocol (`seed_job.py` +
  `runner.py`, cross-ref the poison-guard item below) so the pass never blocks
  even for the bounded in-subprocess window. Closeable: gripe **191351**.

- **Docker Hub egress on spark (gripe 189697) — deferred; needed only for the
  1.5s pause** · Status: blocked · Severity: polish. TLS handshake to Docker
  Hub/ECR (AWS-hosted) stalls from spark; ghcr.io/Cloudflare work. Plus
  `tts_base_image` (`python:3.11`) ≠ Dockerfile `FROM` (`3.12`), so the
  pull-if-missing guard checks the wrong image. Unblock (not executed):
  pre-seed `python:3.12-slim-bookworm` from melchior (arm64, reaches Docker Hub)
  → `docker load` on spark, then `45-tts.yml`. Only the 1.5s inter-article pause
  needs this; the combine + news-retirement do not.

- **News wire still composes ~2h late** (Aug 4: 08:14 vs 06:00 UTC target) ·
  Status: open · Severity: feature. Agent-worker lateness persists after the
  H2/H5 fixes — the deferred H1/H3/H4 reliability track (memory
  `worker-agent-silent-outage`).

---

## Morning audio combine — dispatch runaway FIXED (gr192606); verify next morning cycle

Status: fix shipped, pending one-morning-cycle prod verification · Owner:
`src/precis/workers/dispatch.py`, `cast_audio.py` · Regression:
`test_dispatch_worker.py::test_succeeded_child_job_blocks_deterministic_parent`.

- ROOT CAUSE (gr192606): the daily `briefing` instance todo re-minted 46 jobs in
  23h — each *destructively* DELETE+recreating the `briefing-<date>` news ref
  (`run_briefing`/`put_cache_entry`, same-slug replace) — so `_news_lead_in`
  spliced whichever transient mid-day compose was live at the 16:45 UTC
  narration. The combine code was always correct (slug resolution +
  `markdown_segments` 1:1 verified); the defect was upstream dispatch. Corrected
  arithmetic: a full wire is ~half URL chars → ~6.5 min spoken, so a perfect
  combine is ~25-26 min, not ~30.
- FIXED (this commit): `dispatch._job_blocks_dispatch_sql` — a `succeeded` child
  job now blocks re-dispatch of a deterministic (non-`llm_tier`) parent, so the
  briefing runs ONCE/day and the news ref is stable by narration. Holds even
  when the `auto_check` pass is starved — which is what let it balloon: the same
  wedged catpath `system` worker (since fixed, 7497c30d) also starved auto_check.
- OBSERVABILITY (this commit): `_news_lead_in` logs prepend/skip with ref id +
  char/segment counts (do-next #1 from the old triage — done).
- VERIFY: after one morning cycle post-deploy, confirm the podcast episode is
  full news wire + personal brief ≈ 25-26 min (one episode, `source="brief"`)
  and the "news lead-in prepended N segment(s)" log line appears on spark.
- Residual (low, optional defense-in-depth): (1) a stability contract on the
  daily news ref (version instead of destructive slug-replace) so even a
  legitimate single daily recompose never destroys an already-delivered brief;
  (2) the `derived-from` link from the cast draft to the news ref.
- SUPERSEDED sub-finding: gr192606 also flagged tick-cap "didn't halt at 10" for
  the single-ref briefing instance (unexplained — the monotonic per-ref counter
  *should* have). Moot now: the new brake stops re-mint at #1, long before 10.
  The confirmed planner-guardrail COST-cap hole found alongside is filed in the
  next item.
- REJECTED: the old plan's "reorder news/brief crons" would MASK not fix — it
  narrows the race window but leaves the runaway; unneeded now the runaway's gone.

## Unbraked LLM-pass cluster — persistently-failing rows re-LLM'd every sweep

Status: open · Severity: correctness (token/$ leak) · Found: 2026-08-05 Opus
leak-hunt alongside gr192606 · Owner: `workers/{classify_topics,axis_pass,
inject_scan,paper_glossary,hub_refine}.py` + `planner_guardrails.py`

Shared shape (identical to gr192606's): a candidate query excludes rows only on a
SUCCESS-written done-marker; the failure/exception branch skips the marker and
there is no claim-time lease / attempt-cap / cooldown — so a persistently-failing
row (dead endpoint, breaker refusal, unparseable JSON) is re-fetched and re-LLM'd
every sweep, unbounded. Fix pattern already in-tree: `classify.py` and chunk-level
`axis_pass` take a claim-time `chunk_claims` lease BEFORE the LLM call, braking the
row regardless of outcome. Apply the same (a lease row, or a `failed_at`/attempt
column written AT claim) to each site.

Sites (severity order; several default-OFF, but real when enabled):
1. `classify_topics.run_classify_topics_pass` — HAS PROD HISTORY (gr172740/173317,
   "5570 failed, no rows"); routes to paid OpenRouter. Marker = `TOPICCASCADE=`
   ref tag, success-only; `_classify_one`→None on any failure re-loops. ADR 0068
   per-topic enable.
2. `axis_pass.run_axis_pass` **ref-level** path — no lease (the chunk-level path
   IS leased, not a leak); its own docstring already defers a "failed-lease
   reaper". Default-OFF.
3. `inject_scan.run_inject_scan_pass` — email lane; re-does IMAP fetch + model
   each sweep for `tier<1` rows the model can't parse.
4. `paper_glossary.run_paper_glossary_pass` — `data is None`/bare-except write no
   marker (the converged-empty branches DO — those are fine). Default-OFF.
5. `hub_refine.run_hub_refine_pass` — mechanism confirmed, occurrence plausible
   (a raise after the per-candidate verify-LLM loop rolls back the refresh
   stamp). Default-OFF.

Plus — planner-guardrail COST backstops are INERT (confirmed): `_read_cost_usd`
(per-todo $2 cap) and `_read_daily_cost` (global $20/day ceiling) sum
`job.meta->>'cost_usd'`, but NOTHING writes `cost_usd` onto a job ref's meta (real
cost lands on the subject ref's `ref_events`). Both read $0 forever — the daily
ceiling could never have caught gr192606 or any leak; the per-ref tick cap
(default 10, monotonic, and it DOES apply to deterministic parents) is the only
live planner backstop. Fix: stamp `meta.cost_usd` on the job at completion, or
re-point the two reads at the `ref_events` cost column.

---

## 🎯 Taproot self-plagiarism detection — cross-draft hub reuse

Status: open · Severity: feature · Owner: `src/precis/taproot/` + export handlers · Test: n/a yet (design phase).

- A Taproot claim hub (`finding` tagged `TAPROOT:claim`) is a reusable canonical claim wording + shared evidence set, designed for productivity across multiple drafts via the cross-paper claim graph. But when a **second** of our own papers reuses the same hubs, the verbatim or near-verbatim canonical claim phrasing can constitute self-plagiarism — a distinction that doesn't emerge until multi-draft authoring surfaces.
- **Not a first-paper problem.** A single draft citing external hubs is normal and expected; self-plagiarism risk appears only when the *same hub* is cited by multiple *own* manuscripts, and the shared canonical wording is reproduced verbatim in multiple papers.
- **Directions to explore (not yet decided):** (a) detect cross-draft hub reuse at export time and surface a "this claim already appears in draft X" warning on docx/pdf generation; (b) vary phrasing on reuse — the hub remains the stable citation anchor (ID), but the sentence needn't be verbatim; (c) track which of our drafts cite each hub and surface that relationship explicitly in the claim-hub editor.
- **Raised by:** Reto while building the MCP taproot-authoring surface (worktree `snappy-shimmying-rossum`). No immediate blocking of any workflow; scoped as a detection + guidance feature, not a correctness bug.

---

## 🔧 ship gate — test-db 100-connection ceiling saturates the full-suite `-n6` run on a loaded host

Status: open · Severity: polish · Owner: `docker/dev/compose.yaml` (precis-test-db) + `scripts/ship` · Test: n/a (infra flake, not code).

- The gate's `precis-test-db` runs default `max_connections=100`. Under the **full** suite at `-n6` on a host that's *also* running sibling worktree gates or under desktop RAM pressure, peak concurrent connections (6 xdist workers, each holding a per-session keepalive to its clone DB + churn) saturate the ceiling. New connections get RST'd from the listen backlog **before** Postgres accepts them → surface client-side as `psycopg.OperationalError: server closed the connection unexpectedly` across *every* test dir (not workers-specific), with **nothing logged server-side** (the connection never reached pg, so no "too many clients" FATAL). Subset runs (`tests/workers` alone at `-n6` = 344 pass) never hit the peak, which masks it.
- **Worked around, not fixed (ship `184432bd`):** `scripts/ship` now honours `PRECIS_GATE_N` (`PYTEST_N="${PRECIS_GATE_N:-6}"`); `PRECIS_GATE_N=3 scripts/ship …` halves peak connections + backends and passes green. The default stays `-n6`, so the next shipper on a loaded host rediscovers this unless they know the knob.
- **Durable fix (design call):** raise the test-db `max_connections` (e.g. `-c max_connections=300` in the compose `command`) so `-n6` has headroom — **but** on a memory-pressured host 300 backends × ~10 MB risks trading the RST for a real pg OOM, so this needs the host-RAM tradeoff weighed (or a smarter cap like 150 + a gate-side connection-pressure check). Alternatively auto-detect host pressure in `scripts/ship` and step `-n` down.
- **Raised by:** Opus session while shipping the taproot MCP surface — 4 full-suite red gates on a host at 118–124 GB/128 GB before the `-n3` workaround landed it. Not this ship's diff (pure Python handler logic; all its tests pass isolated).

---

## 🔧 taproot backfill — silent drop of unresolvable [pc] handle on promote-collapse

Status: open · Severity: polish · Owner: `src/precis/taproot/backfill.py` · Test: n/a (pre-existing gap; design call on skip-vs-warn needed before regression test).

- In `_plan_group`, each handle in a contiguous cite run is resolved independently; those raising `BadInput` (dangling ref_id / deleted paper / non-paper kind) are dropped, skipping the whole group only when ALL handles fail.
- For a `[pc]` run like `[pc1][pc_bad]` where pc1 resolves and pc_bad does not: `apply_chunk` collapses the run's span to a single `[fi<hub>]` (intended multi-passage→one-hub promote), silently dropping pc_bad's token with no signal.
- **Why lower severity than [pa] arm:** unlike [pa] re-ground (which shipped with an all-or-nothing resolution guard in slice 2), the [pc] path's collapse-to-one-hub is the *intended* promote semantics, and pc_bad is unresolvable so no citeable evidence is lost — but the user's cite intent to pc_bad vanishes with no warning.
- **Possible fix:** extend the [pa] arm's `len(supporters) < len(group.handles)` skip-with-note guard to the [pc] path too, or at least emit a note listing dropped handles rather than silently collapsing. Needs a design call (skip-vs-warn) since it changes established [pc] backfill behavior (which has its own test suite).
- **Reference:** found in reviewer pass on commit `6fd7a004`.

---

## Residuals (2026-08-03 — worktree reaper raced a live session)

- **Reaper deleted a live session's worktree** · Status: open · Severity: bug ·
  Owner: `scripts/reap-worktrees` / `scripts/inflight` liveness check. During
  the 2026-08-03 session in `bubbly-waddling-heron` (live Claude session, mid
  P2-1 build), a sibling session's `SessionStart` backstop reaped the worktree:
  right after a ship it was momentarily merged+clean with no dirty files and no
  `.claude/purpose` refresh, and the liveness probe missed the live session. A
  background coder recreated it and no work was lost, but the race is real —
  reap decisions should re-verify session liveness (not just git state)
  immediately before `git worktree remove`, and/or treat "session file
  active in the last N minutes" as a hard veto. Repro window: ship → branch
  reset to main → sibling session starts before the next local edit.

---

## Residuals (2026-07-31 — draft table-editing ship b9bc1d4c)

- **Structured enrichment for rich tables** · Status: deferred · Severity:
  feature · Owner: `meta.table` schema + both exporters. Represent column
  alignment, `\multicolumn`/`\multirow` spans, rule placement, and footnote
  markers as *structured* fields — NOT stored LaTeX (breaks Word export, per
  the proposal's rejection). Specified in its own proposal
  `docs/proposals/draft-table-structured-enrichment.md` (graduated out of
  `draft-table-editing.md` item 3); awaits the `ready` gate + a build.

## Residuals (2026-07-31 — taproot hub-refine ship)

- **hub-refine could attach true contradictors as `contradicts` edges** ·
  Status: deferred · Severity: feature. The Phase-0 slice-eval showed the
  verifier flags on-topic-but-counter chunks; `hub_refine` now gates those
  OUT of `corroborates` (verifier `contradicts` field → rejection memo,
  shipped). But the hub model already has a `contradicts` *edge* type
  (ADR 0073) and hub-refine only ever emits `corroborates` — so a genuinely
  refuting paper is dropped rather than surfaced. Distinguishing "actively
  contradicts → attach as `contradicts` edge" from "merely doesn't
  substantiate → drop" (a finer verifier classification) would light up the
  living cite's contradictor list and directly feed the Phase-4
  novelty-claim "your claim broke" alert. Follow-up, not a correctness gap.

- **Re-validate the `contradicts` gate at slice scale post-deploy** ·
  Status: open · Severity: validation (gates enablement). The verify rubric
  was reworked (v2: STANCE-first, counter-result → `no`/`contradicts=true`
  with a worked example) after the first version proved a no-op — the
  MEDIUM-tier model returned `contradicts=False` even on chunks whose caveats
  stated the opposite result. v2 was **probe-validated on the decisive cases**
  (borah26/bara19 → `contradicts=true` → drop; hashmi24/cerf74 → kept): see
  `scratchpad/probe_v2.py` (no-embedder, dispatches a candidate prompt over a
  chosen test set). The attach gate + eval harness now share
  `_chase_llm.is_corroborating`, so a full `slice_refine_eval` run on the
  next-deployed code should now match the pass. **Before enabling:** run the
  full slice-eval on deployed v2 over the Phase-0 slice and confirm hub 176363
  drops its contradicting partials while 176272/176360 keep theirs. Runbook:
  `docs/runbooks/taproot-chase-enablement.md`.

- **Enable hub-refine + chase-trigger in prod (Phase 2)** · Status: deferred ·
  Severity: feature. `src/precis/workers/hub_refine.py` + `chase_trigger.py`
  ship dark (`PRECIS_TAPROOT_REFINE_ENABLED=0`,
  `PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED=0`). **Enablement runbook:
  `docs/runbooks/taproot-chase-enablement.md`** (single-host `service_config`
  override, floor-tuning, one-time re-verify wave, bounds/rollback). NB the
  flip is NOT a plain role-env redeploy — see the single-instance note below.
  Remaining v2 follow-ups (`TAPROOT:saturated` long-backoff after K empty
  passes, paper-version memo invalidation) live in
  `docs/proposals/taproot-hub-refine.md`'s "Out of scope" section, not here.

- **ref-pass single-instance for hub-refine/chase-trigger** · Status: resolved
  (approach) · Severity: latent-correctness (inherited). The claim query
  commits and releases its `FOR UPDATE ... SKIP LOCKED` lock before
  `_refine_one_hub`'s per-hub write connection opens (two-phase shape shared
  with `inbound_chase.py`); concurrent instances double-claim a hub →
  duplicate LLM verify + lost-update on `meta['taproot_rejected']`.
  **Both passes have empty `default_profiles`, and the `agent` profile deploys
  to two hosts (`gateway`+`inference`)** — so setting the enable flag in the
  `precis_worker_agent` plist env would run TWO instances. Resolution:
  **enable via a single-host `service_config` prio override** (structural
  single-instance, live-toggle) per the runbook — never the shared role env.
  Defence-in-depth (make the memo write conflict-safe) is a non-blocking
  follow-up.

- **Give `_evidence_edge_exists` an optional `conn=`** · Status: open ·
  Severity: efficiency. `taproot/authoring.py::_evidence_edge_exists` opens its
  own pool connection per call; `seed_claim_hub` (mint) still calls it once per
  supporter in its loop. (hub-refine no longer uses it — it dedups at the
  **paper** level via a single `_attached_paper_ids` query per hub, which also
  fixes a convergence bug the chunk-scoped edge model introduced.) Thread the
  caller's `conn` through for the mint path.

## Residuals (2026-07-31 — citation-edge chunk-grounding: grounded)

Code shipped + **deployed**; deterministic + semantic backfills **applied to
prod**. Taproot evidence-edge grounding 22%→**63%** (618/986). Deterministic:
16 drafts (`dc`) + 212 `source_handle` paper edges (`pc`). Semantic fan-out
(36 sonnet verifiers over pgvector top-3 candidates): **406** paper edges
grounded (291 at conf≥0.7 + 115 medium 0.5–0.7), each tagged
`meta.src_grounding.method='semantic_backfill'` — reversible as a set. finding
177720 fully chunk-grounded (`dc1507057` + `pc510143`). Remaining ref-level:

- **Triage the 292 "none-fit" evidence edges** · Status: open · Severity:
  feature · Owner: taproot grounding. The fan-out found no top-3 paper chunk
  that grounds these claims. "none-fit" ≠ unsupported — it mixes (a) genuinely
  spurious edges (should be *removed*, not grounded), (b) real-but-diffuse
  whole-paper support, (c) retrieval misses (grounding chunk ranked >3, or claim
  paraphrased far from paper wording). Needs a deeper pass — top-10 retrieval +
  full-claim embedding (not just the claim's lead chunk) + an explicit "is this
  edge spurious at all?" judgment — *before* any edge removal. Candidate set is
  regenerable via the session's pgvector LATERAL query.
- **Embed the 67 papers with no body-chunk embeddings** · Status: open ·
  Severity: feature · Owner: `embed:bge-m3` worker. 67 of the 774 null-
  `source_handle` edges sit on papers with no embedded chunks, so nothing to
  retrieve against; reground after their chunks embed.
- **9 low-confidence (<0.5) groundings held** — left ref-level deliberately;
  revisit only if the none-fit triage changes the retrieval approach.

## Residuals (2026-07-30 — taproot authoring on-ramp ship 02af6721)

- **Taproot chase forward-bridge PILOT — ⚡ LIVE (enabled 2026-07-31, gateway/system worker)** ·
  Status: monitoring · Severity: feature · Owner: `precis_worker` deploy role + melchior
  host_var + `src/precis/workers/chase.py::_taproot_bridge` (chase.py:1157). The
  **automatic** forward hub-population engine (claim→paper; complement to the manual
  `precis taproot mint` on-ramp above), W1 bridge + W2 corroborators.
  **CORRECT PROFILE:** the finding-chase pass is `default_profiles=_SYS` (system-only,
  `registry.py`) — it runs on the **`com.precis.worker`** (system) daemon, NOT the agent
  worker. (An initial enable mistakenly landed on `com.precis.worker-agent`, where the
  flags are inert; reverted 2026-07-31.)
  **⚡ ENABLED — durable (survives redeploy):** a gated block in
  `deploy/roles/precis_worker/templates/precis-worker.plist.j2`
  (`{% if precis_worker_taproot_chase %}` → `PRECIS_TAPROOT_CHASE_ENABLED=1` +
  `PRECIS_CHASE_LLM=1`), default `false`, turned **true** on the gateway via
  `deploy/inventory/host_vars/melchior.yml` (private overlay). Made live immediately by
  a matching direct `PlistBuddy` add to melchior's `com.precis.worker` plist + bounce;
  a `scripts/deploy` regenerates the identical env, so it stays on. Only melchior gets it
  (it alone has the LLM verifier route + remote embedder).
  **⚠ Cost note:** `PRECIS_CHASE_LLM=1` turns the LLM verifier on for **all** chase passes
  on melchior's system worker — capped by `PRECIS_DAILY_COST_CEILING=50.0`.
  **⚠ EMPTY-QUEUE CAVEAT (why edges may not appear):** the forward chase queue is
  currently **0 `STATUS:tracing` findings** — all drained to `established` (276) /
  `dead_chain` (55) / `multi_candidate` (5). The forward bridge only fires when a finding
  establishes, so with no tracing inflow it produces nothing until new findings arrive
  (quest loop, extraction) **or** a claim-hub backfill feeds it (see the completeness items
  below). The 942 claim hubs are `STATUS:canonical` and **excluded** from the outbound
  chase — so the 236 evidence-empty hubs are NOT self-filled by this pilot; that needs the
  backfill.
  **Watch (clean attribution; graph is also filled by the manual on-ramp `set_by='agent'`,
  so raw counts are noisy — `set_by='chase'` is this pilot's unique fingerprint, baseline 0):**
  `scripts/prod-psql "SELECT relation, count(*) FROM links WHERE relation IN ('corroborates','contradicts','establishes') AND set_by='chase' GROUP BY relation"`.
  Write rides the `STATUS:established` flip tx, **savepoint-isolated**.
  **DISABLE:** flip `precis_worker_taproot_chase: false` in `host_vars/melchior.yml` +
  redeploy the worker role (or `PlistBuddy Delete` both keys from
  `/Library/LaunchDaemons/com.precis.worker.plist` + bounce
  `system/com.precis.worker`).

- **Taproot completeness inflow #2 — ground each incoming paper against the claim set** ·
  Status: open · Severity: feature (slow-burn) · Owner: ingest/`inbound_chase`. The user's
  "any new paper should be checked support/deny against claims" intent. Adjacent engine
  **already exists but is dark**: `workers/inbound_chase.py` (`inbound_chase` service,
  `enable_env=PRECIS_INBOUND_CHASE_ENABLED`, system profile) does an exhaustive one-hop
  *citation* sweep (who cites an activated paper; yes/partial/no engagement) — citation-graph
  shaped, not claim-hub shaped, and it carries an **unshipped cost-guard caveat** (landmark
  papers with thousands of citers have no circuit breaker; see "Budget guardrails Piece B").
  Two sub-tasks: (a) evaluate + (cost-guard first) enable `inbound_chase`; (b) a *claim-hub*
  variant — ANN-match a newly-ingested paper's chunks against the hub embeddings, verify,
  attach evidence — so support/contradiction is found even absent a citation edge. · Test:
  ingest a paper known to support an existing hub → an evidence edge appears without a
  citation path.

- **Taproot completeness inflow #3 — full corpus backfill (Phase 5)** · Status: deferred ·
  Severity: feature (slow-burn) · The exhaustive papers × claims pass. Subsumes #1/#2 at
  scale once their per-item mechanics are proven; keep as the batch backstop. Slow burn.
- **Evidence edge records one grounding pointer when a paper grounds a claim
  via >1 passage** · Status: open · Severity: polish · Owner:
  `src/precis/taproot/authoring.py::seed_claim_hub` +
  `taproot/seniority.py::EvidenceEdge` + `utils/refeye.py`. Two supporters that
  collapse to the same `(paper, hub, role)` keep only the first `source_handle`
  (now surfaced via the return's `collapsed`, not silently dropped — that was
  the review fix). To record both passages, widen the edge meta to a
  `source_handles` list and teach `EvidenceEdge`/the ring's Claims render to
  show a list. · Test: seed one paper as two-passage supporter of one claim →
  both grounding handles survive on the single edge.
  **Partial progress (`f899551d`):** `seed_claim_hub`'s dedup key now includes
  the grounding chunk ord (`(paper, hub, role, chunk)`, not just
  `(paper, hub, role)`), so two distinct-passage supporters write two distinct
  `links` rows instead of collapsing at write-time (see
  `test_two_passages_of_one_paper_are_two_edges`) — the write-side data loss
  this item opened with is gone. Still open: `seniority.py::derive_evidence`
  groups evidence rows into a `dict` keyed by `src_ref_id`
  (`support_edges.setdefault(...)`), so it still folds those two now-distinct
  edges back down to one `EvidenceEdge` at read time — the Claims render still
  shows only one grounding handle per paper. The remaining fix is exactly the
  read-side half this item names (`EvidenceEdge`/`refeye.py` render a list, or
  `derive_evidence` stops deduping by ref_id alone).

## Residuals (2026-07-30 session — gr172886 ship)

- **worker-agent daemon silent outage — investigate the `-9` root cause** ·
  Status: open · Severity: critical · Owner: ops (melchior
  `com.precis.worker-agent` daemon). The daemon was SIGKILL'd and stayed dead
  ~4 days (2026-07-26→30), silently stalling qu164903 + all agent-profile
  work. Do: investigate the `-9` (jetsam/OOM/crashloop) so it doesn't happen
  again. See memory `worker-agent-silent-outage`.
- **tool-less BIG-tier LLM calls fragile + swallowed 400 body** · Status: open ·
  Severity: feature · Owner: `src/precis/utils/llm/router.py::resolve_chain`
  (ignores `tools_needed` for chain-override rungs) + `openai_tools.py`
  `_UrllibTransport.post_json`/`run_tool_loop` (records `str(HTTPError)` only,
  drops `exc.read()`). A chain override (`llm.chain.big`) captures tool-less
  traffic (e.g. quest_tick's propose call) onto an OpenRouter model whose
  provider pool can 400 a tools-absent request non-deterministically — killed
  qu164903's coordinator 2026-07-26 (transient, not reproducing now). Do:
  surface the real 400 body into `LlmResult.error`, and make override routing
  `tools_needed`-aware (or pin a provider). · Test: a fake transport raising
  `HTTPError(fp=<body>)` → `LlmResult.error` contains the body text, not just
  "HTTP Error 400".
---
## 🚨 Deploy fresh-resolves deps instead of installing from `uv.lock` — gate-green can deploy-break

Status: open · Severity: critical (prod-outage class) · Owner: `deploy/`
(`redeploy-precis.yml` install step) · Test: a deploy/CI assertion that each
managed venv's installed dep set matches `uv.lock` (or install via
`uv sync --locked`).

- 2026-07-30 incident: PR #35 (`d3123538`) widened `mcp` to `>=1.0,<3`. The
  ship gate and local dev pin from `uv.lock` (`mcp 1.28.1`, works), but
  `scripts/deploy` **fresh-resolves** deps when installing into the cluster
  venvs, so it pulled `mcp 2.0.0` — which dropped `mcp.server.fastmcp` →
  `precis serve` crashed on import → every stdio precis MCP (dev sessions **and
  asa**) returned `-32000`. The gate never saw it (locked at 1.x); only
  deployed venvs broke. Symptom fixed by pinning `mcp<2` (`74350e2d`), but the
  class remains: any dependency whose newest in-range version diverges from the
  lock can break prod while the gate stays green.
- Fix direction: install the cluster venvs **from the lockfile**
  (`uv sync --locked` / `uv pip sync` an exported lock) so deployed venvs equal
  gate venvs; or add a post-deploy assertion that each venv's installed dep set
  matches `uv.lock`. Same "green-here, broken-in-prod" family as the
  deploy-extras-gap memory.

---
## 🔍 Generalize fisheye discovery affordance beyond draft chunk reads

Status: open · Severity: feature · Owner: `handlers/` per-kind chunk renders +
`server.py` · Test: none yet — when generalized, add a per-kind assertion
(parallel to `tests/test_draft_handler.py`'s fisheye-affordance test) that a
plain single-chunk `get` on a paper/memory chunk carries the `→ view='fisheye'`
affordance line.

- The unprompted-discovery affordance footer (`→ view='fisheye' …`) was added
  only to `DraftHandler._render_chunk` (`src/precis/handlers/draft.py`), gated to
  single-chunk reads. But `paper`/`patent`/`web`/`datasheet`/`cfp`/`memory`/
  `finding` chunk reads also have fisheye eyes
  (`src/precis/utils/eye_render.py::render_eye` dispatches per-kind), and their
  plain renders do NOT advertise it — so an agent reading those kinds unprompted
  still can't discover fisheye. Generalize the teach-at-render affordance to
  those kinds' plain chunk renders.
- Related polish: the draft affordance is emitted on every single-chunk read
  (stateless handler — no "seen once" damper). If it proves noisy in read loops,
  add a session-scoped damper or truncation. (pre-ship reviewer finding,
  2026-07-30)
- Optional: a one-line fisheye mention in the top-level MCP server-instructions
  string (`src/precis/server.py`) for session-start visibility.

---
## 🔎 Residuals — whatneedsdoing triage 2026-07-30 (Opus-session, harvestable)

Surfaced during the 2026-07-30 triage (fleet-health + prod transcript mining).
**Most cleared the same day** — `draft view='outline'` (`901f22ec`), skill-doc
gaps finding/regex (`55f80b70`), typed-error dispatch regression test
(`32b6e90b`; the `edit()`-° "bug" was a false positive — already fixed by
`138ed8cf`, see `gripe:175738`), Dependabot #75 (blocked upstream → Snoozed),
PR #35 `mcp<3` (merged `d3123538`), Windows CI (skipif pass `4a1b2e08`, now
green). Still open (two grew mid-session into much bigger finds):

- **Nursery: detect a known env-gated pass silently absent from a live
  worker's rotation** *(repo).* Surfaced by the 2026-07-26→30 agent-lane
  stall — a `ServiceSpec` env-gate mismatch left `quest_loop_reconcile`
  registered but skipped every cycle for days, and nothing flagged the pass
  being silently absent from the worker's rotation. Do: a nursery/health
  check that flags a known env-gated pass missing from a live worker's
  rotation for N hours. Also: `quest_tick`/`catpath_explore` never persist
  `meta.transcript` (confusion-mining blind spot) — worth adding.

- **🟡 Bounce-coverage gap — not every daemon restarts on deploy** *(residual
  from the env-reload verify, 2026-07-30; owner `deploy/redeploy-precis.yml`).*
  After the `bootout`+`bootstrap` deploy, some precis processes still showed
  stale ~8h-old start times (pids 11645/12410) and lacked the new env, while
  others (78772) restarted and picked it up. So the reload mechanism works but
  doesn't uniformly cover every daemon — either those are child/subprocesses,
  or the bounce skips/silently-fails on some (`failed_when: false`). **Not
  urgent — the nightly boot cycle will restart them and pick up the env**
  (Reto, 2026-07-30). Follow-up: confirm whether it's child procs vs. a real
  bounce gap, and if the latter, ensure the bounce covers all managed daemons.

- **balthazar summarizer flood — NOT fixed (corrects the earlier claim)**
  *(ops/config — cosmetic).* The `precis_local_llm_model_override` host_var I set
  is correct but INERT: the worker's `--summarizer-model` CLI arg (from
  `precis_worker_summarizer`, fleet-default `rake-lemma`,
  `deploy/roles/precis_worker/defaults/main.yml:38`) OVERRIDES the env. Two flood
  sources (main worker → `llm:summarizer` via rake-lemma; classify worker →
  `llm:qwen`). Real fix needs a decision: what should balthazar/the fleet
  summarize with — the served local model, or keep `rake-lemma`? Cosmetic
  (litellm fallback works). Host_var edit left in place (harmless).

- **Post-deploy fleet-health assertion: `PRECIS_SUMMARIZE_MODEL` vs served
  `resource_slots`** *(feature — owner `deploy/` verify play or a `cluster-ops`
  check).* The balthazar WARN-flood (fixed 2026-07-30) was a stale host_var —
  its summarizer model resolved to the fleet-default `qwen` alias, which the
  host doesn't serve, silently detouring every SMALL-tier call to litellm for
  ~15.6k WARNINGs/day before anyone noticed. A post-`scripts/deploy` assertion
  that each host's resolved `PRECIS_SUMMARIZE_MODEL` (and any `PRECIS_LOCAL_*`
  model) equals a `model_id` present in that host's own `resource_slots` `llm:`
  rows would surface this class of drift at deploy time instead of via a 24h
  log-volume alert. This is the automated form of the S4 "Verification" step in
  `docs/design/local-model-router-integration.md`.

- **Windows CI — residuals after the skipif pass** *(repo health — owner
  `tests/`).* The 27 chronically-failing POSIX-only tests were `skipif(win32)`-
  marked (Reto's call 2026-07-30: keep Windows a real green signal, don't drop
  the leg); Windows CI should now be green. Two live residuals: (1) **watch
  `tests/test_render_sandbox.py::test_no_output_is_reported`** — a real timing
  flake (1 of 4 recent Windows runs), deliberately NOT skipped so it isn't
  masked; if it becomes the lone red, fix the flake, don't skip it. (2) The
  skipped tests are POSIX-only by *test harness*, not product — the underlying
  behaviors aren't Windows-portability-tested at all; fine while Windows isn't a
  deployed runtime, revisit only if that changes.

*(Prod-ops, not repo backlog — tracked in substrate 2, noted here for
continuity): FRONTIER Claude-subscription 7-day quota exhausted, pausing all
paid work fleet-wide (44 child-failed todos; auto-clears 11:00 UTC daily) — an
ops/routing decision, not a code fix. Plus non-quota prod stalls: nidra
meditation `No module named '_sqlite3'` (host venv), several `Connection
refused` casts.)*

---
## 🗄️ Postgres schema-audit residuals — 2026-07-30 (Opus-session, refs write-churn)

From the 2026-07-30 DB schema+operation audit. All the write-churn fixes have
shipped: the tuning batch (defensive `agent_*` timeouts, `chunk_embeddings`
ANALYZE, checkpoint/WAL/bgwriter, per-table autovacuum, `pg_cron` removal —
`da79761e`), the LLM-advertise no-op guard + chunk-autovacuum-drift capture
(`aa4e7c94`), and the alert-key promotion (`refs.meta` → `alert_source`/
`fingerprint`/`resolved_at` columns, HOT-enabling index rebuild) + nursery
`seen_count` throttle (migration `0099`). One pre-existing gap surfaced during
that work, plus a deploy op:

- **Deploy op — `pg_repack refs` after `0099` deploys** *(one-time).* `0099`
  sets `fillfactor=85` on `refs`, but that only reaches existing pages on
  rewrite. Run `pg_repack` (online, lock-light) on `refs` once post-deploy so
  the now-unindexed-`meta` dedup updates can land HOT in-page; new/updated rows
  adopt it immediately regardless.

---
## material/component: unit conversion — DELEGATE to `calc`, do not build a second engine (DRY)
- Status: deferred (low priority) · Severity: feature · Owner: `material`/`component` handlers
  + `tools/core.py` (if a `units=` convenience is ever added) · Test: n/a yet.
- **Decision (2026-07-29, Reto):** `calc` already does pint-backed unit conversion, so the
  stores do NOT get their own conversion engine — that would duplicate the authority (DRY).
  `material`/`component` stay **canonical-units-only** on write (a non-canonical `unit=` is
  rejected, naming the canonical one); callers convert via `calc` before writing, and read
  back canonical. This RETIRES the original "build a `utils/units.py` + convert-on-write"
  plan. If a read-side `units=` convenience is ever wanted (serve a simulator its own
  units), it must **delegate to `calc`'s pint**, not stand up a new engine. Until a concrete
  consumer needs that convenience, nothing to build here.

---
## material: off-sample estimate / fitting layer (interpolation + published-model eval)
- Status: open · Severity: feature · Owner: `material` handler + a `model` value-type /
  `model_spec` schema · Test: n/a yet.
- Deferred from ADR 0070. Trust-ordered off-sample read: evaluate a published
  correlation (`model` value-type: Sutherland/Arrhenius/Antoine/NASA-poly, form +
  coeffs + validity range) → else return bracketing sourced points → else, only on an
  explicit `estimate=`, a labeled in-range interpolation (`method='estimated'`, basis
  points recorded, extrapolation refused). Never a silently-chosen fit. Define the
  point-query call shape + `model_spec` JSON + one-sided-bracket behavior in its spec.

---
## `component` kind follow-ons (shipped: v1 = ADR 0071/migration 0093; assembly tree = ADR 0072/migration 0094)
- Status: open · Severity: feature · Owner: `component` handler/store · Test: extend `tests/test_component.py`. All `blocked-by` the shipped `component` kind.
- **Assembly-tree follow-ons** (the `contains`/BOM v1 shipped, ADR 0072) — (a) **comparator /
  violator query**: an explicit pass/fail against a target (`spec='grade' min=8.8` → boolean
  verdict + violator list) on the same tree walk (v1 ships only the uniformity summary);
  (b) **price-break-aware costing + uom reconciliation**: qty-break-tiered `unit_cost`
  selection and mixed per-each/per-metre child rollup (ties into the `calc`/units path — v1
  uses the latest single `unit_cost` via `component_current_spec_value`); (c) **optional-part
  modelling**: a genuine "included at qty 0" line item (v1's `qty=0` means remove the edge).
- **Laminate layer structure** — ordered layers (thickness / fiber orientation) for the
  `laminate` category + effective-property homogenization from the stack (v1 admits the
  category but not the structured layer model).
- **Effective-property inheritance** — walk `made-of → material` at read time to
  synthesize a component's intensive properties (density, modulus, …) from its material.
- **`realized_by → part` binding** — link a generic `component` to a concrete JLCPCB
  catalog C-number + pull live price/stock; structured tiered price ladders land here.
  **Scope (per the PCB boundary above):** this is for *discrete non-PCB procurable parts
  that happen to be catalog SKUs* (a connector, a module, a fastener with an LCSC number)
  — NOT for mirroring PCB internals. Optional variant for board traceability without
  losing granularity: a PCBA `component` may `realized_by → pcb` (the *design*), not a SKU.
- **Category taxonomy tree** — parent/child categories with inherited spec sets (v1 flat).
- **Off-sample estimate / fitting** (the remaining shared cross-kind follow-on) — see the
  `material` off-sample entry above; the same `model` value-type + interpolation applies to
  component specs. (The categorical/typed-mint and `value_low`/`value_high` band trims are
  now shipped for both kinds; unit conversion is retired to `calc` — see the DRY entry.)

---
## autocatpath pathway plugin: CI test skips until the dev image is rebuilt
- Status: open · Severity: feature · Owner: `tests/test_pathway_plugin.py` +
  `scripts/build-image` · Test: this file (runs green once the image carries autocatpath).
- `tests/test_pathway_plugin.py` opens with `pytest.importorskip("autocatpath")`, so
  the ship gate SKIPS it silently: the baked precis-dev image predates the rename and
  still carries the old `catpath` module, not `autocatpath` — the import fails and the
  whole file is skipped. Verified manually 18/18 by mounting a pure `autocatpath`
  checkout into the dev container (`-e UV_WITH="--with-editable /autocatpath" -v
  <path>:/autocatpath`) — so the bundled `precis_pathway` plugin currently has NO CI
  coverage in the gate.
- Fix: `scripts/build-image` threads `AUTOCATPATH_REV`; a fresh build bakes autocatpath
  0.4.0 into the image and the test auto-runs. Do it on the next image refresh.

---
## catalyst-gpu (autocatpath[mace]) vs dormant dft-ml torch pin — potential venv conflict on the GPU node
- Status: open · Severity: feature · Owner: `pyproject.toml` extras (`catalyst-gpu`,
  `dft-ml`) · Test: n/a yet.
- Both `[catalyst-gpu]` (→ `autocatpath[mace]`, torch/MACE backend) and the dormant
  `[dft-ml]` torch extra target the SAME GPU node (spark). uv universal resolution
  resolves all extras together, so if `dft-ml` is ever activated there alongside
  catalyst-gpu their torch pins can conflict in one venv. Today only catalyst-gpu is
  installed on spark, so nothing bites. When dft-ml wakes: share one torch pin across
  both extras, or mirror them into `[tool.uv] conflicts` so uv keeps them in separate
  resolutions. File-and-watch; not urgent.

---
## LLM routing: all tiers remote via OpenRouter (local-first DEFERRED)
- Status: RETAINED for the DEFERRED local-first revisit (the dormant latent local-rung budget bugs are now FIXED — see below); the all-remote cutover itself landed 2026-07-26 (git log) · local-first DEFERRED per Reto ("just make it remote, the lot of it… revisit local in a bit") · Owner: `app_settings llm.chain.*`- **Live chains (all cloud/OpenRouter):** small→`z-ai/glm-4.7-flash` (openai_compat), medium→`z-ai/glm-4.7` (openai_compat), big→`z-ai/glm-5.2` (openai_tools; SET this session — was unset⇒local 80B). FRONTIER→`claude-opus-4-8` (claude_agent, subscription) left as-is (already remote; cheaper via sub than routing Claude through OpenRouter). Transport reaches OpenRouter via PRECIS_LLM_BASE_URL=`https://openrouter.ai/api/v1` (all 3 Macs; spark→melchior:11445 local) + vault-injected OPENROUTER_API_KEY. melchior's local 80B is now chain-unused (idle) but LEFT RUNNING for the local revisit.
- VERIFY (pending): BIG is low-volume (~1–2/hr) — confirm the next BIG call lands on openrouter.ai `z-ai/glm-5.2`, 0 errors, via `llm_call_log`. small/medium already verified remote+healthy (small 6689/24h, 0 err). classify reasoning-off/temp-0 passthrough (ADR-0066, `f50894bf`) is live-effective.
- **DEFERRED — local-first revisit** (Reto: "in a bit"): failover-at-load is BUILT (router.py:1281-1285: a saturated/`paused` local slot retries the hosted OSS endpoint before the next rung); resolve_chain supports a LOCAL rung `{"placement":"local","model":"<served_by id>","transport":...}` (router.py:1078-1160, `placement:"local"` authoritative). To localize a tier later: stand up the GGUF on a host's llama-swap (auto-advertises served_by), prepend a `placement:"local"` rung before the cloud rung — cloud is then the high-load failover. Host notes: balthazar is the SMALL Mac (34GB, ~3GB free, runs a 35B — repurpose that slot, don't co-locate); melchior 206GB but is the claude_inproc SPOF; spark GPU but busy/flaky ssh. BLOCKER for local GLM: GLM-4.7-Flash no-think key for llama.cpp is UNCONFIRMED (Unsloth doc silent — the hosted OpenRouter `reasoning.enabled:false` works, but the local chat-template switch does not; `_dispatch_local` NOTE flags it) — verify before trusting local classify. GGUF: `unsloth/GLM-4.7-Flash-GGUF` UD-Q4_K_XL ~10GB. The two dormant latent local-rung budget bugs (`_model_price` dead `local` fast path; `gate_tier` gating on `is_paid(tier)` not resolved transport) are now FIXED — free-local is classified by `served_by` / `_rung_is_cloud` and exempted in the breaker (see git log). So a local rung reinstated below routes + prices correctly out of the box.
- **LIVE GAP — SMALL is band-`FREE` but routes to a PAID remote today (the inverse of the fixed local-rung bugs)** · Status: open · Severity: budget-correctness · surfaced 2026-08-01 (Opus). `bands._TIER_BANDS[SMALL]=FREE`, so `is_paid(SMALL)` is False and `breaker.gate_tier` **never** gates SMALL — the design assumption is "SMALL = free local `summarizer`". But in the current all-remote regime SMALL resolves to a *paid* OpenRouter model (`z-ai/glm-4.7-flash`, openai_compat) at the **highest volume of any tier** (~6689 calls/24h). So the breaker's $ cap does not meter the single largest call stream — a tripped cap pauses BIG/MEDIUM/FRONTIER while SMALL keeps spending. Symmetric to the just-shipped fix (`e6e02d7a`): that made a *paid-band tier on a free-local rung* exempt; this needs a *free-band tier on a paid-cloud rung* to be **gated**. The clean fix is the same generalization taken to its conclusion — gate on the **resolved transport's cost**, not the tier band at all (drop the `is_paid(tier)` determinant; pass `local=not _rung_is_cloud(rung0)` as the sole signal, which SMALL→openai_compat-with-base_url already computes as cloud/paid). Deferred as its own cycle because it removes the `is_paid`-as-gate assumption baked into `bands.py`/`breaker.py` and wants a spend check first (is SMALL's remote $ actually material, or is glm-4.7-flash cheap enough to ignore?).
- GOTCHA: Reto edits `llm.chain.*` live in `/factory` — always re-read the live row before writing (it changed twice mid-session).
- **Candidate local-serving engine for the Macs — turbo-fieldflare (Swift/Metal MoE weight-streaming)** · Status: open (evaluate) · Severity: feature · Reto want, 2026-08-03. `github.com/drumih/turbo-fieldflare` (`youtube:189018`, "Local AI On Apple Silicon uses 7X Less RAM"): a Swift+Metal, Apple-Silicon-native serving engine for MoE models that runs a 26B Gemma-3 MoE in **~2 GB RAM at ~23 tok/s (M3 Max)** by keeping only the always-on parts resident (attention/router/embeddings + one shared expert, ~1.35 GB, mmap'd off disk) and **streaming the experts off SSD just-in-time** — unified memory means the CPU reads a weight straight off SSD into a Metal buffer the GPU runs against (zero-copy, no VRAM hop), weights are pre-stored in the exact 4-bit-quant layout the Metal kernel consumes (no decode step), and an LFU cache parks 16/128 experts per layer resident. The disk read hides behind the shared-expert compute, so most of the fetch is free. **Why we want it:** fast + native + tiny memory footprint — a strong fit for the DEFERRED local-first revisit above, especially balthazar (the SMALL Mac, ~3 GB free) where a streaming MoE could serve a bigger model than fits today, and melchior. **What to evaluate before adopting:** (a) it's a **Mac app / CLI, not obviously an OpenAI-compatible `/v1` server** — the router's local-serving path (`local_serving.py` slot → `served_by.endpoint` on an `llm` card, `docs/design/local-model-router-integration.md` S4) needs a `/v1/chat/completions` endpoint, so turbo-fieldflare would need a `/v1` shim/adapter or it doesn't slot in as a `placement:"local"` rung; (b) Mac/Apple-Silicon + MoE-architecture specific (won't help spark's CUDA node); (c) confirm the model set we care about (a small chat model for SMALL tier) is servable, not just the demo Gemma-3 MoE.
- **"tokenbert the same way" — note it's a DIFFERENT substrate.** Reto floated serving the keyword/"tokenbert" pass via turbo-fieldflare too. But the F20 discovery-layer keyword pass (`src/precis/workers/chunk_keywords.py` → `utils/semantic_keywords.py`) is a **homegrown KeyBERT-*technique* reimpl, not a chat LLM** — its only heavy dependency is the **bge-m3 embedder**, served by the separate HTTP embedder service (`src/precis/embedder_service.py`, ADR 0020), NOT the llama-swap/`llm:<id>` slot path. So a turbo-fieldflare-style streaming win for embeddings would be a **separate effort against the embedder service** (and turbo-fieldflare itself targets MoE *decode*, not embedding models) — don't fold it into the chat-LLM routing story.

## Morning-brief (reading cast) revival — residuals after the 07-24 SPOF stall
- Status: open · Severity: feature · Owner: `src/asa_bot/`, `src/precis/reading/briefing_cast.py` · Test: per-item below.
- 07-24→26 outage root cause: the melchior `claude_inproc` agent worker stalled (jetsam/RAM — the `agent_worker_inproc_topology` SPOF), starving BOTH the news-briefing (Discord) + reading-cast (podcast) compose jobs. Self-recovered on the 09:22 melchior restart (today's brief `dr172646` composed+published). Residuals: (a) **asa_bot durability** — `_handle_outbound` (bot.py:419) never stamps `meta.status='sent'`, and `pg_listen` has no startup sweep of pre-existing `queued` rows → ~65 messages queued during the outage are permanently stranded (decide: re-post recent vs discard stale — a blind sweep floods Discord); (b) **un-deadlock the 07-25 parent todos** blocked on never-completing child-jobs (mirror `spend_limit_parks_todos`); (c) **add a cluster-status lane** to briefing_cast.py (Reto's want; today only open-alerts leak in via the system lane); (d) **SPOF watchdog** — liveness alert on expired-lease `job` refs (verify nursery `dispatch-stall` fired). Flashcard-cast build prompt saved to `docs/proposals/flashcard-cast-prompt.md`.

---
## classify HTTP-400 spike on glm-4.7-flash/OpenRouter (likely gripe #172740 continuation)
- Status: open (root cause UNCAPTURED — instrumentation now in place) · Severity: feature (classify degraded) · Owner: `classify_topics` / `openrouter_routing` · Test: next classify batch's `llm_call_log.error` carries OpenRouter's JSON reason.
- 2026-07-26 12:20–12:56 UTC: a large `classify` batch hit `z-ai/glm-4.7-flash` via OpenRouter at ~77% HTTP 400, then STOPPED (400 = non-retryable per `_is_unavailability`, so each failing paper terminated). NOT caused by the Phase-C ship (spike predates that deploy).
- **Strongest lead:** failing calls have LARGER prompts (~6647 vs ~4497 chars, same request shape otherwise) = exactly classify's thin-abstract→first-5-body-chunks fallback (`f50894bf`). So the appended body-chunk content, or `reasoning:{enabled:false}` (SMALL tier default) interacting with a specific glm-4.7-flash provider, is implicated. `require_parameters` ruled out (no booked endpoint on these calls, per `features`).
- **Instrumentation SHIPPED+DEPLOYED (`a9448ffc`):** `_UrllibTransport.post_json` now folds the provider's HTTP-error response BODY into `llm_call_log.error` (was dropped to bare "HTTP Error 400"). The 12:20 batch ended just before this went live, so the body is still uncaptured — the NEXT classify 400 will show OpenRouter's actual reason.
- **Repro (2026-07-26, post-instrumentation): 400 did NOT reproduce.** A direct large SMALL-tier `dispatch()` to `glm-4.7-flash` via the real vault-backed worker path (`build_runtime()` on melchior) SUCCEEDED — reasoning-off + large prompt both fine NOW. So the 12:20–12:56 storm was **transient** (time-bound OpenRouter provider issue for glm-4.7-flash), not a persistent request-shape flaw. Instrumentation (`a9448ffc`) is live to catch any recurrence. If it recurs, read the enriched `llm_call_log.error` — likely a specific provider/quant rejecting a param or content.
- **Separate bug found during repro → `gripe:173317`:** `precis classify topics --ref-ids <ids>` IGNORES the scope and runs a FULL-corpus sweep (swept 5573 papers instead of 5). Footgun; also its 5570 "failed" made ZERO LLM calls (failed at a precondition BEFORE OpenRouter, 0 cost) — that precondition-fail path (not the transient 400) may be the real substance behind gripe #172740 (classify broken); worth a separate look.

---
## structure IR lacks explicit slab/adsorbate provenance (preflight `detached` heuristic)
- Status: open · Severity: polish · Owner: `src/precis/structure/{scene,ops}.py`
  · Test: n/a yet.
- `src/precis/structure/preflight.py::_slab_adsorbate_indices` falls back to a
  dominant-element heuristic (most-common element = the slab) whenever
  `atoms.info['n_slab']` isn't set — and today neither caller (the structure
  handler's put/edit, `quest.compute.dispatch_autocatpath`) can set it, because
  the Scene/Atom IR has no slab-vs-adsorbate provenance (no op records "these
  N atoms came from the `slab` op"). A doped slab (e.g. a Cu/Ag dopant swapped
  in via `set_element`) risks the `detached` check misreading the dopant as a
  floating adsorbate. Add `n_slab` (or richer op-provenance) metadata at
  slab-op time, then thread it through to `preflight()`.

---
## deploy doesn't actively disable the watcher on excluded hosts (e.g. balthazar)
- Status: open · Severity: polish · Owner: `deploy/roles/` (precis_watch) · Test: n/a yet.
- Low-pri residual from the marker-2.0/surya-leak fix (shipped `8ebbae27`): deploy
  doesn't actively `state: absent` the watcher on excluded hosts — a
  `precis_watch_enabled` flag would enforce it; today relies on the plist-move +
  playbook hosts-list omission (both reboot-validated on balthazar).

---
## spark: nvidia docker runtime not configured by ansible
- Status: open · Severity: polish · Owner: `deploy/roles/` (GPU-node
  provisioning) · Test: n/a yet.
- 2026-07-23: spark's `nvidia-container-toolkit` was installed but Docker
  was never configured to register the `nvidia` runtime
  (`/etc/docker/daemon.json` didn't exist) — broke Marker's OCR path
  fleet-wide (`docker: unknown or invalid runtime name: nvidia`). Fixed
  live (`nvidia-ctk runtime configure --runtime=docker` + docker restart),
  but no ansible role/task runs that command — a fresh bootstrap or a
  from-scratch redeploy of spark would silently re-break it. Add the task
  to whichever GPU-node role owns spark's provisioning.

---
## 🔵 autocatpath harvest bookmark — multi-job concurrency edge case
- Status: open · Severity: polish · Owner: `src/precis/quest/compute.py::harvest_measures`
  · Test: none yet (unconfirmed whether concurrent autocatpath jobs per candidate
  occur in practice).
- Commit `3e746728` fixed `harvest_measures` advancing its
  `quest_autocatpath_harvested_upto` bookmark past a still-unresolved autocatpath job
  (permanently losing that job's barrier once it did complete) — but only for
  the single-in-flight-job case. `_fresh_autocatpath_jobs` returns *all* jobs
  newer than the bookmark, oldest-first; the loop still advances `cp_seen` to
  the newest job that yielded measures even if an *older* job in the same
  batch is still unresolved. If a candidate ever has two autocatpath jobs in
  flight concurrently (e.g. a stale job from a superseded relax version still
  running alongside a fresh retry) and the newer one resolves first, the
  older job's `ref_id` falls at-or-below the new `cp_seen` and is permanently
  skipped once it does complete — same failure mode, now requiring 2+
  concurrent jobs instead of 1.
- Not fixed inline: `dispatch_autocatpath` appears to mint one job per candidate
  today, so this is unconfirmed as a live scenario. Needs a design call before
  a fix — track the bookmark as the *min* over any still-pending job's
  predecessor, or switch to per-job harvested state instead of a single
  high-water mark.

---
## 📄 Elsevier preview-PDF remediation — ~2,800 prod papers, BLOCKED on pilot findings
- Status: blocked · Severity: feature (data quality, not a bug) · Owner: cluster
  ops (not this dev session — see below) · Test: manual verification of
  refetched full-text after pilot.
- **2026-07-23 pilot run: does NOT recover full text, do not scale.** Ran the
  reset SQL + `PRECIS_FETCH_MARKUP=1` against the 5 pilot ref_ids exactly as
  documented — reset+fetch worked (all 5 re-fetched larger companion PDFs) —
  but downstream ingest recovered full text for **none** of them; 4/5 ended
  up more truncated than before the reset, 1/5 (`168074`) stayed empty.
  Root cause **confirmed** via forensics (sidecar files + watch-log sequence):
  a markup-companion-PDF sidecar race, filed as its own actionable item at
  `gripe:170349` — `elsevier_xml`'s "no `<body>`" parse failure's recovery
  path gives up permanently if the companion PDF's filename hasn't yet been
  linked onto the markup sidecar, which is exactly what happened to `168074`
  (companion PDF landed in the inbox but was never imported). Spark's Marker
  OCR was separately broken (nvidia docker runtime never configured) —
  **fixed and verified 2026-07-23**; the durable ansible follow-up is filed
  above (`spark: nvidia docker runtime not configured by ansible`).
  `ref_identifiers` stale rows exist post-reset but forensics show they are
  **not** the primary driver.
- **2026-07-24: `gripe:170349` fixed, not yet shipped** (this worktree). Root
  fix: `_run_markup_cascade` now stages a markup trigger + its sidecar under
  `inbox_dir/.staging/` (unwatched); `_publish_markup_trigger` atomically
  `os.replace()`s both into the real inbox path only once the companion
  PDF's fate is known, so the trigger is never watcher-visible with an
  incomplete sidecar. Tests green (`scripts/test tests/workers/test_fetch_oa.py
  -k Markup` + `--impacted`), ruff/mypy clean. **Next action:** ship this fix,
  then re-run the 5-ref pilot.
- **Scope corrected 2026-07-23.** A prior pass logged 224 affected papers,
  but that count came from a query that was never persisted (only a
  scratchpad list, which didn't survive) and couldn't be reconciled.
  Rebuilt the signature from `refs.pdf_pages` (Marker's own extracted page
  range — a single-page range against a >100KB cached payload is Marker
  directly reporting it only ever saw page 1) and validated it against the
  reference incident (`ref_id=162036`, gr162363's own case: `pdf_pages=
  [0,1)`, 647KB payload). That signature currently matches **~2,796 refs**,
  not 224 — treat 224 as stale. Full methodology + the regeneratable
  scoping query: `docs/runbooks/elsevier-preview-pdf-remediation.md`.
  The code fix (XML markup leg + truncation alert) shipped in `c838c8e9`;
  gr161905 (markup-vs-PDF race) was the blocker for safely re-running the
  fetch — now fixed in `7f3db0cb`. Nothing else blocks this.
- **Remediation plan + reset SQL:** documented in the runbook above (chunk
  delete + null `pdf_sha256`/`pdf_pages`/`pdf_role` + clear the
  `fetcher:%` backoff history so the stub retries promptly, same pattern as
  `paper_hygiene.requeue_stranded_fetches`) — **known incomplete as of the
  pilot** (missing `ref_identifiers` cleanup, see above).
- **Must run on cluster infra, not this dev session.** `PRECIS_ELSEVIER_API_KEY`
  lives in the DB-backed vault (`docs/design/secrets-vault.md`); `agent_rw`
  (the only DSN reachable from a dev laptop session) has **zero vault
  grants by design** — "otherwise the boundary is theater." The reset SQL
  can be prepared/reviewed here, but the actual fetch pass must run where a
  real worker's vault-capable DSN is available (melchior/caspar).

---
## `Backend` (`PRECIS_LLM_BACKEND`) — residual smell, candidate for removal
Status: open · Severity: polish · Owner: `src/precis/utils/llm/router.py` (`Backend`, `resolve_backend`, `select_transport`) · Test: n/a yet

- Surfaced 2026-07-23 while building `docs/proposals/llm-openrouter-bypass.md`'s
  local-tier ladder fixes. `Backend` is a fleet-wide binary switch
  (anthropic/openai) that has to be kept in sync **by hand** with each
  tier's independently-configurable `PRECIS_MODEL_*` id — nothing enforces
  the pairing. Set `backend=openai` without also repointing
  `PRECIS_MODEL_OPUS` off its `claude-opus-4-8` default, and the router
  happily POSTs `model=claude-opus-4-8` to OpenRouter. The exact same
  hand-sync-or-break shape produced a real bug this session (`LOCAL_BIG`'s
  claude-fallback rung pinning the OSS alias `qwen-heavy` as a `claude -p`
  model id — fixed via `_LOCAL_ESCALATION_TIER`).
- Reto's framing: "it should all go to the router, and we decide where it
  goes from there" — `Backend` shouldn't exist as a separate axis at all.
  Confirmed by grep: `resolve_backend()`/`Backend` are consumed *only*
  inside `select_transport` and `dispatch()`'s base-url coercion — nothing
  else needs the enum's identity. A resolved model id already fully
  determines which vendor/transport it needs (`claude-*` → the claude
  transports, everything else → the OpenAI-compatible ones), so
  `select_transport` could infer transport from the model id and drop the
  `Backend` parameter and `PRECIS_LLM_BACKEND` entirely — which, as a side
  effect, also solves the "per-tier backend override" gap
  `llm-openrouter-bypass.md` already flagged as unbuilt (each tier's model
  already resolves independently, so per-tier "backend" falls out for free
  once transport is inferred from it instead of a parallel global switch).
- **Not designed or built** — a raised observation, not a decided refactor.
  Needs: an actual spec for the claude-vs-not detection (a `model.startswith
  ("claude-")` prefix check is crude but may be sufficient — every compiled
  `_TIER_MODEL` claude default already fits that shape), a check on whether
  `live_config.backend_override` (the `/factory` live-switch mirror of the
  same axis) needs the identical removal, and a look at existing callers
  that pass `backend=` explicitly (`plan_tick.py`, `dispatch()`/
  `dispatch_async()`) to confirm none depend on `Backend`'s identity for
  anything beyond transport selection.
- **"Are there other switches like it?" — not audited.** One candidate
  noticed in passing, unverified: `PRECIS_EMBEDDER` /
  `PRECIS_EMBEDDER_BACKEND` (`docs/reference/config-variables.md` §4) look
  like a similarly-shaped two-axis (what + which-backend) pair — worth the
  same "is one of these redundant with the other" check before assuming
  it's the same smell.

---
## Plan for the next big session set
- (also survey the usual thing from /whatnext)
- Do token efficiency stuff (like claude.md rules vs rationale, ensure the search tools and so on all work, an audit of (coding) prompts and a review of the last 2-3 days and what lools claude gets into that are wasteful. Lets schedule the efficiency stuff after a few hours afte token reset on THursday noon. 
- Independent local research. I want the smartest local model we can fit on the big mac to do research with ml-potential on our catalyst (and run the other research processes. Occasional opus consultations are fine and encouraged, but the bulk operational stuff should be local. Right now I don't think it does anything, the local models need some more "encouragement, do things and use tools" system prompts. The nightly and mornign meditationsl also made by this local model, lets put the biggest we can fit. 
- I want the local backup to work - if it comes to this laptop from the file server, that'll be picked up by backblaze. 
= In addition to the NO-Ammonia quest, there will be other quests. I believe the natural state is a state of ... followup (maybe that's not right) but many pending jobs for any quest to be followed up when resources allow, in time. So job priorities and sequencing are kind of important, we should discuss and plan. 
- An additional wrinkle: Local classifiers. We have many papers, we want them classified in many ways. We started a classifier system, let's review it. Should it be hierarchical.  Also, with that classifier system, we can (mcp capability follows: a draft on a topic can "audit the gap" of what classifier finds should be relevant (it's classified atomic transistor relveant) and wether it is in document/subsection where it's relevant. So it is possible to go and add new citations that came in that have been classified relevant continuously and without events - paper identified, ingested, classified for a, b, c and the a, b, c things will see it because of this cite gap analysis. )
- MS Teams posting account - for new paper summaries. As we ingest them, write a pithy 1 liner and post it to MS Teams. (this requires vandichel cooperation.) We filter by relevance to the team with a classifier too, MOF and Catalysis for macatamo uni limerick.
- audio cast: we have added a few rules to the audio cast; things like "Write Mof not MOF, write thousandfivehundred instad of 1,500 etc such that the text to speech has an easier time. Discuss if this is an appropriate course of action or if we write the report normally then "pipe it" through a filter (code or LLM?) to the syntheziser. Is there a chemistry helper (Chemistry to international phonetic alphabeth or something)
- For the no to nh3 converter, we want to see the pareto front in the document, and also, the specific energy diagrams and the atom slabs, and the attached bits at optimum for the ... most relevant cases. 
- I would expect the natural state of the system is to have many pending todos (followups from earlier tasks that got filed). We should make sure that is so (should we?) and if so, triage that. Also, there are some long running types of ... conventions for a document that should be reapplied if new source material gets added (papers/patents)
- We generally search papers well, but patents are neglected. Let's see if this can be fixed at a systemic level. 
- I'd like to have a weekly "new papers" update for: solid catalysis; MOF stuff, atomic transistors, etc.. How do we manage that? We have the paper ingestion date, that may be adequate. Should it just be tacked on to the front of the respective report, then removed when the next one comes out (so, when a new paper gets classified, we go and itegrate it in the body and add that little "weely summary" update in front also); or should we keep a running log of changes? Or should we just update the doc, and programmatically do an eye-focus-like update with only the paras touched in the last week (or an arbitrary selected time span) and their neighbours (like we could make a pdf view (or any view really, do we have a "general view" that includes these eyeballey things that can be seen by robots, pdfs, docs, and the web draft interface)).  << I like the hierarchical view well.
- I'd like to have the patent package writing (draft feature) mostly run locally, and the patent search working (i think the search is ok now). I also want to prep/check the panel screw holder device this week, you will prompt me for that. And I want to find/add the documents for filing that are supplemental to the patent so it's getting more pushbutton. I think eu/us/cn is generally good; can it be done for reasonable cost. 
- I'd like to have a few agents that come on once in a while - an ops guy that makes sure no errors are showing up and all services, apis are not causing troubles and propose solutions. That's moslty pulling together the right context automatically, and have the llm judge reasonability/status. Include db load, fs space on all machines, memory load, temperatures, weird log file entries and all that. We should auto-gather that (maybe a precis-mcp kind - the "status" view="all relevant") that sort of thing. Are queus working, are we ingesting, are we categorizign. Also a prioritization thing (are we working on the right things?) What other agents ought we to have?
- I want to make everything run through the precis-mcp llm router. And wean things off opus if we can, and shift it to local models or cheaper models in the cloud. Even for coding tasks, and also for writing tasks. We can still use claude as the top dog reviewer, but we want to push all the... stupid work down, and out to other models. (Haiku is fine, deepinfra and openrouter and EU variants are good; local is best.)
- In the flashcards, precis cloze ankin 164388 and 164387: In a general way, if we make flashcards and one defines ESB and the other also, we just need just one, ie "An {{c4::ESB::abbreviation}} ({{c1::Environmental Sustainability Body::organization}}) is a framework dedicated to {{c2::ecological preservation::goal}} and {{c3::sustainable practices::methods}}". (721137 721138 are similarly --- kinda copies of eachother). 164400 is kind of weird, that is true, but what _is_ that photocrhomism, and what _are_ the other thing encompassed? Card 146392 is not a good card. C1 and C2 both are part of a very long list; it is impossible to know which one it is. Also the structure should be {{world heritage sites}} include {{site1}} {{site2}} (terse rule). 164396 is common vocab, don't need it, why did it get added? 164391 is ... not relly needed, we know. Why it is added, or better, how to adjust for more complex vocab. Also, we have precis::xxxx id numbers, lets fold those tags under precis::id::xxxx so they can be collapsed in the gui. 

---

## 🤖 asa-slack — live smoke test (ADR 0062)

- Status: `open` · Severity: `feature` · Owner: `src/asa_slack/`,
  `deploy/roles/asa_slack/` · Test: manual (no automated end-to-end harness
  for a live Slack workspace). Code shipped + deployed + connected to Slack
  (`com.asa.slack` on melchior). Since 2026-07-26 the shared `/opt/asa` venv
  code-refresh + `com.asa.*` daemon bounce IS part of `scripts/deploy` / `/go`
  (`redeploy-precis.yml` → `playbooks/32-asa-code-refresh.yml`); only
  prompt/config changes (SOUL/HINTS/config — grimoire-sourced) still need the
  full `48-asa-slack.yml` / `31-asa-bot.yml` run directly, on a controller with
  the grimoire checkout. The remaining work is the smoke test.
- **Smoke test**: confirm threading (never posts to channel root), a
  paper-search question actually works, a "kick off a job" request is refused
  (`Unsupported`, not just declined in prose), and a repeat message from the
  same person shows the per-person `memory` note working.

---

## P1 Update routing layer - make sure nothing does not go thru routing layer

- ticks go to claude -p right now, they should go thru routing layer so we can switch
- **`claude_docker` (`sandbox_run` job type, ADR-0048) is a routing blind spot.**
  It launches an opaque container whose internal `claude -p` invocation never
  touches `router.dispatch`, so it's invisible to `llm_call_log`
  (`route_log.py::spend_rollup`'s docstring already flags this: "non-LLM
  compute (spark DFT / relax / fold, container jobs) never touches dispatch").
  Deferred — fixing it means instrumenting a container image that lives
  outside this repo, and `sandbox_run` is currently dark/unused (slice1
  stub-podman only), so there's nothing running today to lose visibility into.

---

## P2 Support EU llm systems

- Support edenai.co, ShareAI – Built in Romania, explicitly marketed as a European alternative to US Big Tech infra, with multi-provider routing and EU data residency emphasis.

Eden AI – Markets itself as a broad AI aggregation platform, and is frequently cited as a “European alternative to OpenRouter” with multi-vendor support beyond just LLMs (image, video, etc.).

Orq.ai – European platform oriented toward governance, observability, and team workflows on top of multi-provider AI routing.

Requesty – EU-based routing layer; CEO statements emphasize that all data processing and routing remain within EU servers (e.g., Frankfurt), with full GDPR compliance and no data leaving the EU.

EUrouter – Hosted router with EU data residency, routing to 100+ models while keeping processing inside EU data centers.

Cortecs AI – European inference gateway with smart routing across EU providers, pitched specifically for sovereign EU-hosted LLM workloads and privacy-sensitive use cases.

Tensorix, IONOS AI Model Hub, evroc – EU-sovereign inference APIs focused on open-source models and EU data centers; they are more “single-API providers” than multi-provider routers, but fill the “EU-hosted inference” niche.
^ Review and pick best 3?

===

## 🏷️ Corpus auto-tagging cadence (gr51220) — design, not a bug

Re-scoped from gripe 51220 (kept open in prod). This is a *standing
operational process*, not a code fix: "one taxonomy axis per week — pick an
axis (`topic:`, `area:`, `status:`…), sweep under-tagged refs, review, apply"
to keep the corpus navigable without a big-bang effort. Two open design
questions before any build: (a) **mechanism** — a `level:recurring` watch / a
`job` that *proposes* tags for one axis and files them for human review, vs a
manually-kicked periodic sweep; (b) **review gate** — auto-tagging writes to
the prod corpus, so proposals must land in a review queue, not apply blind.
Deferred pending Reto picking a mechanism; until then no agent work is scoped
here.

## 🩹 Containerized-review robustness residuals

The spark *DSN-not-reaching-the-container* retry-storm is **resolved** —
`get_adopted_dsn()` re-inject into `proc_env` (`claude_agent.py:362`), proven
2026-07-19 (a real `precis-agent` container ran ~37s where it previously
`exit 1`'d on the empty DSN); regression test
`tests/test_claude_agent.py::test_container_reinjects_scrubbed_dsn`; full
root-cause is in `git log`. These robustness gaps the incident surfaced remain
open:

- **`PRECIS_MCP_DB_ROLE=agent_rw` in the review container** — reviews are
  *mostly* read-only, so the write role looks wrong; the reason it was
  `agent_rw` is the shared reviewer footer (`review.py::_footer_block`)'s
  deliberate `put(kind='gripe', …)` carve-out so a reviewer can report
  tool-friction mid-review, which a straight `agent_ro` flip would silently
  break (writes refused by the DB, `envelope.py::db_role`). **The DB-layer
  half of option (b) is now shipped**, in-repo, without a new cluster role:
  migration `0079_agent_ro_gripe_carveout.sql` adds a `SECURITY DEFINER`
  function (`public.file_gripe_readonly`) that inserts exactly one gripe
  (ref + body chunk + `STATUS:open`) and works from *any* connecting role —
  `GripeHandler._create` (`handlers/gripe.py`) now routes through it
  unconditionally, so filing a gripe already survives an `agent_ro`
  connection today; no `agent_review` cluster role needed after all. Still
  open: (1) actually flip `PRECIS_MCP_DB_ROLE=agent_ro` on the review
  container — an ops/cluster-side decision, not blocked on code anymore;
  (2) the **tool-layer** deny (`envelope.py::disallowed_tools`) still drops
  the whole `mcp__precis__put` verb for a `write:none` envelope, so a
  *generic* read-only todo/job still can't reach this function even though
  the DB would now allow it — exposing gripe-filing as its own,
  distinctly-named MCP tool (so it's simply never in `_PRECIS_WRITE_VERBS`)
  would close that gap, but adding an eighth top-level tool conflicts with
  the fixed "seven verbs" invariant asserted in `server.py` — a design call
  for Opus/Reto, not mechanical. Decide deliberately — don't blind-flip.
- **OAuth token appears in `docker inspect` `Config.Env`** — the "secret by key,
  never in inspect" goal isn't actually met (docker records inherited `--env`
  values). If that guarantee matters, move secrets to `--env-file`.

---

## 🕯️ Dark-switch audit — orphaned vs staged feature flags

**Audit done** (2026-07-22) — classification table + rationale now lives in
[`docs/conventions/dark-switches.md`](docs/conventions/dark-switches.md).
Recommend-only per the original ask: every flag on the starter list turned
out to be **intentional-staged** (a documented Phase-2 activation step
elsewhere in this file or in the code's own docstring) except one, so
nothing was deleted. Also confirmed in the same pass: `budget/breaker.py`'s
circuit breaker is fully wired on `main`, not a stray dark hook (see the
"Budget guardrails" section below).

- **Revisit `PRECIS_LAYER2_FIXER` (tex_llm_fix)** *(still open — the one
  genuine orphan/superseded candidate)*. `src/precis/utils/tex_llm_fix.py`
  (~220 lines, self-contained) is the Layer-2 chktex LLM-fixer on the
  `kind='tex'` put path, gated behind `PRECIS_LAYER2_FIXER=1` (**default
  off**), one caller (`handlers/plaintext.py:~650`). Drafts are the
  authoring source of truth now, so this dark hook is likely superseded —
  but it's low-complexity and harmless, so **leave it running dark** and
  decide keep-vs-delete deliberately later (not a mechanical rip: removing
  it also drops the Layer-2 fix-*hint* on tex puts).

---

## 🧵 Track 1 — precis-agent image (built + proven, window-wiring remains)

The §13 container-agent executor's image. **Built, distributed, and smoke-proven
end-to-end on melchior** (2026-07-18) — the concrete container-executor proof:

- **Base fixed to `serve`, not `runtime`** (Dockerfile `agent` stage). The agent
  reaches precis over MCP against the real DB + the *remote* embedder and never
  ingests/embeds locally, so it needs neither marker/torch nor the ~3.8 GB baked
  model cache — `serve` is exactly "the wheel the worker installs" (torch-free
  `builder-lite`, ADR 0021). Image **1.48 GB**, not ~5 GB; build is model-bake-free
  (~2 min) so the DockerHub-egress-blocked cluster is a non-issue (build on a
  DockerHub-reachable arm64 Mac → `docker save | ssh | docker load`).
- **Pre-existing latent bug fixed:** the `agent` stage piped `curl | bash` for
  nodesource but `system-base` ships no `curl` and the RUN never `apt-get update`d
  first → the stage *never built* (`curl: not found` → `Unable to locate package
  nodejs`). Now installs `curl ca-certificates` first, like `dev-system`/`code-task`.
- **Smoke (melchior colima, deploy):** auth-only `claude -p` → `PONG`; full path →
  `claude -p` + precis MCP (`--mcp-config /etc/precis/agent-mcp.json`) +
  `PRECIS_MCP_DB_ROLE=agent_ro` ran a real `search(kind='paper','catalyst')` → `42`.
  Vaulted `CLAUDE_CODE_OAUTH_TOKEN` (108 ch) resolves via `precis secret get`;
  the colima VM **does** route the tailscale `100.x:6432` DB (no routing gap).

**SUPERSEDES the "distribution/flip still pending" framing below — cluster
has moved well past it (2026-07-18/19, `~/work/cluster` slices 1/2A/B1/B3,
`PRECIS_DEPLOY_FROM_TREE` now the `scripts/deploy` default main `d41dab63`):**
the decentralized scheduler (migration 0074 leases) is **live** fleet-wide,
thin cron-tick/watch-poll timers retired; pure-cloud review passes
(structural/deep_review/diagram — zero local-model dep) are **relocated to
spark and live there** (deploy-owned docker, no melchior socket fight), which
is also *why* the spark DSN retry-storm above got fixed and proven; melchior's
agent-worker now runs as `deploy` not `hermes` (B1) with colima autostart
(B3). The old "distribution is melchior-only" / "flip is the window action"
bullets are stale — superseded by:

- **Capability probe + infra-fallback breaker shipped, not deployed**
  *(feature, open — owner `workers/executors/agent_container.py`, main
  `e9c915ba`).* `container_capability_ok()` (auth+bin-info+image-inspect,
  ~60s cache, fail-safe→in-proc) + a ~10-min `trip_container_unhealthy()`
  latch that catches OOM 137/image-missing/daemon-unreachable and retries the
  same call in-proc once — this is the safety net that should go out
  *before* trusting the melchior B2 flip above. Two follow-ons noted in the
  design: an empty-result assertion (cost0∧turns0∧0-toolcalls∧no-text ⇒
  raise+alert) and a `/factory` degraded-render of `capability_ok` (deferred,
  no clean seam yet).

---

## 🧵 Track 2 — litellm-retire transport-collapse

Fold the direct-`LlmClient` consumers that bypassed `router.dispatch` through it
so litellm loses its precis consumers. **LOCAL and CLOUD passes both done**
(local: main `7f24cbf0`): every former direct-`LlmClient` call site now routes
through `router.DispatchClient`. Local (`llm_summarize` / `classify` /
`paper_glossary`, `Tier.LOCAL_SMALL`) — `LlmRequest.max_tokens` (glossary keeps
2000) + `log_call=False` (per-chunk backfills add no route-log row); byte-identical
until `served_by` is seeded, then the call reroutes to the host llama-swap
endpoint instead of the litellm proxy. Cloud (`reading/cards`, `workers/briefing`,
`reading/meditation`, `reading/briefing_cast`, `Tier.CLOUD_SUPER`,
`tools_needed=True`) — folds onto `claude_agent` (a `claude -p` subprocess, direct
Anthropic OAuth) instead of the litellm proxy's `claude-opus` alias; litellm now
has no precis consumers left at all. `log_call=True` on all four (low-volume daily
casts, not per-chunk backfills) — `llm_call_log` captures real data on these
passes now. Remaining:

- **`served_by` seeding is live** (`advertise_local_llm()` in
  `src/precis/workers/llm_serving.py`, wired into every heartbeat) — no manual
  ops step needed, each host self-advertises its llama-swap models. But it keys
  `resource_slots` by llama-swap's *real* model ids, so a tier's resolved model
  name must match one of those ids or the slot-gating silently no-ops back to
  the litellm proxy. Bit melchior 2026-07-23: `PRECIS_SUMMARIZE_MODEL=qwen` (a
  litellm alias, not a real id) never matched, so `llm_summarize` never engaged
  local-serving at all — under concurrent load llama-swap's model-swap
  thrashing (10-15s/swap) exhausted litellm's worker pool → connection-refused
  flood (2782+ `worker_logs` ERROR rows in 24h). Fixed live via direct plist
  edit (`PRECIS_SUMMARIZE_MODEL=qwen3-next-80b-a3b-q4_k_m` +
  `PRECIS_LOCAL_SERVE_CONFIG=/opt/llamacpp/etc/llama-swap.yaml` on both
  `com.precis.worker{,-agent}.plist`, restarted, error rate → 0). This repo's
  own `deploy/roles/` templates those plists (not a separate cluster repo —
  only `deploy/inventory/` host_vars/secrets are gitignored); the first
  `redeploy-precis.yml` run after the live fix confirmed this by silently
  reverting it. **Made durable same-day:** `precis-worker{,-agent}.plist.j2`
  and the heartbeat plist/service templates now support a
  `precis_local_llm_model_override` / `precis_local_serve_config` host_var
  pair (skips the shared-env loop's `PRECIS_SUMMARIZE_MODEL` key when the
  override is defined, then sets both explicitly), set for melchior only in
  its `host_vars/melchior.yml`. Shipped + deployed; survives redeploys now.
  `local_serving.acquire()` also logs a rate-limited warning on this exact
  mismatch shape so a recurrence on another host surfaces immediately instead
  of burning silently. **Second gap found + fixed 2026-07-23 (gripe 170073):**
  the template support above only takes effect if the heartbeat plist is
  actually re-templated — `redeploy-precis.yml` never imported
  `playbooks/40-precis-heartbeat.yml`, so a routine deploy re-templated the
  worker plists but left `com.precis.heartbeat.plist` on whatever was last
  hand-applied (confirmed stale since June 15 on melchior, silently missing
  every host_var override added since). Fixed by adding that import to
  `redeploy-precis.yml`'s step-1 reinstall block, right after
  `20-precis-worker.yml` (heartbeat reuses that venv). Takes effect on the
  next full deploy.
- **Capacity re-check — confirmed NOT transient, log-flood fixed, throughput
  tuning still open** *(follow-up, open).* Re-checked 2026-07-23: the
  `DispatchError: all local serving slots … are busy — backing off` contention
  is sustained (1000-2000/hr, not a one-batch bounce blip) — `precis-worker`
  and `precis-worker-agent` genuinely contend for the same `resource_slots`
  row under real corpus-batch load. But prod data shows the pass keeps
  producing throughout (5785 real `chunk_summaries` written in the same 6h
  window as 5527 busy-backoff events) — it's the sibling of the already-known
  `EmptySummaryError` log-flood pattern, not an outage. **Fixed this session**
  (main, `llm_summarize.py` + `router.py::DispatchError.paused`): a paused
  busy-backoff no longer logs a per-chunk ERROR traceback (was misreading as a
  "hot pass" on `/status`) — retried in-process, one aggregated WARNING/batch,
  `transient=True` so contention can never terminally fail a chunk. **Still
  open:** the actual throughput question — if `llm_summarize` needs to clear
  its backlog faster, `precis_worker_summarize_concurrency` (`host_vars/
  melchior.yml`, cluster repo) or the llama-swap `--parallel` for that model is
  the tuning knob, weighed against melchior's known RAM/jetsam fragility
  before bumping (see "De-SPOF the agent worker" / "Co-location relief" below).
- **OpenRouter fallback for local-serving saturation/outage** *(feature,
  open — needs a design pass first).* Wanted: when a host's local llama-swap
  slot for a tier is paused/unavailable, fail over to OpenRouter instead of
  (or as well as) the litellm proxy. The existing `FailoverProvider`/`Rung`
  ladder in `router.py` (~L756-900, gated by `PRECIS_LLM_FAILOVER`) can't
  reach this today: `dispatch()` returns early on a paused local slot for
  `Tier.LOCAL_SMALL` *before* any ladder logic runs, so the flag has nothing
  to act on for this tier. Open questions: trigger condition (slot-busy vs.
  host-unreachable), default-on vs. opt-in (cost — OpenRouter isn't free the
  way local serving is), and whether this is scoped as a `docs/proposals/*.md`
  first given it touches dispatch semantics (ADR 0048). A first draft of that
  proposal exists: `docs/proposals/llm-openrouter-bypass.md`.
- **LLM flip-safety follow-up: `req.model` OSS pins bypass the
  backend-coherence check** *(bug, open — owner
  `docs/decisions/0066-capability-tiers-and-placement-chains.md`)*. `dream`'s
  `PRECIS_DREAM_AGENT_MODEL` env-pin and asa's hard-pinned
  `--model claude-opus-4-8` (`asa_bot/claude_invoke.py`) set `req.model`
  directly, which `dispatch` honors over `resolve_model(tier, backend=)` —
  so the Part-3 coherence check (which lives inside `resolve_model`) never
  runs for them, and an OSS slug can still land on a claude transport under a
  half-applied flip. Reviewer finding #2, deferred from the Phase-1
  flip-safety landing (2026-07-25).
- **ADR 0066 capability-tiers — Phase B surfaces + Phase C sweep remain**
  *(feature, in progress — owner [`docs/decisions/0066-capability-tiers-and-placement-chains.md`](docs/decisions/0066-capability-tiers-and-placement-chains.md)).*
  **Done + deployed (2026-07-25):** Phase A (additive 4 tiers + chain resolver,
  dark) and Phase B steps 1 (`resolve_chain` always-on — operator chains read
  regardless of `PRECIS_LLM_FAILOVER`, main `2d7b304d`) + 3a (`llm.cloud_enabled`
  throttle prunes cloud rungs; FRONTIER/no-local-rung tiers pause, main
  `0a2fc576`). Plus Phase B step 2 (**operator chain editor + cloud-throttle
  toggle** on `/status?tab=services` — per-tier JSON textarea `POST
  /factory/llm/chain` + `POST /factory/llm/cloud`; `status.py::_llm_chain_ctx`).
  All ship **dark** (no operator chain written, throttle defaults on) so zero
  live routing change yet. **UX decided:** JSON textarea (v1), server-validated
  list-only — a structured add/remove/reorder form is a fast-follow if wanted.
  **Remaining Phase B → folded into Phase C** (both deferred for concrete code
  reasons, not oversight):
  (3b) the **`tier_floor` forward migration** (relabel `llm` card meta
  `cloud-super→frontier` etc.) — **clobbered by `llm_catalog.seed_default_cards`**
  (`for tier in Tier` first-wins patches `tier_floor` back to legacy values
  every reconcile tick), so it must ride with the Phase-C catalog reseed
  (`_SEED_PROSE` keys + `TIER_FLOOR_MODELS` → 4 tiers) + the
  `is_cloud`-derives-from-placement renderer change, never land alone;
  (2-residual) the **caller-picker→4-rungs split** (migrate the `LLM:` dropdown
  vocab, `_TIER_RANK`, `is_cloud`) — entangled with the planner-tag-vocab open q
  below. **Phase C (GATED):** the
  call-site sweep collapsing `LLM:local`→`BIG` must NOT ship until the
  content-sensitivity/local-only constraint exists
  ([`docs/proposals/content-sensitivity-placement.md`](docs/proposals/content-sensitivity-placement.md),
  Rollout gate) — design that first. Also resolve the planner-tag-vocab note
  (ADR §"Still genuinely open": `LLM:small`/`medium` no-op via fallback since
  `plan_tick` always `tools_needed=True`).
  **PARKED by decision (Reto, 2026-07-25):** the content-sensitivity proposal
  is deferred, so all of Phase C (call-site sweep + catalog reseed + `tier_floor`
  migration + caller-picker) stays on hold — **not the immediate next step**.
  Resume by writing the proposal when the local-only constraint becomes a
  priority; until then the shipped router (steps 1/3a) + operator chain editor
  (step 2) are the usable surface, dark until an operator writes a chain.

---

## 🧵 Track 3 — factory Phase-2 cutover: remaining ops

Design [`docs/design/factory-console-and-scheduling.md`](docs/design/factory-console-and-scheduling.md)
(11 slices). All buildable-dark code shipped; what's left is cluster-ops —
state lives in the in-repo `deploy/` tree (`~/work/cluster` was retired
2026-07-19, see `deploy/README.md`), verify against the gitignored
`deploy/inventory` overlay before acting.

- **Tier-2 DB role-enforce (`PRECIS_MCP_DB_ROLE_ENFORCE`) — HELD** *(feature,
  blocked — owner `store/pool.py::_apply_db_role`).* Session-level `SET ROLE`
  is only correct on a direct-to-Postgres DSN, not pgbouncer's transaction
  pool (which the agent DSN uses via `:6432`) — a real fix needs a direct-pg
  route around pgbouncer, a security-posture decision, not a mechanical flip.
  `GRANT agent_ro TO agent_rw` prereq is already applied to prod.
- **asa slice-0 ops** *(ops, open).* `asa_bot`'s own OAuth/run-as cutover
  (vault fallback already shipped, mirrors precis's `utils/claude_oauth`) —
  live cutover is an ordered ops sequence (seed vault → verify → flip run-as
  → scope vault read → retire hermes), not yet applied.
- **Plist / `service_unit` collapse — gateway residual** *(op, open).* §L-b
  executed 2026-08-04: balthazar/caspar/spark run the collapsed
  `--profile all` unit (spark's split agent retired); imports flipped to
  20b. REMAINING: melchior cutover + `retire-split-agents.yml --limit
  melchior` (permission-blocked in-session; next `scripts/deploy` applies
  20b there, then run the retire play), and the dream×container decision
  before ever flipping `precis_agent_container_enabled` on the gateway.
- **Deploy factory-console tooltips + per-host errors** *(polish, open).*
  Shipped main `ac7712fa`, needs a `precis-web` redeploy to actually render
  on `/factory`.

---

## 🤖 LLM catalog (`kind='llm'`) — wire the policy to call-sites

All 5 catalog slices (facts/reconcile, `admit()`, ledger+reviews+tote,
`select_offering` policy, task→requirement judge) shipped + deployed, dark
by construction (empty catalog ⇒ byte-identical to today). The general
golden-task eval harness (`src/precis/llm_eval/`, `precis llm eval` CLI, 5
scored axes) and the structure round-trip eval also shipped. Nothing
consumes the policy yet:

- **Local-first CAPACITY failover valve (local primary → spill to cloud on
  demand)** *(feature, DESIGN PASS DONE — `docs/proposals/local-first-capacity-valve.md`,
  status:draft).* Reto: "if model is local, run that first, but if too much
  demand, spin out onto the net" + **local and cloud must be the SAME model** so
  a spill is quality-invisible. The saturation-escape mechanism already shipped
  (`llm-openrouter-bypass.md` item 3); the proposal spells out the three
  activation blockers (small open-weight model served both places · name-mismatch
  `resolve_model(SMALL)=summarizer` vs `served_by` resource · `openai_compat`
  rung-0 not `litellm`) and why a static chain only fails over on ERROR, not on
  SATURATION. **Next:** resolve the two BLOCKER open questions (which model `M`;
  local capacity `N`/host) → `/ready` → build. SMALL-only; MEDIUM deferred.
- **Wire `choose_model`/`select_offering` into deliberative call-sites**
  *(feature, open).* `utils/llm/requirement.py::choose_model` and
  `utils/llm/policy.py::select_offering` exist and are green, but no
  production call-site invokes them — every dispatch still resolves a model
  via the fixed `Tier` table. `Selection.endpoint` (the variant-precise
  OpenRouter booking) is similarly plumbed but unthreaded.
- **`/factory` POST routes have no auth** *(bug, open — owner
  `precis_web/routes/factory.py` / `app.py`).* **No auth on any `/factory`
  POST** (`src/precis_web/app.py` has no auth middleware) — a pre-existing
  gap across all `/factory` writes, sharper for the LLM surface because the
  per-tier chain editor (`POST /factory/llm/chain`) can route prod traffic
  (and cost) through OpenRouter now that `PRECIS_LLM_BASE_URL` +
  `OPENROUTER_API_KEY` are deployed. Mitigated in practice by the console
  being tailnet-scoped (`*.ts.net`); gate it, or consciously accept
  tailnet-trust. (Tracked as gripe 171512, tailnet-accepted for now.)
- **OpenRouter chain rungs need cost capture + backend-aware group-B
  dispatch** *(bug, open — gripe 171782).* The original footgun — the
  fleet-wide global `llm.backend=openai` GLM flip — was **retired in ADR 0066
  Phase C** (it dragged SMALL's `summarizer` litellm alias to OpenRouter →
  HTTP 400, and fed GLM slugs to un-forked `claude_agent` sites → 400).
  Per-tier placement chains replace it: a chain rung pins a concrete valid
  slug + its own transport, so MEDIUM/SMALL reach OpenRouter without touching
  the summarizer/classify local path. Two residuals survive the retirement
  and still bite a cloud chain rung: (a) the ADR-0046 "group B" call-sites
  aren't backend-aware, so a cloud rung on those paths would still mis-route;
  (c) **the `openai_tools` path logs `cost=null`**, so the $85/$20 budget
  breaker is blind to OpenRouter spend — this one is load-bearing for the
  Workstream D MEDIUM-on-`z-ai/glm-4.7` chain and should land before that
  chain carries sustained traffic.

## 🔴 High-priority

- **Run the `kind='cron'` → `level:recurring` backfill against prod**
  *(ops, open, high — owner `scripts/migrate_cron_to_recurring.py`).* ADR
  0061 retired `kind='cron'` in code; the data-migration half
  (`scripts/migrate_cron_to_recurring.py`, `--commit`-gated, dry-run by
  default) has **not been run against prod** — it needs a human to review
  the dry-run report first (the old free-form recurrence vocabulary
  doesn't map 1:1 onto the new cron grammar for every shape; `weekly`
  defaults to Monday post-migration and a few `every <N> <unit>` shapes
  outside the new grammar's range are left as `cron` refs for manual
  handling). Run `uv run python scripts/migrate_cron_to_recurring.py`
  (dry-run) against prod, review, then re-run with `--commit`.

## 📜 Patent freedom-to-operate authoring loop

Shipped + deployed (main `147a984f`): sweep prior art → ingest → iterate to
patent lingo → claims against a comprehensive FTO view → `plan` scoping ledger
→ USPTO-style export with in-text prior-art citations. Design:
[`docs/design/patent-authoring-loop.md`](docs/design/patent-authoring-loop.md).

- **Validate the loop end-to-end on a real draft** *(feature, open —
  verification, not code).* Create a `doc_type=patent` draft ("+ New draft →
  Patent application"), give it an `LLM:opus` planner todo, watch a tick: sweep
  + ingest prior art (needs `PRECIS_PATENT_RAW_ROOT` + EPO OPS on the executor)
  → iterate description → write claims with the FTO `working_set` → log a
  scoping decision → export (confirm in-text cites, no `\printbibliography`).
  Watch the patent-ingest gate on the agent host + surname extraction on
  non-comma bylines.
- **Slice 7 — visual claim tree-eye + interactive `/patent/<slug>` claims
  view** *(feature, deferred).* Today the FTO digest is a text `working_set`;
  a rendered claim-family tree + interactive browser need new render/route
  surfaces. Owner: `precis_web/routes/` + a claim-tree renderer.

## 🎧 Daily audio casts — follow-ups

Daily reading-brief + nidra casts shipped + live. Owner: `reading/*`,
`workers/cast_audio.py`. Skill `precis-audio-help`.

- **Cast length calibration — morning brief still short** *(polish, open).*
  Measured on the 2026-07-30 manual compose: morning brief ref 175895 = 1972
  words ≈ 13 min against a 20-min target — single-call compose, still
  content-bound and ~⅓ short despite the "aim for {words}≈3000" contract line.
  Decide: a segmented brief (like nidra, whose per-segment word budget in
  `ae37657a` now hits its 45-min target) or a stronger length floor. `wpm`
  values accurate, leave them.
- **Wire the quest lane into the morning brief** *(feature, open; td161129).*
  `briefing_cast._lane_quest` is a degrade-to-empty stub; quest slice-1 (kind +
  `serves` + `quest_log`) is live, so surface per-active-quest momentum + recent
  deeds. Nidra could bias its concept walk toward active-quest concepts.
- **Booklet (reading) lane — upgrade past the interim signal** *(feature,
  blocked on reading-prep slice 2).* `briefing_cast._lane_reading` is now LIVE
  off `chunks.last_seen` (papers opened in the web reader, gated past the
  ingest-time default) — a "where you left off" nudge, not the weekly booklet
  gist this item originally scoped. `routes/papers.py`'s reader now also
  stamps `refs.last_viewed_at` (`store.touch_viewed`) alongside the existing
  `bump_salience_for_ref` call — a cleaner, search-hit-free open signal the
  lane can migrate onto once it has accumulated enough history. The actual
  weekly booklet still lights up only when reading-prep slice 2 lands.
- **Cast-draft corpus hygiene** *(polish, open).* Daily cast drafts (`kind=
  'draft'`, `meta.cast`) accumulate + are embedded/searchable; add `meta.no_index`
  and/or a retention GC. Also remove leftover test drafts/episodes
  (`cast-nidra-test-546c21`, `nidra-test-546c21`).

## 📚 Topic dossiers (ADR 0060) — standing paper classification + living syntheses

Classifier slice **SHIPPED** (`src/precis/data/topics/*.yaml` +
`workers/classify_topics.py`, default-OFF `PRECIS_CLASSIFY_TOPICS_ENABLED`,
`tests/test_classify_topics.py`) — paper title+abstract → multi-label
`topic:` tags, no migration needed (marker-tag idempotency, mirrors
`paper_glossary`, not a claims table). `docs/decisions/0060-topic-dossiers.md`
+ `docs/design/topic-dossiers.md`. Full authoring pipeline (rungs, gap
analysis, build order): `docs/design/paper-writing-pipeline.md`. **Pipeline
rungs shipped (1–4):** rung 1 (5 new topics + patent sweep via gist fallback);
rung 2 (4 disposition relations + `view='integration'` + `unintegrated_papers`
minus-query, mig 0085); rung 3 (`chunk_review` memoized approval ledger — mig
0086, `edit(review=)`, `view='review'`/`'review-diff'`, diff-since renderer);
rung 4 (`edit(scaffold=)` MCP-expose + `book`/`summary` classes +
`get(kind='draft', project=)`, `_SCAFFOLDS`→`src/precis/draft/scaffolds.py`).
**Rung 6 substrate shipped (6a–6c):** 6a `src/precis/quest/placement.py`
(`place_papers` — paper→section by embedding centroid, floor 0.30, residual);
6b `src/precis/quest/residual_cluster.py` (`cluster_residual` — agglomerative
cosine over residual gists, keyword labels + exemplars, lazy sklearn); 6c
`src/precis/quest/claims.py` (`extract_claims`/`own_chunks` — claims-v0 over
`ROLE3:own`, injectable client, `pc`-handle grounded). **Cost-attribution
(`quest_tick cost=null`) is NOT a rung-6 pre-req** — the per-quest breaker meters
on chars (gr162594); null cost is cosmetic (design doc failure-mode 7,
corrected). Remaining, design-of-record only:

**Rung 6 loop SHIPPED (6d–6f), dark:** 6d-1 `quest/citation_mint.py` (code-callable
citation minter); 6d-2 `quest/weave.py` (`weave_section` — frontier section recompose
→ source-verified citations + disposition links, safe re-weave); 6e-1
`quest/weave_tick.py` (`weave_tick`: place→weave→residual→scaffold) + `precis quest
weave <qid>` CLI; 6e-2 `quest_tick.py` `_phase_weave_tick` branch (autonomous
coordinator routes `meta.quest_body='weave'` quests, budget/backoff-mirrored, dark
behind `PRECIS_QUEST_LOOP_ENABLED`); 6f `quest/weave_review.py` (`mint_weave_reviews`
— flow+cites review-todos post-weave). **Loop is runnable end-to-end** (`precis quest
weave`); nothing deployed. **Whole-draft review fanout + writeback shipped**
(dark): `quest/review_fanout.py` (`mint_review_fanout` — one
review-todo per reviewable chunk x lens across all four lenses, `precis quest
review-all`); the writeback closes 6f's ledger-gating — a review-mode `plan_tick`
that finishes with 0 findings AND an unmoved anchor `content_sha` now calls
`store.record_review(..., verdict='approved')` itself
(`workers/executors/claude_inproc.py`); a per-document `authoring_enabled` toggle
(`edit(kind='draft', authoring='on')`) lets the `cites`/`structure` lenses edit the
draft inline instead of only filing findings (`precis-review-authoring` persona);
`put(kind='draft', copy_of=..., project=...)` deep-copies a draft (rung-3
prerequisite for a review pass that shouldn't touch the source, mig 0088).
**Smartdraft review-status UI (`docs/proposals/smartdraft-review-status-ui.md`)
now built**, superseding the classic reader's retired F/C/S/A strip: a
per-block 4-state indicator + tooltip matrix, an indicator dropdown
(mark/un-review, run one/all lenses, convert to living cites, diff-since-
approval), a toolbar `N/M` rollup badge with per-checker/hub/wordcount-
balance breakdowns, an incremental `only_dirty`/`scope`-aware fanout, a
fifth document-altitude `toc` lens pinned to a TOC digest, and the
`review ▾` menu's `structural`/`deep_review` names kept only as aliases
onto `structure`/`adversarial`. Detail: `docs/architecture/state-map.md`
§ "Review-status surface". Remaining:

- **Topic-dossier weave-quest creation flow** *(feature, open)*. `mark_weave_quest`
  flags an existing quest, but nothing creates one end-to-end (mint quest +
  `ensure_dossier` + `topic:` tag + `mark_weave_quest`). The ADR-0060 synthesis-tick
  path is the home.
- **Weave v1 refinements** *(feature, open)*: multi-place (6e-1 is top-1 only);
  claim-clustering dedup (bounds re-weave citation duplication, design §Claims);
  review-todos parented on the quest lack a `level:strategic` ancestor (hygiene may
  flag as orphan).
- **Rung 7** (weekly/deep review + batching) and **Rung 8** (freshness + digest +
  contradiction/re-org) remain design-of-record.

- **Stamp `topic:<slug>` on the dossier `draft` at creation/quest-binding**
  *(feature, open — rung-2 residual)*. `view='integration'`'s PENDING/gap half
  reads the dossier's own `topic:` tags to drive `unintegrated_papers`, but no
  producer stamps them today (the classifier tags papers only) — so the gap
  list needs the operator to `tag(draft, topic:X)` manually until the
  dossier-creation/synthesis-tick rung stamps it automatically. Owed by that rung.
- **Synthesis tick body for topic-quests** *(feature, open)*. New tick body
  in `workers/job_types/quest_tick.py` alongside catalyst-discovery's
  propose-experiment body: harvest unintegrated papers (`topic:X` minus
  `integrated-into` link) → merge into dossier `draft` → log → link.
  Decide whether `noxrr` adopts it or stays purely active-search-driven.
- **Weekly digest cast + daily-brief lane** *(feature, open)*. New cast type
  reusing `briefing_cast.py`'s pattern (shareable, fires only on activity) +
  a quiet daily lane for Reto's own visibility.

## 🗺️ Quest layer

All slices (1 structure, 2 reweighting, 3 gaps+health, 4a–4e autonomous loop)
built + shipped + deployed. Skill `precis-quest-help`; tests
`tests/test_quest*.py`. Loop currently dormant (all
quests paused 2026-07-16). Remaining:

- **Link real mission quests to projects + activate the loop** *(feature, open
  — prod-data).* `put(kind='quest')` + `link(rel='serves')` deriving strivings
  from `docs/mission.md` + live research programs; re-activate quests and flip
  `PRECIS_QUEST_LOOP_ENABLED` on the melchior agent worker. Real `struct_relax`
  GPU lane on spark must be live for dispatched sims to run, not just queue.

### Quest-optimization workstream (live quest 164903 — Pd catalyst NO→NH₃)

Surfaced 2026-07-20 optimizing the first real running quest (**quest 164903**,
coordinator loop **job 166379**, dossier draft `quest-164903-dossier`). Ordered
by value.

- **`precis quest status <id>` ops CLI** *(feature, SHIPPED).* Consolidates the
  five by-hand queries into one command: logbook tail, candidate structures +
  measures + `ruled-out:*` tags, sim-job status roll (`struct_relax`/
  `autocatpath_explore` by `parent_id`, STATUS + created_at), coordinator-loop
  `quest_tick` job_event trail, and per-quest LLM spend/errors (`llm_call_log
  WHERE ref_id=<q>`). Read-only. Owner: `precis/quest/status.py` + `cli/quest.py`.
- **autocatpath lease `wall_seconds` wiring — confirmed correct, churn cause still
  open** *(investigation, done; underlying churn unexplained)*. Traced
  `PRECIS_AUTOCATPATH_WALL_SECONDS` end-to-end: it reaches the dispatched job's
  `params.resources.wall_seconds`, which is exactly the field `ssh_node.
  _lease_seconds` reads — no wiring bug (regression test:
  `TestDispatchAutocatpath.test_wall_seconds_env_reaches_the_job_and_the_ssh_node_lease`
  in `tests/test_quest_compute.py`). The observed ~2.5h re-lease churn (164913:
  165035/165286→165386; Pt/Cu/Ni: 165611/165614/165617→165824/6/8) is therefore
  NOT explained by this value being dropped — needs live cluster-log evidence
  (contention? a slower-than-expected full-network run genuinely outliving even
  a correctly-applied 2.5h lease?) before raising the default; don't guess a
  new number without that evidence.
- **Relax the slab box along with the atoms** *(feature, in-repo landed;
  container + bulk-relax follow-ups open — owner `structure/relax.py::_relax_ml`
  + `slab` op + the `precis-dft` container).* **Done (in-repo):** a `relax` op
  `cell` param (`"inplane"`/`"full"`) wraps the atoms in a masked ASE
  `FrechetCellFilter` (in-plane frees a/b + γ, pins the c-axis so the vacuum
  can't collapse), writes the relaxed lattice back onto the Scene, and folds
  into the run-cube cache key; plumbed through `StructureHandler.edit` →
  `_NeedsDispatch` → `struct_relax` job params → the container `params.json`;
  the quest compute lane (`run_compute_step`) asks for `cell="inplane"` on
  reaction (slab) candidates. **Remaining:** (1) the `precis-dft` container
  (`gpaw-relax`, external repo) must actually honour `params.json["cell"]` — the
  param rides the contract but the container-side variable-cell path is unbuilt;
  (2) *better for slabs* — relax the **bulk** once per (element, MLIP) with a
  full cell filter, cache the lattice constant, and have the `slab` op cut the
  surface at that MLIP-consistent constant (removes the spurious in-plane strain
  at build time, amortized across all candidates).
- **Richer structure design ops — holes + hydrogen + subsurface** *(feature,
  open — owner `structure` op set + `quest/tick.py` proposal rules).* Widen the
  proposer's design knobs beyond surface substitution: **remove_atom** (surface
  vacancies / holes), **add H** on the surface *and* subsurface/interstitial
  (hydride/subsurface-H chemistry), and subsurface dopant placement (not just
  adatoms). Each needs a compact op the `slab`-based proposal template can emit
  and autocatpath can inject.
- **struct_relax infra failures no longer launder into a dead-end verdict**
  *(bug, FIXED — owner `workers/job_types/struct_relax.py` +
  `workers/executors/{_common,ssh_node,claude_inproc}.py` + `quest/compute.py`
  harvest).* `struct_relax`'s dispatcher now stamps a `failure_class` (`"infra"`
  vs `"non-convergence"`) on every `record_failure(...)` call — the container/
  runner/executor dying (crash, OOM, malformed output, crash-loop guard,
  uncaught dispatcher exception) is `"infra"`; only a completed run whose
  relax code itself reports `ok: false` is `"non-convergence"`. `quest/compute.
  py::harvest_measures` reads it: an `"infra"` failure no longer tags
  `ruled-out:relax-failed` — it stays eligible for retry. Regression tests:
  `tests/test_struct_relax_job.py` (`test_dispatch_infra_failure_is_classed_
  infra`, updated `test_dispatch_failure_records_no_cache_row`),
  `tests/test_ssh_node_executor.py::test_poison_guard_fails_past_max_attempts`,
  `tests/test_quest_compute.py::TestHarvest` (`test_infra_relax_failure_does_
  not_rule_out_candidate`, `test_non_convergence_relax_failure_rules_out_
  candidate`). **Remaining, live-data ops action for Reto** (not done here —
  deliberately not touched by this fix): un-rule-out the already-poisoned
  prod candidate **st164913** (drop its `ruled-out:relax-failed` tag +
  correct the dossier text that called Pd(111) unstable) now that the fix is
  shipped. Also still open: fix the actual spark `struct_relax`/`gpaw-relax`
  container lane so relaxes genuinely succeed (this fix only stops a failure
  from being *misclassified* — it doesn't make the container run).

**Open design questions** (resolve as steering matures): cost/credit attribution
under overlapping quests (pull = max; cost needs a split/shared-pool rule);
"promise" bid term needs a concrete proxy (frontier-improvement rate); prose
rubric → machine-measurable objective vector; the proposer (propose-next-
candidate) is the crux + least-specified; sub-quest vs achievable-goal boundary
(revisit if authors keep getting it wrong).

## 🧫 External DFT catalyst import (ADR 0053) — residual slices

Sequencing steps 0–2 + a step-3 batch-mirror ingress shipped 2026-07-24 (`emt`
relax rung, `struct_runs` method+provenance schema, `structure_import` write
path, Catalysis-Hub on-demand hydrate, **keyless local-`.db` batch-mirror**
`structure/importers/cathub_db.py` — see `docs/architecture/state-map.md`,
structure kind). Steps 4–6 remain, plus follow-ups:

- **Batch-mirror CLI + a live open corpus** *(feature, open — ADR 0053 §3/step 3).*
  The engine shipped (`cathub_db.batch_import` over a local cathub `.db`, proven
  end-to-end against the real 196-reaction PengRole2020.db). Remaining: a
  `precis import <source> --filter` CLI + a resumable cursor, and a first *open*
  bulk-source adapter (OC20/AQCat25, see "Pivot" below) so the mirror has a live
  corpus rather than only hand-supplied files.
- **Richer citation when `dataset_doi` is null** *(polish, low — owner
  `structure/importers/cathub_db.py`).* Some cathub publications carry no DOI
  (the `publication.doi` is NULL); the import currently passes only the DOI
  through the adapter's `method.dataset_doi`. Carry pub title/authors/year as a
  fallback provenance so a DOI-less imported config still has a legible source.
- **More adapters** *(feature, open — ADR 0053 "Out of scope (v1)").*
  OC20/OC22 LMDB + NCCR/Zenodo tarball adapters; each is "just another
  adapter" once registered against `structure/importers/`.
- **Catalysis-Hub GraphQL now requires an API key** *(blocker, open — owner
  `structure/importers/catalysis_hub.py`; live-verified 2026-07-24).* Verified
  against the live endpoint: **every keyless request 401s** —
  `{"message":"Invalid or missing API key. Provide it via the X-API-Key header
  or as ?api_key=..."}`. This contradicts ADR 0053's "public, key-free" premise.
  The query *shape* is unchanged (the API root still redirects to a GraphiQL URL
  carrying the same `reactions{edges{node{...}}}` Relay shape), so the adapter's
  field names are not the problem — the *fetch* is. The web console's JS bundle
  ships no embedded key, so a key comes from a registration/login flow, not a
  public constant. **The cathub pip route hits the same wall:** the public
  read-only Postgres (`catalysishub.cx2awgo40dih.us-west-2.rds.amazonaws.com`,
  user `apiuser`, hardcoded pw `ubDwfqPw` in `cathub/config.py`, last touched
  2025-12-03) is **live and reachable** (dev-container probe resolved
  `52.41.37.186:5432`) but returns `FATAL: password authentication failed for
  user "apiuser"` — the "public" password was **rotated** server-side. No
  separate public S3 download channel exists either (the web frontend's 4 MB JS
  bundle references only the gated GraphQL). **Verdict: SUNCAT locked down ALL
  public programmatic access ~late-2025; every Catalysis-Hub channel now needs
  SUNCAT-issued creds.** So Catalysis-Hub is parked pending creds; the live
  first-source should **pivot** to a genuinely-open bulk corpus (see next item).
  If creds are later obtained: thread the key as `X-API-Key` from a precis
  secret and turn the keyless-401 into a clean actionable error, not a raw
  `raise_for_status()`.
- **Pivot first live source to an open bulk corpus** *(decision, open — feeds
  ADR 0053 batch-mirror step 3).* Verified keyless-reachable 2026-07-24:
  **OC20** (Meta/fairchem, `dl.fbaipublicfiles.com` S3 — fully anonymous, 200 on
  metadata; `*N`/`*NO`/`*NH_x` adsorbates on metal surfaces) and **AQCat25**
  (SandboxAQ HF `SandboxAQ/aqcat25-dataset` — `gated:auto` = HF login + instant
  click-through; spin-aware, queryable Parquet + ASE `.db` tarballs). Batch-mirror
  a *filtered* Pd/Cu/Ni×N/O/NH_x slice (few-thousand configs, not millions →
  embeds/searches cleanly) through the existing adapter seam; write an `oc20` or
  `aqcat25` adapter as the per-source normaliser. Awaiting Reto's source pick.
- **Promote `source=` to a first-class MCP `get` param** *(polish, open —
  owner `tools/core.py`).* Today an on-demand hydrate reaches the handler
  via `get(kind='structure', args={'source': ...})`; a top-level `source=`
  would be more discoverable.
- **Derivative loop + MLIP fine-tuning** *(feature, deferred — ADR 0053 §4/§7,
  Sequencing steps 4–5).* Wire `derive` off a `provenance:external` anchor
  to the existing `ml`/`dft` dispatch with a `diff`-vs-baseline; fine-tune
  the local MLIP rung on the imported corpus (forces are already captured).
- **`structure_import` isn't atomic end-to-end** *(latent bug, low-probability
  — owner `store/_structure_ops.py::structure_import`; pre-ship reviewer 2026-07-24).*
  `structure_save` commits its own tx, then a second `self.tx()` writes
  `insert_ref_identifiers` + the external run. A crash between the two leaves
  the ref with **no `ref_identifiers` row**, and on retry `structure_save`
  finds the orphaned ref by its deterministic slug (`created=False`) so the
  identifier insert (guarded by `if created`) never fires again → the
  `(dataset, config_id)` lookup permanently misses. Fold the ref create +
  identifier + run write into one transaction, or make the identifier insert
  unconditional/idempotent.
- **GraphQL filter values interpolated unescaped** *(hygiene, low — owner
  `structure/importers/catalysis_hub.py::fetch_config`; reviewer 2026-07-24).*
  `surface_composition`/`facet` are f-string-interpolated into the GraphQL
  query literal; a value containing `"` alters the query shape. Non-security
  (target host is the fixed public read-only API), but escape/allowlist before
  this path takes broader input.

## 🧪 chem-tools (ADR 0056)

`route` (retrosynth) ships dark behind `PRECIS_CHEM_ENABLED`; slices 1–3 built,
slice 1 live on spark. `protein` kind (4a/4b) shipped + deployed + live (fold
proven end-to-end, pLDDT 84.7). Design `docs/design/chem-tools-integration.md`.

- **Deploy slice 2 (LinChemIn normalize)** *(feature, open).* Rebuild the
  aizynth image on spark so the shim emits `route.json` (metrics + engine-
  agnostic steps): `ansible-playbook playbooks/43-aizynth.yml`; `scripts/deploy`
  for the precis-side `parse_syngraph`/`view='metrics'`. Owner:
  `deploy/roles/aizynth`, `docker/aizynth`.
- **ASKCOS (slice 3) live-verification** *(feature, open).* Built + stub-tested,
  inert in prod. Stand up ASKCOS v2 (`PRECIS_ASKCOS_URL`) + a `roles/normalizer`
  play; **verify the Tree-Builder request/response schema against the instance's
  `/docs`** (the one unverified surface, flagged in `src/precis_chem/askcos.py`).
- **Slice 4c — ColabFold MSA engine** *(needs-decision).* De-novo single-seq is
  low accuracy (insulin A pTM 0.1). ColabFold isn't a docker image / on PATH on
  spark; clean path = containerize (`colabfold:ready`) + decide MSA source
  (MMseqs2 API vs local DBs). (The `structure` convergence half is done.)
- **Slice 5 — `sequence` kind (design) + 4c fold accuracy** *(feature, ready to
  build).* Engines chosen: **Boltz-2** (new `protein` engine, hosted MSA) +
  **LigandMPNN** (new `sequence` kind + `design` job). PyTorch-CUDA foundation
  solved: stock `pip install torch --index-url …/cu128` gives working GPU on the
  GB10 (no NGC creds). Build: a `torch-cuda` base image → Boltz-2 layer → LigandMPNN
  layer, each = a precis engine adapter + a `roles/*` mirror of `roles/alphafold`.
- **Slice 6 — chem/bio `plan_tick` executor** *(deferred).* The `precis-lab-help`
  composition skill is built; a dedicated auto-driver couples to the planner
  (the generic planner already does it).
- **MCP-surface design review — chem/bio kinds** *(design-review, filed).*
  Coherence pass over `route`/`protein`/`structure`/(future `sequence`) through
  the seven verbs: consistent `view=` naming; discovery of dark/plugin kinds;
  the **CLI/`repl` `put` arg-allowlist gap** that rejects plugin kwargs
  (`sequence`/`engine`) so only `runtime.dispatch`/MCP JSON-RPC can drive a
  plugin-kind `put`. Its own focused pass.

## 🔌 pcb / EDA (ADR 0042)

`pcb` kind shipped to main (squash `b6a749f`, migration `0047_pcb_kind.sql`) —
store ops, Pcb/Part/Datasheet handlers, jlcparts catalog, the eyes, the
delta-objective autoplacer, BOM/CPL/netlist/DSN/mechanical exporters,
Freerouting round-trip, 8 EDA skills, `[pcb]` extra.

- **v1 done-bar (orderable board) blocked on 3 deploy binaries** *(feature,
  blocked).* `pcb/footprint.py::_easyeda2kicad_fetch` raises `Unsupported`
  when the optional `easyeda2kicad` dep is absent — real EasyEDA→KiCad
  footprint conversion isn't wired anywhere yet. Also needs the Freerouting
  jar, and (Tier 2) `kicad-cli` for gerbers — none installed on any host.
- **Cluster EDA ansible role — in-repo, not yet applied** *(ops, open —
  `deploy/roles/precis_eda`).* Tier-1 only (JRE + jar +
  `PRECIS_FREEROUTING_JAR` on gateway/melchior). Three landmines inside it:
  (1) the role's Freerouting default pins **v1.9.0**, coupled to
  `pcb/route.py::_cmd`'s 1.x batch CLI (`-de in.dsn -do out.ses -mp 0`) — 2.x
  reworked the CLI, don't bump without rewriting `_cmd`; (2) the jar's
  sha256 pin is blank (supply-chain TODO); (3) unverified — the DSN emits a
  via referencing a padstack never defined in its library section, check on
  the first real-jar run.
- **Slice 3 — datasheet lazy ingest** *(feature, open).* Not started.
- **Slice 8 — web ratsnest SVG + BOM table** *(feature, open).* Not started.
- **Slice 9 — design-session orchestration (capstone)** *(feature, open).*
  Not started.

## 💰 Budget guardrails — global spend circuit breaker

Design [`docs/design/budget-guardrails.md`](docs/design/budget-guardrails.md)
(the doc's own "not built" status header is stale — Pieces B and real-cost
capture are shipped; treat it as historical design-of-record, not
present-state). **Piece B (the global circuit breaker) and real-cost capture
are SHIPPED** on `main` (confirmed 2026-07-22 against `tests/test_budget.py`):
`breaker.gate_tier` is called from `router.dispatch`
(`utils/llm/router.py:832`) and `breaker.gate_paid` from the cache fetch path
(`handlers/_cache_base.py:651`); both gate on the rolling dollar meter *or*
(for the `claude -p` OAuth transport) the subscription-quota snapshot; both
alert on trip/clear and auto-clear as the window ages; `/budget`
(`precis_web/routes/budget.py`) exposes web-editable
`PRECIS_BUDGET_HOURLY_USD`/`_DAILY_USD` overrides plus a "resume now" bypass.
Real-cost capture is also done end-to-end: Claude reports its own cost;
`result_from_openai` (`utils/llm/router.py`) prefers OpenRouter's returned
`usage.cost` over the local price-table estimate; `handlers/perplexity.py`
prefers the response's own `usage` cost block over its flat `ClassVar`
estimate. Remaining:

- **Piece A — cost-band affordance** *(feature, open — machinery only).*
  `src/precis/budget/bands.py` has the `Cost`/`Pace` enums, the tier→band
  table, and `Band.label()` (`'free · fast'` etc.) — but nothing outside
  `bands.py`/`breaker.py` imports `band_for_tier`/`is_expensive`, so the bands
  are **not actually surfaced to any model** yet (no prompt/skill references
  them). Still open: wire the label + a permissive "escalate freely when
  needful" policy line into the relevant system prompts. Owner
  `src/precis/budget/bands.py` + wherever agent system prompts are assembled.
- **Piece C — per-entity cost attribution** *(partly shipped).* `LlmRequest.ref_id`
  now stamps `llm_call_log.ref_id` (was never wired → 100% null in prod), so spend
  is attributable to an *entity*, not just a `source` pass — **cannot be
  back-filled**, so it's stamped at dispatch. Live on `quest_tick`/`quest_review`
  (+ lane-split source) and the active job-type lanes (`structure_propose`,
  `cad_propose`, `cad_discuss`, `good_search:triage`). Mining CLI: `precis llm cost
  [--days N] [--by transport|source|ref|model] [--source X]` (read-only rollup —
  calls · real-$ · char volume · wall-clock, units kept *separate*). *Remaining
  follow-ups:*
  - **Stamp the rest of the attributable callsites** — `precis_web/ask.py`
    (`generate_answer`'s `conv_ref_id` param is accepted but not threaded onto
    the `LlmRequest`) + `workers/_chase_llm.py` ×3 (`dispatch(LlmRequest(...))`
    calls carry no `ref_id` — needs threading from callers). Pass-level passes
    (dream, review) legitimately carry no single ref — leave them.
  - **Local-lane visibility** *(shipped — lite logging).* The corpus batch passes
    (`llm_summarize` / `classify` / `paper_glossary`) previously ran
    `log_call=False` (invisible). They now write a **lite** `llm_call_log` row —
    metadata (chars / cost / duration / ref_id) kept, the ~18 KB unique-per-call
    replay blob skipped (`LlmRequest.log_blobs=False`; ~660 B/row). So
    local-vs-cloud volume + wall-clock **is** mineable via `precis llm cost`.
    `route_log.gc` (90d floor, `PRECIS_LLM_LOG_RETENTION_DAYS`) is now wired into
    the sweeper (was defined-but-uncalled) since the batch passes add ~1 row/chunk.
    *Residual — non-LLM compute only:* spark DFT / relax / fold + container jobs
    never touch `dispatch`, so a placement view over those still needs its own
    counter (the factory-console §8 `service_calls` rollup: per `(pass, host, day)`
    count + wall-clock). Build only if the week's data says local *compute* (not
    LLM) capacity is the constraint.
- **Open decisions** (design doc): ledger union without double-count; per-model
  price-table source + upkeep; cheap-band threshold; real cap defaults.

## 🕸️ Citation-chunk grounding — v1 built, dark

Design + full history: [`docs/design/citation-chunk-grounding.md`](docs/design/citation-chunk-grounding.md).
Built (this ship): paper/draft/structure/cad/pcb/plan/pres/patent all gained
`view='links'` (closes the prior link-blindness gap); the `inbound_chase`
worker pass (one-time exhaustive per-paper sweep of S2-known citers,
auto-ingesting missing ones, chunk-precise in **both** directions — reuses
`chase.py`'s existing locate/verify hooks); and a capped verdict sidecar on
chunk reads (`_citer_sidecar.py`, both `cites`/`cited-by` directions). Full
suite green, ruff/mypy clean, not shipped-and-live — dark behind
`PRECIS_INBOUND_CHASE_ENABLED` (default off).

- **Decide when to flip the flag** *(needs-decision, owner: Reto)*. The
  exhaustive-no-cap chase policy leans on the global spend circuit breaker
  (💰 Budget guardrails Piece B above) as its cost backstop — that's
  implemented but **unshipped**. Either ship Piece B first, or accept
  manual-observation risk for an outlier high-citation paper and enable
  anyway. Not a code blocker — an operator judgment call.
- **Type-2 (general-content-similarity, non-citation) linking** *(feature,
  deferred, separate scope)* — `related-to` + `meta.note`, deliberately not
  built alongside this; do not conflate with the `cites`-relation work above.

## 👁️ Draft citation-groundwork pre-pass (ADR 0051 Level 2, unscoped)

*(feature, open — owner: `workers/thread_persona.py` + `planner_prompt.py`,
new thread_type).* Multi-eye-context inspection session confirmed: a draft
citing a paper at whole-ref granularity (`[pa<id>]`) renders as a keyword-
cluster map to any later `render_working_set` pass — never verbatim text —
while a chunk-level citation (`[pc<id>]`) renders real text
(`utils/eye_render.py::_render_doc_eye`). Shipped: a soft write-time
warning (`DraftHandler._whole_paper_cite_hint`) + a read-time "## Hygiene"
footer on the draft outline surfacing undefined abbreviations and
whole-paper citations anywhere in the draft. Deliberately **not**
built: the dedicated pre-pass — a cheap-model tick that reads a section's
cluster-map-level working set + its `Enhancement`-marked gaps, drills/
searches the cited papers for the specific supporting chunk, and rewrites
the citation before the real edit turn runs. Proposed home: a new
`thread_type` in `workers/thread_persona.py`'s `THREAD_PERSONAS` registry,
run as a tick ahead of the edit tick — the next concrete instance of the
already-deferred "Level 2: focus verb + render-loop" ADR-0051 slice, not a
new initiative. **Hold until the shipped warning proves insufficient in
live use** — build only if agents keep leaving whole-paper citations
despite the nudge.

## 🔒 Proprietary / local-only content routing (backlog)

*(feature, open — owner `utils/claude_agent.py`, `utils/claude_p.py`,
planner writer, reviewers).* No tag axis or routing guard exists yet for
"this content must stay local" — a corpus tag search finds nothing under
`proprietary`/`local-only`. Data-governance need: mark refs/chunks that must
never leave the box via a cloud LLM call, and have the agentic dispatch +
one-shot judges + planner + reviewers exclude tagged content from cloud
prompts, routing to a local model instead. Needs a local-model adapter peer
to the cloud transports (the ADR-0046 router's `Tier.LOCAL_*` already
exists as a landing spot) plus a guard that refuses to assemble a cloud
prompt containing any tagged ref. Pairs with per-surface persona work
(writer/chat/reviewer each with their own role + backend).

## 🩹 asa storeless-precis incident — residual

- **conv capture silently stopped 2026-06-27** *(open, investigate — owner
  `asa-bot capture_shim` + `handlers/conv`).* No `kind='conv'` rows since then
  despite `POST /capture` → 200 and no `capture-fallback.jsonl`. Likely the same
  storeless-precis root cause; **verify after the next asa Discord turn** now
  that the double-build fix + monorepo cutover are deployed. If still broken,
  trace the shim's write path (200 despite no persisted row).

## 🔐 secrets vault (ADR 0055) — residuals

Shipped + cut over. Remaining are small/by-design:

- **Left in env by design:** `PRECIS_UNPAYWALL_EMAIL` (a mailto); litellm/openclaw
  ansible-vault secrets stay until those tools retire (sweep with litellm teardown).
- **Deferred by design (ADR 0055):** per-service DB roles + per-name ACL;
  `pg_notify` cache invalidation (currently 60s TTL); out-of-process broker.
- **Cheap/local-model research tier** *(feature, open).* precis's agent/research
  surfaces (asa, reviewers, planner, `perplexity-research` ~$0.50/call) all run
  cloud Claude with no cheap pre-filter. Add a local-model tier (ADR-0046 router
  `Tier.LOCAL_*`) for broad fan-out / low-stakes triage before paid escalation.
- **"Corpus before paid web" cost-ordering line** *(polish, open).* One line in
  `precis-research-help` + asa's SOUL: exhaust free corpus before spending on
  `perplexity-research`.

### Cluster residuals (ops, `deploy/`)
- **daily_briefing references a dead `cluster` DB** — `roles/daily_briefing` runs
  `psql -d cluster` (renamed/retired); repoint at `precis_prod` or remove.
- **extract_watch uv-cache perm error on balthazar** — `~deploy/.cache/uv` has a
  root-owned `.git` blocking `uv pip install`; chown/clear it.
- **Orphan sweep from feynman/quest retirement** — installed venvs/npm bits
  (`/opt/mcps/quest`, `/opt/mcps/extract`, `@companion-ai/feynman`), quest's
  `papers` schema, unused `quest_*`/`feynman` group_vars. Harmless; sweep with
  the litellm teardown.

## 📧 `email` kind — next steps (slices 1–4 shipped)

Slices 1–4 SHIPPED to `main` (slice 4 = `inject_scan` tier-1/2 + quarantine
ladder, `cfb702f9`; dark behind `PRECIS_INJECT_SCAN_ENABLED`). Design +
present-state: `docs/design/email-kind.md`, `state-map.md` `email` bullet.

- **DEPLOY slice-4 code + ENABLE mail_poll — Reto's Phase-2 window.** Slice-4
  code is shipped but **not deployed** (dark, so harmless to lag). The
  `mail_poll` enable flag for melchior was prepared once (`~/work/cluster`
  era) but got `git restore`d 2026-07-19 (Reto: "don't want them just yet")
  and the cluster repo has since been retired — **verified 2026-07-24: the
  current in-repo `deploy/inventory/host_vars/melchior.yml` overlay has no
  `precis_worker_mail_poll` key, and `deploy/roles/precis_worker/templates/
  precis-worker.plist.j2` has no `PRECIS_MAIL_POLL_ENABLED`/
  `PRECIS_INJECT_SCAN_ENABLED` gate block** — the prep needs to be redone
  from scratch in the in-repo tree, not just committed. When ready: add
  `precis_worker_mail_poll: true` to the melchior overlay + the gate block
  to the plist template (mirrors `precis_worker_classify`), then
  `scripts/deploy` picks up slice-4 code + the flag together and starts
  polling `rs@retostamm.com` from melchior.
- **Enable slice-4 `inject_scan` after verifying mail_poll's tier-0 rows** —
  set `precis_worker_inject_scan: true` on melchior (gate block already added);
  it runs on the local `summarizer` proxy there. Kept dark until the tier-0
  verdicts look right in prod.
- **Slice 5 (design-only)** — opt-in promotion (`split_text`→`write_paper`-equiv
  for a chosen clean message) + wire the recurring morning brief to read clean,
  non-quarantined, summarized email rows. Send (SMTP) is a later slice behind a
  confirm-gate.

## 🎨 `figure` kind — deferred slices

Slice 1 shipped (interactive SVG canvas, `/figure` editor). All below are
feature extensions, ordered by value. Owner: `precis/figure/*`, `handlers/figure.py`.

- **PNG / animated-raster export** — a `figure_render` derived-lane job + a
  rasterizer (no SVG rasterizer dep today; `resvg` + declarative keyframes, no
  headless browser). PNG first.
- **three.js / `scene3d` mode** — `meta.render ∈ {svg,scene3d}`; declarative
  scene IR + trusted client renderer (never eval raw three.js).
- **Per-node chunk split** — one chunk per top-level element once per-node edits
  land.
- **Draft-embedding** — a draft includes a figure's rendered raster as an asset;
  add a `figure-in`→draft link.
- **`read(handle)` reference tool in the turn loop**; **pin full
  `precis-figure-svg` skill text into the turn prompt** (polish);
  **formalized-convention hard-checks** (opt-in palette-allowlist lint).

## 🖇️ `mermaid` kind + diagram chunk-binding (ADR 0057)

All five slices shipped; `mermaid` kind live (deployed `c7ac23db`). Design
[`docs/design/diagram-editing-and-chunk-binding.md`](docs/design/diagram-editing-and-chunk-binding.md).
Follow-ups:

- **Engine gaps — gantt / pie / sankey / C4 / block don't render** *(bug —
  owner `mermaid/mermaid.py` + `[mermaid]` extra).* The in-process QuickJS engine
  lacks browser globals (`offsetWidth`, `structuredClone`, `screen`, …). Fix:
  bump `mermaidx` when upstream ships a fuller shim, evaluate `termaid`, or
  polyfill the cheap globals. `precis-mermaid-unsupported` steers the model to
  renderable alternatives meanwhile.
- **Rich cross-kind seed rendering in `diagram_propose`** *(feature — owner
  `workers/job_types/diagram_propose.py`).* Render richer per-kind seed content
  (a figure's SVG, a cad cross-section) instead of a titled reference.
- **Self-directed drawer follow-ups** (from the shipped slice-5 upgrade, main
  `6585223d`): **mermaid L1/L2 auto-context** (add a `mermaid`-owning-draft
  reverse resolver + route `document_context_for`; figures get it free);
  **L2 semantic leg** (embed instruction entities + rank the draft's chunks, not
  just literal term hits — owner `diagram/doc_context.py`).
- **`wip/backlog-docs` branch (primary repo)** *(polish).* One local-only commit
  `e5643873 docs(backlog)`; ship it or drop it.

## 🟣 Turn-taking fisheye (ADR 0051) — Level 2 residual

Level 1 (fisheye context — policy-chosen eyes, no focus verb) shipped +
deployed + live, default-ON at both sites it applies to:
`workers/job_types/plan_tick.py` (planner) and `workers/dream_agent.py`
(dreams), via `utils/fisheye.py::render_fisheye` +
`utils/working_set_render.py::render_working_set`. Reviewers stayed
out-of-scope (they read the strategic todo-tree, not a chunk-tree — a
different render model). Level 2 (fisheye *curation*) is unbuilt:

- **`focus` verb on the MCP surface** *(feature, open).* Wire
  `workers/working_set.py`'s `WorkingSet`/`Eye` + `render_fisheye` behind an
  agent-facing verb so a model can place/remove its own eyes, not just
  planner/dreams' policy-chosen ones.
- **`--max-turns 1` render-loop driver** *(feature, open — owner
  `workers/job_types/plan_tick.py`).* Gate `PRECIS_TURN_LOOP`; the decay
  ladder + bunched eviction (`WorkingSet.crunch`) already exist but nothing
  drives a single-turn render→act→re-render cycle yet.
- **Promote-plan-node→todo** *(feature, deferred).* Needs `TodoHandler`
  `anchor=` support; belongs with the render-loop work.

## 🔵 Turn-as-job routing + context DSL *(deferred — design captured, not sliced)*

Design [`docs/proposals/turn-routing-and-context-dsl.md`](docs/proposals/turn-routing-and-context-dsl.md).
Every turn = `kind='job'`; Part 0 thread persona + cache-ordering + affinity
scheduling; Part 1 delegate-on-confidence routing; Part 2 stateful context DSL
(ADR 0036 handles + fidelity ladder). First slice = persist turn-as-job + shadow
router. Owner: `handlers/job.py` + `workers/dispatch.py` + `utils/prompt/`.

## 🔍 Paper search — `unique_per='paper'` default mode

Tier-1 broad retrieval (RRF fusion, `handlers/paper.py::PaperHandler.search`)
shipped `per_paper=N` as an opt-in diversity *cap* on fused results —
useful for breadth-triage but not the resolved design below. Default is
still chunk-rows.

- **Paper-row default mode** *(feature, open — design resolved
  2026-06-03, unbuilt).* Make `unique_per='paper'` (one row per paper: best
  handle + `more` count of additional hits + best-chunk's own keywords) the
  default; `unique_per='chunk'` (today's shape) becomes the opt-in/drill
  mode, implicit when `scope=` is set. Mode-aware page sizes (`top_k=25`
  paper mode / `10` chunk mode) + a top-line "N papers of M matched (K chunk
  hits)" counter + "refine before paging" guidance in `precis-search-help`
  ship with it. Known edge from review: with `per_paper=1` a `card_combined`
  chunk can consume a paper's only slot before body-chunk dedup runs.

## 🟡 Drive presenter completeness (`/drive`)

The unified item view shipped and graduated into **Drive**
(`docs/proposals/web-ui-rationalization.md`, which subsumed and finished
`docs/proposals/unified-item-view.md`): folder tree + CRUD grafted onto
the cross-kind search/facet/presenter base, and every legacy list route
(`/items`, `/papers`, `/papers/triage`, `/drafts`, `/papers-needed`,
`/refs/{oracle,patent}`, `/cfp`) now redirects to a Drive preset — the
retirement the design doc's own risk list once called "none are a clean
1:1" turned out to reduce cleanly once Drive owned the folder tree +
per-row actions. `ItemPresenter` has the full method contract
(`preview`/`hover_preview`/`thumbnail`/`actions`, generic defaults + a
`youtube` thumbnail override), pagination, kind/tag/folder facets, and
per-row thumbnails + hover popovers. Owner `precis_web/routes/drive.py`,
`precis_web/item_view.py`.

- **`@abstractmethod` promotion** *(open).* The presenter contract has a
  generic default for every method; flipping to the check-time-totality
  guarantee (the design doc's acceptance criterion) needs a dedicated
  presenter per source/artifact kind (~40 kinds) — a separate, larger pass,
  not a mechanical follow-on. Do this alongside (not instead of) the
  kind-taxonomy audit below since both touch every kind's declaration.
- **Kind-taxonomy audit** *(open, coupled).* Reconcile `role`/`corpus_role` drift
  (datasheet, pres); collapse near-dup kinds (perplexity-*/websearch/web/wikipedia;
  calc/math/oracle); rewrite `precis-*-help`. No-legacy-alias license.
- **Slice 4 — "write a document from this view"** *(open).* A tailored filter is
  a serialized query → mint an authoring job scoped to exactly those refs.

## 🟢 Draft inline editor

Shipped + deployed, core complete (click-to-edit prose, ProseMirror + live
squiggle, split/merge, `[`-autocomplete, reveal-on-cursor chips). Design
[`docs/design/draft-inline-editor.md`](docs/design/draft-inline-editor.md).

- **Deferred extensions** *(optional, none block use):* `[`-autocomplete over
  non-paper kinds (chunks/findings); resolved-title chips; structured-block
  creation from a slash-menu; per-draft language selector for spellcheck.
- **Headless-browser verification in CI** *(testing infra, high-value).* The
  interactive editor + virtual-scroller JS has **no gate coverage**; several
  browser-only bugs reached prod. A Playwright-over-SSH-tunnel harness
  (2026-07-05) found+proved the focus bug — wire a slim version into
  `scripts/ship`: boot the web app on the test DB with a seeded draft, assert a
  clean console + a couple of core interactions. (Also listed in the arch review.)

## 📝 Draft footnotes + annotations (deferred design)

Authors slice shipped (`refs.authors` byline + ROR affiliation, LaTeX/docx
export, web edit form — mirrors papers, no new kind). Two siblings from the
same design split are still deferred, unbuilt:

- **Footnotes** *(feature, deferred).* A first-class `footnote` chunk_kind
  anchored to its block via `meta.anchor`, out-of-flow, embedded+citable,
  ships in export — parallels `term`/`figure`/`caption`.
- **Annotations** *(feature, deferred).* A separate editorial layer, NOT in
  `reading_order`; `draft_annotation` chunk_kind + `meta.anchor` +
  `meta.author`, append-only via `chunk_events` (the `gripe_comment`
  idiom), does not export.

## 📓 reMarkable send — device pairing pending

Send-draft-to-reMarkable-2 shipped + deployed (render footnote-cite
excerpts, container uploader via `ddvk/rmapi`, job `remarkable_send`, web
+ CLI entry points). Runs **dark** — the button stays hidden and the job
declines until S0-ops device pairing happens:

- **Pair + arm** *(ops, open — owner Reto, `docker/remarkable/README.md` +
  `deploy/roles/remarkable`).* `rmapi` device pairing (8-char code) →
  vault `REMARKABLE_RMAPI_CONFIG` → `ansible-playbook playbooks/47-
  remarkable.yml` → set `PRECIS_REMARKABLE_IMAGE` in `precis_shared_env` +
  re-run the agent-worker role. First build has 3 unverified externals
  (exact `ddvk/rmapi` release asset names, the `rmapi.conf` format, colima
  bind-mount sharing on macOS) — check at first run, not blind-trust.

## 🔵 Retire the `equation` chunk kind → math as `$…$`/`$$…$$` in prose

*(decided; feature/simplification).* North star: no dedicated `equation` kind —
math is LaTeX inside prose, KaTeX-rendered on read. **Drafts (278) sorted.**
**Papers (~54.6k, the bulk) — the real target, needs its own handling** (see the
deferred paper-side section below): append-only body chunks (DELETE+INSERT
re-runs the cascade at scale), produced by Marker not the LaTeX importer,
rendered by the two-pane PDF reader, and deliberately un-embedded
(`SKIP_EMBED_TYPES`). Shared work: a KaTeX-safe body normalizer (strip
`\label`/`\tag`, `align`→`aligned`, pure tested fn + gold set); numbering/`\ref`
decision; LaTeX export of `$$…$$`. **Interim** if not scheduled: just make
`equation` *render* (wrap bodies in `$$`).

## 🟢 Dark-factory build/deploy workstream

`scripts/deploy` + `/go` + `/whatneedsdoing` + post-ship follow-through shipped.
North star: `claude -w` → spec → `/go` → implemented/gated/merged/deployed. Owner
`scripts/`, `.claude/commands/`, `CLAUDE.md`. Remaining:

- **Backlog groomer — OPEN-ITEMS half** *(open).* The gripe→`fix_gripe`-todo
  groomer shipped (`workers/backlog_groom.py`, default-OFF). The OPEN-ITEMS half
  is blocked on two prereqs: (1) `OPEN-ITEMS.md` isn't packaged into the wheel, so
  a deployed worker can't read it (needs a packaged/DB-backed backlog source);
  (2) no `build_feature` job_type for a free-text feature item. **Activation
  (ops):** flip `PRECIS_BACKLOG_GROOM_ENABLED=1` on a system worker to drain open
  gripes; watch mint count + fixer throughput before widening.
- **`/testfeature <prompt>`** *(open).* Agent loop that exercises the MCP surface
  (`scripts/exercise-mcp` seed), finds bugs, fixes, `/go`. Turn/cost-capped.
- **`/checklogs`** *(open).* Read the recent LLM-error surface (prod `agentlog` +
  `alert` + failed `kind='job'` + error `ref_events`; local logs), cluster the
  top-N recurring failures, fix root cause, `/go`.
- **Cheap-model tiering** *(open).* Route mechanical LLM work (`llm_summarize`,
  triage children, CI-fix) to a small 4B–14B model; reserve Opus for judgment.
- **Out-of-band DB-liveness monitor** *(open, ops).* The 2026-07-05 ~8h prod
  outage ran unalerted because every alerting path is DB-backed. Needs an external
  `SELECT 1` watcher on a different host (fixer host / laptop cron) → Discord on
  failure. A degradation trend-alarm (worker-log volume halving) is a cheap second
  signal.
- **Widen `scripts/ship` auto-fix surface** *(polish).* Auto-fix + amend anything
  the gate can resolve without judgment (import sort, trivial mypy stubs).
- **Deferred:** holdout scenarios (anti-overfit eval outside the repo); digital-
  twin fidelity (richer stubs); auto-deploy as a daemon (vs `/go`-chained).

## 🔧 Autonomous fixer loop (ADR 0048) — residuals

`src/precis/fixer/` — the repo-dev CI scheduler (tick a `docs/proposals/`
proposal or open gripe → headless `claude -p` in a worktree → gate →
report/ship/deploy, wraps `/go`) is **built + shipped + running live** on
Reto's laptop (`com.precis.fixer` LaunchAgent, hephaestus, report mode,
20-min interval, dodges the redeploy-restarts-itself problem by not being a
deploy target). Dial: `PRECIS_FIXER_AUTONOMY` = report/ship/full (full-auto
ship+deploy proven end-to-end).

- **Gripe 49958 — NEEDS_YOU discards a salvageable build** *(bug, open —
  owner `fixer/tick.py::run_tick`).* On a real gate failure (mypy,
  non-auto-fixable lint, non-zero `claude -p` exit) the branch is never
  pushed and the `finally` removes the worktree — an expensive opus build
  that's 90% right is thrown away with nothing to inspect. Proposal:
  push-on-NEEDS_YOU too (pair with branch GC so half-built branches don't
  accumulate), or keep the failing worktree under `.fixer-work/` with a
  pointer in the report.
- **Stale branch cleanup** *(polish, open — needs Reto's OK).* `fix/smoke`,
  `fix/build-prompt-map-freshness`, `fix/fixer-persistent-log`,
  `fix/launchd-smoke` (origin) + `fix/shippath` (local).
- **`PRECIS_FIXER_DISCORD_WEBHOOK` unset** *(ops, open).* Loud NEEDS_YOU
  reports are log-only (`/tmp/precis-fixer.log`), not proactively surfaced.
- **Agentic post-deploy followup is a `/readyz` stub** *(feature, deferred).*
  Real look-at-prod-and-fix-forward is just the next review-gated proposal
  today, not an active post-deploy check.
- **Deferred (ADR-filed):** groomer write-side (the `whatneedsdoing` half),
  automated `ready`-on-gripes, a doc-freshness ship judge, `sandbox_run`
  job-type isolation.

## 🟠 Worker liveness + observability

Slice 1 (observability: boot-event row + `worker-restart`/`dead-worker` nursery
detectors + Discord webhook) shipped + deployed. Owner `workers/nursery.py`,
`cli/worker.py`, `alerts.py`, cluster repo.

- **Set `PRECIS_OPS_ALERT_TARGET` on system-profile workers** *(ops, open).*
  Critical push is dark until set (cluster ansible env); until then
  worker-restart/dead-worker alerts only land in `/alerts`, not proactively.
- **Tier B — lease as the single job-substrate liveness authority** *(shipped
  622dd03c for `ssh_node`/`claude_inproc`/`claude_docker`; NARROWED, still
  open for `coordinator`).* Reclaim now takes over a `running` job whose
  lease expired (epoch-aware `reclaim_stale_running` + a generalized,
  epoch-vs-expiry `poison_guard` attempt cap — `executors/_common.py`), and
  the sweeper's `PRECIS_STUCK_JOB_HOURS` wall-clock is retired for those
  three lease-owning executors (`sweeper.py::_LEASE_OWNING_EXECUTORS`).
  Remaining: `coordinator` deliberately does NOT opt into
  `reclaim_stale_running` (a crashed slice has no re-claim path of its
  own) and still depends on the wall-clock sweep as its only crash
  recovery — giving it one, or accepting the wall-clock as its permanent
  backstop, is the open piece. Spec:
  `docs/proposals/compute-lane-lease-epoch.md` (status: built).
- **De-SPOF the agent worker** *(open, ops — highest-value).* `plan_tick` runs
  only on melchior operationally (hermes `~/.claude` OAuth + `PRECIS_MCP_CONFIG`).
  Provision a second agent host (caspar/balthazar) with the OAuth state + an
  agent daemon. No code.
- **Co-location relief** *(open, ops).* Get the ~73 G `mlock`'d llama.cpp weight
  off the agent host (or drop `--mlock`) so jetsam stops targeting the worker.
- **Sandbox substrate** *(open, big lift).* The `sandbox_run`/`claude_docker`
  substrate (ADR 0048, `docs/proposals/sandbox-run-substrate.md`) runs ticks in
  isolated containers — subsumes the SPOF + co-location. The durable north star.
- **Config-drift guard (`deploy/`)** *(open).* A deploy assert that deployed
  launchd plists match rendered templates (analogue of the venv-commit assert).
  Owner `redeploy-precis.yml`.
- **Rationalize the cluster daemon-user model** *(ops, open, deferred — owner
  `deploy/`, not urgent).* `hermes` (OAuth/`~/.claude` state) vs
  `deploy` (owns `/opt/homebrew` + the colima docker socket) is a two-user
  split that already bit the Phase-2 container cutover once (hermes
  couldn't reach deploy's 0600 docker socket on melchior). The melchior
  instance was worked around via a run-as cutover; the fleet-wide question
  — how many daemon users, what each runs, per host — is still open. Scope
  properly once Phase-2 settles; likely fold hermes→deploy or land on one
  `precis` service account.

### docx / EndNote export — validation-pending
Native EndNote CWYW export shipped (`export/endnote.py`). Round-trip correctness
can only be confirmed by opening the export in real Word+EndNote + "Update
Citations and Bibliography" — Reto is testing. Open notes: `EN.Layout` hardcoded
to `"Annotated"` (make a param if requested); docx `[dc<id>]` cross-refs render
as plain text not Word `REF` fields (pre-existing, low-pri); `[pc<id>]` cited-
passage embedding shipped but round-trip unverified (EndNote drops Research-Notes
on library import; retry with `<custom1>` if persistence wanted).

## 🟢 Chunk-tag classifier (ADR 0047) — remaining

Cascade shipped + deployed + validated. Design
`docs/design/chunk-classifier-cascade.md`. Owner `workers/classify.py`,
`data/axes/`, cluster env.

- **Enable continuous corpus tagging** — worker pass deployed default-OFF; flip
  `PRECIS_CLASSIFY_ENABLED=1` to drain the remaining ~1.29M chunks on the free
  `summarizer` model. Watch load.
- **Tier-2 escalation (optional)** — `PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5`
  to push own-claim precision past 91% (~$200-400 on the residual). Was 429-blocked
  in dev; retry when free.
- **Enable the generic axis runner corpus-wide** — `workers/axis_pass.py` (built,
  ADR 0047 §3: prereq + applies_when enforced, per-axis `axis:<id>`
  `service_config` gate) has never been flipped on for any of the ~10 axes it
  now drives. `domain`/`studytype`/`property` need a stronger model than the
  free one clears the gate on; `material` + `transport` were the eval leaders
  (93% / 97%) — **but those numbers are now STALE**: the 2026-07-25 definition
  pass changed both vocabularies (`material` +metal/zeolite/2d-material,
  `transport` +unknown + a `PROPERTY:electrical|multi` gate), and `material`
  has **no gold rows** for the three new values. **Re-run
  `scripts/classify/eval-classifier --axis material,transport` and add gold
  rows for the new material values before trusting either for a corpus sweep.**
  - **Gold rows for the new/changed topics** — the 2026-07-25 topic pass added
    `nh3-synthesis`, `co2-conversion`, `catalyst-stability`, `ml-general`,
    `bayesian-statistics` and rescoped `llm`/`nanobuds`; `CLASSIFY_TOPICS_VERSION`
    bumped to 3 (lazy re-classify on next enable). No gold/eval exists for the
    topic cascade yet — spot-check Tier-1 precision on the new topics before a
    corpus-wide `PRECIS_CLASSIFY_TOPICS_ENABLED` sweep.
  - **Blocker before any *chunk-level* axis (`role`, `open-question`) sweeps:**
    add a per-axis failed-`chunk_claims`-lease reaper. On a chunk-level LLM
    failure `axis_pass.py` leaves the `axis:<id>-v<version>` lease in place, so
    the chunk isn't retried until the axis `version` is bumped (same gap as the
    `classify` cascade). Harmless while default-OFF; must land before enabling a
    chunk axis in prod. (ref-level axes have no lease and self-retry.)
  - **`open-question` on `memory` is a no-op** until the ref-level path for a
    `level: chunk` axis is built — the runner never matches a dream ref (no
    `ord>=0` paragraph chunk). Only `move` classifies dreams today. See the
    NOT-YET-IMPLEMENTED note in `data/axes/open-question.yaml`.
- **Better table detection (polish)** — the free Tier-0 `numeric_ratio` heuristic
  catches only 0.1%; a pipe/tab/repeated-token heuristic would recover the free
  furniture drop.

## 🏷️ `OPEN`-namespace teardown *(design, awaiting Reto's review)*

Design [`docs/design/open-namespace-teardown.md`](docs/design/open-namespace-teardown.md)
(recovered to main 2026-07-19 from a dangling commit; status: design). The
free-form `OPEN` tag namespace conflates three things (machine control
plane, ADR-0047 curated-axis staging, folksonomy) across ~45 prefixes, 52%
singletons. Not implemented — the doc is the full spec (three piles:
**MACHINE** — ~20 deterministic prefixes to migrate to real axes/columns;
**CONSOLIDATE** — `topic:`/`interest:` (~2000 rows) into a curated axis via
the ADR-0047 minting lifecycle; **DELETE** — junk prefixes) + a migration
table + the exact-match cull rule (`namespace='OPEN' AND value LIKE 'p:%'`,
never `namespace LIKE 'OPEN%'` — that eats the ADR-0047 `OPEN-QUESTION`
axis). Blocks the OA-acquisition roadmap's §G `referenced_works`→topics
wiring (above). *(design-review, open — owner: whoever reviews the doc's
open questions with Reto: `level:` axis-vs-column, `internal-thought`
dual-writer, `sticky:` fate.)*

## 🔵 `serverInfo.title` not set *(blocked upstream)*

*(polish — owner `src/precis/server.py:129`, test
`test_serverinfo_carries_title`).* MCP spec 2025-06-18 §A1 recommends a
`serverInfo.title`; `FastMCP(...)` takes no `title=` kwarg. One-line fix once
FastMCP accepts it — file the request when the next mcp-critic pass surfaces it.

## 🟠 LLM-confusion residuals (from prod plan_tick transcripts)

Root causes (tex workspace-authoring, addressing, merged-handle redirects,
embedder-warmup race, nanotrans_auto spin) all fixed + deployed; a
`plan-tick-spin` nursery detector was added. Parked (none a bounded fix):

- **Chunk-handle (`pc<id>`) of a merged paper doesn't redirect** *(design
  limitation).* `resolve_handle` follows `superseded_by` for record handles only;
  a merged paper's chunks are soft-deleted with different `chunk_id`s. A real fix
  needs a chunk-level supersede mapping at merge time — investigate before building.
- **`plan-tick-spin` detects but doesn't auto-pause** *(behavior extension).*
  Auto-pausing (an `open` tag the doable view excludes) would stop the burn but
  risks halting legitimate long-running planners — needs a progress-signal, not a
  count. Backlog.
- **Ops: cull orphaned tex refs from the nanotrans_auto spin** — dozens of
  duplicate `\section{…}` refs with `workspace=∅`. A one-off cleanup query.

## 🔵 Tool-friction reflection + dream diversification

Spec `docs/design/tool-friction-reflection-and-dreams.md`. Part A (end-of-run
tool-friction footer, `utils/friction_reflect.py`) + the Part B lens seed are
built default-OFF; lens seed rehomed to first-class oracle traditions (shipped).

- **Enable Part A in prod** *(open).* Flip `PRECIS_FRICTION_REFLECT=1` on the
  melchior agent worker *once a downstream grouping/dedup lane exists* to absorb
  `friction` gripes, else raw wishes pile up untriaged. Gauge junk-rate.
- **Gripe → agentlog link (Part A)** *(open).* Link each `friction` gripe to the
  run's 30-day `agentlog`; the filing agent doesn't know its own agentlog id at
  `put` time → needs post-hoc stitching (join by time+source) or an id threaded
  into the run context. (Stopgap: self-tags `friction-model:<model>`.)
- **Dream mode rotation (Part B)** *(open).* Rotate the cycle's *deliverable*
  (connection / library-gap / open-question / consolidation / analogy), not just
  the lens. Deferred: needs surgery on `dream-prompt.md` (connection shape is
  hardcoded into Step 6).
- **Active dreams (DFT / CAD / compute lanes)** *(deferred — wanted).* An
  `active-build` dream mode that kicks a derived-lane job (DFT relax, `cad_propose`,
  structure relax) on a surfaced subject, then connects the result back into a
  memory. Gate behind the load ceiling + a budget cap.


### Paper-dedup / hygiene residuals (ops-gated, not repo bugs)
- **Run Bucket B on prod** — `precis resolve-metadata` (dry-run) over the 94
  `needs-triage`, inspect auto/review/discard lanes, then `--apply`. Network-bound
  (Crossref/S2), on-cluster only. Expected ~20 DOI-track + ~40 title-track auto.
- **Standing worker for future id-less stubs** — build after the CLI proves the
  resolution on prod.
- **id-bearing stubs that title-match a held paper (49)** — deliberately NOT
  auto-merged; real merges need cross-id (S2) equivalence proof → review lane.

## 🔵 OQ-11 — verify FastMCP server-pinned-prompt support

*(polish, verification only; design ships either way).* Does MCP 2025-06-18 +
FastMCP 1.x let a server flag a `prompts/list` entry as "render at session
start", or is the tag client-side only? Read FastMCP `prompts/list` handler +
MCP §prompts. The answer decides whether we can drop the redundant banner line.
Owner `mcp_modalities.py::register_skill_prompts`; artefact
`docs/design/mcp-cold-start-token-budget.md`.

## 🔵 Small backlog asks

- **Stateless `time`/`date` handler** *(feature, open — owner
  `handlers/`).* No `time`/`date`/`clock` kind exists (`handlers/calc.py`
  is the only stateless kind today). Mirror `calc.py`'s shape (`KindSpec` +
  a `get` verb, no DB/embedder): `get(kind='time')` → now UTC+local,
  `get(kind='time', id=<ts>)` → parse/format/convert. `units`
  (conversions) and `regex` (test/match/extract) are sibling candidates,
  same template.
- **Per-tool-call ledger** *(feature, open — owner `runtime.py`).* Today's
  telemetry (`agentlog`, `ref_events`, job chunks, worker logs) has no
  per-tool-call row, so "which verb/kind/arg-shape confuses agents" isn't
  queryable. Proposed: a `tool_calls` table (sibling of `ref_events`/
  `alert`; numeric, not embedded — `call_id, ts, agentlog_id, source,
  verb, kind, arg_shape jsonb, outcome, error_type, result_count,
  latency_ms`) written from the verb-dispatch chokepoint in `runtime.py`.
  Feeds an `error-rate GROUP BY (verb,kind)` MCP-improvement backlog; a
  nursery friction-detector could auto-file a gripe past a threshold.
- **Universal short codes** *(design, deferred).* ADR 0032's base-62
  `chunk_id` encoding (manuscript-only, `5BL5`-style) hasn't been promoted
  beyond that one kind — no `base62` helper exists outside it. Verdict was
  additive-not-replacement (coexist with meaningful handles for top-level
  refs); prove on manuscript chunks first, promote in a later ADR only if
  it earns its keep.

## ⏸️ Snoozed — blocked upstream

- **Dependabot #56–#67 — `pillow` heap-OOB/DoS/decompression-bomb-bypass (12
  alerts, mostly high).** `Recheck-after: 2026-08-08`. `Unblock-when:`
  `marker-pdf` drops its `Pillow<11.0.0` cap. **Re-verified 2026-07-25:** still
  blocked — latest `marker-pdf` on PyPI is unchanged (`2.0.0`, still pins
  `pillow<11,>=10.1.0`), the fix is `pillow>=12.3.0`, so the constraint
  intersection is empty (no patched Pillow exists below 11); deployed cluster
  (`72fc227`) confirmed running `pillow 10.4.0`. `uv lock --upgrade-package
  pillow==12.3.0` fails resolution outright; same shape as #44/#45's
  `transformers` block, documented as a known ceiling in `pyproject.toml` lines
  95-102. Tolerable:
  precis only ever feeds Marker/Pillow trusted PDF ingestion behind the
  `[paper]` extra, none of the specific vectors (PSD/FITS/JPEG2000/TGA/mmap
  font-loading paths) are reachable from precis's own code. **Recheck:**
  re-run `uv lock --upgrade-package pillow`; if it reaches ≥12.3.0 take the
  fix; else bump `Recheck-after` +2 weeks. Cleared in the same pass: GitPython
  #71-74 → 3.1.55, pyasn1 #69/#70 → 0.6.4, setuptools #68 → 83.0.0 (main
  `ce531a4c`) — those were not blocked, just needed a lockfile bump.
  **2026-08-05:** same lockfile-only clearance for the next round —
  cryptography #80 (high, PKCS#7 Bleichenbacher) → 50.0.0, GitPython #77
  (high) / #78 / #79 (git arg-injection) → 3.1.58; both transitive, no cap,
  resolved cleanly via `uv lock --upgrade-package`. Pillow #56–67 remain the
  only still-blocked entry.

## 🔵 Paper-ingest `equation` chunk kind — retire later *(deferred)*

*(feature — owner `ingest/{marker,pipeline,literature}.py`).* Companion to the
done draft-side retirement. ~54.6k `equation` chunks are `kind='paper'` (99.5%),
minted by the Marker PDF path, rendered by the two-pane PDF reader (so the
"renders as raw `<p>`" motivation doesn't apply), and deliberately un-embedded
(`SKIP_EMBED_TYPES`). Migrating requires deciding the paper-equation **embed
policy first** (strip-to-placeholder? keep skipping? a `math`-marker paragraph the
embedder skips?), then change the Marker classification + batch-migrate the 54.6k
chunks (throttle the cascade). Until then the FK row stays alive.

## 🔵 CAD — spoked-wheel spokes don't bridge rim↔hub + no job-log link

*(feature — owner `cad/` geometry + `precis_web/routes/cad.py`; reported on
`/cad/make-a-spoked-wheel-with-a-mounting-bracket-v2`).*

1. **Spokes don't connect rim to hub.** The spoke op `spoke cyl:r2.5h28 polar
   n16 r26 z` centres spokes at r=26 spanning ±14, reaching neither the rim wall
   (~34–40, `torus:R40r6`) nor the hub (r12). A model-parameterisation problem —
   worth a spoke-radial-length lint / connectivity check fed back into the propose
   loop so a disconnected result is caught before it lands.
2. **No link to the failing job from the CAD page.** The page shows "answer
   failed — see the job log" (job r50911) but renders no link. Surface a link to
   the owning job when a propose/derive step fails.

## 🔵 OA acquisition + structured ingest + external search *(roadmap; little built)*

*(feature — owner `workers/fetch_oa.py`, `ingest/`, search/discovery).* Root
diagnosis: "it's OA but we don't have it" is publisher-side Cloudflare/Akamai
`403` (Wiley, bioRxiv, science.org, MDPI) — TLS/fingerprint/IP-reputation, **not**
a UA gate, so `_BROWSER_UA` is dead for this class. Prod nodes have open egress.

**Cascade design (revised 2026-07-08):** free legs first (publisher-deterministic
→ PMC-OA JATS → arXiv → Crossref/OpenAlex `oa_url`, all $0, version-of-record),
then **OpenAlex Content API** as the first *paid* fallback (~$0.01/file, gated by
`has_content`, from the fixed host `content.openalex.org` — kills the whole
Akamai/Cloudflare-403 class publisher-agnostically, verified vs ref 53423), ahead
of a paid web-unlocker proxy (last resort, ToS-grey, off by default; **never
Sci-Hub**). Prefer GROBID **TEI** for text/chunks when present, still store the
PDF for the reader + highlight coords.

**The 9-item roadmap (interdependent):**
1. **PMC OA / Europe PMC fetch leg** *(keystone).* DOI→PMCID → OA package
   (`.tar.gz`: JATS + figures + supplementary) or `oa_pdf`. Biomedical only —
   whiffs on MDPI/chemistry (hence #1b).
1b. **OpenAlex Content leg** *(co-keystone).* §B above — publisher-agnostic paid
   fallback; **built (unshipped)** as `_try_openalex_content`, double-gated
   `PRECIS_OPENALEX_CONTENT_KEY` + `_AUTO` (default OFF).
2. **bioRxiv/medRxiv S3 leg** — for `10.1101` preprints not in PMC (requester-pays);
   add preprint→VoR dedup.
3. **Paid web-unlocker proxy** — Cloudflare-only-OA not in PMC/S3; config-gated,
   off by default; CC-licensed only.
4. **Supplementary / methods ingestion** — the PMC OA `.tar.gz` bundles SI; design
   the storage shape (child refs `has-supplement` vs extra chunks).
5. **JATS/TEI structured ingest** — `extract_blocks_jats(xml, paper_id)` emitting
   Marker's block-dict shape reuses the whole downstream + `mathnorm`. Phase 1
   (new papers, prefer-XML, keep PDF) low-risk; Phase 2 (re-ingest existing PDF
   papers) is a **hazard** — citations anchor by string `source_handle="slug~ord"`,
   so a re-chunk restales them → must reanchor by `source_quote` text + snapshot at
   ref scope + add an `ingest_source` marker column; Phase 3 = stable per-chunk
   `handle` + citation-by-quote.
6. **Parallel scholarly-graph providers** — fan out `{OpenAlex, Crossref,
   OpenCitations, Europe PMC, Lens}` + RRF-fuse (robust to cross-lingual score
   gaps), dedup by DOI→title-fuzzy. OpenAlex/Crossref clients already exist. Lens
   adds paper↔patent linkage.
7. **Chinese-lit abstract discovery** — abstract-level via OpenAlex/Crossref +
   translation; **not** CNKI full-text scrape.
8. **Historical & foreign-language archive import** — bulk, scan-derived,
   identifier-less. Bulk fetcher (IA/HathiTrust/J-STAGE) + copyright-era gating
   (pre-~1930 PD = full; in-copyright = index/abstract-only) + specialized OCR
   (Fraktur/Cyrillic/CJK). **Pilot: German *Chemische Berichte* (1868–1997)** via
   IA + HathiTrust. Legit routes only; no Sci-Hub.
9. **Measure bge-m3 cn↔en placement for technical content** *(Reto's ask —
   measure, don't assume).* Probe the live embedder (`POST /embed`, port 8181)
   with N zh technical abstracts + English equivalents; report cross-lingual vs
   same-language cosine gap + top-k retrieval. RRF-per-language-pool mitigates the
   clustering bias.

**Bulk arm (§D — "set up for a big pass"):** a shared **bulk-ingest substrate**,
unified with the historical importer (#8). Money fact: OpenAlex free S3 snapshot =
**metadata only** (index/planner layer — mines *what*+priority); free bulk full
text = **S2ORC** (S2 Datasets API, keyed, no per-file charge — *priority-one
adapter*) + **CORE**; OpenAlex Content (paid) = gap-filler for the blocked residual.
`BulkSource` adapter roster (build order): `s2orc` → `core` → `oai_repositories`
(Zenodo/PMC-OA/arXiv/UoL via OAI-PMH) → `openalex_snapshot` (index-only) →
`internet_archive`/`hathitrust`/`jstage` → `east_view`. Reuse the #5
`extract_blocks_*` seam (skips Marker) + `dedup.py` + copyright gating.

**Embedding-prioritization (§E — OPEN, deliberately unsolved per Reto).** A bulk
pass dumps millions of NULL-embedding chunks; naive FIFO starves fresh on-demand
papers for weeks. Reto's instinct: "prioritize the things we already have stuff
on" — signals to weigh: referenced by todo/draft/project/citation (warm set),
recently viewed/flagged, `PRIO`/in-a-project, creation recency, lexical/keyword
adjacency. Mechanism sketch: an embed-priority ordering in the claim query; bulk
chunks stamped low-priority `meta.ingest_source='bulk'` that trickles behind live
traffic (like `llm_summarize`). Captured so the bulk pass doesn't ship without a
queue policy.

**§G OpenAlex free-metadata enrichment (wanted, built unshipped):**
`ingest/openalex_meta.py` (`fetch_openalex_work` + `normalize` + `enrich_ref`)
writes `meta.openalex` (abstract, topics, funders, fwci, 110 `referenced_works`
W-ids, ORCID+ROR authorships), registers `openalex:W…`, fills byline when empty;
CLI `precis enrich-openalex <doi|ref_id> [--backfill --limit N]`. Deferred within
G: `referenced_works` edge materialization (rides on #6; raw W-ids captured now);
topics→`ref_tags` (waits on OPEN-namespace teardown); wiring the backfill CLI into
a scheduled pass. **Verify on first real key:** OpenAlex Content auth is `?api_key=`.

**Also built unshipped:** `precis fetch-openalex <doi|ref_id>` (manual one-shot,
bypasses the auto gate); failure-reason surfacing (`/papers-needed` renders "fetch
failed: mdpi.com 403 — retry in 24h"). **NOT built:** the TEI structured path (#5),
the bulk arm (§D), the auto-leg budget cap for when AUTO is flipped on.

**Stub↔ingest dedup residuals (ops-gated):** multi-host inbox race writes spurious
`no such file` `error.txt` when watchers race the shared NFS inbox (the winner
ingests fine; recognize the wrapped file-vanished error in `cli/watch.py` + skip
silently); **187 titleless chunked papers** — `resolve-metadata` re-resolves by
DOI (32) or S2-title-search (≥0.85 gate) — run the dry-run over the cohort → gold-
check → `--apply`, then **schedule it** into `paper_reconcile` (manual-only today);
verify the 7 existing split orphans self-heal post-deploy.

**Markup-first ingest (separate feature, `ingest/markup.py`) — decide the
PDF-race before flipping the flag** *(design-review, open — owner
`workers/fetch_oa.py::_run_markup_cascade`/`_markup_fetch_enabled`).*
JATS/HTML/LaTeX-before-PDF+OCR ships dark behind `PRECIS_FETCH_MARKUP`
(still default-off). Per-stub, the markup pass runs first (best-effort,
swallows its own errors) then the PDF cascade runs unconditionally after —
the live-drop ordering between the two hasn't been decided (which body
wins when both succeed). Decide before enabling on any host.

## 🔊 LaTeX → speech for voice drafts

*(feature, open — owner `precis/draft/narrate.py`).* Voice-draft narration
`speakable()` currently skips math (a spoken "equation" cue, drops inline `$…$`) —
weak for math-heavy drafts. Add a `math_speech ∈ {skip, brief, full}` mode. v1
lean = a **pure-Python heuristic** (`^`→"to the power of", `\frac`→"over", greek,
operators); accessibility-grade = MathSpeak/ClearSpeak via the Speech Rule Engine
over MathML (`latex2mathml` is in hand; MathML→speech is a `node` shell-out);
per-equation author override (pronunciation-lexicon pattern). Default stays `brief`.

## 🟠 Architecture review / compaction / footguns

*(refactor, open — owner: multiple).* Cross-cutting; intentionally not one PR.
Security excluded.

**P0** — **Schema reconcile must preserve PostgreSQL ACLs** (`scripts/reconcile`,
`store/migrate.py`): `migra` diffs don't emit `GRANT`s, so new tables end up owned
by `deploy` with no `agent_rw`/`agent_ro` grants — add an ACL diff/re-grant step.

**P1 — compaction/modularization:**
- **Compact ADRs with a "Rest in Git" archive** (`docs/decisions/`). Convention
  established (ADR-0058 + `archive/` scaffold). Remaining (each its own reviewed
  change): supersede each major chain with one condensed live ADR + move
  predecessors to `archive/`. Chains: identifier (`0002/0006/0008`→`0036`),
  derived-queue (`0007/0017`→`0044`), image/embedder (`0004/0009/0012/0019`→
  `0020/0021`), figure/asset (`0034/0035`→`0057`), keystone kinds
  (`0041/0042/0043`→`0053/0056`), argument/turn-taking (`0051`↔`0054`).
- **Unify anchored-edit region resolution** — investigated as "extract
  `EditableFileHandler` from draft/plaintext/python/markdown/tex"; corrected
  premise after reading the code (2026-07-23): only `plaintext.py` and
  `python.py` define `_put_anchored` — `markdown`/`tex` already inherit
  `PlaintextHandler`'s for free, and `draft` has none (chunk-native model,
  not file-based — see ADR re: `tex_vs_draft_authoring`). The two real
  implementations diverge for most of their body (paragraph-block splicing
  vs. AST symbol-region splicing + qualname-drop/ruff gates); only the
  `find=`/`text=` front-matter validation (~45 lines each) was genuinely
  duplicated, and that's now factored into
  `plaintext._require_find_and_text` (imported by `python.py`). A real
  `EditableFileHandler` base class would mean designing a shared region-
  resolution abstraction across paragraph-blocks vs. AST-symbol-ranges —
  an actual core-abstraction call, not a mechanical extraction; needs an
  owner who can make that design call (Opus-tier), not a repeat of this
  bullet as scoped.
- **Split `store/_blocks_ops.py` + `_draft_ops.py`** by concern (SQL builders /
  rankers / card writers; `_draft_ops.py` has 72 functions).
- **Split `precis_web/routes/drafts.py`** (3078 lines) into per-concern modules.

**P2 — quality/discoverability:**
- **Centralize `PRECIS_` env vars** (`config.py`, `kind_gate.py`). 381 unique
  `PRECIS_` strings, `PrecisConfig` declares 19; replace ad-hoc `os.environ.get`
  with `requires_env`/`requires_secret`, then flip `PrecisConfig.extra` to `forbid`.
- **Tighten broad `except Exception`** (317 across 141 files; many hide spin loops).
- **Add headless-browser tests for the draft editor** (also above).
- **Smartdraft review-parity remainder** — the retired classic reader had a
  read-only per-block **F/C/S/A checker-flag strip** (mirroring `view='review'`:
  ✓ current / ~ stale / – unreviewed) and a **machine-authored** border marker
  for grounded-authoring-reviewer edits. Neither is ported to `/smartdraft`
  (which surfaces review state via the focus ✓ + the "Needs · in-flight" rail);
  the `chunk_review` ledger + `view='review'` are unchanged, so this is UI-only.
  Port into the smartdraft focus/TOC if the per-block lens view is still wanted.
  Also unported: the classic reader's `POST /drafts/{id}/around` bulk
  "expand around here into eyes over the reference ring" affordance
  (`draft_eyes.expand_around`, ADR 0051 §6) — retired with the page; smartdraft
  has single pin/unpin but no bulk-expand. `expand_around` is still in
  `draft_eyes.py` (only its route + UI went), so re-wiring it into smartdraft is
  a UI-only add if the working-set bulk-expand is still wanted.

**P3 — type/platform/debt:**
- **Fix Windows `O_DIRECTORY` + Python 3.12 urllib circular import** (also above).
- **Recheck `transformers>=5.3.0` / `marker-pdf` pin** (Dependabot #44, snoozed).
- **Re-evaluate `ruff` ignores `RUF012` + `B905`** (can hide real bugs).

## 🕸️ Graph completeness — still-open findings *(2026-07-23 audit)*

Link-blindness item shipped `885bd1ea`; ADR 0054 argument-graph shipped
`2d19290e`. The remaining findings stay open:

1. **`MemoryHandler.supersede()` never fires on near-duplicate memories**
   *(bug/wiring, open)*. Supersede machinery exists for papers/runs
   (`collapse_superseded_chains`), but memory-dedup isn't wired — prod
   `superseded_by` was **0** at audit; ~80% of live memories (~6.3k/7.9k) are
   `DREAM`-tagged synthetic insight with near-duplicate clusters it should
   collapse. Wire the dream/review pass to call `supersede`, or surface a
   "candidate duplicate" nudge. Re-confirm `superseded_by=0` before building.
2. **No isolated-memory nursery check** *(polish, open)*. ~10% of live
   memories (778/7,893 at audit) have zero links either direction — findable
   only by tag/text. Widen `autolink_mentions` adoption and/or add an
   "isolated memory" nursery finding.
3. **No two-ref intersection query** *(feature, open)*. Nothing answers "does
   ref A share links/tags with ref B" — `view='shared'`? a `compare` verb? The
   SQL (`INTERSECT` over each ref's link-target/tag-set) is trivial once the
   verb shape is picked.
4. **No aggregate/fan-in graph view** *(feature, open — biggest lift)*.
   `links_for` is one-ref/one-hop; no multi-hop or fan-in summary exists.

## 🖥️ Web quest editor *(feature, open)*

Create/reprioritize the hierarchical quest tree from `precis_web` (with
linting) — no dedicated quest route on main today. Owner `precis_web/routes/`
+ the quest layer.

## 📰 News: Reddit / Mastodon sources *(feature, partially open)*

The framework exists (`news_sources` registry + `news_poll` worker,
`handlers/news.py`). Reddit subreddits and Mastodon accounts expose public
`.rss` feeds → ingestible **with no credentials**, just `news_sources` rows.
A bespoke API client (a few accounts + summaries) is unbuilt and *would* need
API credentials — only build that if the RSS path proves insufficient.

## 🕸️ Graph-locality architecture — held *(design, needs a framing pass)*

`docs/design/graph-based.md` proposes conditioning one agent's admissible
tools/context on *where it is* in the quest/document/citation graph, instead
of today's zoo of per-`job_type` passes. **Held** (2026-07-23): before
prototyping, resolve which passes are "mechanical prep" (LLM as a
narrow/checkable retrieval utility) vs. "actual work" (judgment-laden
synthesis where graph-locality might change behavior) — that determines
whether the framing even applies to a given pass.

## 🛠️ Repo-dev Claude tooling — backlog

Tooling for developing precis-mcp (not the product). Bulk shipped (prose
convention, `docs/codebase.md`, `scripts/test --impacted`, `scripts/prod-psql`,
code search/index, `rtk`, navigator agent, guard hooks). Cross-session facts:
memory `repo_dev_claude_tooling.md`. Remaining:

- **Even-application follow-ups** *(refactor, open).* (1) **`state-map.md` stale**
  — factory Phase-1/2 commits shipped after its last edit; re-verify + add a
  `_Verified` stamp (it has none). (2) **136 product skills unaudited** for
  currency. (3) user-facing/runbooks/reference assumed-current, unverified. (4)
  **ADR status labels inconsistent** (case drift; several "proposed" ADRs are
  shipped). (5) **`email` worktree `0074`→`0075` renumber** before it ships.
- **Memory currency-auditor → own pip? 1-month check-in** *(feature, deferred
  — decide by 2026-08-19; owner `scripts/memory-lint`).* Shipped
  `scripts/memory-lint --currency`: treats each memory as falsifiable anchors
  (gone kebab branch/worktree naming unshipped work · repo path missing on main)
  and runs the exact git+fs oracle, so the once/day reconsolidation pass gets a
  suspect punch-list instead of re-reading every file (git+fs only — gripe-status
  / deployed-sha oracles need the prod MCP, stay in the judgment pass). Prior-art
  scan (`perplexity-research:164887`) found **no** open-source Claude-Code memory
  tool that verifies memories against repo ground truth — claude-mem (74.8k⭐),
  MCP `server-memory`, Mem0/Zep/Letta, memsearch all store/compress/retrieve, none
  audit; the repo-dev-toolkit half (worktree ship, doc-guardian orphan-docs, `rtk`
  itself, awesome-claude-code) is a crowded commodity. So the *only* novel slice is
  this auditor. **Decision to make ~2026-08-19:** after a month of our own use, is
  it worth extracting as a standalone pip/plugin (genericize oracles off precis
  coupling, own maintenance), or does it stay a repo-local script + a line in
  `docs/how-to-setup-like-this.md`? Prior is **transient at best** — the recipe
  doc is likely the right home; only extract if the month proves recurring value.
- **Repo-dev hooks — 2 deferred** *(feature, deferred — marginal).* The tier-1
  guards (PROD-write / sealed-migration / git-stash), the map-staleness extension
  (ADR + skill triggers + `migration-check` at write), the PreCompact
  persist-residuals reminder, and `session-size-nudge` (propose `/compact` at
  transcript-size tiers) all SHIPPED. Deferred as low-value / noise-risk, build
  only if the pain shows up: bare-`pytest`→`scripts/test` nudge;
  Stop-with-dirty-worktree reminder.
- **Mutation testing via `cosmic-ray`** *(polish, blocked-on-adoption — owner
  `pyproject.toml` + nightly).* `mutmut` is incompatible with our `-n auto`;
  `cosmic-ray` runs the test command as a subprocess so `pytest -n0` works. Scope
  to one pure-logic module (SSRF guard), nightly.
- **`subsystem-analyst` (opus) agent** *(feature, conditional — owner
  `.claude/agents/`).* A deep "how does the whole X work" synthesis subagent —
  build ONLY if the haiku `navigator` proves too shallow. Don't pre-build.
- **Test-suite setup tax — serialized per-worker template clones** *(polish,
  open — owner `tests/conftest.py::_initialise_test_db`).* Profiling
  (`--durations`) shows the suite is **setup-dominated**: ~340 s of fixture
  setup vs ~120 s of actual test-logic (7774 tests, ~100 s wall @ `-n6`). After
  the leak fix, the dominant remaining cost is the **6 per-worker `FILE_COPY`
  template clones, fully serialized under the session advisory lock** (the
  76/50/30/15 s "setup" tail — the last worker waits behind all prior clones).
  Options, none free: cap gate workers (fewer clones — already `-n6` not
  `-n auto`); shrink the template (lighter clone); or let clones proceed with
  less lock overlap. Real correctness/speed tradeoff — measure before touching.
  The per-test TRUNCATE base (~40 ms × ~3000 DB tests ≈ 128 s CPU / ~21 s wall)
  is the other aggregate; TRUNCATE is already the cheap isolation choice.
  No coverage is measured anywhere (no `pytest-cov`/`--cov`) — a separate gap.

---

_Last compacted 2026-07-18: removed all done/shipped entries (history in
`git log`), condensed open items. Prior detail is recoverable from git._

---
## 🔵 context-audit round-2 findings (2026-07-24) — agent-facing render bugs
Surfaced by `scripts/context-audit` (round-2, 27 real-prod contexts, sonnet judge).
Precisely root-caused; not yet fixed. See memory `context_quality_eval_build`.

- **Halted-todo reason dropped in attention view** · Status: open · Severity: feature
  · Owner: `src/precis/handlers/_todo_views.py::render_attention` (halted loop) —
  `_attention_halted` builds `h['reasons']` from `halt:<reason>` tags but the loop
  only prints id+title (the sibling child-failed loop shows its reason). · Test:
  attention render with a `halt:<reason>`-tagged leaf shows the reason inline.
- **Cross-kind / `view='keywords'` TOON tables drop the universal handle for
  numeric-ref kinds** (memory/orcid/gripe/finding/job/… render a bare integer) ·
  Status: open · Severity: feature · Owner: `src/precis/handlers/_numeric_ref.py::_body_search_hits`
  (missing `uhandle=`) + `src/precis/utils/search_merge.py` `_render_toon_table`/
  `_render_keywords_table` (bare `str(ref_id)` fallback; align to `hit.handle`) +
  `src/precis/utils/handle_registry.py` `CHUNK_CODES` (missing `"orcid"`). · Test:
  `search(kind='*')` + `view='keywords'` render `m<id>`/`oi<id>` not bare ints.
- **quest-frontier shows default `objective: energy (min)` for non-materials quests**
  · Status: open · Severity: polish · Owner: `src/precis/handlers/quest.py::_render_frontier`
  + `src/precis/quest/frontier.py` `DEFAULT_OBJECTIVES` — suppress/qualify the
  objective line when no candidates and `meta.rubric_objectives` unset.
- **`sort='recency'` source-search omits `N of K` total + per-kind breakdown** (present
  on plain cross-kind search) · Status: open · Severity: polish · Owner:
  `src/precis/runtime/search.py::_dispatch_source_search` — pass `total=`/per-kind to
  `merge_and_render`. · Test: `search(sort='recency')` headline shows `N of K` + per-kind.
- **`view='strategic'` has no scoping/pagination** (unconditional full dump; `doable`
  has `args={'under':N}`) · Status: deferred · Severity: polish · Owner:
  `src/precis/handlers/_todo_views.py::render_strategic`. Possibly intentional dashboard.
- Note: the quest_tick "materials-discovery frame forced onto every quest" finding
  (classifier gap — no pass tags a quest's domain) is already tracked by open gripe
  **gr170252**; not re-filed. The round-1 `precis-overview`/registry drift class
  appears already fixed (`skill-overview` footer matches the live kind roster).
