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

Three classes of bearer secret exist or should:

1. **Install token** (`PRECIS_MCP_TOKEN`; infra: "a legitimate client").
   Minted by `deploy/mcp-http/precis-mcp-http-ensure.sh`.
2. **Per-user token** (`web_user_mcp_tokens`; identity: "which person").
   Minted at `/account`; scope-gated to learning by
   `tools/core.py::_personal_call_allowed`.
3. **Client capability token**: per consumer, not per person, e.g. a
   claude.ai chat connector or a podcast feed listener (today the per-user
   feed credential in `quest-learning-plan.md` is this class, informally).
   Revocable, digest-only storage, labelled, minted and listed at `/account`
   beside the user tokens. Carried as `Authorization: Bearer`, or
   URL-embedded where the client cannot set headers. Scope is a per-token
   allow-list of kinds and verbs. Filed as gr595554.

gr595554 built (2026-11-13). Class 3 is now a token kind in
`web_user_mcp_tokens` (migration 0317: `token_class`, `allow_verbs`,
`allow_kinds`, `feed_access`; digest-only, same revoke path):

- `mcp_identity.mint_capability_token` mints `precis_cap_...`; `/account`
  mints it (`POST /account/mcp-capability-tokens`) and lists it beside the
  user tokens with its allow-list.
- `user_context.Capability` is the allow-list; `_personal_call_allowed`
  applies it. Blank kinds = `get`/`search` on every kind except
  `learning`, `email`, `profile`; named kinds restrict to exactly those, and
  only named kinds can carry write verbs or private kinds. A capability
  token is neither the install token nor the learning-only personal token.
- Podcast: `?t=` on `/podcast/feed.xml` accepts a live capability token
  minted with feed access. The legacy per-user feed credential (vault-
  recallable, so Basic callers get enclosure URLs) still works; retiring it
  is a follow-up once listeners re-subscribe with capability tokens.
- Carriage is `Authorization: Bearer`. URL-embedding is built for the feed
  only; the MCP endpoint reads the header alone (no client needing a URL
  token for MCP has appeared).

Gateway ruling (claude.ai chat). The runbook (`docs/runbooks/mcp-oauth.md`)
documents both client modes: OAuth via **Use your own OAuth client**
(`precis-claude`, preferred: per-user, independent revocable grants, full
surface) and a static `authorization` header under **No sign-in** (a Claude
beta). The header mode is exactly what a class-3 token serves, so a chat
connector can carry a capability token (Reto's choice: scoped, no OAuth
consent) or an OAuth grant (per-user full access). Not verified against
Anthropic's live connector UI from this lane; the paste step re-checks.
Still open: the ask-user todo for Reto to mint the token at `/account` and
paste it into claude.ai chat (or connect via the OAuth runbook).

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
