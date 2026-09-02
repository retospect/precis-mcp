# token-review: bash-reflex-nudge misses real traffic; compact-thrash re-reads

Transcript sample (6 largest sessions) shows the nudges don't move behavior:
(a) Rule A matches only bare-identifier greps while real traffic is
multi-pattern tree-wide greps — add an exploratory-grep rule nudging
search_code/navigator, rate-limited per session; (b) Rule B fires per call
but never escalates — count per session, escalate after ~5, and wrap the
retyped DSN-extraction boilerplate as a helper (`scripts/agent-dsn`?);
`_SSH_RE` misses spark/castor/pollux; (c) post-compact sessions re-Read the
same governing doc — extend the PreCompact nudge to ask for a state-so-far
note naming the doc + line ranges. Owner `scripts/hooks/bash-reflex-nudge.py`,
`scripts/hooks/precompact-persist.sh`. Two further hooks stay deferred unless
pain shows: bare-pytest nudge; Stop-with-dirty-worktree reminder.

2026-08-15 pass adds (d) **Rule D**: `sed -n '<N>,<M>p' <local-file>` used
instead of Read `offset`/`limit` (36–63 calls/session, ~72K tokens of
tool_result across just two sessions) and `echo "=== label ==="` narration
wrapping compound Bash probes (308/420 Bash calls in the worst session, 73%) —
both named in CLAUDE.md but outside Rules A–C and outside rtk's rewrite list
(`sed` isn't a known command, so the reflex is invisible to compression AND
nudging). Nudge `sed -n` range-reads on existing local paths toward
`Read(offset/limit)`, and flag any Bash command containing a literal
`echo "===` independently (cheaper detection, no path resolution).
Concentrated in primary-checkout orchestration sessions, not worktree feature
sessions. The 07-29 fixes are holding (cluster-ops delegation + coderef nudge
confirmed working in the same sample).

2026-08-23 pass adds two more, from the 6 largest sessions since 08-15
(19.5MB–5.3MB; `echo "==="` narration is now essentially gone — 0/6 files —
so that half of Rule D shipped; `sed -n` is not fixed, still 2–76 calls/file):
(e) **Rule E**: raw `Bash tail -N .../tasks/<id>.output` polling for
background-job status instead of the `Monitor` tool, often bundled with a
`git log`/`git rev-parse` probe in the same call — ~111 raw polls vs. 9
`Monitor` calls across the 6-file sample (3 of 6 files used `Monitor` zero
times). Nudge repeated `tail .../tasks/*.output` on the same path within a
session toward `Monitor`. (f) **Rule F**: session `b30f9d07` (web-basic-auth
feature build) ran almost entirely un-delegated — 366 raw `Bash`, 104 `Edit`,
32 `Write`, **zero** `coder` dispatches for standard multi-file feature work
(`auth.py` edited 22×, `users.py` 16×, `test_auth.py` 13×), plus 34 raw
`ssh melchior` calls with zero `cluster-ops`/`cluster-admin` dispatch (Rule B
confirmed still firing-but-ignored, at unusually high volume in this one
session). Candidate: a PreToolUse-on-Edit nudge that counts same-session
Edit/Write calls against feature-shaped files and suggests `coder` dispatch
past a threshold — orthogonal to the existing Bash-only rules, so likely a
separate hook. Not yet confirmed as a repeat-session pattern (single session
so far); watch the next pass for recurrence before committing to the hook.

test: hook unit tests on the new patterns.

2026-09-02 pass (6 largest sessions since 08-23, 4.1–73MB — the top one,
`8b8de41e`/humble-honking-plum, a 5-day 25023-line PCB-build marathon, is
~4x the next-largest; see below). Findings:

- **Rule D `echo "==="` regressed**: the 08-23 "0/6 files, shipped" read was
  premature — `bash-reflex-nudge.py` as shipped only ever implemented Rules
  A–C (coderef, cluster-ops, redundant-cd); the echo/sed-n halves of Rule D
  were **never hook-coded**, only added to CLAUDE.md prose. Prose-only
  enforcement decayed: `echo "===` narration is back at 184/2028 Bash calls
  (9%) in the worst session (`8b8de41e`), 58/742 (8%) in `3bb9607b`
  (greedy-gliding-anchor), 2–4% in three more, 0% in one. `sed -n` (never
  claimed fixed) is worse than the 08-23 range: 303/2028 (15%) and 172/742
  (23%) in the two worst sessions, vs. 2–76/file previously. Actually ship
  the two Rule-D sub-nudges as hook code this time, not prose.
- **Rule E (tail-poll vs `Monitor`) improved but uneven**: ratio dropped from
  ~12:1 raw-tail:Monitor (08-23, 6-file aggregate) to session-level ratios of
  1.75:1 (`8b8de41e`: 65 vs 37), 2.3:1 (`378acf66`: 37 vs 16), 3:1 (`nano3d`:
  21 vs 7) — real progress, plausibly the CLAUDE.md Monitor-tool prose
  landing. But `7a148de6` is still 19:1 (38 tail vs 2 Monitor) and
  `3bb9607b` 6:1 (37 vs 6) — adoption isn't uniform. No hook exists yet;
  still prose-only. Leave as-is one more pass (trending right direction) but
  flag for a nudge if the next pass doesn't converge further.
- **Rule F CONFIRMED RECURRING** (was "watch for recurrence" after single
  occurrence `b30f9d07` on 08-23): session `378acf66` (main checkout,
  nanopub web-routes/templates feature build) ran 179 `Edit` + 32 `Write`
  across 20+ files (`test_nanopub_routes.py` 32x, `nanopub/index.html.j2`
  31x, `base.html.j2` 11x, `auth.py` 9x, `nanopub_render.py` 8x, `routes/
  nanopub.py` 7x) with **zero** `coder` agent dispatches — 15 `Agent` calls
  total, all `cluster-ops`/`issue-closer`/`reviewer`/`Explore`, none for the
  actual feature implementation. 892 of the session's assistant turns ran on
  `claude-opus-5` (vs. 1178 on `claude-fable-5`) — this is opus-tier main-loop
  doing mechanical multi-file edits a `coder` dispatch would have done
  cheaper. Two independent sessions, two different repos of work
  (auth feature / nanopub feature), same shape: build the PreToolUse-on-Edit
  nudge now (count same-session Edit/Write against feature-shaped files,
  suggest `coder` past a threshold — e.g. ~15–20 edits with 0 prior `Agent`
  dispatches this session).

Aggregate delegation health otherwise looks fine this pass: the other 4
sampled sessions show healthy `Agent` dispatch (bash:agent ratios 9–30,
subagent types spanning `coder`/`reviewer`/`navigator`/`extract`/
`cluster-ops`/`issue-closer` as intended) — Rule F is a real but
session-specific failure mode, not a fleet-wide default.
