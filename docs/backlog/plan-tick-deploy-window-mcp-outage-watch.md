# Watch: plan_tick no-precis-tools bursts in deploy windows (post-gr208726)

gr208726 closed (a19f829a, 2026-08-15): darwin worker units now pin
`PGPASSFILE`, removing the libpq home-dir-resolution fragility that explains
the short-lived-CLI `fe_sendauth` failures. Unexplained residue: in the
2026-08-15 15:26–17:17 UTC burst (8 ticks) the tick's **MCP sidecar** also
failed to register — and the sidecar already had `PGPASSFILE` pinned via
mcp.json since 41f6a586, so the home-dir mechanism can't be its cause. Prime
suspect: the deploy rebuilding `/opt/asa/venv` (the mcp.json `serve` command
path) mid-tick — a tick live across a venv reinstall spawns a half-installed
`precis serve`. Deploy-window-bounded either way; zero failures Aug 7–14.

Not actionable until observed again. If a `no-precis-tools` burst recurs in a
deploy window **despite** the env pin: correlate the burst minute against the
gateway venv-reinstall task timestamps in the ansible log, and check whether
the failing ticks' sidecar stderr shows an import/entrypoint error rather than
`fe_sendauth`. Fix shape if confirmed: pause/drain the claude_inproc lane (or
skip claiming) while the deploy replaces the venvs, or point the worker's
mcp.json `serve` command at the worker's own venv instead of asa's.

test: n/a (observational; regression tests for the env pin shipped in
tests/test_deploy_templates.py).
