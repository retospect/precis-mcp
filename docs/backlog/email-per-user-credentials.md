---
status: idea
pillar: personal
---

# email — mailbox credentials owned by a user, not the cluster

A mailbox password is personal, but `email.<account>.password` (e.g.
`email.rs@retostamm.com.password`, from `precis.mail.account.default_secret_name`)
sits in the one cluster-wide vault beside API keys. Surfaced 2026-10-06 on the
`/secrets` page (`src/precis_web/secret_status.py`), where the mailbox shows as
a cluster key. Parent design: [`email-kind.md`](./email-kind.md); per-user
modelling precedent: [`per-user-library-link.md`](./per-user-library-link.md).

## Motivation / why

- The vault has flat names and no scoping: every `vault.*` function is granted
  to PUBLIC and a DSN is the whole boundary (`src/precis/secrets.py` module
  docstring; the per-name `vault.acl` is designed but not built). Any worker or
  agent process holding a DSN can `get_secret` any user's mailbox password.
- `email_account` is keyed on the address alone (migration
  `0075_email_account.sql`): no owner column, so `get(kind='email')` serves any
  configured account to any caller.
- No per-user rotation or revocation: removing a user leaves the mailbox
  credential behind with the API keys.

## In scope

1. Ownership: `email_account` gains an owner referencing a precis user
   (`web_users.id`; web-basic-auth-users.md §2a).
2. Credential scope in the vault: a per-user name convention, e.g.
   `user/<id>/email/<account>`, enforced by the vault's access layer. The
   flat-name vault cannot enforce it today, so this item depends on the
   `vault.acl` per-name ACL (or an equivalent per-user check in `get_secret`).
3. The `email` handler and the `mail_poll` / `inject_scan` workers
   (`handlers/email.py`, `workers/mail_poll.py`, `workers/inject_scan.py`)
   resolve the mailbox through the requesting user; a worker acts for the
   account's owner and no other.
4. `/secrets` lists mailboxes under their owner, not under cluster keys; the
   probe stays presence-only.
5. A one-shot migration script moves `email.rs@retostamm.com.password` to the
   owner's scope and updates `email_account.secret_name`.

## Explicitly NOT in scope

- Send (still gated separately, `email-kind.md` §Send).
- An OAuth flow redesign; XOAUTH2 stays the `Account` stub unless the vault
  already carries refresh tokens.
- Sharing a mailbox between users, or delegated read.
- Moving non-mailbox secrets (API keys stay cluster-scoped).

## Acceptance criteria

- After migration no vault name starts `email.` in the cluster scope.
- An email read made on behalf of user A cannot reach user B's mailbox
  (handler and worker paths, test with two users).
- `/secrets` shows each mailbox with its owner.
- Removing a user disables their `email_account` rows and deletes their vault
  scope.
- `precis email add|list|rm|test` take an owner (default: the invoking user).

## Target + blast radius

`src/precis/secrets.py` · `src/precis/mail/account.py::default_secret_name` ·
`src/precis/handlers/email.py` · `src/precis/workers/mail_poll.py` ·
`src/precis/workers/inject_scan.py` · `src/precis_web/secret_status.py` ·
`precis email` CLI · a new forward migration (owner column) · the one-shot
migration script. Drift to fix on the way: `email-kind.md` §Storage names the
secret `email.<account>.imap_password`; the code uses `.password`.

## Open questions / decisions log

- 2026-10-06 (Reto): the owner is the web user, not an orcid — orcid is an
  optional attribute of a user, never the key (not every orcid record has a
  precis account). The key is `web_users.id` (bigint identity PK), used for the
  `email_account` owner column, the vault ACL and the secret-name scope;
  `login` and `abbrev` (e.g. `rs`) are display only, joined in where a human
  reads them. Keyed on `id` for rename stability. Migration 0164 keys
  `refs.owner_login` on `web_users(login)`; that is the existing exception, to
  move to an id column only when something touches it.
- 2026-10-06 (Reto): MCP sessions carry no requesting-user identity today; the
  binding is a per-user token minted at /account, never a hardcoded user. Filed
  as mcp-session-user-identity.md; the handler reads the requesting user from
  that item's session context.
