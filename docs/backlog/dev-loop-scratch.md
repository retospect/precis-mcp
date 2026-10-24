---
status: in-progress
title: Dev loop — scratch runs, a per-worktree MCP server, file-backed trace views
pillar: 3d-design
prio: high
---

# Dev loop — scratch runs, a per-worktree MCP server, file-backed trace views

## Why

Reto 2026-10-23: "work on the small dev loop first, make it awesome, it can
be deployed. And it can run parallel to many builds, fast and independent."
A session working on 3D code (atom tiler, assembly, PCB) should write a
script, run it on the host, see the result through the precis MCP and
change the MCP code itself, all within seconds. It should not need an LLM
driving a container, a dev DB, or a release before the MCP shows a change.
Order across domains: atoms, then PCB, then macro (Reto 2026-10-23).

What already exists (r87, 3c45d9f2f): `precis_geom.trace` (`@traced`
capture, `Tracer.dump()` → schema-1 trace document),
`precis_se/workspace_import.py` (reads an `out/` dir: `manifest.json`,
`trace.json`, `blocks/*.extxyz|*.bonds.json|*.cad.json`; goldens under
`tests/workspaces/data/`), and `precis/structure/importers/extxyz.py`.
The session MCP is served behind a Caddy proxy with a blue/green backend
(`precis-mcp-http-ensure.sh` in the infrastructure checkout; env overrides
`PRECIS_MCP_HTTP_NAME`, `PRECIS_MCP_HTTP_PORT`, `PRECIS_MCP_SRC`). A
restarted backend 404s the old `Mcp-Session-Id` and Claude Code
re-initialises by itself, with no manual `/mcp` reconnect.

## Slices

Each slice ships alone. D0, D1 and D3 are independent and can run in
parallel. D2 needs D0's layout and is easiest to dogfood with D1.

| Slice | What | Acceptance |
|---|---|---|
| D0 | scratch dir + host run + `jiggle` relaxer | a scratch script on the Mac writes an `out/` that `workspace_import` accepts; `jiggle` passes the relax report tests |
| D1 | per-worktree MCP server (read-write) | edit a view in a worktree → the same session's next `get` shows it, with no release and no manual reconnect |
| D2 | file-backed trace views, atoms first | `get` on an atom/group in a scratch run shows its creator frame (file:line, call k of n, params); the python view of that line shows what it made |
| D3 | spec: local edits on processed designs | `docs/backlog/local-edits-processed-designs.md` at status draft |

### D0 — scratch dir, host run, `jiggle`

- `scratch/.keep` committed; `.gitignore` gets `scratch/*` and `!scratch/.keep`.
  Every worktree has the dir, and nothing in it is ever committed.
- `uv run python scratch/x.py` works on the macOS host for `precis_geom`
  plus the atom tiler path (`precis_se.atomic`). Find and fix (or document)
  any import that pulls container-only deps. A smoke test imports the
  host-run surface without the container extras.
- `precis_geom` gets a helper that writes a run's outputs in the importer's
  layout: `out/manifest.json`, `out/trace.json`, `out/blocks/…`, the same
  shape as `tests/workspaces/data/two_blocks/out/`. Default target is
  `scratch/out/<run-name>/`. Prod import later reads the same files.
