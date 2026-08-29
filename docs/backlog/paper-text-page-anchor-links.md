# MCP paper-read still leaks Marker `[N](#page-M)` link form

The Marker page-anchor fix (shipped 2026-08-06: `strip_page_anchor_links()`
in `precis/utils/mentions.py`, wired into ingest `marker.py`, draft-reader
`linkify.py`, and claim-page `claim_render.py`) never covered the MCP
agent-facing paper-read path. `src/precis/handlers/_paper_text.py` scrubs
only the `<span id="page-N-M"></span>` span form (`_PAGE_ANCHOR_RE`, applied
at the `_scrub_block_text` / `_render_block_body` sites) — never the
`[N](#page-M)` markdown-link form. So `get(kind='paper', …)` still shows raw
`[11](#page-5-0)` in already-ingested chunks. Future ingests are clean for
every consumer (the ingest fix); only pre-fix stored text leaks, only on
this read path. Severity low — agent-read hygiene, not correctness. The
root-cause dossier that found it called it a sibling ticket.

Fix (in-reach): call the already-shipped `strip_page_anchor_links` alongside
each `_PAGE_ANCHOR_RE.sub` in `_paper_text.py`.

Explicitly NOT in scope: corpus DELETE+re-INSERT remediation of stored raw
text (chunks are append-only; a separate optional data-policy call — the
read-path scrub makes display correct without it).

Test: regression test on `_paper_text` rendering a chunk containing
`[11](#page-5-0)` → `[11]`; container-only (host skips the paper extras).
