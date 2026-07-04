# 0048 — Autonomous backlog execution: the laptop fixer loop

- **Status**: accepted (design, 2026-07-04) · **MVP pending build** ·
  **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0044 — the derived-job lane](./0044-derived-job-lane.md) — `fix_gripe`
    is the existing seam. This ADR **retires its dispatch coupling** and
    generalizes its `run()` guts into the fixer loop's execution core.
  - [ADR 0030 — `job`/`finding`/`cron` stay separate from `todo`](./0030-job-finding-cron-stay-separate.md)
    — the precedent for not collapsing kinds. Here: the **ideation → decision →
    execution** stages stay distinct, and — critically — **repo-dev scheduling
    stays out of precis dispatch** (which is content-only).
  - [ADR 0005 — forward-only migrations](./0005-greenfield-migrations.md) — the
    forward-only ethos this loop's **fix-forward-only recovery** mirrors.
  - [ADR 0047 — controlled tagging cascade](./0047-controlled-chunk-tagging.md)
    — cheap coarse call, escalate the residual: reused as *"trust the agent to
    judge what's auto-safe"* rather than a bespoke classifier.
  - **OPEN-ITEMS.md → Dark-factory workstream** — the north star and the open
    **Backlog groomer** item this ADR specifies.

## Context

The dark factory already has a **proven exit**: `scripts/ship` (auto-fix ruff →
ruff/mypy/pytest in a container → squash `commit-tree` + CAS push to `main`) and
`scripts/deploy` (ansible redeploy), driven interactively by `/go`. That core
has been reliable. What remained were three human keystrokes:

1. **No entrance gate** on the *spec*. A vague spec → a gate-green, confidently-
   wrong build. The binding constraint is no longer "can the model code" (Opus
   4.7+ shows no test-gaming — OPEN-ITEMS deferred the holdout eval for exactly
   this) but **"is the spec precise enough."** A better model does not remove
   that; it moves the bottleneck to spec precision.
2. **`fix_gripe` pushes an *unverified* branch** and stops — a human gates+ships.
3. **Deploy is a manual keystroke.**

Two facts about *this* deployment collapse most of the risk: it is **single-user**
(no external blast radius) and has **daily DB backups** (worst-case loss < 24h,
much of it re-derivable). Those two facts are what make full autonomy defensible
here that would not be on a multi-tenant system.

## Decision

**Wrap the proven `/go` core in an autonomous loop bookended by two gates of
different natures, run from a git-world scheduler on the laptop, recovering by
fix-forward, reporting by exception.**

The fixer ≈ **the proven `/go` core + an autonomous pick+build front + an
agentic look-at-prod → fix-forward tail.** The risky deploy heart is unchanged;
the new surface is only the intake and the tail.

```
pick (ready-gated) → build (Claude, host OAuth) → scripts/ship (gate+merge)
  → scripts/deploy → agent looks at prod (diff-scoped + main pages)
  → fix-forward if wrong → report (by exception)
```

### 1. Two gates — one a judge, one deterministic; `ready` is universal

- **`ready` — the entrance gate.** "Can this be built without a human
  mid-flight?" is *judgment*, so `ready` is a `utils/claude_p.py` JSON judge, not
  a script. Rubric: **ambiguity, underspecification, open loops, overreaching
  scope, unsupported goals, deferred-as-in-scope, and — load-bearing — missing
  acceptance criteria.** Two severities: **blocker** (→ open questions to the
  human) vs **advisory**.
- **`ready` gates gripes too — and *is* the classifier.** A gripe is an input,
  not a vetted spec (often a bare symptom). Running `ready` on a gripe body *is*
  the "trust the agent to judge auto-safety" decision: crisp+bounded → **passes**
  (fixer takes it, zero human effort); vague → **fails** → `spec-needed`, bounced
  to the human. Nothing builds without passing the gate; clear items flow because
  they *pass* it, not because they skip it.
- **`ship`/`go` — the exit gate.** Deterministic, unchanged. Red aborts.
- **The weld.** A spec is not `ready` until it **states its own acceptance
  criteria**. Those criteria close the coverage gap (green-ship ≠ correct unless
  the spec says what "done" means) and steer the post-deploy look.

### 2. Spec substrate: transient files in the repo; discourse is conversation

A repo-change spec is **code-adjacent** — it graduates into an ADR + a code change
in the same clone the builder works in. So: **`docs/proposals/<slug>.md`**,
ADR-shaped (intent · scope · non-scope · **acceptance criteria** · open-questions
log). **Transient: graduate to an ADR (the durable "why") or die** on ship — never
accumulate. A `spec`/`proposal` precis kind is *reserved, not built* (volume-gated).

