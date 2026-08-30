---
status: in-progress
title: bring the 139 claim hubs behind the nanobud draft above board
prio: high
---

# Nanobud claim remediation (dr173020)

In-flight. Full phase plan + rationale:
`~/.claude/plans/greedy-gliding-anchor.md`. This file is the durable
resume pointer — the measured state and the decisions already made.

**Target is `dr173020`** ("WC:…", 180 body chunks, 139 claim hubs).
**Not `dr43020`** — that one has *zero* claim hubs (its 100 cites are
paper-level) despite a later `updated_at`; the taproot work is all on
173020.

## Measured state (2026-08-29, prod)

139 claim hubs cited; **108 (78%) clean on both axes**, 31 are not:

| problem | n |
|---|---|
| fail the approve-time blocking lint | 18 |
| live `contradicts` edge | 4 |
| zero verdicts (15 withheld-only, **2 with no evidence edges at all**) | 17 |
| overlap lint ∩ evidence | 8 |

**Those counts are raw query output; the sections below revise what they
mean.** Investigation on the same day found the 4 `contradicts` edges
contain no evidence conflict at all, and 1 of the 2 "no evidence" hubs is
a compound whose atoms *are* corroborated. Read the tallies as "rows the
query flagged", not "claims that are wrong".

Zero refuted. All 3 `signed` hubs in the whole corpus are nanobud hubs
and all 3 are clean. The draft also touches 42 non-hub chase findings —
no posture, no gate, invisible to every check above.

The 18 lint-failers:

```
189535 189536 189542 189543 190987 191014 191169 191260 191307
191318 192836 192855 211522 269443 269509 269510 269543 269548
```

(fi189542 is also disputed, so `reword.py::_COHORT_SQL` excludes it —
expect 17 to actually process.)

The 2 with no evidence edges at all: **fi211522** and **fi191014** —
both resolved under *Phase 3* below. Neither is the superlative-to-cut
this table originally implied.

## The ordering constraint

**Reword before verify.** `taproot/hub.py::refine_claim_sentence` never
touches `links.meta`; `nanopub/preflight.py::withheld_edges` compares
`meta.verified_claim_sha` against `claim_sha(live refs.title)` at *read*
time and withholds on mismatch. So rewording a hub stales every verdict
it already had. Verify-first pays for the same LLM work twice.

## The 4 disputed — none is an evidence conflict

Investigated 2026-08-29. **All four dissolve on inspection**; the
original read ("only fi189542 is real") was wrong.

Three are contradicted by **review findings against the draft's own
prose**, not by opposing evidence — their titles literally begin
`dc<chunk>: fi<hub>`:

- fi191315 &larr; fi255164 *"citation overstates beam-damage artifact as
  'engineering'"* (chunk dc2445944). Source attributes the transformation
  to beam-induced energy input, and the fullerene collapses entirely.
- fi191316 &larr; fi192706 *"claim-strength inflation — 'will ultimately
  require' vs source's 'could be used'"* (dc2445944).
- fi191329 &larr; fi255165 *"doesn't cover 'two distinct methods / CO
  disproportionation' clause"* (dc2445957). The clause **is** supported —
  by pc209495 in the same paper, currently uncited. Attach, don't cut.

Those are text fixes. See
`docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` for why
fixing the prose will *not* clear the hub's `disputed` posture on its own.

### fi189542's contradicts edge is simply wrong

Not a human judgement call after all — the two passages **agree**.

- Hub: nanocone opening angles "approximately 19°, 39°, 60°, 85°, 113°",
  corroborated by pc64732 (`sin(θ/2) = 1 − P/6`).
- Supposed contradictor pc972022 (pa5828) states `θ = 2·arcsin(1 − n/6)`.
  Evaluating n = 1…5 gives **112.9°, 83.6°, 60°, 38.9°, 19.2°** — the same
  discrete set, same relation, rearranged.

The edge (link_id 999192, `set_by='agent'`, 2026-08-14) carries meta
`{"source_handle": "pc972022"}` — **no recorded rationale**. Nothing
justifies it and the arithmetic refutes it.

Action: delete link_id 999192 (or re-point it as `corroborates`). This is
a prod mutation and needs the user. Deleting it also drops fi189542 from
the disputed cohort, so `reword-sweep` will then accept it.

## Phase 3 — the two "no evidence" hubs, resolved

**fi211522 is not an evidence gap.** It is a *compound* hub; all three
conjunct atoms (fi211519/20/21) are corroborated by pc42017 (Lee et al.
2008). Posture simply doesn't roll up — filed as
`docs/backlog/compound-hub-posture-ignores-conjunct-evidence.md`. Its
real defect is the malformed title: unclosed paren, no terminal period.
Reword only.

