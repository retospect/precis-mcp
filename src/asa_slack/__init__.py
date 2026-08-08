"""asa-slack — Slack bridge to Asa, routed through the ADR-0046 LLM router.

Socket Mode daemon (ADR 0062); each turn is one blocking
``router.dispatch()`` at ``Tier.BIG`` (asa_bot's Discord bridge, by
contrast, streams via ``dispatch_async`` at ``FRONTIER``). Slack is a
semi-trusted multi-user surface, so turns carry a hard kind-allowlist
(:mod:`asa_slack.kind_policy`, baked in via ``LlmRequest.env_overlay``'s
``PRECIS_KINDS_DISABLED``): research lookups + memory only —
job/quest/cron/todo are *unreachable*, not just prompt-discouraged.
Replies are thread-only (never a channel root); every message asa sees is
captured as a ``conv`` turn, not just the ones that trigger a reply.
Per-person memory rides ``asa_bot.preamble.build()``'s ``user:<handle>``
memory notes, keyed on the resolved sender identity.
"""

__version__ = "0.1.0"
