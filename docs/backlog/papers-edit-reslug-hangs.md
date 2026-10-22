---
status: draft
title: POST /papers/{ref_id}/edit with a cite_key change hangs the request
model: sonnet
pillar: memory-graph
---

# Paper-edit re-slug hangs the request

Live repro (2026-08-17, prod): `POST /papers/211494/edit` with metadata
fields **plus** `cite_key=rupp20` applied the metadata half (title /
year / DOI landed via the edit dispatch, visible in `refs` +
`ref_identifiers` with `source=edit`) but the HTTP response never came
back — the curl hung >12-minute and was killed; the cite_key stayed at
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

**Landed 2026-10-21 (worktree bug-papers-pcb), items 1–2:** the blocking
call is the whole `_rename_slug` body — corpus-mount probes
(`is_file`/`mkdir`/`rename` on the NFS corpus) plus the
`set_ref_identifier` write, which can wait on a row lock; neither carries
an in-process timeout. `edit` and `resolve_duplicate` now await it via
`_rename_slug_bounded` (`asyncio.wait_for`, `_RENAME_SLUG_TIMEOUT_S` = 30s);
a timeout renders the half-applied error page naming the retry instead of
hanging, and the regression test in `tests/precis_web/test_routes.py`
pins it. Item 3 (the ref 211494 retry) is a prod write — Reto's call.
