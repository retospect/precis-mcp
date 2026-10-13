---
status: ready
title: pose/rot vector unit ingest — strict, three accepted forms, canonical hint
prio: high
pillar: 3d-design
---

# pose/rot vector unit ingest — remaining rounds

SHIPPED (2026-10-12): `precis.utils.units.parse_vector_quantity` (+
`format_vector`, `vector_canonical`); se/nm `pose`/`rot` ride it through
`blocktree.ops._pose_vec` — strict (bare nonzero rejects with a hint
echoing the caller's vector under nm/mm/m or deg/rad), three forms
(trailing unit = canonical; per-component; `pose_unit`/`rot_unit` beside
the vector), bare `0` legal, e-notation fine, mixing rejects. Fixtures,
`precis-se-help`/`-fasten-`/`-print-help` examples and the
never-convert-yourself directive (overview, se, cad, structure, calc help)
swept. Not touched: `add_port` `direction` (unitless).

SHIPPED (2026-10-12, ruled by Reto 2026-09-12):

- **Echo on write** (se only): every quantity-ingesting se op
  acknowledges in canonical form (`understood: pose = [3 0 0] nm`, via
  `format_vector`, once per field). The result channel is
  `op_notes_scope` (`precis.utils.units`), bound by
  `blocktree.ops.apply_ops(notes=)` and inherited by nested applies;
  `precis_se.atomic.apply.apply_ops_with_atomic` flushes each op's notes
  into the echo after that op's own lines. Tree-view `pose=`/`rot=` and
  the ports-table cells render via `format_vector` (unit on every
  value). OPEN: nm-side writes do not echo (the retired `nm` kind's
  atomic ops share the se walk but were not swept as a checklist), and
  cad writes (`put`/`derive` DSL) do not echo at all — no
  `understood:` lines there yet.
- **TOON column formatter**: `format_quantity_column` in `units.py` —
  one common unit per homogeneous column (mantissas ~0.001–10000) in the
  header (`gap (nm)`), bare cells; per-value neat units with no header
  unit when no common unit fits; never both. Wired at se's clearance
  digest + sweep hits and cad's probe (point/ray/arc) + connectivity
  columns.
- **prefunits** per call (`prefunits=nm,deg,kg` or a
  `{dimension: unit}` map): `args.prefunits` on se/cad `get`, payload
  `prefunits` on se `put`/`edit`, and a `prefunits:` DSL directive for
  cad. Display only; resolved precedence: explicit arg > per-call >
  used (respond-in-kind) > sticky `meta.prefunits` > neat default.
  Design-meta shipped with it: `meta.prefunits` on the se ref, the
  cad `prefunits:` directive on `spec.meta`.
- **Respond-in-kind** for cad/se query answers (gr336352's rule):
  `capture_used_units` records the unit the caller authored inside a
  call (`record_used=False` marks stored-data re-parses so they can't
  steer it); `format_quantity`/`format_vector`/the column formatter
  resolve through it. (`nm` reads are `se` reads — the kind is retired;
  nothing nm-specific was threaded.)

## Angle units across every MCP surface (gr336352)

SHIPPED: shared `parse_angle_deg` / `format_angle_both` /
`format_angle_in_kind` in `precis/utils/units.py` (`\circ` accepted);
calc (`sin(30 deg)`, bare numeric rejected, `view='rad'` removed, inverse
trig answers in both forms); pcb `rot` (bare = declared degrees, unit
strings accepted, `rot_deg` alias); structure `add_adsorbate rotate`
(unit required). Already explicit: cad/se joint `state`/`limits`, cad
`rot:` DSL. Self-named, left: draft `camera.*_deg`, cad `sweep_deg`.

SHIPPED (2026-10-12): pcb `group_offset.rot` now rides the same
`parse_angle_deg` grammar as instance `rot` (bare = declared degrees,
unit strings accepted) — `precis/pcb/ir.py::_offset_rot`; respond-in-kind
for cad/se query answers shipped with the display-preference scopes
above.

## Notation robustness eval (gr336349)

Open question whether LLM design authors do better with the canonical
trailing-unit form, e-notation, or per-component units. An A/B over
`src/precis/llm_eval` (harness, tasks, scorers) would measure
mis-scaled-quantity rates per notation. Who: needs-Reto: wants data before committing the canonical form's
strictness; agent-code builds the eval.

**Status and ruling 2026-10-12 (Reto, design-fold).** The eval is built:
`src/precis/llm_eval/notation.py`, 16 tasks over length, force, mass and
volume from nm to km, with arms `si_prefix` / `e_notation`. Haiku 4.5
(medium, repeats 1) scored exact 16/16 on both arms. The tasks are at
ceiling and do not separate the arms. Next steps:

1. Rerun on a weaker or local model (gpt-oss on castor, repeats 3).
2. If that also saturates, keep both notations (status quo) and close
   gr336349.
3. Harder tasks (mixed prefixes, ranges, intent-to-value) only if the
   weaker model shows a gap worth isolating.
