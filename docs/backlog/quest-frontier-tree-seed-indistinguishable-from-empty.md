# Frontier-tree seed text is byte-identical to "rendered, no candidates"

> Minor observability wart. Filed 2026-08-13 because it cost a full
> false-positive investigation — an earlier version of this file claimed
> `update_frontier_tree` was broken. It is not.

## The ambiguity

`precis/quest/dossier.py::_FRONTIER_TREE_SEED` is `"_(No candidates yet.)_\n"`.
`precis/quest/frontier.py::render_frontier_tree` returns **the same string** on
its no-candidates path.

So a pinned `frontier-tree` chunk holding that text is indistinguishable, by
inspection, between:

- never written since seeding (a real bug), and
- regenerated every tick, correctly, on a quest that has no candidates yet.

Worse, `edit_text` with identical text is a no-op, so even `updated_at` does not
separate the two.

## What the data actually says

Prod, 2026-08-13 — chunk length against the count of live `structure` refs
linked `serves` → owner quest:

| dossier | owner | tree_len | candidates |
|---|---|---|---|
| 202546 | 202469 | 23 | 0 |
| 202529 | 202468 | 53 | 1 |
| 202513 | 202467 | 136 | 2 |
| 180329 | 175733 | 6007 | 89 |

Monotone and exact. The update path is healthy; length tracks candidate count.
(Dossier 164905 renders 1079 chars but predates the `meta.dossier_of_owner`
key, so it resolves its owner via the `dossier-of` link and this particular
query reports 0 candidates for it spuriously — not a defect, just a join that
misses older rows.)

## Fix

Make the two strings differ, so the chunk is self-describing. Cheapest version:
give the seed its own wording (e.g. `_(Frontier not yet generated.)_`) and leave
the renderer's `_(No candidates yet.)_` alone. Then seed text present after a
tick genuinely *is* a bug, and can be alerted on.

Consider also querying `chunk_edit_stats` rather than raw text when asking "has
this ever been regenerated" — the edit history distinguishes the cases today,
it is just not where anyone looks first.
