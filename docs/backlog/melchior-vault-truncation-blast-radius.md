# Which deploys ran without 14 vault secrets, 18–22 Aug?

- **Status**: open, filed 2026-08-22. Needs investigation before it can
  be scoped — the fix may be "nothing was affected".
- **Found**: incidentally, while unjamming an unrelated overlay push.
  Nothing was watching for it; see the root pattern at the bottom.

## What happened

melchior's overlay working copy (`/Users/reto/git_deploy_helper`, which
`precis-mcp/deploy/inventory` symlinks to) had
`group_vars/all/vault.yml` **truncated from 29 keys to 15** on
2026-08-18 08:32, with a `vault.yml.bak-20260817` written the same
minute. Restored from git HEAD on 2026-08-22 — verified safe because the
backup and git HEAD decrypt to **identical plaintext**, and the live
file was a strict subset with no unique key and no differing value.

Missing for those four days:

```
vault_anthropic_api_key      vault_email_imap_host    vault_kagi_api_key
vault_bitwarden_token        vault_email_pass         vault_kagi_summarizer_engine
vault_discord_home_channel   vault_email_smtp_host    vault_openclaw_api_key
vault_google_api_key         vault_email_user         vault_openclaw_webhook_secret
                             vault_outlook_client_id  vault_outlook_client_secret
```

## The open question

**Any play launched *on melchior* between 18 and 22 Aug rendered its
templates without those 14 values.** A deploy launched from the Mac was
unaffected — the Mac's copy was intact throughout. So the blast radius
is exactly "what was deployed from melchior in that window", which is
not yet established.

At least one such run exists: a full `redeploy-precis.yml` started on
melchior at 18:59 UTC on 2026-08-21 and completed. precis-web itself is
known-good (redeployed from the Mac on 2026-08-22 with the full set).

To answer this:

1. Find melchior-launched plays in the window — the Mac's
   `.deploy-logs/` will NOT have them (that is why this went unnoticed);
   check shell history on melchior and `/Users/reto/precis-mcp`.
2. For each role touched, check whether it references any of the 14 keys
   and whether it uses `| default(...)` (renders empty, silent) or fails
   on undefined (loud, so it would already have been noticed).
3. Prime suspects by dependency: **asa bot** (`vault_anthropic_api_key`,
   `vault_discord_home_channel`), **email/mail_poll**
   (`vault_email_*`, `vault_outlook_*`), **Kagi**, **openclaw**.

If any of those misbehaved between 18 and 22 Aug, this is the
explanation rather than a coincidence — check before chasing another
cause.

## Root pattern (the actually important bit)

Config edited **in place on a node** silently diverges from the Mac
source of truth, and nothing compares them. This bit twice in one
session: the vault truncation, and `precis_web_funnel` being reverted by
a deploy from a stale checkout. Both surfaced only as side effects of
unrelated work.

A periodic `git status --short` on `git_deploy_helper`, alerting on any
dirty tracked file, would have caught both on day one. That is the
durable fix and it is cheap — worth doing regardless of what this
investigation concludes.
