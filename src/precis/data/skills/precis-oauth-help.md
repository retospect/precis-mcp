---
id: precis-oauth-help
family: core
title: precis — OAuth app connections
summary: connect Claude or another registered MCP app to a user, inspect permissions and revoke one connection
answers:
  - how do I connect Claude with OAuth?
  - can one user connect multiple MCP apps?
  - how do I revoke one OAuth connection?
applies-to: get/search/put/edit/delete/tag/link (authenticated OAuth MCP)
kinds: [learning]
status: active
tags: [workflow]
---

# OAuth app connections

Each browser authorization belongs to one web account and one registered
client. One user can authorize several apps or authorize the same app more
than once. Each connection can be revoked independently at
**Account → Connected apps** (`/account/connections`).

Claude web: add the public MCP URL under **Customize → Connectors**, choose
OAuth **Sign in now → Use your own OAuth client**, enter `precis-claude`
with no client secret, then sign in to Precis and approve the requested
permissions. The older UI places client fields under Advanced settings.
Team/Enterprise administrators must first add the connector. Public endpoint
configuration and registered client redirects belong to the deployment owner.

## Permissions

`mcp:full` permits all seven MCP tools, including writes/deletions and shared
data/configured services, subject to the existing runtime restrictions. This
is the default requested capability when OAuth is enabled. It does not limit
access to the user's own documents. Owner-aware personal features still read
the authenticated account. Writes record the OAuth connection and live user
actor; an OAuth credential never becomes the install principal.

`learning` is a narrower capability for existing private learning calls.
Read `get(kind='skill', id='precis-learning-help')` for those calls. It may be
offered instead of full access by the deployment owner.

This guide and the exact learning-help read are available to OAuth callers
without extra view/args fields. A learning-only token cannot elevate itself
by asking for another kind or supplying an owner. Scope refusals mean
reconnect with an available permission; do not retry with the infrastructure
token.

Connections expire after thirty days. Access tokens renew via refresh until
then; revoking a connection or disabling the account prevents further use.
Start a fresh MCP session for another app, connection or account. Legacy SSE
does not accept OAuth credentials. Existing personal API tokens remain
learning-only and keep their separate revoke controls.

User guide: `/manual/mcp-connections`. Operator runbook:
`docs/runbooks/mcp-oauth.md`. Claude's supported flow:
https://claude.com/docs/connectors/building/authentication (2026-11-10 UTC).
