---
status: draft
title: POST /papers/{ref_id}/edit with a cite_key change hangs the request
model: sonnet
---

# Paper-edit re-slug hangs the request

Live repro (2026-08-17, prod): `POST /papers/211494/edit` with metadata
fields **plus** `cite_key=rupp20` applied the metadata half (title /
year / DOI landed via the edit dispatch, visible in `refs` +
`ref_identifiers` with `source=edit`) but the HTTP response never came
back — the curl hung >12 minutes and was killed; the cite_key stayed at
the auto-generated `anon20h`, so the rename half never committed.

Suspect: the `_rename_slug` path after `await_dispatch("edit")` in
`src/precis_web/routes/papers.py` (`edit` handler) — it re-slugs and
moves the PDF on disk (NAS), and something there blocks without a
timeout. Metadata-only edits on the same ref return promptly.

Work:

1. Reproduce against dev with a slug-only edit; find the blocking call
   (disk move on the NAS mount? a lock held by the watcher that just
   ingested the file? a second dispatch that never resolves?).
2. Bound it: the rename path needs a timeout + error surface instead of
   an indefinite hang; a half-applied edit (meta yes, slug no) should
   say so in the response.
3. Clean up the repro artifact if desired: ref 211494 ("48 Years of
   Microprocessor Trend Data", the Rupp Zenodo dataset) still carries
   `cite_key=anon20h` — retry the rename once fixed.
