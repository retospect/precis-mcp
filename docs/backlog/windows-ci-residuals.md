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

Round 2 (2026-10-17; before: nightly run 39015281827, after: dispatch run
39028873157 on `ci/windows-ci2-diag`, windows-latest 3.12 and 3.13 both
green). Of the 15 "Windows" failures listed from 39015281827 only 6 were
Windows; the other 9 are macOS-leg failures (below). Fixed in src, POSIX
unchanged: `stall_capture` imported `fcntl` at module top (crossref);
`install_watchdog` read `signal.SIGHUP` at import, so `skill._dotgit_info`
swallowed the ImportError and returned `{}` (the 7 `test_mcp_modalities`
checkout-identity tests, skip removed; `DRAIN_SIGNAL` falls back to SIGTERM
and `install_drain_signal` declines without SIGHUP); `realize._negotiate`
compared `monotonic() > deadline` (now `>=`, so a coarse clock honours a zero
budget; skip removed).

Left: (1) 5 `test_se_tunnel_reservoirs` tests, skipif(win32) (`_WIN_TRIM`).
Diagnosed on run 39022671881: on Windows the terminator trims 13 tube atoms
(`tb/(46,0,A)`, ...) and one 5-atom seam face (ordinals 6614, 4307, 4318,
6616, 6615) vanishes entirely, so `tunnel_strain` divides by zero on the empty
face. POSIX does not trim that face; the cap-clash trim differs across
platforms (float tie-break, undiagnosed). `tunnel_strain` is deliberately
unchanged: find why the trim differs, then un-skip. With an empty-face
`continue` in `tunnel_strain` the five passed on Windows (run 39028873157),
so no further drift was hiding behind it. (2) Still skipped without a pinned cause:
test_select_tests (module key), test_fix_gripe (rmtree of read-only objects),
test_python_tests_view (backslash coverage keys), test_python_worktree_roots
(PermissionError on the `.git` pointer). (3) macOS-leg reds in the same
nightly, not Windows: test_install_watchdog (`/private/var` realpath),
test_pcb_generator_version_tripwire[ewod_pad_array], test_remote_bootstrap
(4), test_remote_ssh (PermissionError), test_structure_relax_xtb (worker
crash), test_skill table chunk id (3.13 only).

Several FAILEDs are nightly-suite drift red on the Linux legs too
(test_pcb_dead_exports, test_skill_vocabulary, the mcp verb-kwarg parity
ratchet) — not Windows work. The O_DIRECTORY guard and the Py 3.12 urllib
circular-import fix shipped in f23ba89f0.
