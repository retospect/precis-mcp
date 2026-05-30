# Precis-MCP usability review — picky pass (round 2)

You are reviewing the **precis** MCP server's tool surface as a
**ruthlessly picky senior reviewer**. Your job is to find problems
other reviewers missed — *including* problems the previous picky
review missed, and *including* regressions introduced by the
previous round's fixes.

You have access to the `precis` MCP server plus the built-in tools.

## Before you start

`WaitForMcpServers` first — the server cold-starts in docker
(historically 2–5 s; the previous round saw the *first three*
calls all time out at 120 s due to a botched embedder warmup, so
do not assume "connected" means "responsive"). Block on
`connected=precis`, then immediately call something cheap and
non-embedder (`get(kind='skill', id='toc')`) before doing anything
else. If that hangs, that's finding-zero — record the verbatim
timeout and continue from there. **Do not retry blindly; report
the wedge.**

## Context — fix history

This server has been through two rounds of fixes. The first round
addressed 13 findings from a broad pass (call them B-1…B-13). The
second round was prompted by a picky pass that uncovered 10
additional issues (F-1…F-10) *and* flagged that several B-fixes
were partial, regressed, or self-referentially broken. You are
verifying the *combined* state.

**For each item below: probe the verbatim symptom, then verify the
fix landed cleanly with NO new regression.** A partial fix that
solves one path but breaks another is *worse* than the original
finding — call those out as BLOCKER.

### Round-1 fixes (B-1 … B-13)

