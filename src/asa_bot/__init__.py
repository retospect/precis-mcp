"""asa-bot — the Discord bridge to Claude Code + precis-mcp.

A single-instance asyncio daemon (``asa-bot`` console script, ``python -m
asa_bot``; ``[asa]`` extra). It owns no data: precis is the memory and the
bridge reaches it over a long-lived ``precis serve`` stdio subprocess
(:mod:`asa_bot.precis_client`). Its sibling ``asa_slack`` is the Slack bridge
over the same router seam.

**Per-turn flow** (:mod:`asa_bot.bot`; one serial queue per Discord thread,
threads in parallel): compute the ``conv`` slug from the Discord context
(:mod:`asa_bot.conv_slug`: ``discord/<guild>/<channel>/<thread>``, DMs
``discord/dm/<user>``); capture the user message as a ``conv`` ref; build the
4-tier preamble from precis in parallel (:mod:`asa_bot.preamble`: hot recent
turns, warm keyword digest, sticky memories, last-turn anomaly signal); invoke
the LLM (:mod:`asa_bot.claude_invoke`: a concrete claude id streams
``claude -p`` through ``dispatch_async``; the ``local`` sentinel, the deployed
default, walks the ``llm.chain.big`` placement chain via sync ``route``);
stream progress and post the reply split for Discord's size limit. The
assistant half of the conversation is captured by Claude Code's Stop hook
POSTing to :mod:`asa_bot.capture_shim`, which relays through the precis client
and, with precis down, spools to a fallback JSONL replayed on recovery.
Text-prefix slash commands (:mod:`asa_bot.slash`) are intercepted before any
LLM call.

**Inbound from the cluster.** :mod:`asa_bot.pg_listen` holds a dedicated
connection ``LISTEN``ing on ``precis.cron`` (a due scheduled tick with
``meta.deliver``: synthesize a user message, run a turn, post the result) and
``precis.messages`` (post a ``message`` ref's text and attachments, stamp
``meta.status='sent'``). NOTIFY is not durable; multi-part briefings are
therefore reassembled and posted in order by
:mod:`asa_bot.briefing_buffer`, with a per-set timeout so a dropped part cannot
stall delivery.

**Seams.** :mod:`asa_bot.config` (defaults, then YAML at ``$ASA_CONFIG``, then
selected ``ASA_*`` env overrides; frozen). :mod:`asa_bot.secrets` reveals
secrets from the precis DB vault over the DSN the NOTIFY listener already
holds (env override wins; best effort). :mod:`asa_bot.oauth` bootstraps the
Claude Code OAuth token into a subprocess env. :mod:`asa_bot.llm_runtime`
warms the in-process router runtime — every bridge must call
``warm_runtime`` before its first dispatch, or chain resolution reads dark and
silently falls back to the default claude chain. Deploy role:
``deploy/roles/asa_bot``.
"""

__version__ = "0.1.0"
