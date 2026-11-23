# Enable per-user OAuth for MCP

OAuth uses the existing web account login and a separate grant for each app
authorization. The web process serves consent and token exchange; the MCP
process validates bearer credentials against the same database. Implementation:
`precis.oauth`, `precis.oauth_transport`, `precis_web.routes.oauth`.

## Configure and release

Ship the forward migration through the normal gated release. The feature is
off until both canonical URLs are configured. It does not publish a listener
or change Tailscale automatically.

Set these values in the private deployment environment of **both** services:

```text
PRECIS_OAUTH_ISSUER=https://precis.example.com
PRECIS_OAUTH_RESOURCE=https://precis.example.com/mcp
PRECIS_OAUTH_SCOPES=mcp:full
```

Replace the example origins with the deployment's public HTTPS names. The
issuer is the web origin with no path/trailing slash; the resource is the
canonical MCP URL. Missing scopes defaults to `mcp:full`, as requested by
the deployment owner: every enabled web account can consent to all MCP
verbs, including writes and access to shared corpus/configured services.
Existing runtime restrictions still apply; this does not add per-document or
per-mailbox ownership to kinds that lack it. For a narrower deployment set
`learning`, or offer both by setting `learning mcp:full`; each grant records
the scopes actually requested and approved. Where enabled, manual personal
tokens permit access to the owner's learning and GTD data.

The web launchd template includes the private `precis_identity_env` mapping;
keep actual coordinates in that overlay. Supply the same values to the
dedicated MCP service's environment. Both must use the same migrated database
and pinned application version. Restart both after configuration changes.

The gateway's `precis_web` role can configure both daemons and the persistent
MCP listener without reinstalling the application or applying migrations.
In its private `host_vars/<gateway>.yml` set:

```yaml
precis_mcp_public_enabled: true
precis_mcp_public_funnel: true
precis_mcp_public_port: 8766
precis_mcp_public_https_port: 443
precis_mcp_public_oauth_env:
  PRECIS_OAUTH_ISSUER: https://precis.example.com
  PRECIS_OAUTH_RESOURCE: https://precis.example.com/mcp
  PRECIS_OAUTH_SCOPES: mcp:full
```

After the normal release has installed the OAuth code and migration, run the
existing web play with `--tags precis-web-config,precis-mcp-public` and the
gateway limit. This renders the same OAuth map into the web and dedicated
MCP environments, safely reloads changed daemons, verifies a discovery-bearing
401 before exposure, and asserts the exact MCP and existing web mappings.
The dedicated `com.precis.mcp-public` daemon uses the deployed web venv,
loopback only, and an `agent_rw` DSN. It refuses pending migrations and never
auto-migrates. The public daemon is OAuth-only by default:
`precis_mcp_public_credential_classes: oauth` is passed to it and to the web
daemon as `PRECIS_MCP_CREDENTIAL_CLASSES`, so the endpoint refuses install,
slice, personal and capability bearers and the account page offers no
personal-token or capability-token minting. In that mode the role creates no
infrastructure token and removes an existing
`/opt/mcps/mcp-public-secrets/PRECIS_MCP_TOKEN`. Listing more classes in the
private overlay (comma or space separated, from `install`, `slice`, `personal`,
`capability`, `oauth`) re-enables them; listing `install` makes the role
generate the token again (once, deploy-owned, mode 0600; directory 0700; only
its directory is in the plist) and reapplying preserves it. The list must keep
`oauth`; a list without it, or a name outside it, fails the role before any
plist is written. Reapplying never touches the
existing session MCP. The dedicated HTTPS ports 8443 and 10000
remain available when the web and MCP services need different exposure.
On shared HTTPS 443, both services must use the same Serve or Funnel mode.
Setting `precis_mcp_public_enabled: false` skips management. To remove
exposure, remove the configured MCP path mappings or the dedicated port
mapping and unload the listener.

The public daemon also exposes the `python` kind read-only: its plist env sets
`PRECIS_PYTHON_ROOTS` (the `precis` and `catpath` server-code checkouts) and
`PRECIS_PYTHON_READONLY=1`, which makes `put`/`edit`/`delete` on `python`
refuse while every other kind (gripe filing, etc.) keeps its normal writes.

