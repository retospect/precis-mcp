# asa ops residuals — OAuth cutover, Slack smoke, outbound durability

Three asa post-deploy residuals, merged from asa-oauth-cutover /
asa-slack-smoke / asa-outbound-durability.

- **OAuth / run-as cutover (ops).** asa_bot's vault fallback shipped
  (mirrors precis's `utils/claude_oauth`); the live cutover is an ordered
  ops sequence — seed vault → verify → flip run-as → scope vault read →
  retire hermes — not yet applied.
- **asa-slack live smoke (ADR 0062).** Code shipped + deployed + connected
  (`com.asa.slack` on melchior); remaining is the manual smoke: threading
  (never posts to channel root), a paper-search question actually works, a
  "kick off a job" request is refused with `Unsupported` (not just declined
  in prose), a repeat message from the same person shows the per-person
  memory note. Note: prompt/config (SOUL/HINTS) changes still need the full
  48-asa-slack.yml / 31-asa-bot.yml run from a grimoire-checkout controller.
  Owner `src/asa_slack/`.
- **Outbound durability + conv-capture verification.**
  `src/asa_bot/bot.py::_handle_outbound` never stamps meta.status='sent' and
  pg_listen has no startup sweep of pre-existing queued rows — messages
  queued during an outage strand permanently (~65 from the 07-24→26 stall).
  Decide re-post recent vs discard stale; a blind sweep floods Discord.
  conv capture silently stopped 2026-06-27: no kind='conv' rows despite
  POST /capture → 200 and no fallback jsonl. Verify after the next asa
  Discord turn (post double-build fix + monorepo cutover); if still broken,
  trace the shim's write path. Owner `src/asa_bot/`,
  `src/precis/handlers/conv`.
