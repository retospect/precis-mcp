---
status: draft
title: Bind an MCP session to a web user via a per-user token minted at /account
pillar: personal
prio: normal
---

# Bind an MCP session to a web user via a per-user token minted at /account

## Motivation / why

Install and slice callers carry no requesting-user identity. The HTTP
transport is gated by one install-wide bearer token
(`src/precis/server.py::_install_token_auth`, `_check_bearer_token`; minted
by `deploy/mcp-http/precis-mcp-http-ensure.sh` into `PRECIS_MCP_TOKEN` and
written into the client's `mcp.json` `Authorization` header), and every
session runs as the shared `agent_rw` DB role. That token says "a legitimate
client", never "which person". Per-user features that start on the web side
still stop at the MCP boundary for every kind except `learning`:
`email-per-user-credentials.md` (mailbox resolved through the requesting
user) and `per-user-library-link.md` (MCP-triggered exports fall back to the
install default).

## Shipped

Per-user MCP tokens exist; only the `learning` kind accepts them.

- Storage: migration `0275_web_user_mcp_tokens.sql`, table
  `web_user_mcp_tokens` (many labelled tokens per `web_users.id`, SHA-256
  digest only, `revoked_at`). Plaintext is shown once and never vaulted.
  Helpers: `src/precis/mcp_identity.py::mint_user_token`,
  `list_user_tokens`, `revoke_user_token`, `resolve_user_token`.
- Mint/revoke at `/account` (`src/precis_web/routes/account.py::create_mcp_token`,
  `revoke_mcp_token`); rotate is mint-then-revoke.
- Transport: `src/precis/server.py::_install_token_auth` resolves a
  `precis_user_` bearer token per request (not per MCP session), stamps the ASGI
  scope, and answers 401 for an unknown or revoked token. Personal tokens are
  accepted only on the streamable-HTTP path.
- Context: `src/precis/user_context.py::current_principal`, `user_scope`,
  `require_user` (re-checks account and token on every call, so revocation
  bites mid-session); no env, config or literal login supplies an owner.
- Scope gate: `src/precis/tools/core.py::_personal_call_allowed` lets a
  personal principal call only `learning`, `get skill precis-learning-help`
  and `get(kind='profile', id='me')` (the whoami read, gr633675); every
  other kind refuses.

Ruling (Reto, 2026-10-06): the owner is Reto, but the identity is never
hardcoded; the binding is a per-user credential the user manages on their own
account page, `/account`.

## Secret classes (2026-11-08)

Three classes of bearer secret exist:

1. **Install token** (`PRECIS_MCP_TOKEN`; infra: "a legitimate client").
   Minted by `deploy/mcp-http/precis-mcp-http-ensure.sh`.
2. **Per-user token** (`web_user_mcp_tokens`; identity: "which person").
   Minted at `/account`; scope-gated to learning by
   `tools/core.py::_personal_call_allowed`.
3. **Client capability token** (`web_user_capability_tokens`; consumer:
   "which thing", gr595554 — landed): per consumer, not per person — a
   claude.ai chat connector, a feed listener. Revocable, digest-only,
   labelled, minted and listed at `/account` beside the user tokens as
   `precis_cap_` (`src/precis/mcp_identity.py` —
   `mint/list/revoke/resolve_capability_token`). Scope is a per-token
   allow-list — `read` (the full get/search surface, the mint default),
   `read:<kind>`, `write:<kind>`, `feed` — enforced at dispatch by
   `tools/core.py::_require_capability_scope`; the token stamps a
   `CapabilityIdentity`, never a personal principal, so personal kinds
   still refuse at `require_user`. Carried as `Authorization: Bearer` on
   the streamable-HTTP endpoint (the MCP auth rules and Anthropic's
   connector guidance forbid query-string credentials there), or
   URL-embedded as `?t=` on `/podcast/*` where the feed scope folds the
   per-listener podcast credential (quest-learning-plan.md) into this
   class. claude.ai custom connectors send a static `Authorization`
   header (Request headers, beta) — no OAuth needed for the gateway.

Reto authorized OAuth for multiple per-user app connections on 2026-11-10
(gr621409), with full MCP access including writes. `precis.oauth` implements
independent grants and per-user browser consent; manual personal tokens keep
their learning-only boundary. The [runbook](../runbooks/mcp-oauth.md) and
packaged MCP-connections manual carry setup instructions. This item still
owns the per-kind adoption work below.

## In scope (still open)

Orchestrator proposal, inside Reto's ruling (not yet reviewed by Reto):

1. **Widen the manual-token scope gate.** `_personal_call_allowed` admits only
   `learning`, the learning-help read and the whoami read for personal API
   tokens; per-user kinds beyond it (mailbox,
   library link, per-user vault names) need an allow-list decision per kind.
   Such a token calling anything else is refused by `_require_personal_scope` ("personal MCP
   tokens permit learning calls, learning help and whoami only"). An anonymous call
   to a per-user feature already refuses naming `/account`
   (`user_context.py::_require_account`).
2. **Client carriage snippet.** The token travels in the `Authorization`
   header the install token already uses. `/account` shows only the
   one-time token and a bearer-setting hint, not a copy-paste `mcp.json`
   snippet (the URL comes from the deployment overlay, never the repo).
   `scripts/mcp-http-install` and the ensure script keep emitting the
   install-token config for infra.
3. **Callers adopt it.** `email-per-user-credentials.md` (scope check and the
   `vault.acl` per-name ACL key on `web_users.id`) and
   `per-user-library-link.md` (MCP-triggered exports carry the bound id) read
   `current_principal()` / `require_user`.

## Explicitly NOT in scope

- Per-tool ACLs beyond the identity binding (who may call which verb/kind).
- External IdPs; OAuth in `precis.oauth` reuses the existing web login.
- Replacing the install bearer token or the shared `agent_rw` DB role.
- Defining the per-name vault ACL (`vault.acl`, designed in
  `email-per-user-credentials.md`); this item only supplies the key it checks.
- Hardcoding any user, including Reto, as a default identity.

## Acceptance criteria

Met by the shipped slice: /account mints, shows once and revokes; only the
digest is stored; a revoked token gets 401; an anonymous per-user call
refuses naming `/account`; no hardcoded identity.

Open:

- Each adopting kind (mailbox, library link) reads the principal via
  `require_user` and is exercised by a test with two users' tokens in
  interleaved concurrent sessions.
- `scripts/mcp-http-install` output for infra stays unchanged.

## Target + blast radius

Remaining work touches `src/precis/tools/core.py::_personal_call_allowed`
(per-kind allow-list) · the adopting kinds (email, library link) ·
`src/precis_web/templates/account/index.html.j2` (`mcp.json` snippet) ·
`src/precis/secrets.py` (per-user vault names) ·
`deploy/mcp-http/precis-mcp-http-ensure.sh` and `scripts/mcp-http-install`
(snippet/docs only) · skill `precis-overview` (anonymous-refusal hint).
Related: gr595554 (capability token class), `email-per-user-credentials.md`, `per-user-library-link.md`,
`web-basic-auth-users.md`, `threads/session-mcp-shared-server.md`.

## Open questions / decisions log

- 2026-10-06 (Reto): "It's my thing, reto. But don't hardcode that. It goes
  here: /account."
- Decided in the shipped code: a `web_user_mcp_tokens` table (many,
  labelled; no last-used column), identity resolved per request from the
  header, not per MCP session id.
