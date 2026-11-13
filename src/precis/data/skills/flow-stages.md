---
id: flow-stages
family: work
title: precis — the repo-dev flow, from request to shipped
summary: the repo-dev flow's stage map — when a change needs a spec first, when spec review applies, and which stages the ship commands already run internally so you never rebuild them
answers:
  - how do I run the repo-dev flow on a request — which stages in which order?
  - how do I tell whether this request needs a spec before coding?
  - which stage of the flow applies next — spec, spec review, coding or ship?
  - when do I skip spec review on a spec?
  - do I need to spawn a code review before shipping?
  - when do I checkpoint-commit during a multi-agent build?
applies-to: a piece of repo-dev work — spec → build → ship
status: active
tags: [workflow]
governs: factory
---

# flow-stages — request to shipped, without reinventing a stage

**The problem this isn't.** The flow's stages already exist as separate
tools —
`ready` (spec review), `coder` (implementation), `reviewer` (code review, now
auto-gated inside `[[land]]`/`[[go]]`), `[[land]]`/`[[go]]` themselves (ship),
and `issue-closer` (close resolved issues, also auto-gated inside the ship
pair). Nothing was missing — what was missing was a name for the sequence, so
it's easy to forget a stage exists or to accidentally rebuild one the ship
commands already run. This is a checklist, not an orchestrator: skip any stage
that doesn't apply, don't manufacture ceremony for a small change.

Agent names (`ready`, `coder`, …) and `/`-commands are this harness's
surface — with no subagent runtime or slash commands, do that step yourself
or map to your runtime's equivalent; Kimi maps commands via `/skill:<name>`.

## The flow's stages

1. **Request.** The ask, as stated. If it's a one-line fix, a well-scoped
   single-file change, or something the user gave exact instructions for —
   skip straight to **Coding**.

2. **Spec.** For anything architectural, multi-file, ambiguous, or where more
   than one reasonable approach exists — write it down before coding. The
   item lives as `<slug>.md` in the backlog dir: start as an idea (a few
   lines), grow the spec **in the same file** (front-matter `status: draft`
   per the backlog `TEMPLATE.md`; mint via the `scaffold` agent —
   boilerplate, never content). Put the plan there when the work will span
   more than one session, so it survives compaction. Whether a spec is even
   needed is the same kind of judgment call as [[bug]]'s three-bucket triage.

3. **Spec review.** Spawn `ready` against the backlog spec before flipping
   `status: draft` → `ready` (the fixer's pick signal; branch `fix/<slug>`).
   Iterate on blockers; advisories are your call. Skip this for a spec you
   and the user finalized together in conversation (e.g. via `ExitPlanMode`)
   — `ready` exists for specs nobody adversarially checked yet.

4. **Coding.** Implement the approved spec. Genuine architecture/domain
   judgment (API shape, schema, CFD/DFT/catalyst reasoning) stays on the Opus
   main loop; a well-scoped, already-decided change delegates to `coder`
   (sonnet) per the Agent-sizing table in `CLAUDE.md` — that table, not this
   skill, is the authority on tiering. A build too large for one coder call
   chains as fresh rounds per [[coder-chain]].

   **Checkpoint-commit at every agent boundary.** Nothing else in this
   workflow asks for a commit — `scripts/ship` commits WIP itself, which makes
   commit feel like ship's job. It is not, and ship must not run while agents
   are editing (it snapshots mid-write, then resets the branch). On a long
   tree that pairs into a trap: deferring the ship defers the *only* commit,
   and a whole session accumulates as unstaged files with zero commits ahead.
   A bare `git commit` is not a ship — no gate, no push, no `main`. Run one
   after every agent reports, before every ship, and before every compaction.
   Agents themselves stay out of git entirely (one ran `git checkout -- <file>`
   and destroyed hours of unstaged work), so nobody commits unless you do.

5. **Ship.** `[[land]]` (ship only) or `[[go]]` (ship + deploy). On ship, fold
   surviving truth into the owning package docstring and **delete the backlog
   item** in the same commit (`docs/README.md` §Backlog lifecycle — the
   verdicts are [[docs-triage]]'s). **Both commands already own the next two
   stages internally** — do not spawn `reviewer` or `issue-closer` yourself
   before calling one of them:
   - **Code review** is size/risk-gated inside `[[land]]`/`[[go]]` (their own
     "Review risky diffs before shipping" step) — it fires automatically on a
     large or sensitive diff, and is skipped for a small one. Use the
     standalone `/code-review` only when you want a review *outside* a ship
     (e.g. mid-session, before you're ready to land).
   - **Closing resolved issues** is a background `issue-closer` spawn inside
     `[[land]]`/`[[go]]`, after a green ship — it checks the shipped diff
     against open gripes and backlog items and closes what it's confident
     this ship fixed, then relays a one-line note. Nothing else to do here.

6. **Report.** `[[land]]`/`[[go]]`'s own closing steps (residual harvest, then
   summarize-and-handoff) already tell the user what shipped and what's left.
   `flow-stages` doesn't add a report on top of that — relay what those steps
   produced.

## When NOT to reach for this

A trivial fix, an exploratory research question, or anything where writing a
spec would be pure ceremony (nothing to review, nothing ambiguous) — just do
the work. `flow-stages` is a map for substantial changes, not a mandatory gate in
front of every edit.

## See also

- [[bug]] — the same "does this need a spec" judgment, on the fix side.
- [[docs-triage]] — verdicts for the backlog items this flow creates and deletes.
- [[coder-chain]] — when the Coding stage is too big for one coder call.
- [[next]] — picking which open item to flow.
