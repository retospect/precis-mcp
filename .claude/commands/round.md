---
description: Coordinate one peer round — peers dogfood on prod, fix, qland; you gate the integrated main, deploy the gated sha pinned, verify, and open the next round. Run from the coordinating (deploy) session's worktree.
argument-hint: "[optional note for the round ping]"
allowed-tools: Bash(scripts/round:*), Bash(scripts/ship:*), Bash(scripts/deploy:*), Bash(scripts/inflight:*), Bash(scripts/qgo-guard:*), Bash(git:*), Bash(cat:*), Agent, SendMessage, ListAgents
---

You are the coordinator of a peer round. Many sessions land onto `main` with
`/qland` (no pytest); one session — you — settles the debt with a full gate
and puts the result on the cluster. A round is one pass of that.

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

3. **Gate the integrated main.** When `pending` is empty, or the stragglers
   are `eta` and what has landed is worth a deploy:
   ```
   scripts/ship --mutate --full "<message>" > /tmp/go-gateN.log 2>&1; echo "GATE_EXIT=$?" >> /tmp/go-gateN.log
   ```
   in the background, from a script file, with a waiter on `^GATE_EXIT=`.
   Never pipe ship into a filter. No edits in this worktree while it runs —
   ship's final reset destroys them. The full gate holds the ship lock, so
   peers' qlands queue until it exits.

4. **Red gate → route, do not absorb.** Read the `FAILED` and `E  ` lines.
   Find the owning commit (`git diff <prod>..origin/main` on the failing
   path) and send that peer the test id, the decisive line, and "qland the
   fix". Ratchet failures are fixed at the new site, never by raising a
   ceiling. Fix it yourself only when it is trivial sibling drift and you can
   run the test. Re-gate when the fix is on main.

5. **Deploy the gated sha, pinned.**
   ```
   scripts/deploy "$(cat .ship-sha)" --pinned > /tmp/go-deployN.log 2>&1; echo "DEPLOY_EXIT=$?" >> /tmp/go-deployN.log
   ```
   Background, waiter on `^DEPLOY_EXIT=`. Never bare, never the branch name.
   A migration or `safe_fetch.py` in the range is why this is a full gate
   first. Not between 03:00 and 04:20 UTC. If the preflight reports a host
   unreachable, probe it once and retry once; a second failure is a report,
   not a loop.

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
