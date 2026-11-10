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
PRECIS_OAUTH_RESOURCE=https://precis.example.com:8443/mcp
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
the scopes actually requested and approved. Manual personal tokens retain
their existing learning-only boundary.

The web launchd template includes the private `precis_identity_env` mapping;
keep actual coordinates in that overlay. Supply the same values to the
dedicated MCP service's environment. Both must use the same migrated database
and pinned application version. Restart both after configuration changes.
The MCP service still requires its own `PRECIS_MCP_TOKEN` for infrastructure
access. Keep that credential separate from user app authorizations.

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

Reuse the web deployment's persistent Funnel-to-loopback pattern. Keep the
web listener on its existing HTTPS 443 mapping. Run the dedicated MCP service
on loopback port 8766 (confirm it is free), then the deployment owner can set:

```sh
tailscale funnel --bg --yes --https=8443 http://127.0.0.1:8766
tailscale funnel status --json
```

Record the intended mapping in deployment automation/private inventory, just
as the web role does. The public MCP URL is the returned HTTPS origin on
8443 followed by `/mcp`. Do not run `serve` on that port afterward: it changes
public Funnel exposure to tailnet-only. Keep the raw backend on loopback.
Funnel supplies HTTPS, not caller authorization.

The MCP SDK's Host/Origin checks retain localhost allowances and add the exact
configured public resource host/origin and issuer origin. Configure proxy
forwarding consistently; do not disable these checks to make a URL work.

## Verify before connecting users

1. Verify the deployment's exact served version and migration receipt.
2. Public unauthenticated `GET /.well-known/oauth-protected-resource/mcp`
   on the MCP origin must return the configured resource and issuer.
3. An unauthenticated MCP request must return 401 with a `WWW-Authenticate`
   discovery challenge. No data request is public.
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
authorization codes. Keep authentication request and concurrency limits at
the public ingress; the authorization store also bounds outstanding requests.

Protocol references, checked 2026-11-10 UTC:
[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[Claude OAuth](https://claude.com/docs/connectors/building/authentication),
[Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel).
