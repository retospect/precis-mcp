---
status: draft
title: pcb exports write to any agent-supplied directory; confine dir to the export root
pillar: 3d-design
prio: normal
---

# pcb exports write to any agent-supplied directory; confine dir to the export root

## Motivation / why

`PcbHandler`'s file exports take `args['dir']` as given:
`Path(str(raw_dir)).expanduser()`, then `mkdir(parents=True)`, then a
write. So an agent-supplied absolute path writes wherever the serving
process can write. The sites are `_render_export` (bom/cpl/netlist/dsn/
mechanical), `_render_gerber`, `_render_epro` and the Freerouting
`_render_route` (since deleted, 2026-10-25): four places doing the same
unconfined write. The
filename is `f"{slug}.<ext>"`. A slug is a precis slug and is
constrained at mint, but the export never re-checks it, so the
confinement rests on an invariant held somewhere else. Found by the
orchestrator's round-1 diff review, 2026-10-02. It is not a regression
(epro copied the gerber pattern).

## In scope

- One helper (`_export_dir(slug, args)`) that every export calls. With no
  `dir`, it returns `<PRECIS_CORPUS_DIR>/pcb/<slug>/` as today. With a
  `dir`, it resolves the path (`resolve()`, symlinks followed) and refuses
  it with BadInput unless it lies under the export root (the corpus
  `pcb/` dir or the temp dir).
- Sanitise the filename stem (the epro writer's `safe_stem` is the
  model) instead of trusting the slug.

## Explicitly NOT in scope

- Changing where exports land by default.
- Any network-facing change; this is local filesystem confinement only.

## Acceptance criteria

- `view='gerber'|'epro'|'bom'|…` with `args={'dir': '/etc'}` (or
  `../../x`, or a symlink pointing out of the root) raises BadInput and
  writes nothing. One parametrized test covers all four sites.
- A `dir` under the export root still works; no `dir` still lands where
  it did.

## Target + blast radius

`src/precis/handlers/pcb.py` (`_export_dir` and its four callers). Skill
`precis-pcb-*-help` lines that advertise `args={'dir': ...}` say
"under the export root". Thread: `pcb-easyeda-round-trip`.

**Landed 2026-10-21 (worktree bug-papers-pcb):** `_export_dir(slug, args)`
resolves `args['dir']` (symlinks followed) and refuses anything outside
the corpus `pcb/` root or the system temp dir with `BadInput` + `next=`.
All five callers — `_render_export` (bom/cpl/netlist/dsn/mechanical),
`_render_gerber`, `_render_fab_package`, `_render_epro` and the
Freerouting `_render_route` — go through it, and every file stem goes
through `epro_write.safe_stem`. Tests in `tests/test_pcb_fab_export.py`
parametrize the five sites over out-of-root/`..`-escape/symlink dirs plus
the in-root and no-`dir` happy paths.
