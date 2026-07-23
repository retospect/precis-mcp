# ADR 0062 — asa-slack: a Slack bridge routed + capability-gated through the ADR-0046 router

- **Status**: accepted (2026-07-22)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0046 — the LLM routing layer (`Tier`/`dispatch`) — asa-slack is the
    first live caller that routes a *chat* turn through it rather than
    calling `claude -p` directly
  - `src/asa_bot/` — the Discord bridge; asa-slack is a sibling, not a
    rewrite (reuses `precis_client.py`, `preamble.py`, `secrets.py`)
  - `precis.kind_gate` / `PRECIS_KINDS_DISABLED` — the existing boot-time
    kind gate, repurposed here as a per-turn capability allowlist

## Context

Asa (the assistant persona backing `asa_bot`) gets a second front door: a
Slack workspace whose members are people Reto knows (plus a few other
bots — Rocky, Bullwinkle, Natasha) who will poke at it for fun, not
attackers. Two things make this a different bridge design from Discord
rather than a copy:

1. Discord's bridge (`asa_bot/claude_invoke.py`) hand-rolls its own
   streaming `claude -p` subprocess with a hardcoded model. asa-slack
   instead calls `precis.utils.llm.router.dispatch()` forced to
   `Tier.CLOUD_MID` (→ `claude-sonnet-5`) — trusted more than a cheaper
   tier against casual prodding, and the router's budget breaker +
   route-log + admission control apply for free (the actual answer to
   "don't burn too many tokens" — not a new mechanism, reuse of an
   existing one).
2. Slack users may ask for research (papers, patents, citations, some
   Perplexity) and must not be able to kick off compute (jobs, quests,
   cron). This is enforced, not just requested in the prompt.

## Decisions

1. **Router, not a hand-rolled subprocess.** `asa_slack/bot.py` builds one
   `LlmRequest(tier=Tier.CLOUD_MID, tools_needed=True, ...)` per turn and
   calls `dispatch()` via `asyncio.to_thread` (dispatch is synchronous —
   it blocks until the whole turn completes, no incremental
   `on_progress` callback like Discord's). Accepted trade-off: no live
   "🔍 tool_use…" ticker in v1 — a "_thinking…_" placeholder is posted and
   edited once with the final text. Reliability is the same class as
   Discord's own live-turn path (an awaited subprocess call): a turn is
   lost only if the asa-slack process itself crashes mid-call. A
   job+`PgListener`-NOTIFY design (durable across a bot-process restart,
   mirroring the existing cron/message delivery consumer in
   `asa_bot/bot.py`) was considered and rejected for v1 — real added
   complexity (new job_type, worker wiring, ref→Slack-thread mapping) for
   a failure mode that's rare in practice.

2. **Hard kind-allowlist via the existing kind gate, not prompt language
   alone.** `asa_slack/kind_policy.py` defines `ALLOWED_KINDS` (paper,
   patent, citation, semanticscholar, orcid, edgar, cfp, web, websearch,
   wikipedia, perplexity-research, perplexity-reasoning, memory, skill)
   and computes `KNOWN_KINDS - ALLOWED_KINDS` as the `PRECIS_KINDS_DISABLED`
   value threaded onto the spawned agent subprocess via
   `LlmRequest.env_overlay`. `job`/`quest`/`cron`/`todo` are **unreachable**
   for a Slack-originated turn — the tool call fails `Unsupported`, not
   just discouraged. The Slack-hints prompt segment (below) still explains
   *why*, for a user who asks. One known gap: a kind added to the live
   registry but never added to `KNOWN_KINDS` here defaults to *enabled*
   (the unsafe direction) — `tests/test_asa_slack_kind_policy.py` is the
   only guard, no live-registry diff yet.

3. **Every conversation is a thread; asa never posts to a channel root.**
   Conv slug is `slack/<team_id>/<channel_id>/<thread_ts>` — a fresh
   top-level message's own `ts` becomes the thread's ts the moment asa
   replies (`thread_ts = incoming.get("thread_ts") or incoming["ts"]`).

4. **Capture is unconditional, not gated on "did asa reply."** Every
   message asa observes — human or bot, including the other Slack bots in
   the workspace — is written to `kind='conv'`, and so is asa's own reply.
   There is no separate "just capturing" vs "replying" distinction: asa
   replies to (and captures) every message it sees, subject only to the
   channel allowlist and a self-loop guard (never react to its own posts).
   No bot-to-bot loop breaker — deliberately "let it ride"; the other bots
   in the workspace are valid interlocutors asa is meant to talk with, not
   a hazard to filter out.

5. **Per-person memory reuses `asa_bot.preamble.build()`'s existing
   mechanism, unchanged.** That function already searches
   `kind='memory', tags=['user:<author_handle>']` before a turn and
   instructs the model to keep the note current — built for Discord's
   `name#discriminator` handles, and transport-agnostic as written. asa-slack
   passes `author_handle = "<real name> (<@slack_id>, human|bot)"` as the
   tag key — no new memory-writing code needed. The one addition to
   `preamble.py` is a `platform: str = "Discord"` parameter (default
   preserves Discord's behavior byte-for-byte) threaded into
   `_render_conv_pointer`'s wording so the rendered "This turn" section
   says "Slack" instead of "Discord."

6. **Identity check is informational, not a hard gate, by default.**
   `asa_slack/identity.py` calls `auth.test` at boot and logs the resolved
   bot name/id/team prominently. It does **not** treat an unexpected name
   (e.g. an admin-assigned "ada") as an error — only raises
   `IdentityMismatch` when the operator has explicitly pinned
   `expected_bot_user_id` and it doesn't match. The point is catching a
   genuinely wrong token (pointing at some other app entirely), not
   policing what the workspace admin named the app.

7. **No OAuth bootstrap, no capture-shim HTTP server, no Stop-hook
   wiring.** Unlike `asa_bot`, which needs `ensure_oauth_token` +
   `capture_shim.py` (a port-9876 HTTP endpoint Claude Code's own Stop
   hook POSTs to) because its raw subprocess relies on Claude Code's hook
   mechanism to capture the assistant turn — `call_claude_agent` (which
   `dispatch()` calls) already parses the final text directly from the
   stream-json output and already bootstraps
   `CLAUDE_CODE_OAUTH_TOKEN` itself (`precis.utils.claude_oauth`). asa-slack
   captures both the user and assistant turns directly via its own
   `PrecisClient`, the same way `asa_bot/bot.py` already captures the user
   turn.

## Non-goals (v1)

- No hard rate limit on `perplexity-*` calls beyond the per-turn
  `max_turns=8` / `max_usd=0.75` caps (`RouterConfig`) — revisit if usage
  looks like a problem.
- Not migrating `asa_bot` (Discord) onto the router — a separate,
  behavior-preserving follow-up.

## Code

`src/asa_slack/` — `config.py` (tokens + router knobs), `kind_policy.py`
(the allowlist), `conv_slug.py`, `identity.py`, `bot.py` (Socket Mode +
turn handling), `__main__.py`. Deploy: `deploy/playbooks/48-asa-slack.yml`
+ `deploy/roles/asa_slack/`, mirroring `deploy/roles/asa_bot/`.
