precis-kinds-disabled-help.md — Check what's actually live in this build — drift — `get(kind='skill', id='precis-help')` has no matching file under `data/skills/` (likely meant `precis-overview`, referenced on the next line).
precis-folder-help.md — Cross-refs — drift — lateral links use a plain "Cross-refs:" paragraph (not a `## See also` heading) and backtick names, not `[[wikilinks]]`; out of this batch's rewrite scope but won't be picked up by the slice-1 wikilink/graph scan as-is.
precis-health-digest-help.md — Related skills — drift — heading is `## Related skills`, not `## See also`; entries are backtick names, not `[[wikilinks]]`; same graph-scan gap as precis-folder-help.
precis-inner-life-help.md — Related skills — drift — same `## Related skills` (not `## See also`) + backtick-not-wikilink pattern.
precis-figure-help.md — Call sequences — pin — put/get/edit/delete/link sequence for kind='figure' is a clean end-to-end round-trip candidate.
precis-files-help.md — How do I create or rewrite a file? — pin — create/append/replace/delete write-mode recipes are the high-traffic cross-cutting file-write surface.
precis-finding-help.md — Register a finding so the worker can chase its source — pin — put() output shape (`created finding id=42  pub_id=ab12c3`) verified against precis/handlers/finding.py; good exact-output pin.
precis-fix-gripe-help.md — Slice-5 canonical pattern — pin — todo-with-meta.executor → dispatch-worker-mints-job is the canonical end-to-end recipe for the whole job substrate.
precis-gripe-help.md — File a bug I just noticed — pin — put() output line (`created gripe id=42 (STATUS:open)`) verified verbatim against precis/handlers/gripe.py.
precis-job-help.md — Re-submit a failed job — pin — `put(kind='job', id=<id>, mode='retry')` shape verified against precis/handlers/job.py; worth pinning both the plain retry and the model-swap variant.
precis-material-help.md — The canonical-unit rule — pin — rejection message shape verified against precis/handlers/material.py; exact wording is part of the contract (no conversion).
precis-i2c-help.md — Capture it — pin — nested components/nets/connections put() payload for kind='pcb' is intricate enough that a round-trip pin would catch drift early.
