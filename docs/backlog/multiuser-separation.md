---
status: idea
title: True multi-user support with serious user separation
pillar: platform
prio: normal
---

# True multi-user support with serious user separation

Reto, 2026-10-16: "no super secrets yet. We should keep a list in the
backlog for true multiuser support with serious user separations, lest we
forget." Today precis has one real user. Owner checks keep users from
reading each other's data on the paths that were reviewed. Several paths
still fail open for a caller with no identity, and that is the accepted
intermediate state. This item collects every known gap, so a later pass can
close them together instead of one at a time.

## Known gaps

From the profile access review (gr490266, fixed paths in a679e81fa and
665e3a87e):

- `profiles.acting_login` returns None for actor `agent`, on a lookup
  error, and for a stale login abbreviation, and None passes every check.
  The session MCP can read and edit every user's profile, and `owner=` on
  put/edit can name any login. The closed version fails closed. A cluster
  agent acts as the owner of the quest or todo that dispatched its job; the
  session MCP acts as its bound user (`mcp-session-user-identity.md`).
- An unowned draft with no acting identity resolves its poster profile
  unscoped, across every owner. The closed version falls back to the
  product default profile only.
- Profile rows with a NULL owner are readable, writable and claimable by
  anyone. The closed version backfills an owner once and refuses new
  ownerless rows.
- A web viewer with no login sees every live profile, the same fail-open as
  `acting_login`.
- Existence oracles: `/r/{kind}/{id}`, `/resolve`, quick-open and
  `/c/{handle}` answer 302 vs 404 for a foreign profile.
- Link-degree counts (`count_links_for_refs`) include links to hidden
  profiles, which leaks a number.
- A link to a foreign profile made by an unidentified agent cannot be
  removed by the identified user, because the link check refuses both ways.
- Tag strings are global (tags index, tag suggest), with no owner scoping.

Related items that carry their own design: `mcp-session-user-identity.md`
(per-user MCP token), `email-per-user-credentials.md`,
`per-user-library-link.md`, `cluster-daemon-user-model.md`, and the
residuals of `web-basic-auth-users.md`.

## Scope of the later pass

One audit across every kind, not only profile: which kinds are per-user,
which are shared, and how each read and write path scopes to the caller.
Then fail closed for unidentified callers, everywhere at once.
