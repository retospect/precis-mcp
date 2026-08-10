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

- **2026-08-10** — full sweep (issue-closer agent) over 66 open (gr197709
  skipped — closed separately by the caller; 2 new gripes, gr202117 +
  gr202116, filed mid-pass, landing the end count at 67). **Closed 0.** Every
  shipped-claiming or ambiguous gripe either already carried a prior pass's
  accurate partial-fix/leave-open comment (gr196953, gr171931, gr175799,
  gr171782, gr180155, gr171431, gr192827, gr187627, gr191125) or turned out
  unshipped/deploy-pending on fresh verification: gr162141 (OpenAlex balance
  — needs a product decision, not a diff), gr55762 (draft-reader storage cost
  — fix options never implemented since base 162d56e9), gr192371 (autocatpath
  GPU-slot crash — code fix 47671907 shipped but explicitly deploy-pending
  per its own comment), gr199339 (worker-compute crash-loop — none of its 3
  fix directions landed), gr191673 (ssh_node submit/compute_handle race —
  confirmed still non-atomic in `_run_one`), gr194088 (baseline schema
  missing `cfp` row — confirmed absent in the current snapshot), gr171512
  (`/factory` POST routes — confirmed still no auth middleware), gr180189
  (pa1056 bibtex — instance fixed but stated systemic sweep still open). The
  2026-08-07 pass already cleared the closeable backlog; this pass found
  nothing new to close. 67 remain open.
- **2026-07-29** — cadence established (this runbook +
  `scripts/gripe-gc-review`). Baseline: ~64 open gripes in prod, ~11
  already shipped-marked but unclosed — first real pass deferred to the
  first DUE firing.
- **2026-08-07** — full sweep (issue-closer agent) over 68 open. Closed 4
  total this session: gr196678 (clusterize COPY timeout, verified via
  cluster_runs 415), gr193963 (frozen taproot hubs, all 8 advanced),
  gr196736 (pgpass brew-link conflict, b0c7d74d), plus dup gr194401 found
  already closed by a prior pass alongside gr196635/gr192606. Verified-
  still-unfixed and left open: gr191673, gr194088, gr196720, gr192372;
  partial-fix noted on gr196447. All four watchdog gripes re-checked live
  and still stale (taproot_edges 59.8h, anki_sync ~2.5d overdue,
  briefing_audio 111h silent, embed backlog 99 undrained) — real prod
  stalls, not closeable. 67 remain.
