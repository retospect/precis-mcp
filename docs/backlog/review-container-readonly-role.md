# Review container: flip to agent_ro + close the gripe tool-layer gap

The DB half shipped (migration 0079 SECURITY DEFINER `file_gripe_readonly`;
`GripeHandler._create` routes through it), so gripe filing survives an
agent_ro connection. Open: (1) actually set PRECIS_MCP_DB_ROLE=agent_ro on
the review container — an ops decision, no longer code-blocked; (2) the tool
layer (`src/precis/workers/envelope.py::disallowed_tools`) still drops the
whole `put` verb for write:none envelopes — exposing gripe-filing as its own
MCP tool conflicts with the seven-verbs invariant asserted in server.py: a
design call for Opus/Reto, not mechanical; don't blind-flip. Also: the OAuth
token appears in `docker inspect` Config.Env — move secrets to `--env-file`
if the never-in-inspect guarantee matters.
