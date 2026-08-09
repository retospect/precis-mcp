# Reviewed non-problems — don't "fix" these

Findings that code reviews keep re-flagging and that were adjudicated
FINE (2026-08-02 six-dimension review; re-adjudicate only if the cited
code changes shape). A reviewer hitting one of these should cite this
file and move on, not re-litigate.

- **safe_fetch** (`utils/safe_fetch.py`): DNS resolve-and-pin at the
  httpcore connect layer (closes the rebinding TOCTOU),
  IPv4-mapped-IPv6 handling, correct TLS hostname/pool keying,
  fail-closed backend assert. Well-engineered; leave it.
- **Raw httpx on fixed keyless API hosts** (`openalex_meta.py`,
  `orcid.py`, S2, Wolfram; `_edgar_client.py` redirects justified
  in-code) — the safe_fetch convention is applied with judgment, not
  cargo-culted. These hosts are constants, not agent-supplied URLs.
- **Derived-queue core** (`workers/base.py`, `runner.py`, `embed.py`):
  claim/process/write separation, `EmbedderUnavailable` deferral,
  poison markers, capped waiting-backoff in `chase.py`.
- **Pool tuning** (`store/pool.py` constants tied to pgbouncer
  semantics), `FOR UPDATE SKIP LOCKED` claim discipline, migration
  checksum enforcement + the sealed-file guard.
- **Web XSS posture**: Jinja autoescape + `linkify.py`
  escape-then-Markup + a named regression test from a real incident;
  zero `|safe`.
- **Test infra**: per-session DB cloning, connection-leak hard-fail,
  load-gate pinning; no rotting skips/xfails; FakeStore doubles seed
  from `tests/_fakes.py` (documented can't-parse-SQL limitation).
- **CI**: full suite on real pg17+pgvector, Py 3.12+3.13, SHA-pinned
  actions; baseline staleness + migration-prefix uniqueness gated in
  `tests/test_schema_baseline.py`.

## Accepted risks (dated; revisit on the named trigger only)

- **No web auth / CSRF** (accepted 2026-08-02: local, single-user,
  tailnet-only). `precis_web/config.py::WebConfig.auth_token` stays
  dead config; `routes/console.py` remains an unauthenticated generic
  verb runner as `web:owner`; mutating POST forms carry no CSRF token
  (narrower `/factory` slice: gripe 171512). **Revisit trigger:** the
  app becomes reachable beyond the tailnet, or gains a second user —
  then wire `auth_token` into Starlette middleware gating POSTs +
  `/console`, and add a same-origin check as interim CSRF.
- **No asa_bot per-user spend caps** (declined 2026-08-02:
  trusted-user Discord; the per-thread $50 ceiling in
  `claude_invoke.py::_MAX_USD_CEILING` is enough). Revisit only if the
  bot gains untrusted users.
