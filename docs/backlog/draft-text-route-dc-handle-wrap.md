---
status: draft
title: /drafts/{ident}/text wraps dc-handles into an unresolvable ¶dc<id> address
model: sonnet
---

# Inline-edit route breaks on universal dc-handles

Live repro (2026-08-17, prod): `POST /drafts/173020/text` with
`handle=dc2445897` returns
`[error:NotFound] draft chunk '¶dc2445897' not found` even though
`store.drafts.get_draft_chunk("dc2445897")` (the same route's own
pre-check) resolves it fine. Cause: the route unconditionally builds the
edit-verb address as `id=f"¶{handle}"` (`routes/drafts.py`,
`edit_text_inline`), which for a universal `dc<chunk_id>` handle
produces `¶dc2445897` — parsed by `_CHUNK_ADDR` as a *legacy base58*
handle `dc2445897`, which matches no `chunks.handle` row. Workaround
used live: look up the chunk's real base58 `chunks.handle` (`YwhJYv`)
and post that instead.

Fix: pass the handle through unwrapped when it already parses as a
universal `dc<id>`/`pe<id>` address (the edit verb accepts both forms);
only prepend `¶` for bare base58. One-line conditional plus a test
posting a dc-handle through the route.
