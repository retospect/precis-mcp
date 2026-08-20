---
status: draft
title: Taproot reground — one hub-improver that audits, prunes, re-discovers, escalates, and (last resort) retires+rewrites a claim
prio: high
model: opus
---

# Taproot reground — the one hub-improver

**DRY invariant: there is exactly ONE mechanism that improves an existing
claim hub, and it is `hub_refine`.** Reground is not a new worker; it is
`hub_refine.py` grown from *additive-only enrichment* into a full
*re-grounding* pass. Every capability below extends the existing
claim→discover→verify→write→stamp spine. Do not mint a parallel
`reground_claim` producer — that is the failure mode this item exists to
prevent. This spec superseded and absorbed the additive-only follow-ons
that used to live in `taproot-hub-refine.md` (contradicts edges,
grounding-depth policy — both folded into stages 2–3 below) and the
ref-level batch view that used to live in `taproot-grounding-none-fit-triage.md`
(prune spurious edges — folded in as the pilot dataset below); both files
are deleted, `git log` keeps them.

## Motivation / why

A claim hub can carry *proxy grounding*: an edge whose passage literally
utters the claim sentence but only as a review paragraph deferring to
uncited references — it asserts, it does not substantiate. Worked example
(fi192855, "carbon nanomaterials can be adjusted by doping/dedoping and
charge transfer"): the sole corroborator pc1556680 is an `ord 3` review
paragraph deferring to `[5-24]`, `chunk_citations` empty — a proxy. Yet the
corpus *does* hold the real primary (pa36266, extension p-doping of CNT FETs,
pc1154051: "drain current from 5.09 to 13.95 μA" via extension doping) — it
was simply ranked below un-retagged bibliography paragraphs and never
attached.

`hub_refine` today cannot fix this: it is **strictly additive** (pre-filters
already-attached sources at `hub_refine.py:701`, so it never re-judges or
removes an existing edge) and its verifier (`_verify_support_with_caveats`)
returns *yes* on a proxy because the sentence states the claim. The additive
invariant is load-bearing for convergence + bounded spend — so reground must
*extend* it deliberately, not discard it.

The goal is **solidly-supported facts, not defending outrageous claims.** A
hub with no primary support anywhere (corpus or external) should be marked
questionable, and — with authorization — retired/regenerated and its draft
text altered, rather than propped up on a proxy.

## In scope

One pass, invoked per-hub, with these stages (all on the `hub_refine` spine):

1. **Fisheye assemble.** For the hub: the claim text + claim embedding +
   every current grounding chunk (`meta.source_handle` → `src_chunk_id`) with
   its neighbours (prev/next `ord` in the same paper) — the "fisheye on all
   the pcs we point to, with neighbours."
2. **Audit existing edges (prune).** For each current supporter, an LLM judge
   answers *does this passage substantiate the claim with primary content, or
   merely assert/defer to references (proxy)?* — **strictly stricter than the
   minter's verifier.** Non-substantiating edges are **auto-removed** with a
   per-hub audit trail (`meta.reground_log`: edge, verdict, reason, sha).
   True contradictors are re-attached as `contradicts` edges (ADR 0073), not
   dropped. **Convergence guard:** an edge is re-judged at most once per
   `claim_sha` (memoed like `_META_LAST_REFINED_SHA`); a sha-reopen (edited
   claim) clears the memo. This preserves bounded spend — reground converges,
   it is not a periodic re-scan.
3. **Re-discover.** Existing two sources (corpus semantic ANN +
   citation-following), with **deeper top-k** — real primaries rank low while
   the `references` categorization is still converging, so recall floor must
   go deeper than enrichment used. Rank/attach via the same strict judge.
   **Grounding-depth policy** (fi189527): definition/existence claims accept
   abstract-only; measurement/mechanism claims require a body-passage
   primary.
4. **Re-sort.** No new state — seniority (`establishes` originators vs
   corroborators) and cite-key order are **read-time derived** (`.seniority`,
   `.cite`); attaching the real primary and dropping the proxy re-sorts the
   evidence view automatically on next read. "Mark questionable" is likewise
   **emergent**: a hub stripped to zero print-visible supporters reads
   `unverified` via `.trust` — no questionable flag to write.
5. **External last resort.** When the corpus cannot substantiate: mine the
   grounding papers' own reference lists for DOIs (resolve → hold), and query
   Perplexity + Semantic Scholar for primary support. Attach what verifies;
   leave the hub `unverified` (questionable) meanwhile. All agent-supplied
   URLs via `safe_fetch`.
6. **Retire / regenerate + surrounding-text repair (authorized escalation).**
   When no solid support exists anywhere and the claim is unsupportable as
   written: retire or regenerate the claim hub and **repair the citing draft
   prose**, because a hub is cited by an inline `[fi<id>]` marker — retiring
   without editing the sentence leaves an unsupported assertion + a dangling
   cite, so the prose edit *is* the retirement. Three per-paragraph modes,
   chosen by reading the whole paragraph (not just the sentence):
   (a) **reword-in-place** — narrow the sentence to the supportable claim,
   keep the cite, reground the hub; (b) **replace-with-a-supportable-fact** —
   keep the rhetorical slot, swap in a related solidly-groundable fact
   (this is the discover-insert path — expansion meeting maintenance);
   (c) **stitch-delete** — remove the sentence and rejoin neighbours for
   cohesion (last resort). Bias toward (a)/(b): the goal is *true and
   substantive*, not merely *not-wrong*. A **frontier model (opus) may apply
   these draft edits without per-edit approval** (Reto, 2026-08-12); the
   author reviews the whole diff at the end in the smartdraft reader, every
   edit reversible. **Scope to the target draft's own prose** — hubs also
   cited by other artifacts (briefs, other drafts) are flagged, never
   silently rewritten. Run this pass *after* evidence-regrounding so the
   RETIRE/reword set is complete and rewording regrounds in one motion.

**Rely on the converging `references` categorizer, not a bespoke filter.**
References chunks are never embedded, so they cannot appear in `mode='semantic'`;
the residual proxy-ranking noise is un-retagged `paragraph` bibliographies
that `bib_retag`/the classifier are still draining. Reground rides that
convergence; it must not hard-code its own bibliography filter.

**Dispatch glue.** A thin `reground_claim` job_type (params: hub id / claim
scope / draft scope), same shape as `taproot_backfill.py` — JobTypeSpec +
`_dispatch` reading `ctx.meta.params`, checkpointing done-hub-ids via
`ctx.set_meta`, per-hub `job_event`/`job_summary` chunks. `hub_ids` override
bypasses the due-set so a draft's whole hub set can be regrounded on demand.

**Applier must enforce add-first in code, not in a prompt.** The manual
123-hub pass over draft 173020 ran this as an applier-prompt instruction only;
under partial failure (a permission classifier blocked `link` adds but let
paired prunes through) two of four affected hubs (fi191307, fi192836) were
pruned to zero live evidence edges before being caught and restored by hand.
The `reground_claim` job_type must instead, deterministically:

1. Issue all adds first, and **read back** that each is actually committed
   (query `links`, don't trust the write call's return).
2. Issue a prune only for the subset whose replacement add is confirmed
   committed; an add that failed withholds its paired prune and flags the hub
   for review instead of silently skipping it.
3. After the transaction, **re-check `count(live edges) > 0`** for the hub
   and refuse/roll back if it would land at zero.
4. Surface partial-failure counts in the job result — a caller reading only
   the summary must still see that something was withheld.

The technique that found the original damage — an **intent-vs-committed
diff** (rebuild each hub's intended end state from its scout result, diff
against handles actually committed, apply the delta adds-first) — surfaced
residue no error string mentioned (10 missing adds + 8 stale edges in one
wave). The job_type should expose this diff as a first-class
verification/repair mode, not just a one-off recovery step.

## Explicitly NOT in scope

- **No second mechanism.** Not a new worker, not a parallel producer — every
  line lands in `hub_refine.py` / its job glue.
- **No new trust/questionable state.** Questionable = emergent `unverified`;
  do not add a flag or column.
- **No stored sort order.** Re-sort is read-time derivation, untouched.
- **No unbounded re-scan.** The sha-memo convergence guard is mandatory;
  reground must remain converging-by-construction.
- **No bespoke bibliography filter** duplicating the categorizer.
- **Retire/regenerate + draft rewrite runs only under explicit
  authorization**, never as an unattended default of the enable flag.

## Acceptance criteria

- fi192855 dry-run: proxy pc1556680 flagged non-substantiating; primary
  pc1154051 (pa36266) discovered + attached; log records both with reasons.
- Re-running an unchanged hub is a near-no-op (memo hit, no LLM re-spend) —
  convergence preserved.
- A hub stripped to zero supporters reads `unverified` with no schema change.
- Prune only removes edges the strict judge rejects *and* records each in
  `meta.reground_log`; a contradictor becomes a `contradicts` edge, not a
  deletion.
- `reground_claim` job over a draft scope regrounds every hub, checkpointed
  and resumable, isolating a single hub's failure.
- Retire/regenerate path is exercised only with the authorization gate set
  and every draft-text change is logged + reversible.

## Target + blast radius

- `src/precis/workers/hub_refine.py` — audit/prune stage, strict judge,
  deeper top-k, external escalation, retire/regenerate; the `attached`
  pre-filter (line ~701) becomes memo-gated re-verify.
- `src/precis/workers/job_types/reground_claim.py` — new thin job glue.
- `src/precis/taproot/hub.py` — an edge-removal door (attach has one; removal
  must log to `meta.reground_log`).
- Reads only: `.trust`, `.seniority`, `.cite` (emergent questionable/re-sort).
- External: Perplexity + S2 kinds, `paper_bib_entries` DOI mining, all via
  `safe_fetch`.
- Draft handler (retire/regenerate stage only) — draft-text edit path.
- Flag: `hub_refine` is its own service — `precis service prio <host>
  hub_refine <n>` (`_should_register` in `cli/worker.py` never reads
  `enable_env`; the env var is dead, see `taproot.md`'s decisions log). The
  destructive retire/regenerate sub-stage stays behind its own explicit gate,
  separate from the enrichment flip.

## Pilot dataset: the 292 ref-level none-fit edges

Precedes the 173020 hub-level pilot below; same audit/prune stage, ref-level
grain. The semantic backfill took grounding 22%→63%; the remaining ref-level
edges split three ways: (a) **292 "none-fit"** — a mix of genuinely spurious
edges (remove, don't ground), real-but-diffuse whole-paper support, and
retrieval misses; needs top-10 retrieval + full-claim embedding + an explicit
"is this edge spurious at all?" judgment before any removal (candidate set
regenerable via the pgvector LATERAL query); (b) **67 papers** have no
body-chunk embeddings — reground after `embed:bge-m3` catches up; (c) **9**
low-confidence (<0.5) groundings deliberately held. Semantic rows are tagged
`meta.src_grounding.method='semantic_backfill'` — reversible as a set.

## Pilot findings (2026-08-12, 10 hubs of draft 173020)

A 10-hub agent pilot (fi192855 by hand + fi192860, fi189549, fi191323,
fi191296, fi191281, fi191293, fi191262, fi191322, fi190987, fi191169 via
read-only scouts, writes applied through the session MCP) established the
dominant failure mode and de-risked the mechanism:

- **"Right paper, wrong chunk" is the dominant failure, not "wrong paper."**
  5 of 6 proxy prunes were same-paper depth corrections — the hub was
  grounded on the paper's **abstract / title-byline / front-matter / cover
  page**, while the real primary (measured values, Fig. caption enumerating
  the result, DFT body passage) sat deeper in the *same already-linked
  paper*, un-dereferenced. This makes most reground actions LOW-RISK: the
  paper was already vetted; reground just moves the grounding pointer to the
  data. Only fi191169 was a true wrong-paper swap; only fi192860 was
  genuinely unsupportable.
- **Upstream signal:** the minter over-grounds on abstracts/front-matter.
  Worth a separate intake to make the canonicalizer prefer body passages for
  measurement/mechanism claims (ties to the grounding-depth policy).
- **Single-source is common and legitimate:** many narrow computed-value
  claims have exactly one corpus paper (fi191262, fi191322, fi191281) — the
  scout correctly returned KEEP / depth-upgrade, not an external escalation.
  External-last-resort fired on none of the 10.
- **RETIRE path exercised once (fi192860):** vacuous compound capability
  claim, no corpus primary fits it as worded — independently reconfirmed an
  already-open todo (td204876) and flagged inbound draft cites (a morning
  brief, dc2445854) that must be repointed before retire/reword. Held for
  human sign-off, as designed.
- **Net:** 13 primary adds + 6 proxy prunes; 9/10 now solidly grounded on
  primaries, 1/10 correctly demoted to `unverified` (questionable). Two items
  held for sign-off (fi192860 retire/reword; fi191169 "major OEMs" →
  "touch-module manufacturers" wording overshoot). One data nit: fi191322's
  link `source_handle` is stale (chunk handle NULL; resolves by ref_id+ord).

Implication for the 123-hub run and the build: prune should be **confident
proxy-only** (the conservative judge), and the highest-yield, safest action
is same-paper depth re-grounding — the mechanism should treat that as its
primary move, with cross-paper discovery and external escalation as the
rarer tail.

## Open questions / decisions log

- **DECIDED (Reto, authorized):** auto-remove proxy edges with a log; corpus
  → external → retire/regenerate escalation ladder; draft text may be altered
  to reach a solidly-supportable statement; writes to the claim hub are
  authorized. Goal is solid facts, not defending a weak claim.
- **DECIDED (Reto, 2026-08-12):** a frontier model (opus) may apply the
  draft-prose edits (reword-in-place / replace-with-fact / stitch-delete) for
  retire/reword hubs **without per-edit approval**; the author reviews the
  full diff at the end in the smartdraft reader. Still scoped to the target
  draft's own prose; other-artifact citers are flagged, not auto-edited.
- **Gate for retire/regenerate + draft rewrite:** own env flag distinct from
  the enrichment flag, or a per-hub authorization tag? (Leaning: distinct
  flag for the sweep + a `TAPROOT:reground-ok` opt-in tag for unattended
  runs, so the destructive stage never fires on a hub nobody vetted.)
- **Interim agent run vs job_type:** run now from the session via bounded
  agents (one hub per agent, capped concurrency) for expediency on draft
  173020; the job_type is the durable form. Keep any agent run bounded — an
  unbounded taproot pass monopolizes the serial `claude_inproc` lane.
- **Strict-judge rubric** must be eval-gated before the prune stage enables
  in prod — **live blocker, re-run `slice_refine_eval` on the deployed v2
  rubric before any prod enable**: hub 176363 must drop its contradicting
  partials, 176272/176360 must keep theirs; over-prune is the dangerous
  direction, mirroring canon's zero-false-`same`.
- **Conflict-safe `taproot_rejected` memo write** carried over from
  hub-refine's follow-ons, unresolved: defence-in-depth against a lost-update
  when two passes touch one hub's `meta` concurrently.
- **v2 notes** carried over from the hub-refine build ticket, unresolved:
  `TAPROOT:saturated` long-backoff after K empty passes; paper-version memo
  invalidation; a queryable `taproot_evidence_judgment` table if judgment
  analytics are ever wanted.

## Running it with agents (interim runbook, draft 173020)

How an Opus regrounds the remaining hubs **now**, before the durable
`reground_claim` job_type exists. The orchestrating Opus **never judges a hub
itself** — it fans scouts out and applies their proposals; that is the cost
lever (self-done work bills Opus). Packaged as a Workflow script at
`…/workflows/scripts/reground-draft-173020-wf_3f14fecb-54d.js`.

**The unit of work (one hub).** For a `finding` hub `fi<N>`: (1) fisheye —
claim text + current evidence edges (`get(kind='finding', id=N,
view='evidence')`), each edge's grounding chunk (`meta.source_handle`, e.g.
`pc1154051`) **plus prev/next neighbours** in the same paper; (2) strict-judge
each edge — PRIMARY content → KEEP, asserts/defers/review/abstract-for-a-
measurement/title/byline/cover/bibliography → PRUNE, primary-against →
CONTRADICTS, **default KEEP on uncertainty**; (3) discover better primaries
with a *short 3–6 word* semantic query, **preferring the same-paper deeper
body chunk** (empirically the #1 fix is "right paper, wrong chunk"); (4)
verdict SUPPORTABLE / NEEDS_EXTERNAL / RETIRE (+ one-sentence groundable
`reword`).

**Two-stage split (why it is agent-safe).**
- **Scout** — one *read-only* agent per hub (`general-purpose`, sonnet),
  emits a structured proposal (`SCOUT_SCHEMA`). Forbidden from writing: no
  put/edit/link/delete/tag, no file writes, no sub-agents.
- **Apply** — a *mechanical* agent (`general-purpose`, effort:low), executes
  exactly the scout's prune+add list via `mcp__precis__link`, no judgment.
- Between them **deterministic JS, not an agent**, enforces guardrails:
  `RETIRE` → held, never written; would-strand (`adds==0 && prunes >=
  n_current_edges`) → prunes held; no-op → skipped. This is what stops a
  fleet from ever leaving a hub at zero edges.

**1 — enumerate the target hubs (read-only):**
```
scripts/prod-psql "SELECT f.id FROM refs f
  WHERE f.kind='finding' AND f.deleted_at IS NULL
    AND f.tags @> ARRAY['TAPROOT:claim','STATUS:canonical']
    AND EXISTS (SELECT 1 FROM links l WHERE l.dst_ref_id=f.id)
    AND f.id = ANY(<hubs cited in draft 173020>)
    AND f.id NOT IN (<the 11 already done>)
  ORDER BY f.id"
```
Done set to subtract: fi192855, fi191293, fi189549, fi191281, fi191296,
fi190987, fi191323, fi191169, fi192860, fi191262, fi191322.

**2 — launch (args = a REAL JSON array):**
```
Workflow({ scriptPath: "…/reground-draft-173020-wf_3f14fecb-54d.js",
           args: [191xxx, 191yyy, …] })
```
Gotcha that already bit us once: a stringified `"[...]"` reaches the script
as one string and `pipeline()` throws `expects an array`. The script guards
with `Array.isArray(args) ? args : JSON.parse(args)`, but pass a real array.
Concurrency auto-caps (~10 live); `pipeline()` streams each hub scout→apply
with no barrier. Run in waves of ~30–40 to eyeball each batch, or all at once.

**3 — applier contract (verbatim guardrail): ADD FIRST, then PRUNE.**
- add: `link(kind='finding', id=H, rel='corroborates', target='<handle>')`
- prune: `link(kind='finding', id=H, mode='remove', rel='corroborates',
  target='<handle>')`
- contradicts: remove the corroborates edge, then re-add `rel='contradicts'`.
On a bad handle: record the error, continue. `links` has **no `deleted_at`
column** — `mode='remove'` is a hard delete; a `deleted_at IS NULL` filter on
`links` errors, not just under-returns.

**4 — read the result.** Workflow returns `{summary, held}`. `summary`:
total / applied / noop / retire_held / would_strand / reword_flagged /
needs_external / scout_nulls / **total_adds** / **total_prunes** / errors[].
`held` is the worklist for the prose pass — every RETIRE, NEEDS_EXTERNAL,
would-strand, or reword hub with its claim + proposed reword.

**5 — verify independently of the MCP.** A `link` add can wedge the session
MCP in-process on the post-add Crossref retraction cascade even though the
write commits — so confirm committed state read-only:
```
scripts/prod-psql "SELECT dst_ref_id AS hub, count(*) edges
  FROM links WHERE dst_ref_id = ANY(<ids>)
    AND rel IN ('establishes','corroborates','contradicts')
  GROUP BY 1 ORDER BY 1"
```
Any hub at 0 edges that was **not** a deliberate RETIRE is a bug — stop and
investigate.

**6 — hand off to the prose pass.** `held` is the input to the opus
draft-rewrite pass (reword-in-place / replace-with-fact / stitch-delete;
reground on reword; auto-applied per the standing liberty; scoped to draft
173020's own prose; flag other-artifact citers; full before→after diff for
end-review).

## Open residual: draft 173020's 123-hub run, `fi189542`

The manual pass (evidence + prose + hub-claim, 123 hubs: 108 edge adds, 36
prunes, 24 draft rewrites, 2 retitles) is otherwise complete. One hub is
still open: `fi189542` ("opening angles … 85° …") and its sibling `fi189543`
both hung off one proxy edge (ref 783 @ pc64732, deferring to a corrupted
bibliography entry — see `citation-matcher-title-mismatch.md`, read that
first before chasing either primary). `fi189543` is resolved — regrounded
onto `pc972025` (ref 5828), quantifier "most abundant" dropped since its only
support was the pruned proxy. `fi189542` carries a `contradicts` edge
(`pc972022`, ref 5828 — its formula predicts 83.6°, not 85°) rather than a
unilateral number fix, since the hub faithfully reports its source's error.
Remaining:

1. Acquire Krishnan et al. 1997 (`10.1038/41284`, paywalled) —
   `put(kind='paper', doi='10.1038/41284')` — to adjudicate 85° vs 83.6°.
2. Once held, either correct `fi189542` from the primary or let the
   `contradicts` edge stand as the honest record.
3. Optional: Iijima et al. 1999 (`10.1016/S0009-2614(99)00642-9`) if the
   abundance question is ever reopened — no longer load-bearing since
   `fi189543` doesn't assert abundance.