**Discourse is a conversation, not a document** — argument is dialogic; you don't
*argue into* a file or a DB draft. Hold it in a session while synchronous +
human-driven; distill the residue (decision + rejected alternatives) to the file.
Move to a threaded ref only when it goes asynchronous (human + agents over days).

### 3. Two schedulers, substrate-matched — repo-dev does NOT ride precis dispatch

The scheduler lives in the same world as its state:

- **Repo-dev lane → a git-world CI loop (the fixer).** Specs are files; the fixer
  polls `docs/proposals/` + open gripes, runs the pipeline in a clone. **precis
  dispatch is not in this loop.** precis is touched only as a **source** (read
  gripes) + **sink** (write status). This restores the two-substrate separation
  `whatneedsdoing` already encodes (substrate 1 = repo-dev, executed git-world;
  substrate 2 = precis todos, self-run by dispatch).
- **Content lane → precis dispatch, unchanged.**

Routing repo-dev *through* precis dispatch was illusory reuse — repo-dev needs a
serial CI queue, not rotation/coroutines/cross-host GPU dispatch. Today's
`fix_gripe`-on-dispatch is the thing to **migrate off**.

### 4. Host = the laptop; `fix_gripe` keep-guts / retire-coupling

**The laptop is the fixer box** — and it **dodges the self-deploy ouroboros for
free**: `redeploy-precis.yml` restarts the *cluster* (melchior/caspar/balthazar/
spark), **not** the laptop, so the scheduler process is never killed by the
deploy it runs. No detach/supervised-unit machinery needed at the MVP. It already
has everything (Docker + compose, `scripts/ship`/`scripts/deploy`, `claude` +
`~/.claude` OAuth, git, a precis store connection). Runtime: **launchd
(LaunchAgent), skip-on-battery, Amphetamine keeps it awake.** Auth: `claude` runs
**on the host** (OAuth works; only the *gate* containerizes, needing no creds) —
the one thing to verify is a headless launchd `claude -p` reaching Keychain.

`fix_gripe`'s fate: **keep the `run()` guts** (clone→branch→`claude`→push, prepush
hook, repo resolution, prompt) as the fixer's execution core; **retire the
`claude_inproc` job_type registration** at cutover (exactly one consumer of the
gripe queue — no double-run race); **generalize intake** gripe→work-item (gripe
*or* proposal); the "push branch for review" tail becomes the autonomy dial.

The eventual always-on home (spark / a dedicated box) is deferred; only *then* do
the pinned-fixer split and supervised-detach (`systemd-run`/launchd transient
unit, killable via the init system, recovery-by-convergence since ansible is
idempotent) become necessary. On the laptop they are not.

### 5. Keyless deploy: fix-forward only; post-deploy is an agentic look

The human `/go` keystroke drops — the gates carry the safety, not the keystroke.

- **The loop runs the full `/go`** (`scripts/ship` + `scripts/deploy` directly —
  they are built for no-LLM-in-the-loop). Serial: one item end-to-end (lock held
  through ship+deploy) — no concurrent ships/deploys.
- **Prod is the test surface** — dissolves the twin-fidelity gap (no twin to be
  unfaithful).
- **Recovery is fix-forward ONLY.** No automated rollback, no circuit-breaker, no
  migration-class detector. A broken deploy is fixed forward carefully; a
  catastrophe is a **manual backup restore** (break-glass, bounded < 24h loss).
  This matches the forward-only ethos and is maximally simple.
- **The post-deploy check is an *agentic look*, not a scripted smoke.** The agent
  loads the (diff-scoped) pages, makes the MCP calls, reads the responses, judges
  "broken" like a human would, and **fix-forwards in the same context** (it holds
  what it just changed). With rollback gone there is **no drastic lever**, so the
  observer need not be deterministic — a false positive wastes a fix (the gate
  catches it), a false negative is caught next tick / by the confusion-mine. The
  laptop makes "fix from outside the burning building" hold: the *fix* path
  (git/ship/deploy) does not depend on prod being up; "can't reach prod" is itself
  an observation. Only the *invocation* stays deterministic (the tick always runs
  the look; optional cheap `curl /readyz` precheck before spending a Claude call).

### 6. Reporting by exception; loud on trouble → #news

