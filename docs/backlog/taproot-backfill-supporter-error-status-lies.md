# taproot backfill: partial supporter-attach failure sets action="error" but still rewrites prose

Pre-existing (traced to before the atomic-claims build; that build extends
the shape verbatim to the atom loop): in `apply_chunk`, when the hub has
landed and a later `attach_evidence` for `plan.supporters[1:]` raises, the
`except` sets `plan.action = "error"` / note says "prose left as [pc…]" —
but only `not hub_landed` triggers the `continue`, so execution falls
through and the `[fi<hub>]` rewrite is appended anyway. The dry-run/apply
report is factually wrong in that branch (claims prose untouched when it
was rewritten). Fix: either honor the note (skip the rewrite on error) or
report truthfully ("partial: supporters missing, prose rewritten"). Matters
for whoever reads apply reports during the existing-hubs migration pass.
Owner `src/precis/taproot/backfill.py`.
