# Precis-MCP usability review — picky pass

You are reviewing the **precis** MCP server's tool surface as a
**ruthlessly picky senior reviewer**. Your job is to find problems
other reviewers missed.

You have access to the `precis` MCP server plus the built-in tools.

## Before you start

`WaitForMcpServers` first — the server cold-starts in docker (~2–5 s
with the embedder background-warming). Block on `connected=precis`,
then begin.

## Context — what changed since the last pass

A broad pass on 2026-05-30 logged 13 findings against this server.
The maintainer has applied fixes for each. Your job is partly to
**verify the fixes hold up**, partly to **find what the broad pass
missed**. The fix list (quote the verbatim symptom when probing):

1. Verb-help skill banners (`precis-get-help`, `precis-put-help`,
   `precis-search-help`, `precis-edit-help`, `precis-delete-help`,
   `precis-tag-help`, `precis-link-help`) previously carried
   `"this skill documents kind='get' which is **not wired**"`. Fixed
   by treating the seven verb names as non-kinds in the slug-derived
   gate. **Probe each verb-help skill — confirm no false banner.**
2. The TOC's "Hidden (kind not wired or status: planned)" section
   previously listed all seven verb-help skills. Same fix.
   **Probe the TOC.**
3. `STATUS:foo` on `kind='memory'` previously fired the value-check
   error (`invalid STATUS value`) before the axis-check error
   (`STATUS: axis not allowed on kind 'memory'`). Order swapped.
   **Probe `put(kind='memory', text='x', tags=['STATUS:doing'])` —
   expect axis error, not value error.**
4. `put(kind='gripe', tags=[...])` previously accepted tags silently
   despite `precis-gripe-help` saying no. Fixed via `spec.supports_tag`
   gate on the put-create path. **Probe — expect BadInput rejecting
   `tags=`. Also probe `put(kind='gripe', link=...)` for symmetry.**
5. `put(kind='gripe', ...)` ack previously read `created gripe id=N`
   with no signal that the write was irreversible. Now names the
   write-only constraint. **Probe — confirm the ack mentions it.**
6. `search(q='exercise-mcp-throwaway')` (tag-shaped string, no kind,
   no tags=) previously returned semantic garbage. Now emits a HintBus
   tip pointing at `tags=`. **Probe with a tag-shaped `q=` and confirm
   the hint fires; probe with normal prose and confirm it does not.**
7. `search(kind='memory', tags=[...])` previously required a non-empty
   `q=`. Now degrades to a recency-ordered list. **Probe — confirm
   list mode works.** Also confirm `search(kind='memory', q='', tags=[
   'really-bogus-tag-12345'])` returns "no memory entries tagged …"
   without crashing.
8. `edit(kind='markdown', ...)` previously returned `NotFound: unknown
   kind` when markdown was registered-but-disabled (no `PRECIS_ROOT`).
   Now returns `Unsupported` with the missing precondition named.
   **Probe and inspect the error class + breadcrumb.**
9. `get(kind='random', view='slug')` previously returned a bare
   4-char token. Now appends a `Next:` trailer with example uses.
   **Probe.**
10. `search` errors for unknown kinds previously listed only the
    verb-supporting kinds with no labelling. Now the `next:` line
    says `"options above are kinds that support verb='search'"`.
    **Probe `search(kind='nonsense-xyz', q='whatever')`.**
11. First semantic search previously timed out at 120 s while
    bge-m3 cold-loaded. Now warmed in a background thread at server
    boot. **First call may still be slow if the agent races the
    warmup, but should not time out.**
12. Verb tool descriptions previously suggested brittle
    `search(kind='skill', q='get <kind>')` shapes. Now suggest
    natural-language queries. **Inspect the loaded tool descriptions
    via `mcp__precis__get` etc. and confirm the new wording.**
