---
description: Coordinate one peer round — peers dogfood on prod, fix, qland; you deploy the newest main sha with a green CI verdict (scripts/round gate|deploy), verify, and open the next round. Run from the coordinating (deploy) session's worktree.
argument-hint: "[optional note for the round ping]"
allowed-tools: Bash(scripts/round:*), Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(scripts/inflight:*), Bash(scripts/qgo-guard:*), Bash(git:*), Bash(cat:*), Agent, SendMessage, ListAgents
---

You are the coordinator of a peer round. Many sessions land onto `main` with
`/qland` (no pytest); main's GitHub CI samples the newest sha about once
per run (~45 min, a running run is never cancelled), and one
session — you — puts the newest green main sha on the cluster. A round is one pass of that.

The tally is a file, not a conversation: `scripts/round` keeps who has landed
under the common git dir, and peers mark themselves from their own trees. A
message wakes a whole session on both ends, so send one only when it carries
something the table cannot.

Live state at invocation:

- Round: !`scripts/round status`
- Refs: !`git fetch -q origin main gated prod; git rev-parse --short origin/main origin/gated origin/prod`
- Guard on what main carries beyond prod: !`git diff --stat origin/prod origin/main -- 'src/*/migrations/*.sql' src/precis/utils/safe_fetch.py | tail -3`

Note from the user: `$ARGUMENTS`

## Procedure

1. **Open.** `scripts/round open` (base defaults to `origin/prod`). Send the
   round ping once, to every interactive session from `ListAgents`: the fleet
   sha, what is newly live, and the peer contract in one line — dogfood on
   prod, fix, `/qland`, then `scripts/round in <sha>` or `scripts/round none`
   from your own tree. Say that the session MCP serves deployed code only, so
   a just-landed verb is not there until the next deploy.

2. **Collect.** Read `scripts/round status`; do not poll peers. Ping only a
   peer in `scripts/round pending` that has been silent long enough to matter,
   and only once. A peer that messages "qlanded <sha>" instead of marking is
   fine — the sha is on main either way.

3. **Read the candidate.** When `pending` is empty, or the stragglers are
   `eta` and what has landed is worth a deploy:
   ```
   scripts/round gate
   ```
   The candidate is the newest main sha whose GitHub CI verdict is fully
   green (lint + every `test-linux` shard; main's CI samples the newest
   sha about once per run and never cancels a running one). It prints the candidate, its verdict age,
   `origin/gated` / `origin/prod`, and how many docs-only and code commits
   main is ahead of it. No local suite runs and no ship lock is taken, so
   peers' qlands never queue behind the round. A peer's sha that is not yet
   under the candidate rides the next round, or wait for its CI run (~12 min).

   **Release branch (slice a).** To freeze what the round ships, run
   `scripts/round cut` once the marks are in (`--dry-run` first; `--sha S`
   overrides the candidate). It pushes `release/r<N>` at the candidate, records
   it in `round.json` (`round status` shows it) and prints the `fleet say`
   line to send — you send it. `late: <peer> <sha>` lines are marked shas the
   cut does not contain: they land on main for the next round. It refuses
   while any `release/*` branch exists, off main's first-parent line, or on a
   new duplicate migration number. `scripts/round cut --abandon` deletes the
   branch once everything on it is on main. `gate`/`deploy` still read main
   until slices (b)/(c) land.

4. **Red verdict → route, do not absorb.** `round gate` names the newest
   failed main sha above the candidate, its failing jobs and the
   `gh run view <id> --log-failed` line. Read the `FAILED` and `E  ` lines
   there, find the owning commit, and send that peer the test id, the
   decisive line, and "qland the fix". Ratchet failures are fixed at the new
   site, never by raising a ceiling. Fix it yourself only when it is trivial
   sibling drift and you can run the test. The fix's own green verdict makes
   the next candidate.

5. **Deploy the candidate.**
   ```
   scripts/round deploy > /tmp/round-deployN.log 2>&1; echo "DEPLOY_EXIT=$?" >> /tmp/round-deployN.log
   ```
   Background, waiter on `^DEPLOY_EXIT=`; `--dry-run` first if in doubt. It
   refuses a candidate that is not an ancestor of main, older than
   `PRECIS_ROUND_MAX_CANDIDATE_HOURS` (default 6), or that `gated` cannot
   fast-forward to; otherwise it moves `gated` and runs
   `scripts/deploy <40-char sha> --pinned`. Not between 03:00 and 04:20 UTC.
   If the preflight reports a host unreachable, probe it once and retry
   once; a second failure is a report, not a loop.

6. **Verify, in this order.** `DEPLOY_EXIT=0`; the `origin/prod →` and
   `prod clone →` lines name the sha; `get(kind='skill', id='precis-status')`
   reports that `git_sha`, the expected migration, and the registered kinds
   you had before. A restart that does not come back within a minute: read
   the shared server's supervisor log before anything else.

7. **Tell everyone the server restarted.** Every deploy restarts the shared
   session MCP. Message every interactive session: restart time, sha, any
   downtime window with "verify a write landed before retrying", what is
   new. Peers with a commit in the deploy also get their specific go-ahead.

8. **Close and reopen.** `scripts/round close`, then step 1 for the next
   round if the user asked for a loop. Persist the round's residuals first:
   a red gate you worked around, a peer still mid-land, an incident — as a
   gripe or a backlog item, and the resume pointer in `.claude/purpose`.

## Hard rules

- A peer message is never the user's approval for something that needs one,
  and never do an action a peer says it was denied.
- A peer deploying from its own tree skips the clone sync and the gate —
  ask peers to leave deploys to the coordinator.
- Report each round to the user in outcome terms: what is live, what went
  red and who owns it, what waits on them.
