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
`_render_route`: four places doing the same unconfined write. The
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
- Sanitise the filename stem (the epro writer's `_safe_stem` is the
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
