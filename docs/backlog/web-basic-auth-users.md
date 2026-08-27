# Web basic auth + a real users table

- **Status**: **shipped + deployed + live** (2026-08-22). Kept open only
  for the residuals below; the design sections are retained as the
  rationale record until they are folded into the package docstrings.
- **Live state**: melchior serves this to the **public internet** via
  `tailscale funnel` (not just the tailnet — see §8). Roster: `reto`/`rs`,
  `scrypt-pepper-v1`.
- **Residuals**, each tracked separately:
  - no failed-auth logging / rate limiting →
    `web-auth-failed-login-observability.md`
  - the non-atomic feed-token rotate race (§4, known-accepted)
  - a full CSP (`default-src`/`script-src`); only `frame-ancestors` ships
    today because the templates carry inline scripts and styles, and
    `tests/precis_web/test_security_headers.py` has a tripwire asserting
    the narrow policy so widening it forces the templates to be fixed first
- **Scope decided with Reto**: precis-web only (not the embedder service,
  not the MCP network transport — the latter keeps its bearer token).
- **Deliberately NOT** `docs/backlog/user-identity-and-ask-routing.md`.
  That spec is about *routing* asks to a named human and de-hard-coding
  "reto"; this one is about *authenticating* HTTP callers. They meet at
  one column (`abbrev`) and are otherwise independent.

## 1. Problem

`precis-web` serves the whole tab UI — including `/secrets`, `/env`,
`/console`, and every mutation route — with **no authentication at all**
(`WebConfig` says so in its own docstring: "no auth in cut 1"). The
`auth_token` field it declares is dead: nothing reads it. The only thing
between the UI and the world is `tailscale serve --https=443`, i.e.
tailnet membership. Any device or process on the tailnet is root on the
corpus.

## 2. Shape

A **fully-authorized user set**: every row in the table gets the same
access. No roles, no per-route ACL — that is the multi-user spec's job.

### 2a. `web_users` (migration 0131)

| column | why |
|---|---|
| `login` | the Basic-auth username. Unique, lowercased. |
| `abbrev` | short display handle (`rs`) for future per-user edit attribution + a link to the user. Unique, lowercased. Carried now, unused by the UI in this cut. |
| `full_name` | display. |
| `email` | contact / future notification. **Not** a recovery channel — see §5. |
| `password_hash`, `password_salt`, `password_algo` | scrypt, per-user 16-byte salt, algo string so the KDF can migrate. |
| `feed_token_sha256` | per-user podcast credential (§4). |
| `disabled_at` | soft-disable without deleting attribution history. |
| `created_at` / `updated_at` / `last_login_at` | audit. |

### 2b. Hashing — scrypt + per-user salt + a vault pepper

`hashlib.scrypt` (stdlib, memory-hard; no new dependency — the repo has
no bcrypt/argon2/passlib). `n=2**14, r=8, p=1, dklen=32`, 16 random
bytes of salt per user.

**Pepper.** `precis.secrets` guarantees a *logical* `pg_dump` is safe to
share — vault values are pgcrypto ciphertext and the passphrase lives in
`postgresql.auto.conf`, which a logical dump never emits. A plain
salted-hash `web_users` would quietly break that guarantee: the dump
would carry crackable password hashes. So the password is HMAC-SHA256'd
under a vault-resident pepper (`PRECIS_WEB_PASSWORD_PEPPER`) *before*
scrypt. A leaked logical dump is then uncrackable without the vault key.

- `password_algo = 'scrypt-pepper-v1'` when a pepper was used,
  `'scrypt-v1'` when not. The row records it, so the two coexist.
- `precis users add` mints a 32-byte pepper into the vault on first use
  if one isn't there (`--no-pepper` opts out).
- If a row says `scrypt-pepper-v1` and the pepper can't be resolved,
  auth **raises loudly** (503, "pepper unavailable") rather than
  degrading to "wrong password" — a lost pepper must not look like a
  typo.

### 2c. The gate

A pure-ASGI middleware in `precis_web/auth.py`, installed by
`create_app` when `WebConfig.auth_required`:

- 401 + `WWW-Authenticate: Basic realm="precis"` on missing/bad creds.
- **503** when no *enabled* account exists — fail closed, with the exact
  `precis users add` line to run. (Reto's call: a fresh deploy is dark
  until a user exists, rather than briefly wide open.) Checked *before*
  the 401 challenge, so a fresh deploy opened in a browser explains
  itself instead of prompting for credentials that don't exist yet.
  Disabling the last enabled account lands here too — same situation,
  same fix; a disabled user with someone else still enabled gets 401.
