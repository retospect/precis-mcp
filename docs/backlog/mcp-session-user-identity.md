---
status: idea
title: Bind an MCP session to a web user via a per-user token minted at /account
pillar: personal
prio: normal
---

# Bind an MCP session to a web user via a per-user token minted at /account

## Motivation / why

MCP sessions carry no requesting-user identity. The HTTP transport is gated
by one install-wide bearer token (`src/precis/server.py::_install_token_auth`,
`_check_bearer_token`; minted by `deploy/mcp-http/precis-mcp-http-ensure.sh`
into `PRECIS_MCP_TOKEN` and written into the client's `mcp.json`
`Authorization` header), and every session runs as the shared `agent_rw` DB
role. Authentication says "a legitimate client", never "which person".
Per-user features that start on the web side therefore stop at the MCP
boundary: `email-per-user-credentials.md` (mailbox resolved through the
requesting user) and `per-user-library-link.md` (MCP-triggered exports fall
back to the install default).

Ruling (Reto, 2026-10-06): the owner is Reto, but the identity is never
hardcoded; the binding is a per-user credential the user manages on their own
account page, `/account`.

## In scope

Orchestrator proposal, inside Reto's ruling (not yet reviewed by Reto):

1. **Mint at /account.** `src/precis_web/routes/account.py` already manages
   password, profile, logout, the podcast feed token (minted/rotated/revoked;
   plaintext vaulted, digest in `web_users.feed_token_sha256`, via
   `precis.users.mint_feed_token` / `remember_feed_token` /
   `recall_feed_token` / `forget_feed_token`) and the reMarkable device token.
   Add an "MCP identity" section on the same pattern: mint, show once, rotate,
   revoke. Digest stored keyed on `web_users.id` (bigint identity PK,
   migration 0131; `login`/`abbrev` stay display-only); plaintext in the vault
   under a colon-bearing name so an env var cannot shadow it (cf.
   `precis.users.feed_token_secret_name`). A user may hold several tokens (one
   per machine) so revoking a laptop does not break the desktop.
2. **Session binding.** The HTTP MCP layer accepts the install bearer token
   (unchanged: infra callers stay anonymous) or a per-user token; a per-user
   token resolves by digest to `web_users.id`, bound for the whole session and
   held in the session/request context.
3. **Tools read the user from context only.** A `requesting_user()` accessor
   is the single source; never config, env, or a literal login. Returns
   `web_users.id`.
4. **Anonymous refuses with a hint.** A session without a per-user token is
   anonymous; any per-user feature (mailbox, library link, per-user vault
   names) returns a refusal naming `/account` ("mint an MCP token at /account
   and add it to your MCP client config").
5. **Client carriage.** The token travels in the header the install token
   already uses: `"headers": {"Authorization": "Bearer <per-user token>"}` in
   the client's `mcp.json`. `scripts/mcp-http-install` and the ensure script
   keep emitting the install-token config for infra; the local HTTP proxy
   forwards the client `Authorization` header unchanged. /account shows a
   copy-paste `mcp.json` snippet (the URL comes from the deployment overlay,
   never the repo).
6. **Callers adopt it.** `email-per-user-credentials.md` (scope check and the
   `vault.acl` per-name ACL key on `web_users.id`) and
   `per-user-library-link.md` (MCP-triggered exports carry the bound id) read
   the accessor once it exists.

## Explicitly NOT in scope

- Per-tool ACLs beyond the identity binding (who may call which verb/kind).
- SSO / OAuth / external IdPs; the web side stays basic auth
  (`web-basic-auth-users.md`).
- Replacing the install bearer token or the shared `agent_rw` DB role.
- Defining the per-name vault ACL (`vault.acl`, designed in
  `email-per-user-credentials.md`); this item only supplies the key it checks.
- Hardcoding any user, including Reto, as a default identity.

## Acceptance criteria

- /account mints a per-user MCP token, shows it once, and can rotate and
  revoke it; revoke takes effect on the next request.
- Only the digest is in the DB; plaintext is recoverable only through the
  vault; a `pg_dump` yields no working token.
- A session presenting a user's token reports that user's `web_users.id` from
  `requesting_user()` for every call in the session; a rotated or revoked
  token gets 401.
- A session with only the install token (or none) is anonymous; a per-user
  feature call returns a refusal containing `/account`.
- No hardcoded login or id serves as an identity default anywhere in `src/`
  (grep-checked).
- Two users' tokens in two concurrent sessions never see each other's
  identity (test with interleaved calls).
- `scripts/mcp-http-install` output for infra is unchanged.

## Target + blast radius

`src/precis_web/routes/account.py` (new section + route) ·
`src/precis/users.py` (mint/digest/vault helpers, mirrors the feed token) ·
new migration (token digest storage; forward-only) ·
`src/precis/server.py` (`_install_token_auth`: per-user token resolution,
session context) · `src/precis/secrets.py` (vault names) ·
`deploy/mcp-http/precis-mcp-http-ensure.sh` and `scripts/mcp-http-install`
(snippet/docs only) · skill `precis-overview` (anonymous-refusal hint).
Related: `email-per-user-credentials.md`, `per-user-library-link.md`,
`web-basic-auth-users.md`, `threads/session-mcp-shared-server.md`.

## Open questions / decisions log

- 2026-10-06 (Reto): "It's my thing, reto. But don't hardcode that. It goes
  here: /account."
- Open (orchestrator): digest column on `web_users` (one token) vs a
  `web_user_mcp_tokens` table (many, labelled, last-used). Proposal: table.
- Open (orchestrator): the shared session server is one process serving many
  clients; resolve identity per request from the header, not per MCP session
  id. Proposal: per request.