- **`jiggle` relax rung** (Reto: "FakeRelaxer… jiggle things about a random
  amount so we can test how the system tolerates moving/relaxed things").
  It is a new `fidelity='jiggle'` branch in `precis/structure/relax.py::relax`
  next to `clean`/`geo`/`emt`. It applies a seeded Gaussian displacement
  (`sigma` default 0.05 Å, capped at 3σ per atom, fixed axes respected),
  fabricates a monotone convergence curve, and returns the same report
  shape as a real rung (converged, n_steps, max_force, energy). It emits
  the same progress lines and, once gr508493 lands, the same per-step
  checkpoints. Option `die_at_step=k` raises mid-run so kill/resume can be
  tested. It is never chosen by default and only exists when asked for by
  name.

### D1 — per-worktree MCP server, read-write (built; review-ready)

Reto 2026-10-23: the worktree server is read-write and **replaces** the
normal precis server in that session ("then we just need the one and there
is no confusion… I am happy enough with that risk"). Built as
`scripts/wt-mcp` (`up|down|status|restart|watch|claude`, library
`scripts/wt_mcp.py`, tests `tests/test_wt_mcp.py`); operator doc:
`deploy/mcp-http/README.md` §Worktree servers. Measured on dl-d1
(2026-10-23, host docker under ~90 gate containers): first `up` 1337 s
(proxy + backend create/start waited minutes on docker; the backend's
prepare step itself was copy 77 s + dependency sync 50 s + metadata 8 s);
edit a skill file → the worktree server serves the new text 28.8 s after
the edit (2 s debounce, then a child respawn behind the held port of
21–25 s, most of it the `/src`→`/app` copy on the loaded host); a session
re-initialises on its next call, no `/mcp` reconnect. The instructions
open with `worktree server dl-d1 @ ea1eae8958d2` (sha resolved through
`PRECIS_PYTHON_GITDIR_MAP`).

Security review round 2 (2026-10-23) closed: the ensure script must carry
worktree mode (an older copy is refused, never the main checkout served by
accident); the migration guard fails closed (committed, staged and
untracked files vs `origin/prod`, any git error refuses, checked before
every watcher restart and recovery, `PRECIS_AUTO_MIGRATE=0` pinned); the
reaper tears a tree's server down by name from its own checkout, bounded,
and keeps the tree when teardown fails.

Left for Reto: the installed ensure script (the infrastructure checkout's
copy) was NOT touched (the permission gate refused the copy; a `.pre-d1`
backup exists, identical to the installed file). The repo copy
`deploy/mcp-http/precis-mcp-http-ensure.sh` carries the worktree mode;
`scripts/mcp-http-install --apply` installs it (default-path container
spec verified hash-identical). Until then `scripts/wt-mcp` uses the tree's
own repo copy by default and refuses a script without worktree mode.

### D2 — file-backed trace views (atoms, then PCB, then macro)

- A read-only source for `wt-<tree>/scratch/out/<run>/`, resolved like the
  python and md kinds resolve `wt-<tree>` roots. `get(kind='structure',
  id='wt-<tree>/scratch/out/<run>')` renders the run's blocks without
  importing them.
- **Trace lines on existing views, not a new viewer** (Reto: "just more
  bits on the fisheye on the atom or group of atoms. Or the LOC in python
  kind, or a spec memory"):
  - atom or atom group: "made by `<qual>` at `<file>:<line>`, call k of n,
    params …" plus the parent frames up to the script entry;
  - python kind, on a function or line in that tree: "made N atoms in run
    R" with a pointer;
  - one renderer, two sources: the scratch files now, the imported rows
    (0230 trace tables) in prod. Do not write a second implementation.
- **Normalise before writing** (Reto: "normalize it nicely before
  writing"): worktree-relative paths, `%.6g` params (the trace already
  does this), stable frame keys (`trace_schema.frame_key`), sibling
  collapse applied, no absolute home paths or hostnames. `Tracer.dump()`
  is the one place this happens, so prod import and the dev view read
  identical files.
- PCB (D2p) and macro (D2m) follow as their own slices once the atom path
  is in use.

### D3 — spec: local edits on processed designs

Write `docs/backlog/local-edits-processed-designs.md` (draft). Seed from
the 2026-10-23 discussion:

- **An edit is a build step** (swap H→F, fillet, hole, move a part) that
  selects its target by stable identity (atom uid, refdes, named face),
  never by index, on top of a stored version. Everything upstream is a
  cache hit (the build-step stack ruling, se-flow-kernel §4b).
- **Local repair, verify, widen only on failure:**
  - atoms: buffer relax from the stored relaxed geometry, residual-force
    check, full relax warm-started only if it fails;
  - PCB: rip up and re-route only nets touching the change, others locked,
    then DRC;
  - CAD: rebuild, then DRC and mould checks on affected faces.
- **Record:** a new immutable version whose parent is the old one, the
  step (op, params, selection, script sha or author), the repair record
  (radius or nets, touched count, residual or DRC verdict, steps) and a
  diff view against the parent.
- **Prerequisite:** identity survives relax and routing. Test it with
  `jiggle` (D0).

## Not in scope

The sandbox container lane (sandbox-python-script) and workspace `run`
(structure slice 1) stay as the path for agents inside the system. Heavy
physics stays on cluster jobs; the dev loop uses `jiggle`, `emt` or tiny
fixtures.