- `/healthz` exempt (supervisor probe).
- Verified credentials cached in-process, `(login, sha256(password))` →
  the `password_hash` they were verified against, 5 min TTL, bounded.
  Without it every static asset would pay a ~60 ms scrypt. The row is
  still read every request — see §4a for why that is load-bearing.
- **403 on a cross-site state-changing request** (`check_same_origin`):
  `Origin`, else `Referer`, must match the addressed origin for anything
  but GET/HEAD/OPTIONS. Basic auth is what creates this exposure — the
  browser attaches its cached header to a form on any page, so without
  the check an attacker's page could drive `/console` or `/secrets` with
  the victim's credentials, and there are no cookies to mark `SameSite`
  and no session to hang a token on. Neither header present ⇒ allowed:
  that's `curl`/scripts, which hold no ambient credential to replay.
- A websocket scope is denied with `websocket.close` 1008, not an HTTP
  response frame. Nothing speaks websocket yet; this is so the first
  route that does inherits a correct denial instead of a protocol error.
- The authenticated user lands on `request.state.web_user` so
  attribution (the `abbrev` column) can be wired later without touching
  the gate.

`WebConfig.auth_required` defaults **True** out of `from_env()`
(`PRECIS_WEB_AUTH=off` opts out for local dev) and **False** on the bare
dataclass, which is what the ~20 in-process test call sites construct. A
test pins `WebConfig.from_env().auth_required is True` so the production
default can't silently regress.

## 3. CLI — `precis users`

`add` / `list` / `passwd` / `edit` / `disable` / `enable` / `rm` /
`feed-token`. Passwords never come from argv (no `ps` / history leak):
interactive no-echo prompt by default, `--password-stdin` for scripts.
Mirrors `precis secret`'s shape.

## 4. Podcast

Basic auth breaks the phone flow — podcast clients handle Basic
inconsistently on enclosure URLs. `/podcast/feed.xml` and
`/podcast/audio/{name}` additionally accept `?t=<feed-token>`, a
per-user 32-byte urlsafe secret stored as SHA-256 (high-entropy, so no
KDF needed). `precis users feed-token <login>` mints + prints the full
feed URL; minting rotates, invalidating the old one. Everything else on
the app requires Basic.

The token is threaded into the `<atom:link rel=self>` **and every
enclosure URL** the feed emits (`audio_feed.build_rss(credential=…)`) —
the app fetches the audio later, in a request carrying no session and no
reliable Basic support, so the credential has to live in the URLs the
feed hands it. A *Basic*-authenticated feed request gets the caller's own
token looked up and threaded the same way, so subscribing with the plain
`/podcast/feed.xml` doesn't yield a feed whose episodes silently fail to
download.

**The plaintext is also vaulted** (`PRECIS_WEB_FEED_TOKEN:<login>`, via
`precis.secrets`) so `/account` can show the URL again. The row keeps
only the digest and that is still the *only* thing that authenticates —
what the vault buys is that looking up your own subscribe URL doesn't
require minting a new one, which would unsubscribe the phone that was
already working. Vault values are pgcrypto ciphertext, so the
"a logical dump is safe to share" guarantee behind §2b is unchanged. No
vault (or a token minted before this) ⇒ `/account` says so and offers
the only fix, a fresh link. `WebUser.has_feed_token` (derived column, not
the digest) is what tells the page which of the three states it is in.

**Known, accepted:** the row write and the vault write in
`account.py::feed_token` aren't atomic, so two overlapping rotates (two
tabs) can leave the row on B's digest and the vault on A's plaintext —
`/account` then shows a URL that 401s until the next rotate. Reviewer
finding, left unfixed: self-correcting, needs concurrent rotates by one
person, and the fix is a transaction spanning two stores.

## 4a. `/account` — the signed-in user's own page

