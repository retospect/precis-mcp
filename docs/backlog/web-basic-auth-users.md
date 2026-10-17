---
status: in-progress
pillar: platform
---

# Web basic auth + a real users table — residuals

Shipped + live 2026-08-22 (`precis_web/auth.py`, `web_users` migration 0131,
`precis users` CLI, `/account`, `/podcast` feed tokens, funnel exposure,
`SecurityHeadersMiddleware`; 4bfe8a99). Lockout/failed-auth warning shipped
(gr343746). Design: `precis_web/auth.py` and `git log`.

Open:

- **Non-atomic feed-token rotate race.** The row write and the vault write
  (`PRECIS_WEB_FEED_TOKEN:<login>`) in `precis_web/routes/account.py::feed_token`
  aren't atomic: two overlapping rotates (two tabs) can leave the row on B's
  digest and the vault on A's plaintext, so `/account` shows a URL that 401s
  until the next rotate. Known-accepted: self-correcting, needs concurrent
  rotates by one person, and the fix is a transaction spanning two stores.
- **Full CSP** (`default-src`/`script-src`). Only `frame-ancestors` ships
  because the templates carry inline scripts and styles;
  `tests/precis_web/test_security_headers.py` has a tripwire asserting the
  narrow policy, so widening it forces the templates to be fixed first.
