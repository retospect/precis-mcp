---
name: flow
description: >-
  A map of this repo's request → spec → spec-review → coding → ship stages and
  which existing tool owns each one — not a new pipeline, a reminder of the
  order and a guard against duplicating what `/land`/`/go` already do (code
  review, issue-closing). Reach for it when a request is substantial enough to
  need a spec before coding, or when you're unsure which stage-tool applies
  next. Repo-dev tool for developing precis-mcp; NOT a precis product skill.
---

# flow — request to shipped, without reinventing a stage

**The problem this isn't.** The stages already exist as separate tools —
`ready` (spec review), `coder` (implementation), `reviewer` (code review, now
auto-gated inside `/land`/`/go`), `/land`/`/go` (ship), `issue-closer` (close
resolved issues, also auto-gated inside `/land`/`/go`). Nothing was missing —
what was missing was a name for the sequence, so it's easy to forget a stage
exists or to accidentally rebuild one `/land`/`/go` already runs. This is a
checklist, not an orchestrator: skip any stage that doesn't apply, don't
manufacture ceremony for a small change.

## The stages

1. **Request.** The ask, as stated. If it's a one-line fix, a well-scoped
   single-file change, or something the user gave exact instructions for —
   skip straight to **Coding**. Same triviality test `EnterPlanMode`'s own
   guidance already uses; `flow` doesn't add a new bar.

2. **Spec.** For anything architectural, multi-file, ambiguous, or where more
   than one reasonable approach exists — write it down before coding. Mint a
   `docs/proposals/*.md` via the `scaffold` agent (numbering/boilerplate,
   never content) and fill in the actual decision yourself; this is the Plan
   this repo's plan-mode workflow already produces — put it in a proposal
   file when the work will span more than one session, so it survives
   compaction.

3. **Spec review.** Spawn `ready` against the proposal before marking it
   `status: ready`. Iterate on blockers; advisories are your call. Skip this
   for a spec you and the user finalized together in conversation (e.g. via
   `ExitPlanMode`) — `ready` exists for specs nobody adversarially checked yet.

4. **Coding.** Implement the approved spec. Genuine architecture/domain
   judgment (API shape, schema, CFD/DFT/catalyst reasoning) stays on the Opus
   main loop; a well-scoped, already-decided change delegates to `coder`
   (sonnet) per the Agent-sizing table in `CLAUDE.md` — that table, not this
   skill, is the authority on tiering.

5. **Ship.** `/land` (ship only) or `/go` (ship + deploy). As of this
   session, **both already own the next two stages internally** — do not
   spawn `reviewer` or `issue-closer` yourself before calling one of these:
   - **Code review** is size/risk-gated inside `/land`/`/go` (their own
     "Review risky diffs before shipping" step) — it fires automatically on a
     large or sensitive diff, and is skipped for a small one. Use the
     standalone `/code-review` only when you want a review *outside* a ship
     (e.g. mid-session, before you're ready to land).
   - **Closing resolved issues** is a background `issue-closer` spawn inside
     `/land`/`/go`, after a green ship — it checks the shipped diff against
     open gripes/`OPEN-ITEMS.md` and closes what it's confident this ship
     fixed, then relays a one-line note. Nothing else to do here.

6. **Report.** `/land`/`/go`'s own closing steps (residual harvest, then
   summarize-and-handoff) already tell the user what shipped and what's left.
   `flow` doesn't add a report on top of that — relay what those steps
   produced.

## When NOT to reach for this

A trivial fix, an exploratory research question, or anything where writing a
spec would be pure ceremony (nothing to review, nothing ambiguous) — just do
the work. `flow` is a map for substantial changes, not a mandatory gate in
front of every edit.