Top-bar chip (the user's `abbrev`, far right) → `/account`. Three
sections: **change password** (current password required — a browser
holds Basic credentials for the life of the tab, so being authenticated
says nothing about who is at the keyboard), **profile** (full name,
email — display only), **podcast link** (the subscribe URL shown whole
with a copy button, plus mint/revoke; rendered inline, never through a
redirect, so the token stays out of history and referrers).

Roster management stays CLI-only: every account is fully authorized, so
a web "add user" button would let one stolen credential become several.

Two things the design turns on:

- **The gate's cache holds the scrypt, never the authorization
  decision.** Every request re-reads the row and re-checks `enabled`; a
  hit only means "this password already derived to *this stored hash*,
  skip the 60 ms". Two indexed reads on a tiny table buy the property
  that matters: `precis users passwd|disable|rm`, which run in another
  process over SSH and cannot reach this dict, take effect on the next
  request rather than up to a TTL later. An entry that outlives its row
  is inert, not a live credential. Getting this wrong is the failure the
  design doc's "recovery is `precis users passwd`" promise rests on.
- **Changing your password signs you out.** Basic has no session to
  re-issue; the browser re-sends the old header and now fails. So the
  POST redirects to a GET the browser gets challenged on, it re-prompts,
  and the page it lands on says what happened. The alternative — a 200
  that looks fine and breaks on the next click — is worse.

Password policy is length-only (`users.MIN_PASSWORD_LENGTH`, 8),
enforced in `users.validate_password` and called from *both* the form
and `cli/users.py::_read_password`. Composition rules produce `Passw0rd!`
and were dropped from NIST 800-63B for that reason.

## 5. Email recovery — explicitly out

HTTP Basic auth has no recovery affordance whatsoever: it is one request
header, with no session, no reset flow, no server-side state to expire.
Password recovery would mean building a reset-token mailer + a public
unauthenticated reset route — precisely the attack surface this change
exists to remove. **Recovery is `precis users passwd <login>` over SSH.**
The `email` column is carried for display/notification, not auth.

## 6. Definition of done

Standard per AGENTS.md. Plus: fresh DB + no users → 503 with the fix
line; `/healthz` reachable unauthenticated; a wrong password is
indistinguishable in timing from an unknown user (both pay a dummy
scrypt); README env table lists `PRECIS_WEB_AUTH` and
`PRECIS_WEB_PASSWORD_PEPPER`.

## 7. Deploy note

`redeploy-precis.yml` applies 0131, but the table lands **empty** — the
web UI answers 503 until `precis users add` runs on the gateway. Do that
in the same window as the deploy. **Done** for melchior 2026-08-22.

## 8. Public exposure (`tailscale funnel`), 2026-08-22

Reto's explicit call, after being shown the blast radius: the **whole
UI** is on the public internet, not just `/podcast`. HTTP Basic against
`web_users` is the only barrier.

`serve` is tailnet-only; `funnel` is public — and **running `serve` on a
funnelled port reverts it**. The `precis_web` role used to run `serve`
unconditionally with `changed_when: false` + `failed_when: false`, so any
redeploy silently un-published the site with nothing in the play recap.
This actually happened, 17 minutes after the funnel first went up. Now
the role picks the verb from `precis_web_funnel` (default **false**;
`true` lives in the private overlay, never this repo) and a follow-up
task asserts the end state. Two operational traps:

- **Funnel fails by hanging**, not erroring: a node lacking the `funnel`
  nodeAttr makes the CLI print an enablement URL and block forever. Hence
  `async`/`poll` on that task.
- **The overlay var must be pushed**, not just edited on the Mac —
  otherwise a deploy launched *on melchior* reads the role default.

## 9. Security validation, 2026-08-22

Run against the deployed public endpoint. Everything below passed:

- **120/120 GET routes → 401** unauthenticated. Unknown paths 401 too, so
  route existence isn't enumerable by status code.
- **No dot-segment bypass** of the `/podcast` exemption (8 encodings) —
  the router doesn't normalise `..`.
- **`/podcast/audio/{name}`**: allowlist match on the episode index, then
  `resolve()` + `is_relative_to(root)`. Traversal structurally impossible.
- **Feed token** compared as a SHA-256 digest, so no timing oracle (an
  attacker supplies preimages, not digest bytes); an invalid `?t=` is a
  dead end rather than a fallback to Basic.
- **CSRF** enforced: cross-origin POST 403, same-origin 303 (control).
- **Timing**: no usable enumeration oracle (numbers in
  `web-auth-failed-login-observability.md`).

Two findings, both fixed in `4bfe8a99`:

1. The `/podcast` exemption matched by **string** prefix, so
   `/podcastfoo` skipped auth — 404 only because no such route existed.
   Any future `/podcasts` would have been born unauthenticated on the
   public internet. Now matches by path segment (`_is_self_auth`).
2. **No framing headers.** This is the gap `check_same_origin` cannot
   close: a click inside a hostile `<iframe>` produces a request whose
   `Origin` is *ours*, so the CSRF check passes. Fixed with
   `SecurityHeadersMiddleware` (`X-Frame-Options: SAMEORIGIN` + CSP
   `frame-ancestors 'self'`, plus `nosniff` / `Referrer-Policy` / HSTS),
   installed outermost so it covers the 401 challenge and `/static`.
   Same-origin, not `DENY`/`'none'`: the UI frames the reader's PDF.js
   viewer (and, until the workbench went single-document, /nanopub's two
   panes), and `'none'` blanked every one of them.
