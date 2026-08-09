# context-audit rubric

Judge prompt for one sampled artifact under `out/NN-<slug>.md`. Applied by a
Sonnet agent walking `PROCEDURE.md`, one artifact at a time. This file is the
*executable* form of `docs/backlog/context-quality-eval.md` §"Section 2 — The
inspection rubric" — that doc is the source of truth for the six dimensions
and the severity vocabulary; this file only turns it into a per-artifact
judge prompt + a fixed output shape. If a dimension below seems to assume
context you don't have (which catalog row this is, why it's in scope), go
read that doc's §Section 1 catalog first.

You are shown: the artifact's header (which call/builder produced it, which
ref it was sampled from) and its rendered body. Judge the body against each
of the six dimensions below. Not every dimension applies to every artifact —
an agentic prompt (planner tick, reviewer digest) has no "next=" breadcrumbs
to check, for instance; skip a dimension that doesn't apply and say so, don't
force a finding.

**Root-cause before you file.** A rendered artifact shows a *symptom*; don't
report it as a defect until you've opened the render source under `src/precis/`
(grep/Read) and confirmed the cause — it distinguishes a real bug from
intentional formatting, and pins the fix to a `file:line`. A finding with no
code anchor is a hedge; mark it so. **Thin vs legitimately-empty:** a tiny
render (a near-empty `todo-ask-user`, an empty `quest-frontier`) is often
*correct* — there genuinely is nothing to show. Only call it a defect if the
source proves an agent needs a field the render dropped. Use the manifest's
`kind_roster` to tell "kind absent from a cross-kind disclosure = real gap"
apart from "this build never registered the kind."

## The six dimensions

(Verbatim in substance from `docs/backlog/context-quality-eval.md` §Section 2.)

1. **Skills reachable?** Does the context name/link the skill the next action
   needs, and does `get(kind='skill', id=…)` actually return it (not a 404,
   not a stale toc entry)?

2. **Info sufficient to make the call?** Every field the next action needs is
   present in-band, or does the agent have to guess or round-trip for it?
   (E.g. a `Next:` hint that names a handle the response never surfaced.)

3. **Breadcrumb / `Next:` correctness.** Does the trailer point at a real,
   runnable next verb — not a stale view name, not a kind the build has
   disabled?

4. **Progressive disclosure.** Right altitude for the ask — not a wall of
   text for a one-line question, not a truncated stub when the agent asked
   for the whole thing. Pagination (`# N of K`) sane and consistent with
   `format_search_headline`'s contract.

5. **Surface↔behavior drift.** Does the render match what the corresponding
   skill/`precis-overview` row claims it does? (The MAJOR-C
   `precis-overview` drift-from-live-registry finding in the mcp-critic
   review is the canonical example of this class.)

6. **Classifier / pre-worker gap.** Is a needed field empty because *no*
   upstream pass populated it yet (missing `chunks.keywords`, unclassified
   `role3`, absent chunk summary, un-run health check)? This is the
   load-bearing "do we need another classifier/pre-worker" signal — file it
   as its own finding class, not folded into #2, so it's queryable
   separately from "the field exists but the render forgot to show it."

Not every dimension applies to every artifact — an agentic prompt (planner
tick, reviewer digest) has no `Next:` breadcrumb to check, for instance; skip
a dimension that doesn't apply and say so, don't force a finding.

## Severity vocabulary

This repo already has a gripe-finding taxonomy
(`docs/mcp-critic-review-2026-05-02.md`) — reuse it verbatim, don't invent a
parallel scale:

| Tag | Meaning |
|---|---|
| **MAJOR-C** | Correctness: the context is actively wrong or misleading — a breadcrumb that 404s, a field claimed present that silently isn't, drift between prose and render, a call an agent has no way to discover it needs. |
| **MAJOR-$** | Cost: the context is *correct* but burns tokens/turns it shouldn't — an unbounded body with no view/pagination, a wall-of-everything default where a cheap summary would do, a redundant round-trip forced by a design choice (not a bug). |
| **MINOR-C** | Real but small correctness friction — recoverable in one extra call, a slightly awkward drill-in path, a stale-but-guessable hint. |
| **NIT** | Polish only — wording, a slightly suboptimal default size, cosmetic inconsistency. Wouldn't derail an agent. |

A classifier/pre-worker gap (dimension 6) is filed as its own finding class
regardless of which of the four tags above it's severity-banded at — see
`classifier_gap` below.

## Output shape — one finding

Emit one block per defect found (zero is a valid, good outcome — say so
explicitly rather than manufacturing a finding). Machine-readable shape,
mirroring the `findings.json` block in `docs/mcp-critic-review-2026-05-02.md`:

```json
{
  "context_slug": "todo-tree",
  "dimension": "breadcrumb-correctness",
  "severity": "MAJOR-C",
  "defect": "one-line, specific — quote the offending string",
  "suggested_fix": "one line — file/call/prose to change, not a redesign",
  "classifier_gap": false
}
```

Field notes:

- `severity` ∈ `MAJOR-C` | `MAJOR-$` | `MINOR-C` | `NIT` — the exact tags
  from `docs/mcp-critic-review-2026-05-02.md`, not a new scale.
- `dimension` ∈ `skills-reachable` | `info-sufficient` | `breadcrumb-correctness`
  | `progressive-disclosure` | `surface-behavior-drift` | `classifier-gap`.
- `classifier_gap` is `true` only for dimension-6 findings (kept as its own
  boolean too, since `PROCEDURE.md`'s final tally reports these separately
  from ordinary defects regardless of which severity tag they landed at).
- `suggested_fix` names the smallest concrete change — not "improve X", the
  actual file/prose/call to touch, mirroring the review doc's `fix_file`
  convention.

## Per-context verdict

After applying all six dimensions to one artifact, record one verdict:

- **pass** — no `MAJOR-C`/`MAJOR-$` findings; artifact does its job.
- **thin** — no `MAJOR-C`, but `MAJOR-$` (or several `MINOR-C`) findings
  recorded (works, but a real gap).
- **bad** — at least one `MAJOR-C` finding.

`PROCEDURE.md` collects these into the run's final tally.