### Public MCP deploy restarts

For a code-only deploy, launchd's `precis serve` process re-execs as
`precis.mcp_supervisor` at startup and owns the listening socket. The deploy
sends SIGHUP to the supervisor, which forwards SIGUSR1 to the serving child
to drain calls already in flight within the configured timeout, without
waiting for a quiet window. Calls that arrive during the drain can be
interrupted. The install watchdog stays disabled on this daemon. The new
child starts from the updated venv on the same socket. New connections can
queue during startup and may time out if the client deadline expires.

The reload checks for a new generation and an unauthenticated HTTP 401
with a `resource_metadata` challenge. This substitutes for MCP initialize
and one authenticated read, which still need an OAuth client to verify.
The deploy uses the same listener throughout a drain.

The first deploy with this helper performs a full launchd bootout/bootstrap.
It also does so when the plist changes, so launchd picks up new arguments
and environment, or when the daemon is absent. If the draining reload
cannot verify turnover within 300 seconds, it forces a full bounce.
The full bounce can briefly take the listener down and interrupt active
calls. Set `PRECIS_SERVE_SUPERVISOR=0` in the daemon environment to opt
out of socket supervision. Restarts in that mode can interrupt connections.

Reconnect or retry reads after a timeout. For a write with no reply,
look up its expected effect before retrying. The write may have committed
before the connection closed.

### Web database identity

After the database ownership cutover, the web login uses `precis_ingress`;
the dedicated public MCP stays `agent_rw`. General agent principals must
not read `web_users.password_hash`. Granting password access to `agent_rw`
would break the credential boundary.

Before rendering the web plist, both the full role and
`--tags precis-web-config` run a read-only `SELECT ... LIMIT 0` against
those three credential columns. The probe uses the template's exact web DSN,
the deployed venv and the daemon's `deploy` user/`.pgpass`; it reads no
credential rows and suppresses connection output. Failure stops before the
plist changes and names the identity/connection repair.

An anonymous health response or Basic 401 does not prove login works: neither
requires a credential-column read. Verify the configured principal and an
authenticated read before retrying Claude sign-in. If ownership cutover is
verified but the gateway's private cutover flag is absent, restore the narrow
gateway override `precis_web_db_user: precis_ingress` after checking that
principal's connection credentials. Do not flip the fleet-wide cutover flag
as a shortcut; it also controls worker and migration identities. Reapply the
configuration-only web play and leave the MCP principal unchanged.

Register additional public clients through `PRECIS_OAUTH_CLIENTS`:

```json
[{"id":"research-app","name":"Research app","redirect_uris":["https://app.example.com/oauth/callback"]}]
```

The built-in `precis-claude` client has the exact redirect URI
`https://claude.ai/api/mcp/auth_callback`. Additional client IDs must be unique.
Only exact HTTPS redirect URIs are accepted. Clients use authorization-code
flow with S256 PKCE and token endpoint authentication method `none`; there
are no client secrets, dynamic registration or client-credentials grants.

## Tailscale exposure

On HTTPS 443 the root mapping still goes to the web listener. The role mounts
`/mcp` and `/.well-known/oauth-protected-resource/mcp` on the dedicated
loopback listener. Each proxy target repeats the mount path because Funnel
removes that prefix before forwarding. OAuth issuer routes stay on the web
listener.

Use the canonical resource URL `https://precis.example.com/mcp` in clients.
When moving from a dedicated HTTPS port, existing OAuth grants are tied to
the old resource URL. Users sign in again on the new URL. Moving between a
dedicated port and 443 removes the retired MCP mapping in the same run: the
role removes every handler that proxies to the MCP loopback port and is not
one of the mappings it declares (never the web mapping), then re-reads
`tailscale funnel status --json` and asserts none remains. If that assertion
fails, the message names the manual `tailscale ... off` command.

Static tokens are refused on an OAuth-only endpoint; use OAuth for per-user
access.

