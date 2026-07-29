# Runbook — gripe-gc (weekly gripe-GC cadence)

A recurring, **weekly** backstop pass that asks: *are there open gripes that
have already been fully shipped and just never got closed?* Cadence is 7
days, enforced advisory-style by `scripts/gripe-gc-review` (surfaced in
`/whatneedsdoing`, next to `token-review` / `db-thrash-review`). It does
**not** run the pass itself — it only tells you WHEN one is due.

## Why this exists (why `issue-closer` isn't enough)

`issue-closer` (a sonnet agent spawned from `/land`/`/go`) inspects the
**single just-shipped commit** and closes any gripe it names as resolved.
That per-ship check misses a real slice of the backlog:

- **Multi-commit fixes** — a gripe resolved across several ships, where no
  single commit's message names it.
- **Pre-existing gripes** — filed before `issue-closer` existed, so no ship
  since has re-touched them even though the underlying bug is long dead.
- **Silent fixes** — the shipped commit that actually resolved the gripe
  doesn't mention it by number/title (a refactor, an unrelated fix that
  happened to cover it, a fix landed via a different flow).

As of the pass that established this runbook, prod carried **~64 open
gripes, ~11 already "shipped"-marked** (title/body says fixed, but never
closed) — proof these slip through. A weekly sweep across *all* open gripes,
independent of any single ship, catches them regardless of how they were
fixed.

## When

`scripts/gripe-gc-review` prints `gripe-gc-review: DUE` when the newest dated
line in this file's `## Log` is >7 days old (or absent). Inside the window
it's quiet. Run the pass when DUE, then append a dated line (below) — that
resets the clock. If prod is reachable read-only, the script also prints a
best-effort count of currently-open shipped-marked gripes (`title ILIKE
'%shipped%'`) as a nicety — offline, it just prints the DUE line.

## The pass

1. `get(kind='gripe', id='/open')` (or the shipped-marked subset the script
   surfaced) — list open gripes whose title/body claims they're shipped.
2. For each: verify the fix is actually on `main` (and ideally deployed) —
   `git log -S'<callsite>'` / `git merge-base --is-ancestor <sha>
   <deployed_sha>` as needed.
3. Close the **genuinely-fully-shipped** ones via the MCP soft-delete
   convention: a one-line resolution comment naming the sha, then
   `delete(kind='gripe', id=N)`. History is preserved (soft-delete).
4. **Do not close** a gripe marked "shipped" whose fix landed **with open
   follow-ups** — those are still real open work, just relabeled; leave them
   open (fix the misleading title if you have time, but don't delete).
5. Append **one** dated line here summarizing the pass (count closed / left
   open and why) — newest first, so the script reads the top.

## Log

- **2026-07-29** — cadence established (this runbook +
  `scripts/gripe-gc-review`). Baseline: ~64 open gripes in prod, ~11
  already shipped-marked but unclosed — first real pass deferred to the
  first DUE firing.
