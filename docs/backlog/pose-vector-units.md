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
swept. Not touched: `declare_states` `port_pose_overrides` pose/rot
deltas (separate path, still bare SI), `add_port` `direction` (unitless).

OPEN, ruled by Reto 2026-09-12 (design stands, build what is left):

- **Echo on write**: every quantity-ingesting op acknowledges in canonical
  form (`understood: pose = [3 0 0] nm`, via `format_vector`). Blocked
  on a result channel: blocktree op functions return `None`
  (`apply_ops`), so it needs a per-op notes list threaded to the handler.
  Tree-view `pose=[...]` rendering (`precis_se/handler.py`) is still bare
  SI numbers.
- **TOON column formatter** in `units.py`: one common unit per
  homogeneous column (mantissas ~0.001–10000) in the header
  (`gap (nm)`), bare cells; per-value neat units with no header unit when
  no common unit fits. Never header unit + divergent cell suffixes.
- **prefunits** per call (`prefunits=nm,deg,kg`): preferred-unit override
  threaded into `format_quantity` / the column formatter, display only.
  **Sticky** form: `meta.prefunits` on the design/ref (per-call
  overrides); decide at build whether design-meta ships with it.
- **Respond-in-kind** for cad/se/nm query answers (gr336352's rule).

## Angle units across every MCP surface (gr336352)

SHIPPED: shared `parse_angle_deg` / `format_angle_both` /
`format_angle_in_kind` in `precis/utils/units.py` (`\circ` accepted);
calc (`sin(30 deg)`, bare numeric rejected, `view='rad'` removed, inverse
trig answers in both forms); pcb `rot` (bare = declared degrees, unit
strings accepted, `rot_deg` alias); structure `add_adsorbate rotate`
(unit required). Already explicit: cad/se joint `state`/`limits`, cad
`rot:` DSL. Self-named, left: draft `camera.*_deg`, cad `sweep_deg`.

OPEN: pcb `group_offset.rot` still takes bare degrees only; respond-in-kind
for cad/se/nm query answers (above) is not yet threaded through.
(se/nm `rot` vectors are now per-component angle quantities via
`parse_vector_quantity`.) Who: agent-code.

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