**fi191014 is a genuine gap, and the number looks wrong.** Claim: "The
original aerosol CVD process yields a broad distribution spanning
0.7–2 nm" (scope: fullerene size). It has zero evidence edges — its only
link is the draft's own `cites`. Searching the held corpus for the
fullerene-size distribution turns up the opposite shape:

- pc209507 (Nasibulin 2007, MALDI-TOF, *the* original aerosol CVD paper):
  main peaks are **C₆₀ and C₄₂** derivatives.
- pc110255 / pc110256 (pa1209, HR-TEM statistics): majority are C₄₂ and
  C₆₀ derivatives; C₆₀-sized buds most abundant at ~25%, with significant
  *smaller* populations.
- pc258863 fixes the scale: C₆₀ diameter = **0.72 nm**.

So 0.7 nm is the *modal* size, not a lower bound, and nothing in the
corpus reports 2 nm fullerenes — that upper figure looks like a
conflation with SWNT diameter (pc258863 gives SWNT distributions peaked
at 0.9 and 1.3 nm). Do **not** simply attach a supporter: the sentence
asserts a range the evidence contradicts. Correct the claim to the
C₄₂–C₆₀ distribution and attach pc209507, or cut the parenthetical from
dc2445891.

## Phase 6 recon — read the prose before editing it

Two surprises on reading the cited chunks verbatim (2026-08-29):

**fi191316's prose defect is already fixed.** fi192706 quotes the draft
as "will ultimately require"; dc2445944 now reads "**may** ultimately
require". Someone already softened it. Only the stale `disputed` posture
remains — exactly the failure mode
`contradicts-conflates-evidence-and-prose-misuse.md` describes. Do not
re-edit this sentence.

**The draft contradicts itself on the fullerene size range.** dc2445944
says "a broad **0.4–2 nm** size distribution [dc2445891]"; dc2445891
itself says "roughly **0.7–2 nm** [fi191014]". Neither figure is
supported (see Phase 3). One prose fix must settle both sites — and
dc2445944 cites a *draft chunk*, not a hub, so no claim gate ever looked
at it.

Still genuinely open in the prose:

- dc2445944 sentence 1 — the fi191315 beam-damage overstatement. Real,
  unfixed. Suggested: "…observed that electron-beam irradiation can
  transform an attached fullerene into a tube-like intermediate before it
  collapses, indicating nanobud geometry is not fixed after synthesis" —
  drops "engineered", names the mechanism, keeps the finding.
- dc2445957 — the fi191329 compound sentence. The uncovered clause is
  **supported** by pc209495 (same paper, uncited). Preferred fix is to
  mint/attach a hub for it and cite alongside, not to cut the clause.

## Decisions already taken

- Uncited-assertion hunt: **full adversarial pass** over the draft body
  (`quest/review_fanout.py::_FANOUT_ONLY_BRIEFS['adversarial']`, opus).
  No mechanical detector exists — every hygiene check is token-anchored
  and only inspects citations already present.
- A claim with no hub *and* no supporting passage in the held corpus:
  **soften or cut the prose**, don't go acquire. Report every cut.
- Reach `refine_claim_sentence` only through `reword-sweep` — the freeze
  guard lives in the cohort SQL, and the manual retitle door
  (`edit(kind='finding', title=…)`) has **no** freeze check at all.
- Attach evidence via MCP `put(kind='finding', supporters=[…])`, not
  `direct-mint --apply` — see
  `docs/backlog/direct-mint-apply-rerolls-the-reviewed-sentence.md`.

## Done 2026-08-30 (prod)

- **Deleted link_id 999192** (user-approved) — the unjustified
  `pc972022 --contradicts--> fi189542` edge. fi189542 now reads clean:
  corroborated by pc64732, no dispute. Disputed cohort 4 → 3.
- **dc2445891** — replaced the unsupported "0.7–2 nm [fi191014]" with the
  C₄₂/C₆₀/C₂₀ distribution, re-cited to **fi269510** (already corroborated
  by pc209498, same Nasibulin 2007 paper). *fi191014 was a duplicate of a
  claim the corpus already states correctly and with evidence* — that is
  why no reword or new supporter was warranted.
