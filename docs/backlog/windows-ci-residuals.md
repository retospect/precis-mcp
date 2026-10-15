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

Still open — its own sweep, ~120 FAILEDs on the leg: tests that exec
extensionless scripts die `WinError 193` (test_fleet_report ×25,
test_claude_gated ×15, and most of the fleet/deploy/script harnesses);
`signal.SIGHUP`/`os.killpg` inside test bodies (test_skill ×7,
test_server_transport ×6, test_mcp_modalities, test_mcp_liveness,
test_acting_user); one-offs (UnicodeEncodeError charmap in
test_deploy_orphan_worker_lanes, PermissionError in
test_python_worktree_roots, test_python_tests_view, test_fix_gripe).
Several FAILEDs are nightly-suite drift red on the Linux legs too
(test_pcb_dead_exports, test_skill_vocabulary, the mcp verb-kwarg parity
ratchet) — not Windows work. The O_DIRECTORY guard and the Py 3.12 urllib
circular-import fix shipped in f23ba89f0.