| # | Symptom to probe | Acceptance |
|---|---|---|
| B-1 | `get(kind='skill', id='precis-<verb>-help')` for each of **all seven** verbs (`get/put/search/edit/delete/tag/link`). Round 1 missed `precis-edit-help` — its `applies-to:` frontmatter names `markdown`/`plaintext`/`tex`/`python`, none of which were wired, so the kind-gate still false-fired. | No `> **Heads up:** … not wired …` banner on any of the seven verb-help skills. |
| B-2 | `get(kind='skill', id='toc')`. Same root-cause cluster as B-1. | None of the seven verb-help skills appear in `## Hidden`. |
| B-3 | `put(kind='memory', text='x', tags=['STATUS:doing'])` and `tags=['STATUS:bogus']`. | First error is `'STATUS': axis not allowed on kind 'memory'` (axis check), NOT `invalid STATUS value`. |
| B-4 | `put(kind='gripe', text='x', tags=['ignored'])` AND `put(kind='gripe', text='x', link='memory:1')`. | Both reject with `[error:BadInput]` naming the unsupported arg and pointing at `precis-gripe-help`. |
| B-5 | The new ack on `put(kind='gripe', text='probe — picky round 2 verification')`. **You ARE allowed to file ONE gripe this round, for B-5 verification only.** | Ack body names the write-only / cannot-be-deleted constraint. Note the *verbatim* ack. |
| B-6 | `search(q='exercise-mcp-throwaway')` (no kind, no tags=). Then `search(q='photocatalysis')`, `q='cells'`, `q='spaced repetition flashcards'`, `q='topic-x'`. | Hint fires on the genuinely tag-shaped queries (`exercise-mcp-throwaway`, `topic-x`), does NOT fire on plain English words ≥ 4 chars (`photocatalysis`, `cells`). Round 1 found that almost every common-English query tripped the hint — that was a HIGH finding. |
| B-7 | `search(kind='memory', tags=['really-bogus-tag-12345'])` (no `q=`) and `search(kind='memory', q='', tags=['topic:something'])`. | Degrades to a recency-ordered list / empty-set message; never `BadInput "search requires q="`. |
| B-8 | `edit(kind='markdown', id='notes/x.md', mode='find-replace', find='a', text='b')`. Round 1 found this still raised `NotFound: unknown kind: markdown` — fix didn't land. | `[error:Unsupported]` naming `PRECIS_ROOT` as the missing precondition. **And the breadcrumb must not say `options above are kinds that support verb='edit'` if there are no options actually above the `next:` line** (round 1 found this self-contradiction). |
| B-9 | `get(kind='random', view='slug')`. **Then run every line of the `Next:` trailer verbatim.** Round 1 found the trailer recommended `args={'len': 4}` which itself fails with `args= keys ['len'] not accepted by random.get`. | Every `Next:` suggestion in the trailer must execute successfully. |
| B-10 | `search(kind='nonsense-xyz', q='whatever')`. | Options line lists search-supporting kinds; `next:` line says `options above are kinds that support verb='search'`. |
| B-11 | **Critical regression to check.** As soon as `WaitForMcpServers` returns, fire `get(kind='skill', id='toc')` immediately. Round 1 round-2 saw three back-to-back calls (including `toc`, which doesn't touch the embedder) all time out at 120 s because the bge-m3 warmup thread held the GIL and blocked the stdio asyncio loop. | First call completes in < 5 s. If anything early hangs > 60 s, that's a fresh BLOCKER and the round-2 fix to round-1 fix didn't work either. |
| B-12 | Re-read the loaded descriptions of `mcp__precis__get`, `mcp__precis__search`, `mcp__precis__put`, `mcp__precis__edit`, `mcp__precis__delete`, `mcp__precis__tag`, `mcp__precis__link` (their full `description:` text from the MCP tool registration). | Each ends with the natural-language `search(kind='skill', q='<sentence>')` shape, NOT brittle `q='<verb> <kind>'` token. |
| B-13 | Echo of B-7 — same probe covers it. | — |

### Round-2 fixes (F-1 … F-7 that the maintainer addressed)

| # | Symptom to probe | Acceptance |
|---|---|---|
| F-1 | `get(kind='random', view='slug')` and execute every `Next:` line verbatim (same probe as B-9). Round 1 also found the dispatcher's "args= key not accepted" error self-contradictorily lists top-level kwargs (`args`, `view`) as if they were dict keys; verify the wording is fixed too: `get(kind='random', view='slug', args={'len': 5})` should either succeed or fail with an error message that distinguishes top-level kwargs from `args=` dict keys. | Trailer runnable verbatim. Error wording, if any, distinguishes kwargs from args-dict keys. |
| F-2 | Re-run the B-6 hint sweep but **expand the natural-English vocabulary**: `q='cells'`, `q='photocatalysis'`, `q='ecology'`, `q='memo'`, `q='context'`, `q='topic'`, `q='link'`, `q='tag'`. None of these should fire the tag-shape hint. Then verify the actual tag shapes still do: `q='STATUS:done'`, `q='exercise-mcp-throwaway'`, `q='topic:foo'`. | Hint fires on tag-shaped only; does not fire on common nouns/verbs. |
| F-3 | The B-11 probe doubles as F-3 verification. The maintainer either kept the warmup but moved it off the asyncio thread, or reverted to lazy-load. Either way, **the very first call after `WaitForMcpServers` must not block subsequent calls**. Fire three cheap calls (`get` skill / `toc` / `precis-help`) back-to-back within 10 seconds of connecting; all three should return in < 5 s each. | No call wedged ≥ 60 s. |
| F-4 | Same as B-8. Round 1's fix was supposed to land but didn't reach the `edit` code path — verify it does now. Also probe `delete(kind='markdown', id='x')` and `put(kind='markdown', text='hi', mode='create')` for symmetry: each should be `Unsupported`-with-PRECIS_ROOT, not `NotFound`. | All three error paths return `Unsupported` + named env var. |
| F-5 | `precis-edit-help` no banner (overlap with B-1), AND `precis-edit-help` does not appear in TOC `## Hidden` (overlap with B-2). The maintainer needed a second pass through the kind-gate logic to handle `applies-to:` lines naming real-but-unwired kinds for verb skills. | Both probes clean for `precis-edit-help`. |
| F-7 | Call **every verb with no `kind=`**: `get()`, `search()`, `put()`, `edit()`, `delete()`, `tag()`, `link()`. Round 1 found `put()` leaked a raw pydantic dump including the `https://errors.pydantic.dev/...` URL instead of `[error:BadInput]`. | Every response is the canonical `[error:BadInput]` envelope with `next:`. No pydantic URLs, no `validation error for putArguments`, no raw stack traces. |

### Round-2 findings the maintainer did NOT address this cycle

(These were the picky pass's findings F-6, F-8, F-9, F-10. The
maintainer chose to defer them. You don't need to probe whether
they're fixed — they're not — but you should note any *worsening*
of these areas and flag any new related findings.)

- **F-6**: `search(kind='skill', q='')` returns `BadInput` instead of degrading to a list, inconsistent with `search(kind='memory', tags=[...])`.
- **F-8**: cross-kind search hides per-kind hit counts.
- **F-9**: `precis-paper-help` (or whichever paper-search trailer) has a `q='cells' + ' <salient term>'` Python-flavoured pseudo-code suggestion.
- **F-10**: `precis-edit-help` weighs in around 7 KB.

## Picky reviewer stance

You are not here to be charitable. Specifically:

- **No benefit of the doubt.** If a response looks fine on the
  surface, look harder. Does it cost more tokens than it should?
  Is the formatting consistent across kinds? Does the `Next:`
  trailer rank the right option first?
- **Quote verbatim, always.** "The error was confusing" is not a
  finding. "The error said *'<exact text>'*, but a 7B agent would
  read that as X when the correct interpretation is Y" — that's a
  finding. Every finding must carry verbatim text.
- **Raise the severity bar.** LOW is reserved for *genuine
  nitpicks* (a typo, an em-dash where an en-dash belongs). If you
  would care as a maintainer, it's MEDIUM. If a 7B agent would
  fail because of it, it's HIGH or BLOCKER.
- **Run every Next: line verbatim.** The previous round found
  that the `random/slug` trailer suggested its own broken call.
  This is now a *pattern probe* — wherever you read a `Next:`
  trailer with executable suggestions, paste each one back and
  see if it works. Brokenness here is BLOCKER.
- **Run heuristics on realistic input.** Heuristics that fire on
  edge cases are forgivable. Heuristics that misfire on the most
  common shape of input on the planet — like the B-6 tag-shape
  detector firing on `'cells'` — are HIGH at minimum. Sample
  natural inputs from `precis-search-help`'s own examples; if
  the help skill teaches a shape and the runtime warns against
  it, that's a contradiction.
- **Test missing-kwarg envelopes.** Round 1 round-2 found
  `put()` leaked a raw pydantic exception. Probe every verb
  with omitted required args; any of them returning anything
  other than `[error:BadInput] … next: …` is a finding.
- **Test second-order navigation.** Not just "does the first
  call work" — does the suggested `Next:` actually work? Does
  the help skill's referenced sibling skill exist? Does an
  error's `options:` retry actually succeed?
- **Watch token cost.** When a single `get` returns 4+ KB of
  response, that's a problem for an agent budgeting context.
  Flag it. Note byte counts when they matter.
- **Watch consistency.** Memory vs. todo vs. fc — do their
  search hits format identically? Do their error breadcrumbs
  use the same vocabulary? Drift between near-twins is a
  finding. (Recall: F-6's spirit is "consistency across paths.")
- **Verify the *quality* of help skills**, not just their
  existence. Pick three help skills, **run the examples
  verbatim** and report what breaks.
- **Look for asymmetric verbs.** Does `tag(... add=[...])`
  accept the same vocabulary that `put(... tags=[...])` does?
  Does `link(... mode='remove')` actually remove what was
  added? Run round-trips.

## Ground rules

- **Do not modify pre-existing user data.** Tag throwaway memory
  refs `topic:picky-r2-throwaway` (lowercase open-tag form; the
  closed `STATUS:` axis is rejected on memory by B-3). Clean up
  everything you create at the end.
- **You may file ONE gripe**, for B-5 ack verification only.
  Do not file additional gripes — they're write-only and cannot
  be retracted. Round 1 broad pass left a permanent stray
  gripe; round 1 picky avoided that. Match the picky standard.
- Aim for **~30–45 tool calls** total. The combined fix list
  is bigger than round 1, so the budget is slightly higher;
  use the headroom on the pattern probes, not on redundant
  re-runs.

## Phase 0 — first-move plan from native surface only

(Same as before — without reading further, describe what the
precis tool descriptions + `initialize.instructions` would push
you to do first. The B-12 fix changed the tool descriptions
between rounds; we want to see whether the new wording continues
to point at the discovery path cleanly, OR whether the rewrite
has introduced ambiguity. Quote the verbatim text that informed
your plan.)

## Output

Your final response is the report. Use this structure:

```
# Precis-MCP usability findings — picky pass (round 2)

## Fix verification

### Round-1 fixes
| # | Verdict | Notes |
|---|---|---|
| B-1 | ✅ / ⚠️ / ❌ | … |
| B-2 | … | … |
…

### Round-2 fixes
| # | Verdict | Notes |
|---|---|---|
| F-1 | … | … |
…

## First-move plan (from native surface only)
(Phase-0 plan. Did the round-2 wording changes affect it?)

## Findings — new in this pass
(Numbered, severity-tagged, verbatim quotes. Bar: would a maintainer care?
LOW only for true nitpicks.)

## Findings — broken pattern probes
(Specifically: every Next: line you found that doesn't run; every
heuristic that fires on realistic input; every missing-kwarg path
that leaks pydantic / stack traces; every help-skill example that
diverges from runtime behavior.)

## Findings — broad-pass regressions
(Anything from B-1…B-13 that is still broken after round 2's fixes.)

## Findings — picky-pass round-1 regressions
(Anything from F-1…F-7 that is still broken after round 2's fixes.)

## What still works well
(Keep this honest. If everything has an asterisk, say so.
Things working well in round 1 should still work — call out anything
that *was* clean and is now degraded.)

## Coverage + cleanup
```

## Cleanup

Reap every memory you tagged `topic:picky-r2-throwaway`. Confirm
`search(kind='memory', tags=['topic:picky-r2-throwaway'])` returns
empty. The one B-5 verification gripe is permanent — note its id
so the maintainer can grep it out of the human-triage queue.
