# A literature-only quest still runs the compute lane

> The open remainder of a two-part defect found 2026-08-14 in spark's worker
> log. The other part — candidates dying silently, with no feedback to the
> proposer — shipped as the `rejected proposal` observation in
> `precis.quest.compute`, and its backlog item is deleted.

## Symptom

Quest 202469's own dossier declares its mode: *"Literature tracking and
synthesis; no compute lane, no simulations, no frontier entries."* It proposes
structure candidates anyway — every tick that reaches the compute step — and
those proposals were what filled the log with `ensure_candidate` warnings
(22 dropped candidates across three days: 13 on a `slab` arg shape, 7 on
preflight geometry, 2 on unknown ops).

The drops are no longer silent — the proposer now gets a `rejected proposal`
observation naming the reason, so it can self-correct. But a quest that
declared it has no compute lane should not be spending tick budget proposing
candidates at all, correct spec or not.

## Why it matters

Now that rejections are visible, the model will start *fixing* these specs.
That is worse, not better, for a literature-only quest: it converts wasted
proposals into successfully-minted candidates, each of which dispatches a
relax (and, on a reaction quest, an autocatpath run) — real cluster spend on a
lane the quest said it didn't want.

So this item's cost went up when the other half was fixed. It should be
settled before the next batch of ticks lands on a quest with a declared mode.

## The decision it needs

**Where does "mode" live?** Today it is prose inside the dossier narrative —
not a field, not a tag, nothing the tick can branch on. Options:

1. **A closed-axis tag on the quest ref** (`MODE:literature` / `MODE:compute`),
   read by the tick before the compute step. Cheapest, and consistent with how
   the rest of the system gates behaviour.
2. **A `meta.compute_lane` boolean**, set by whatever seeds the quest. More
   precise, but only the seeder knows to set it — the existing quests would all
   need backfilling.
3. **Derive it** — a quest with no `meta.reaction_config` and no candidates
   arguably has no compute lane already. Zero new surface, but it silently
   flips the moment someone adds a reaction config, and it can't express
   "compute deliberately off".

(1) is the recommendation; (3) is a trap worth naming so nobody reaches for it
as the "free" option.

Whichever wins, the gate belongs before the proposal step in
`precis.quest.tick`, not inside `ensure_candidate` — refusing a well-formed
candidate at the storage layer would be the wrong place to express a policy
about which lanes a quest runs.

## Verify

A literature-mode quest should log no `rejected proposal` observations and mint
no `relax` / `autocatpath` jobs:

```
ssh spark "grep -c 'rejected proposal' /var/log/precis-worker.log"
```

## Related

- `docs/backlog/quest-frontier-tree-seed-indistinguishable-from-empty.md` — the
  empty frontier on this quest is now explained; that item is back to being the
  cosmetic one it looked like.
