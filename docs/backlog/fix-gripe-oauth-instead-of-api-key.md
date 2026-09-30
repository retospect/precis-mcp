---
status: draft
pillar: platform
title: fix_gripe burns metered API dollars because --bare forces ANTHROPIC_API_KEY; the OAuth subscription path already exists
prio: high
---

# fix_gripe should be able to use the OAuth subscription, not only a metered API key

On 2026-09-29 the whole auto-fix tier stopped for ~9 hours on
`API Error: 400 You have reached your specified API usage limits`, and a
leaf (`td450742`) was permanently latched by a 3.9 s failure that never ran
an agent. That outage is only possible because `fix_gripe` bills per-token
API dollars while the rest of the agent tier prefers the subscription.

## The asymmetry

`utils/claude_agent.py` already prefers OAuth and warns when it can't:

> "claude_agent: no OAuth token (CLAUDE_CODE_OAUTH_TOKEN) — auth is falling
> back to ANTHROPIC_API_KEY, billed per token at API rates. Install
> ~/.claude_oauth_token to use the subscription."

`fix_gripe._spawn_claude` opts out of that on purpose:

> "Uses `bare=True` so auth is strictly `ANTHROPIC_API_KEY` (no OAuth, no
> keychain reads, no plugin sync, no CLAUDE.md auto-discovery) — the
> claude_inproc executor runs where Claude Code's OAuth state from an
> interactive host is unreachable, so an API key is the only workable auth
> path."

**That justification looks stale.** "OAuth state is unreachable" was true of
reading an interactive host's keychain, but the same module already solved
it for launchd daemons by shipping the token as a *string*:
`ensure_oauth_token(proc_env)` loads `~/.claude_oauth_token` precisely
because "launchd-spawned daemons don't run interactive shells". And the
container tier already supports a non-API secret mode —
`container_mode = "api" if bare else _container.agent_run_mode()`, with the
secret-by-key channel able to request `CLAUDE_CODE_OAUTH_TOKEN` instead of
`ANTHROPIC_API_KEY`. So the blocker is not reachability; it is that
`bare=True` is hardcoded and `--bare` forces the API key.

## What actually has to be untangled

`--bare` bundles four behaviours: no OAuth, **no keychain reads, no plugin
sync, no CLAUDE.md auto-discovery**. The last three are deliberate isolation
properties for an agent running untrusted on a clone — the point of §H's
chokepoint. Simply dropping `bare=True` would hand the sandboxed agent
CLAUDE.md discovery and plugin sync, which is a real regression, not a
detail. The work is therefore:

1. Separate "use OAuth" from "skip keychain/plugins/CLAUDE.md" — either a
   claude flag that keeps the isolation without forcing API-key auth, or an
   explicit `CLAUDE_CODE_OAUTH_TOKEN` injection alongside `--bare`'s other
   effects.
2. Replace the dollar budget. `FixGripeConfig.max_usd` (default 10.0,
   `PRECIS_FIX_GRIPE_MAX_USD`, added in 735d9a5f) only means something on a
   metered key. On a subscription there is no per-call dollar meter, so the
   guard has to become turn-based (`max_turns`, already 120) and/or
   wall-clock (`PRECIS_FIX_TIMEOUT_SECONDS`, 1800 s).
3. Decide the shared-pool question. A subscription allotment is shared with
   Reto's own interactive sessions; ten queued `fix_gripe` jobs would eat
   into the same weekly pool. Metered API spend is isolated from that. This
   is a genuine trade, not a pure win — it may argue for OAuth as the
   default with an API-key fallback when the allotment is short, rather than
   a straight swap.
4. Confirm the usage terms allow an automated fleet of `claude -p` runs on
   the subscription. Owner call, not a code question.

## Anchors

- `src/precis/workers/job_types/fix_gripe.py::_spawn_claude` — the
  `bare=True` decision and its stale justification.
- `src/precis/utils/claude_agent.py` — the `--bare` flag construction, the
  OAuth-preference warning, `ensure_oauth_token`, and
  `container_mode = "api" if bare else ...`.
- `deploy/roles/precis_worker_agent/templates/precis-worker-agent.plist.j2`
  and `deploy/playbooks/35-precis-worker-sandbox.yml` — where the credential
  actually reaches the fleet.

test: a `fix_gripe` run authenticates with `CLAUDE_CODE_OAUTH_TOKEN` and
still gets no CLAUDE.md auto-discovery, no plugin sync, and no keychain
read inside the sandbox.

Context: gr452384 (the ceiling history — max_turns 20 → 120, $2 → $10, then
the account limit), gr456240 (infra failures should not burn unpark
attempts).
