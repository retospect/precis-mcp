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

**Speaker identity.** :mod:`asa_bot.identity` maps ``message.author.id`` to
the precis account that set that numeric id on ``/account``
(``web_users.discord_user_id``); the account ``abbrev`` becomes the conv
``author`` and the ``user:<handle>`` memory tag. Unmapped users keep the
Discord display name. The same abbrev attributes the turn's LLM writes
(:mod:`precis.acting_user`): asa sets ``PRECIS_ACTING_USER`` on the
``claude -p`` run, the deployed mcp.json passes it to the ``precis serve``
that run spawns over stdio, and that server records its writes as
``user:<abbrev>`` in the ``revisions`` log; the in-process chain lane gets
the same actor through ``revision_context``. Only a stdio server honours the
variable, so no client of the shared HTTP MCP can claim a user. Unmapped
speakers and cron turns write as ``agent``, as do asa's own bookkeeping
writes over the long-lived client (conv capture, the per-user placeholder
memory), which carry the speaker in ``author`` / ``user:<handle>`` instead.

**Paper attachments.** :mod:`asa_bot.attachments`: a PDF attached to a message
in a served channel is fetched (``safe_stream``, 50 MB, ``%PDF`` magic) into
the watch inbox (``$PAPERS_INBOX``) for the normal ingest; asa polls
``refs.pdf_sha256`` and replies with the ``pa<id>``, tagged ``user:<abbrev>``.
Only mapped speakers may ingest; others and non-PDFs get a refusal reply.

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