The MCP SDK's Host/Origin checks retain localhost allowances and add the exact
configured public resource host/origin and issuer origin. Configure proxy
forwarding consistently; do not disable these checks to make a URL work.

## Verify before connecting users

1. Verify the deployment's exact served version and migration receipt.
2. Public unauthenticated `GET /.well-known/oauth-protected-resource/mcp`
   on the MCP origin must return the configured resource and issuer.
3. An unauthenticated MCP request must return 401 with a `WWW-Authenticate`
   discovery challenge. No data request is public. A request carrying any
   static credential (an infrastructure, personal or capability token) must
   return the same 401 challenge as the unauthenticated one. Read any token
   from its file in the command, never onto the screen.
4. `GET /.well-known/oauth-authorization-server` on the web origin must
   advertise `/authorize`, `/token`, S256 PKCE, configured scopes and public
   client auth method `none`.
5. Follow the packaged manual chapter `06-mcp-connections.md` to connect
   Claude. Its OAuth client setting is **Use your own OAuth client** with
   `precis-claude` and an empty secret; the static-header beta is not needed.
6. Create two connections for one account and one for a second account.
   Revoke one from `/account/connections`; confirm only that connection fails.
   Confirm the two accounts see their own learning data, a full grant reaches
   the intended MCP surface, and a learning-only grant cannot call other kinds.

Tests use the dev DB through `scripts/test`; never exercise token writes on
production as a test. The final user connection is the operator's acceptance
check, not permission for agent write-path testing on production.

## Operational limits

Authorization state and codes live for at most ten minutes; access tokens
for one hour; refresh/grant lifetime is capped at thirty days. Refresh rotates
the token atomically. Replaying a consumed refresh token revokes that grant's
family. Disable an account or revoke its connection to invalidate requests
and renewal. Removing a configured client or capability also fails closed.

Secrets are random and digest-only in PostgreSQL. Never log request bodies,
Authorization headers, token responses or redirect Location headers carrying
authorization codes. The authorization store also bounds outstanding requests.

Tailscale Funnel has no request-rate or concurrency limits, and uvicorn runs
with its defaults. The controls that exist are in-process: the web gate's
failed-sign-in lockout (per login and address, per address, and a global
per-login ceiling; it also covers the podcast route's Basic path), a cap on
concurrent password verifications, the 64 KiB cap on OAuth protocol bodies,
and the MCP bearer check before any body is read. Volumetric load is not
handled in-process. The options are a real reverse proxy in front of the
gateway, or taking the funnel down (`--tags tailscale` with the funnel
variables false).

## External security assessment

Use an independent machine outside the tailnet (for example a laptop on a
phone hotspot with Tailscale disconnected) to verify the configured HTTPS
routes, TLS certificates, public OAuth discovery and authentication refusals.
Cover every funnel-capable port (443, 8443 and 10000), not only the configured
one: a mapping left on another port is exactly what this check should find.
Funnel's public DNS points to shared Tailscale relay infrastructure: a blanket
IP port scan measures that relay rather than this gateway. Scope such testing
to the owned hostname with TLS SNI, and inspect `tailscale funnel status --json`
plus local socket bindings for the actual exposed services. A full TCP scan is
appropriate only for a separately owned public gateway address.

Against production, use low-volume unauthenticated probes and invalid credentials:
MCP requests without or with a random bearer must refuse with 401; `/authorize`,
account and consent management must require login; malformed OAuth requests must refuse,
and no token or authorization code may appear in logs or redirect to an
unregistered callback. Do not follow attacker-controlled redirect probes.
Run active scanners such as ZAP and the stronger OAuth checks (PKCE omission,
wrong verifier, code replay, scope escalation, cross-account access, refresh
replay and selective revocation) against an isolated dev DB with test users.
Production grants are created only by real user acceptance through Claude.
Passing these checks is evidence for the tested boundaries, not a guarantee
that every application kind is free of vulnerabilities.

Protocol references, checked 2026-11-10 UTC:
[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[Claude OAuth](https://claude.com/docs/connectors/building/authentication),
[Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel).