13. (Echo of #7.) Same as #7.

## Picky reviewer stance

You are not here to be charitable. Specifically:

- **No benefit of the doubt.** If a response looks fine on the
  surface, look harder. Does it cost more tokens than it should? Is
  the formatting consistent across kinds? Does the `Next:` trailer
  rank the right option first?
- **Quote verbatim, always.** "The error was confusing" is not a
  finding. "The error said *'<exact text>'*, but a 7B agent would
  read that as X when the correct interpretation is Y" — that's a
  finding. Every finding must carry verbatim text.
- **Raise the severity bar.** LOW is reserved for *genuine nitpicks*
  (a typo, an em-dash where an en-dash belongs). If you would care
  as a maintainer, it's MEDIUM. If a 7B agent would fail because of
  it, it's HIGH or BLOCKER. The previous pass had four LOWs — that's
  too many; either they were really MEDIUM or you're scraping.
- **Test second-order navigation.** Not just "does the first call
  work" — does the suggested `Next:` actually work? Does the help
  skill's referenced sibling skill exist? Does an error's
  `options:` retry actually succeed?
- **Watch token cost.** When a single `get` returns 4+ KB of
  response, that's a problem for an agent budgeting context. Flag
  it. Note byte counts when they matter.
- **Watch consistency.** Memory vs. todo vs. fc — do their search
  hits format identically? Do their error breadcrumbs use the same
  vocabulary? Drift between near-twins is a finding.
- **Verify the *quality* of help skills**, not just their existence.
  Does each `precis-<kind>-help` skill name every required arg?
  Every supported view? Every error path? Are the examples
  runnable as-is? Pick three help skills, **run the examples
  verbatim** and report what breaks.
- **Look for asymmetric verbs.** Does `tag(... add=[...])` accept
  the same vocabulary that `put(... tags=[...])` does? Does
  `link(... mode='remove')` actually remove what was added?
  Run round-trips.
- **Probe the empty / boundary cases.** What does
  `get(kind='skill')` (no id, no q) do? `search(kind='skill',
  q='')`? `put(kind='memory', text='')`? `delete` without `id=`?
  These shouldn't crash with `Internal`.

## Ground rules

- **Do not modify pre-existing user data.** Tag throwaway state
  `STATUS:exercise-mcp-throwaway` is *no longer rejected* (post-fix
  #3) on memory because the axis error fires first — use the
  lowercase open-tag form `topic:picky-throwaway-2026-05-30`
  instead. Clean up everything you create at the end.
- The previous pass left a permanent gripe (id=222 at the time)
  because gripe is write-only — don't repeat that. The fix in #5
  surfaces the irreversibility on the ack; **respect it** and do
  not file any new gripes during this pass.
- Aim for **~30–40 tool calls** total. Coverage matters but ranking
  the findings by sharpness matters more.

## Phase 0 — first-move plan from native surface only

(Same as before — describe what the precis tool descriptions +
`initialize.instructions` would push you to do first, without
having read any help skill yet. Reuse this section verbatim from
the previous pass shape; we want to see if the new tool
descriptions in fix #12 change the plan.)

## What to exercise

The 13 fix probes above are the *floor*. Beyond that:

- **Help-skill content quality.** Pick three `precis-<kind>-help`
  skills at random. For each: enumerate its examples; run each
  example verbatim; note every drift between example and outcome.
- **Tag/link round-trips.** Pick a kind, `put` it, `tag` it,
  `search` for it with `tags=`, then `tag(remove=)` and confirm
  `search` no longer matches. Same for `link`/`link(mode='remove')`.
- **Empty-input behavior.** Hit every verb with one missing-required
  arg and one empty-string required arg. Confirm no `Internal`.
- **Cross-kind search shape.** `search(kind='*', q='something')` —
  inspect the merge, confirm the per-kind hit counts and ranking
  make sense. Compare against `search(kind='paper,memory', q=...)`.
- **`get(kind='skill')`'s `Next:` trailer.** The previous pass
  praised this as "well-formatted with six pre-canned Next: lines".
  Now: does each one actually do what it claims? Run all six.
- **Cold-start regression check (#11).** As soon as the connection
  is up, call a semantic-search verb (`search(kind='skill', q='how
  do I save a note for later')`). Time it. Was the warmup enough
  to avoid the 120-s timeout?
- **HintBus deduplication (#6 follow-up).** Make the tag-shaped
  query twice in quick succession. The second one should be
  suppressed (cooldown). Then make it again on a different topic
  — the original hint should re-fire if cooldown has expired.
  Confirm or document the behavior.

## Output

Your final response is the report. Use this structure:

```
# Precis-MCP usability findings — picky pass

## Fix verification (13 probes from the broad pass)

| # | Verdict | Notes |
|---|---------|-------|
| 1 | ✅ / ⚠️ / ❌ | … verbatim quote when the fix is wrong / partial |
| 2 | … | … |
…

## First-move plan (from native surface only)
(Phase-0 plan. Did the tool-description rewrites in fix #12 change
your first move?)

## Findings — new in this pass

(Numbered, severity-tagged, verbatim quotes. Bar: would a maintainer
care?)

## Findings — broad-pass regressions

(Anything from the previous pass that's still broken or only
half-fixed, beyond the fix-verification table.)

## What still works well
(Keep this honest. If everything has an asterisk, say so.)

## Coverage + cleanup
```

## Cleanup

Reap every ref you tagged `topic:picky-throwaway-2026-05-30`.
Confirm `search(kind='memory', tags=['topic:picky-throwaway-2026-05-30'])`
returns empty. If anything else escaped (e.g. a stray gripe — see
ground rules above; you should not have filed any), note it.
