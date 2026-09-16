# worktree-shiny-toasting-bunny — continuation prompt

Session of 2026-09-15: the se **fastening** campaign (rungs 2c/3b/3c of
`docs/backlog/se-off-the-shelf-fabrication.md`). Shipped `86dd66b0`
(feature, deployed to the cluster) and `94ceb129` (three mutation-survivor
tests, test-only so undeployed). The worktree ends clean and level with
`origin/main`.

Paste the second block as the first message of a fresh session; run the
first one before compacting if the transcript is still live.

## 1 — `/compact` retention argument

```
/compact Keep: the decisions this session locked and why — hex-socket+Torx only, parametric-from-standards over imported 3D models, stock as a live supplier number with the curated tier as its offline prior, and the rule that a *choice* (printed thread strategy, counterbore) is reported rather than stamped while a *requirement* (countersink) is stamped. Keep the verification state: full gate green at 20414 tests, diff coverage 93%, cluster deployed at 86dd66b0, Digi-Key adapter never exercised against a live response. Preserve branch/worktree names verbatim. Drop tool-output dumps and file contents — everything else this session produced is on disk and named in the recovery prompt.
```

## 2 — recovery prompt

```
Resuming after /compact. Reorient, then continue.

**Where:** worktree `/Users/reto/precis-mcp/.claude/worktrees/shiny-toasting-bunny`
on branch `worktree-shiny-toasting-bunny` (clean, level with origin/main).

**Goal:** finish the tail of the se fastening campaign — the part that needs
prod access and credentials rather than more code.

**Done so far:**
- `86dd66b0` shipped + deployed: eleven new ISO fastener families (hex socket
  and Torx, including ISO 14585/14586 Torx tapping screws), migration 0163,
  printed thread strategies, head-form features, tool access, the stock port.
- `94ceb129` shipped (test-only): killed the three real mutation survivors.
- Decisions and the supplier survey are recorded in the backlog item; the
  campaign state, traps and unverified claims are in project memory.

**In flight:** nothing. The tree is clean.

**Next logical steps:**
1. Bolt `unicycle-mk2` together in prod — it still has one fastener-less
   `screw` joint (`saddle.rail—seatpost.top`). Needs the `precis` MCP, which
   was disconnected for the build session. Follow the worked example in
   `tests/test_se_fasten_seatclamp.py`.
2. Register a Digi-Key developer app, set `PRECIS_DIGIKEY_CLIENT_ID` /
   `PRECIS_DIGIKEY_CLIENT_SECRET`, then run
   `get(kind='component', id='iso-4762-m4x12', view='stock')` — the FIRST
   real call is the acceptance test for `precis/supply/digikey.py`, whose
   field names came from documentation, not from a response.
3. Have Reto read the `stocking` tiers in `component_series.json` — they are
   a curated judgement about his drawer that he has not seen.
4. Optional: apply for JLCMC API access (the mechanical catalogue with real
   fastener depth; they review order history).

**Re-read to reground:** `docs/backlog/se-off-the-shelf-fabrication.md`
(especially "Left open by the 2026-09-15 build" and "Stock as a selection
signal") · skill `precis-se-fasten-help` · `src/precis_se/fasten.py` and
`src/precis/thread_forming.py` docstrings · project memory
`fastening-campaign-0915.md`.

**Watch out:**
- A `screw` connect must name the **screw block** as one endpoint
  (`a='bolt.thread'`), not the two members it joins.
- A member whose mode says it is printed and declares no `thread_strategy`
  now gets **no far-end hole** — that is deliberate, not a bug.
- Editing an already-applied migration leaves a checksum mismatch: drop this
  worktree's `precis-test-db` (and its gate cache) before re-running.
- Two gate slots for the whole machine; sibling worktrees can hold both for
  an hour. Queue, don't churn.

Start by reading the "Re-read to reground" pointers, then do step 1.
```

Copy the `/compact` line → run it → paste the recovery block as your first
message.
