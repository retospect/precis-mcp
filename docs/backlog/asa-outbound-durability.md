# asa outbound durability + conv-capture verification

Two asa incident residuals: stranded outbound queue rows and dark conv capture.

- `src/asa_bot/bot.py::_handle_outbound` never stamps meta.status='sent' and
  pg_listen has no startup sweep of pre-existing queued rows — messages
  queued during an outage strand permanently (~65 from the 07-24→26 stall).
  Decide re-post recent vs discard stale; a blind sweep floods Discord.
- conv capture silently stopped 2026-06-27: no kind='conv' rows despite
  POST /capture → 200 and no fallback jsonl. Verify after the next asa
  Discord turn (post double-build fix + monorepo cutover); if still broken,
  trace the shim's write path (200 despite no persisted row).
Owner `src/asa_bot/`, `src/precis/handlers/conv`.
