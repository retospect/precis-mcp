---
id: precis-polish-paper
title: precis — paper polish runbook
summary: pre-submission paper review runbook — orchestrate adversarial/citation reviewer personas, severity ranking
flavor: runbook
status: active
applies-to: orchestrating multi-pass paper review before submission
invokes-personas:
  - precis-adversarial-reviewer
  - precis-citation-reviewer
last-updated: 2026-06-05
---

# precis-polish-paper — polish a paper before submission

## What this runbook does

Coordinates multiple specialist reviewer personas through
`<handle>` and produces a consolidated report ranking
findings by severity across passes. Designed for the
week-before-submission moment when the manuscript is
content-complete and the question is "what would block this
from passing review?"

The two ways to drive it:

1. **Shell-harness mode.** Run
   `scripts/review-paper/run.sh <handle>` to fan out the
   passes serially via `claude -p`. Each persona's report
   lands as a timestamped markdown file under
   `scripts/review-paper/out/`. The aggregation step is
   currently manual — read the per-persona reports and
   produce the consolidated report by hand or via a final
   `claude -p` pass on `precis-polish-paper` itself.
2. **Agent-driven mode.** A host agent adopts the
   `precis-polish-paper` runbook, spawns one sub-agent per
   persona (via `claude_p` or the Agent tool), collects each
   sub-agent's structured findings, and produces the
   aggregated report. Each finding can optionally land as a
   `kind='finding'` ref linked to the paper.

Personas this runbook orchestrates:

- [[skill:precis-adversarial-reviewer]] — claims and method.
- [[skill:precis-citation-reviewer]] — bibliography integrity.

(More personas land here as they're authored —
flow-and-arc, paragraph-structure, statistics, novelty.)

## Aggregate the findings

After every per-persona report is in hand, produce the
master report:

1. **Cross-pass dedup.** When two reviewers flag the same
   sentence or section, merge into one finding citing both
   reviewers. Keep both severity ratings — diverging
   severity between reviewers is itself signal.
2. **Group by severity first, then by chunk.** Blockers
   first, then high, then medium, then low. Within each
   severity tier, sort by chunk handle so the author can
   fix in linear file order.
3. **Verdict line.** One sentence at the top: ship / revise
   / don't-ship + the headline reason.
4. **Reviewer drift.** A separate section listing cases
   where two personas disagreed on severity or interpretation
   — these are often the most informative.

## Output format for the consolidated report

```
# Paper polish — <handle>

## Verdict
<one line: ship / revise / don't ship + headline reason>

## Blockers (N)
[merged findings, severity = blocker, sorted by chunk handle]

### N. <title>
- **Reviewers**: precis-adversarial-reviewer (high),
                 precis-citation-reviewer (blocker)
- **Where**: `<handle>~12..15`
- **Observed**: <verbatim quote>
- **Why it blocks**: <one-line synthesis from both reviewers>

## High (N)
[…]

## Medium (N)
[…]

## Low (N)
[…]

## Coverage
- precis-adversarial-reviewer: <N findings, run at <stamp>>
- precis-citation-reviewer: <N findings, run at <stamp>>
- […]

## Reviewer drift
<list of cases where two reviewers disagreed on severity,
with each reviewer's reasoning side by side>
```

## When to add a new persona to this runbook

When a category of finding keeps appearing in the manual
review but no shipped persona is set up to catch it
systematically:

1. Author a new persona under
   `src/precis/data/skills/personas/precis-<name>.md` with
   `flavor:persona` and an `## Adopt this persona` H2.
2. Add the slug to this runbook's `invokes-personas:`
   frontmatter list.
3. Add a one-line description in the **Personas this
   runbook orchestrates** list above.

The persona then becomes discoverable by every other agent
via `search(kind='skill', q='...')`, and the harness picks
it up automatically the next time the runbook fans out.
