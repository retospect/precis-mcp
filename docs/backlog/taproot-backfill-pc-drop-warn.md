# taproot backfill silently drops an unresolvable [pc] handle on promote-collapse

In `_plan_group` a `[pc1][pc_bad]` run where pc_bad raises BadInput collapses
to a single `[fi<hub>]` with pc_bad's cite intent vanishing unsignalled.
Lower severity than the [pa] arm (collapse-to-one-hub is the intended promote
semantics and pc_bad has no citeable evidence), but needs a skip-vs-warn
design call: extend the [pa] arm's `len(supporters) < len(group.handles)`
guard to the [pc] path, or emit a note listing dropped handles. Found in
review of 6fd7a004. Owner `src/precis/taproot/backfill.py`.