- **dc2445944** — "0.4–2 nm" → the same C₂₀–C₆₀ range, killing the draft's
  internal contradiction; and rewrote the fi191315 sentence to name the
  beam-induced mechanism and drop "engineered" (fi255164's fix).
- **fi272040 minted** — "Aerosol synthesis experiments show that NanoBuds
  can be selectively produced by two distinct one-step continuous
  methods…", grounded in pc209495. Sentence pre-validated against
  `lint_claim_sentence` + `lint_notation` locally: **zero blocking
  codes**. **dc2445957** now cites [fi191329] and [fi272040] separately,
  closing fi255165.

### Residual: fi191014 is now orphaned

It has **no links at all** — no evidence, no citation. It asserts a range
(0.7–2 nm) the corpus contradicts, and fi269510 supersedes it. It should
be retired; that is an unapproved prod write, so it is left standing.

### Lint-validation shortcut worth reusing

`lint_claim_sentence` / `lint_notation` are pure regex and import fine
locally — `uv run python -c` against a candidate sentence gives the exact
blocking set with no LLM and no prod round-trip. Note `_BLOCKING_LINT_CODES`
holds bare codes while the linters return `code: description`, so split on
`:` before intersecting or the check silently passes everything. Useful
tokens are narrow: `EPISTEMIC_MODE_TOKENS` has 66 entries and includes no
synthesis/CVD term, so a synthesis claim needs `experiments`/`analysis`/
`imaging` to satisfy `no-epistemic-mode`. `past-tense` is advisory;
`past-passive` is blocking.

## Blocker — `claude` on melchior is logged out

The 18-hub `reword-sweep --dry-run` **ran** on 2026-08-30 (the classifier
let it through once the user asked for it directly). The cohort logic was
confirmed: 17 hubs processed, fi189542 excluded as predicted. But **every
one returned `status: "llm-failed"`**, zero rewords proposed:

```
rung 0 (cloud, claude-haiku-4-5) failed: claude -p exited 1 ... "Not logged in - Please run /login"
rung 1 (cloud, claude-haiku-4-5) failed: claude -p (agent) exited 1 ... (terminal_reason=api_error)
taproot reword-sweep: 1 hub(s) processed -- llm-failed=1, applied=0
```

Both configured rungs are `claude -p`, so there is **no non-Claude
fallback** — the whole sweep fails closed. This is auto-memory
`live-model-tests-need-host-claude` biting the reword path.

**It is NOT a bad or expired API key** (checked 2026-08-30):

- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`
  are **unset** on melchior, and neither `com.precis.worker` nor
  `com.precis.web` sets any Anthropic/OpenRouter key in
  `EnvironmentVariables` — so there is no long-lived key to have gone bad.
  Auth is OAuth, and on macOS Claude Code keeps it in the **login
  keychain**.
- In a non-interactive SSH session `security list-keychains` returns only
  `/Library/Keychains/System.keychain` — the login keychain is never
  unlocked, so `claude` cannot see its credentials and says "Not logged
  in". `~/.claude/.credentials.json` does not exist (that is the
  Linux/file-backed path, not macOS).
- The credential itself is **fine**: `llm_call_log` shows **1,586
  successful `claude_p` calls in the last 24 h** (plus 137,951
  `openai_compat`, 36 `claude_agent`). MEDIUM tier is 439/439 successful
  over 3 days, zero errors.

So the failure is scoped to *headless SSH invocation*, not to the account.
`source='taproot:reword'` has **zero** rows in 3 days — the failed calls
never reached the log, so this outage is invisible to the usage ledger.

Unblock, in preference order:

1. **Give MEDIUM a non-Claude rung.** `_default_chain(MEDIUM)` is two
   `claude_p` rungs, so a host without keychain access fails closed with
   no degradation. `live_config.chain_override` /
   `llm.chain.medium` already supports an `openai_compat` rung, and
   `PRECIS_LLM_BASE_URL` is configured on every node. Read
   `router.py`'s tool-filter warning first — that override is what sent
   agentic planner ticks to a tool-less wire for days. Reword is a
   one-shot JSON call with no tools, so it is a safe consumer.
2. **Run the sweep where `claude` is authenticated** — a GUI-attached
   session on melchior, or dispatch it as a worker job (the daemons
   evidently have a working context). Plain `ssh melchior …` will
   **not** work, including for a human: an interactive SSH login does
   not unlock the macOS login keychain either.
3. **Run it from reto's workstation against the prod DSN.** `claude -p`
   succeeds there and pgbouncer `100.126.127.107:6432` is directly
   reachable. Blocked for the agent only because the worktree guard
   refuses the nested `ssh …` command substitution needed to fetch the
   DSN, and there is no local `~/.pgpass` entry for `precis_prod`.

Open question: whether those 1,586 `claude_p` successes ran on melchior
or on another node — if melchior's own daemons are succeeding as user
`deploy` without a GUI session, there is a working headless path worth
copying.

The dry run was not wasted: it returned all 17 `old` sentences with exact
lint codes. Dominant failures are `no-evidence-verb` + `no-epistemic-mode`
(the sentences state a result without naming how it was established).
Several are trivially hand-fixable (`no-terminal-period` on fi190987,
fi191260, fi211522; `hyphen-numeric-range` on fi191307).
