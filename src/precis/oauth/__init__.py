"""OAuth connections bind a web account to one independently revocable app.

The pinned MCP SDK owns protocol parsing, redirects and PKCE checks; Precis
owns consent, persistence and atomic code/refresh consumption. State is shared
in Postgres because authorization and MCP can run in different processes.
Only digests persist for codes and bearer credentials. A grant is the revoke
unit, and refresh replay revokes its entire token family. Explicit deployment
scopes bound every grant; OAuth never confers the infrastructure principal.
Pending requests retain a bounded, ten-minute SDK parameter snapshot as JSON
and revalidate it on load; durable grant and token fields use typed columns.
Enabled deployments default to the user-requested ``mcp:full`` capability,
including writes, while each connection retains its authenticated owner.
"""