- **Clean ship → silent** (no #news ping). **Self-fixed → one-line note.**
  **Needs-you** (gate red 3×, couldn't verify, bubbled) → **full loud report.**
- **Channels:** #news (the existing wired Discord channel) for the loud/one-line
  pushes; an **`agentlog`** (existing per-run attribution kind: prompt + model +
  `touched`) + a **gripe comment** as the durable record on *every* run incl.
  greens; **`/whatneedsdoing`** as the pull dashboard.
- **Accepted risk:** silence-on-green means a gate-green-but-wrong ship won't ping
  in real time — caught via `/whatneedsdoing`/agentlog/next-tick, not instantly.
  Owner accepts this (single-user, prod-as-test, fix-forward).
- **"Loud mode"** = loud-*on-exceptions*; a bootstrap posture (watch the *unproven
  fixer*, not gate the changes), dialled down once it has a clean track record.

### 7. Intake normalized; the groomer = `whatneedsdoing`, write-side

`/whatneedsdoing` is a **survey**, not a **queue** (ephemeral, spans both
substrates, includes non-actionable items). The fixer polls a **durable queue**
(gripes + proposals). The **groomer** is `whatneedsdoing`'s aggregation run
*write-side*: read all sources, apply the `ready` classifier, file the repo-dev
buildable subset as gripes/proposals (idempotent, dedup-by-fingerprint). Survey
stays a human report; queue stays filtered. *Deferred* — MVP hand-files proposals
and polls gripes directly.

### 8. Third judge: diff-scoped doc-freshness at ship (advisory)

The agent-context maps (CLAUDE.md, AGENTS.md, `precis-*-help`, OPEN-ITEMS) are read
every agent run, so drift *misdirects*. `/go` gains a **diff-scoped, advisory**
freshness check (`claude_p`: `diff × implicated map-sections → stale findings`),
enforcing the same-commit norm at the chokepoint; the builder auto-fixes stale
sections in the same commit. Never a blocker. Excludes archival prose + the schema
SVG (drift-note is honest) and ADRs (append-only, drift-immune).

## Why not

- **A hand-written holdback test.** The owner won't author them; the signal
  (verify the change did its thing where the builder can't fake it) comes free
  from the `ready`-forced acceptance criteria, checked by the **agentic
  post-deploy look** against real prod.
- **Automated rollback / circuit-breaker / migration-class detector.** Dropped:
  fix-forward + manual backup restore is simpler, matches forward-only, and
  removes a pile of machinery (SQL-phase parsing, reverse-deploy). A single-user
  system with daily backups can afford the bounded manual case.
- **A scripted post-deploy smoke.** The determinism it offered mattered only to
  gate a *rollback* — which is gone. An agentic look is more human-like and
  unifies liveness + feature-check + fix into one step.
- **Repo-dev on precis dispatch.** Illusory reuse; crosses the git↔precis
  boundary and violates the two-substrate separation. The fixer is a git-world CI
  loop; precis is source+sink.
- **A `spec`/`proposal` precis kind now; discussing in a DB `draft`.** A
  repo-change spec is code-adjacent (belongs in the repo); argument is dialogic
  (belongs in a session, not a document).
- **Keeping the deploy keystroke for model-trust; a separate fixer box now.** The
  keystroke guarded twin/mechanism (handled by prod-as-test + fix-forward); the
  laptop already sits outside the deploy's restart radius, so no separate box is
  needed until an always-on host is wanted.
- **A blocking doc-freshness gate.** A stale doc must never block a correct fix.
  Advisory + auto-fix.

## Consequences

- **The human has two touchpoints, both principled — not model-distrust:**
  (a) **write specs** (against `/whatneedsdoing`; `ready` in tandem), and
  (b) act on the **rare bubbled item** a full report surfaces. Everything else —
  pick, build, ship, deploy, verify, fix-forward, report — happens without a
  keystroke.
- **The loop is the proven `/go` plus a small new surface** (intake + agentic
  tail), so it extends a battle-tested path rather than replacing it.
- **MVP critical path** (report format = exception-based, loud on trouble to
  #news, full-auto):
  1. `scripts/fixer-tick` — lock → pick (ready-gated proposals; gripes deferred to
     the dial-up) → worktree/branch → `claude` build → `scripts/ship` →
     `scripts/deploy` → agentic prod-look → fix-forward → report.
  2. launchd plist — interval + skip-on-battery.
  3. Three conventions to pin: **idempotent pick** (skip items already branched),
     **proposal-ready marker** (`status: ready` front-matter), **verify headless
     launchd OAuth**.
- **Deferred, not built:** automated `ready`-on-gripes (the dial-up that lets
  clear bugs auto-flow); the groomer (`whatneedsdoing` write-side); the
  doc-freshness ship-judge; the always-on host + pinned-fixer/supervised-detach; a
  `sandbox_run` job_type for arbitrary code (shares the container capability,
  outputs to refs not loose files).
- **Autonomy dial:** MVP is full-auto with loud-on-exceptions during bootstrap.
  Turning it "up" later = automated `ready`-on-gripes (clear bugs flow with no
  human promotion). Cost is unbounded-but-self-limited (human spec-writing is the
  throughput ceiling).
