# Watch: plan_tick no-precis-tools bursts (root cause FOUND 2026-08-15)

**Resolved-in-code, watch remains.** The 2026-08-15 15:26+ burst (23 ticks,
~$13/day) was NOT deploy-window venv-rebuild residue: the melchior worker
runs with `PRECIS_AGENT_CONTAINER=1`, so every tick executes `claude -p` in
a throwaway container. The container path re-injects the adopted DSN — which
is password-free by design (§L, password lives in host `~deploy/.pgpass`) —
and **no pgpass exists inside the container**, so every in-container
`precis serve`/CLI call died `fe_sendauth` regardless of the env pins
(gr208726's `PGPASSFILE` fix pins a path that isn't mounted in the image).
Fix shipped: `secrets.complete_dsn_password()` completes the password from
the host pgpass before the DSN crosses the container boundary
(`claude_agent.py` container branch), plus loop-proofing (success-counting
tool guard, runner-side `verdict: halt` honoring, `halt:planner-stuck`
parking, `agent-ticks-toolless` condition probe).

Historical suspicion (kept for the record): a tick live across a deploy's
`/opt/asa/venv` reinstall could still spawn a half-installed `precis serve`.
Zero failures Aug 7–14; if a `no-precis-tools` burst recurs in a deploy
window with the container fix deployed, correlate against the gateway
venv-reinstall timestamps and check sidecar stderr for import/entrypoint
errors rather than `fe_sendauth`. Fix shape if confirmed: drain the
claude_inproc lane during venv replacement, or point mcp.json's `serve` at
the worker's own venv.

Un-halt checklist after the fix deploys and a tick round-trips green:
remove `halt:env-outage-20260815` from todos 201737, 204876, 200460, 200984
(204878 was already soft-deleted).

test: a container-path DSN with a matching pgpass entry crosses the boundary
with its password completed and never appears in argv
(tests/test_claude_agent.py::test_container_dsn_password_completed_from_pgpass);
an all-calls-errored tick is not marked succeeded
(tests/test_plan_tick_claude.py::test_claude_exit_all_tool_calls_errored_is_not_success).
