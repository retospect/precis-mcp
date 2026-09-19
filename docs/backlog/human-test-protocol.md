---
status: draft
title: Human test protocol for the web UI — task scripts beside the manual, mechanical + naive-agent tiers
prio: normal
model: sonnet
---

# Human test protocol for the web UI

## Motivation / why
Usability defects in this product have been found by *use*, never by
reading a diff (search facets swallowed, stale-server error that did not
say "restart", viewer embed sized wrong only at one size class). The web
UI has no protocol for "do the thing a human wants to do and report
snags", so each such defect waits for Reto to trip over it. The manual
chapters (`src/precis_web/manual/*.md`) are already task-shaped and the
tour manifests (`manual/tour/*.json`) already name `[data-tour]` anchors
with a drift tripwire in `scripts/guide-capture`. Merge the two into a
runnable protocol.

## In scope
- One protocol file per human task under `src/precis_web/manual/protocol/
  <nn>-<slug>.yaml`: `task:` (one sentence), `start:` (DB fixture +
  route), `steps:` each `{anchor, action, expect}`, `log:` dated lines
  (`agent: N snags` / `human: N snags`). Five seeds, one per manual chapter.
- **Mechanical tier**: extend `scripts/guide_capture_inner.py` (Playwright
  in the container, as today) with `--protocol`: perform each step's
  `action` on its anchor, assert `expect` (text present / route matched /
  element visible). Missing anchor stays a HARD ERROR. Runnable against
  `scripts/guide-web --db test`.
- **Judgment tier**: `.claude/agents/dogfood.md` (Sonnet) — input is the
  `task:` sentence and `start:` ONLY, never the steps. Drives the browser
  (container Playwright), reports every call it had to guess, retry, or
  re-read, ranked; `clean` if none. Each snag → `gripe` tagged
  `protocol:<slug>`; the run appends one `log:` line.
- **Cadence**: `scripts/protocol-review` prints `DUE` per protocol whose
  newest `log:` line is >14 days old; bunched into `/whatneedsdoing` next
  to `token-review`.
- **Ownership tripwire**: test that every route named in a protocol's
  `start:`/`expect` exists in the app's route table, and every `anchor`
  exists in some template (static grep, no browser) — cheap gate-time
  drift check.
- Test-DB fixture: one draft, one paper, one claim, one figure seeded so
  every chapter's task has an object to act on.

## Explicitly NOT in scope
- Replacing Reto's runs. The agent tier is a filter before a human run,
  not a substitute; latency, label meaning, visual hierarchy stay human.
- Any prod write. Both tiers target `--db test` only.
- MCP/seven-verb dogfood (separate item if wanted; same naive-agent
  pattern, different surface).
- Screenshot pixel-diff regression.

## Acceptance criteria
- `scripts/guide-capture --protocol writing-a-paper --base-url …` exits 0
  on main against the seeded test DB and exits non-zero naming the step
  when an `expect` fails or an anchor is missing.
- Dispatching `dogfood` with only `task:` + `start:` for writing-a-paper
  produces a ranked snag list (or `clean`), files gripes tagged
  `protocol:writing-a-paper`, and appends a `log:` line.
- `scripts/protocol-review` prints `DUE` for a protocol with no log line
  and is quiet after a run; `/whatneedsdoing` surfaces it.
- Route/anchor tripwire test is red when a template drops a `data-tour`
  anchor a protocol references.

## Target + blast radius
`scripts/guide-capture`, `scripts/guide_capture_inner.py`,
`scripts/guide_lib.py`, new `scripts/protocol-review`,
`.claude/agents/dogfood.md`, `.claude/commands/whatneedsdoing.md` (one
step), `src/precis_web/manual/protocol/`, test-DB seed fixture, one new
test under `tests/precis_web/`. No runtime/product code paths change.

## Open questions / decisions log
- Fixture source: hand-written minimal rows vs prod-copied rows per
  memory `local-web-demo-recipe`. Lean: hand-written, so the gate can run
  it without prod access.
- Does the judgment tier use container Playwright driven by the agent
  via small scripts, or the `claude-in-chrome` skill against the local
  port? Lean: container Playwright — it already works and needs no host
  browser.
- DECIDED 2026-09-19: judgment tier stays. Hand-run writing-a-paper
  dogfood (Sonnet, task sentence + start state only, container
  Playwright vs `--db test`, 33 tool calls / ~80K tokens) completed the
  task and returned 3 snags, 2 verified from screenshots: (1) create form
  says writing starts, but the draft's Meta tab shows `auto-author: OFF`
  and the only "will this run" surface is `/todo`'s agent-API pseudocode —
  no human-readable answer; (2) `+ New` defaults Kind to CAD; (3) section
  status dots have no label/tooltip. Filed as gr348555 / gr348556 / gr348557, tagged
  `protocol:writing-a-paper`.
- Writable stack is required for create-tasks: `scripts/guide-web` forces
  read-only (PGOPTIONS); the dogfood ran the compose `precis-dev` web
  command directly without it. The protocol runner needs a `--writable`
  flag or its own launcher.
