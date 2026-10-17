---
status: ready
pillar: platform
---

# Windows CI residuals after the skipif pass

(1) Watch `tests/test_render_sandbox.py::test_no_output_is_reported` — a real
timing flake deliberately NOT skipped so it isn't masked; if it becomes the
lone red, fix the flake, don't skip. (2) The skipif(win32) tests are
POSIX-only by harness, not product — the underlying behaviors aren't
Windows-tested at all; revisit only if Windows becomes a deployed runtime.

## Nightly leg is broadly red (2026-10-15, windows-latest 3.12, run 38717559796)

Rot accumulated since the 27-test skipif pass; none of it is the watched
flake. Cleared 2026-10-15: eight test modules errored at collection on
module-level POSIX imports (`signal.SIGHUP` in `install_watchdog` and
`mcp_supervisor`; `fcntl` in `remote/slurm`, `remote/credentials`,
`utils/stall_capture`, `scripts/fleet-review`) — now `pytest.skip(
allow_module_level=True)` on win32; and ~58 pcb-epro ERRORs from `.epru`
fixtures checked out CRLF by autocrlf and zipped via `read_bytes()`
(`stream.split("\n")` leaves the `\r`, the trailing `|` is not stripped, the
body stops being JSON) — pinned `eol=lf` in `.gitattributes`.

Cleared 2026-10-17 (137 FAILEDs on run 38883842456, windows-latest 3.12;
now skipif(win32) naming the POSIX-only API, no POSIX change): WinError 193
on shebang/`#!/bin/sh` stubs (test_fleet_report, test_claude_gated,
test_deploy_compute_build_assert's `rig`); `signal.SIGHUP` at import of
`install_watchdog` (test_skill, test_server_transport, test_mcp_liveness,
test_acting_user, test_mcp_modalities); `fcntl` in product paths
(test_codex_efficiency_hook, test_retry_transient, test_drain_sleep,
test_bib_parse, test_si_discovery); `os.sysconf`/`os.killpg`; whole POSIX
harness modules (test_fleet_codex, test_deploy_failure_reporting). Two
one-offs made portable via `PYTHONUTF8=1` (test_guide_scripts,
test_testmon_exit_guard) — cause unconfirmed.

Still open, skipped without a pinned root cause: 7 test_mcp_modalities
git-identity tests (`skill._dotgit_info` path handling — look in src/),
test_select_tests (module key), test_fix_gripe (rmtree of read-only git
objects), test_python_tests_view (backslash coverage keys),
test_python_worktree_roots (PermissionError on `.git` pointer),
test_pcb_maze_negotiation (coarse timer vs zero deadline in
`realize._negotiate`; needs a src fix), test_se_tunnel_reservoirs (1.716 vs
1.70 numeric drift). Unmeasured on Windows until the next nightly or a
`workflow_dispatch` full=true run.

Several FAILEDs are nightly-suite drift red on the Linux legs too
(test_pcb_dead_exports, test_skill_vocabulary, the mcp verb-kwarg parity
ratchet) — not Windows work. The O_DIRECTORY guard and the Py 3.12 urllib
circular-import fix shipped in f23ba89f0.
