# finding.edit dry_run preview — redo against the three-op surface

The paper/cfp/datasheet dry_run-preview arms shipped; the finding arm was
deferred because `finding.edit` became a three-op surface (pick_candidate /
title= retitle / unacquirable_note=) that deliberately rejects dry_run.
Design call: give pick_candidate a real no-write preview (the validated
picked_link/other_links computation is faithful — it rewrites links + flips
status); title= keeps rejecting (a retitle has no preview); unacquirable_note=
previews its meta patch or keeps rejecting. A test scaffold existed
(test_finding_edit_dry_run_previews_pick_and_does_not_write). Owner
`src/precis/handlers/finding.py::FindingHandler.edit`.
