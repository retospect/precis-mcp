---
status: draft
title: pcb — argue with the design: anchor-grammar reference + residual QA
prio: low
model: opus
pillar: 3d-design
---

# pcb: argue with the design — shipped; anchor-grammar reference

The mechanism shipped across slices 1–2, `%TO.C`, and the se back-port
(`git log`: `feat(pcb): argue with the design …`, `feat(pcb): argue slice 2
…`, `feat: gerber %TO.C …`, `feat(se): argue box on the 3D viewer …`).
Truth lives in the code, not here: `src/precis/pcb/argue.py` (handle
grammar + resolver — `resolve`/`classify`/`act`/`vitals`), the `pcb_argue`
job type (`src/precis/workers/job_types/pcb_argue.py`), and
`render_fab_svg`/`gerber`/`gerber_view` for the stamped `data-handle`s.
This file stays as the **anchor-grammar reference** other items cite
(`pcb-keepout-does-not-bind.md`, `pcb-platform.md`,
`pcb-lazy-netlist-and-checks.md`).

Handle grammar — the `data-handle` roster `render_fab_svg` stamps and
`argue.resolve` accepts:

| clicked | handle | resolves to |
|---|---|---|
| part body (extent of its pads) | `U_TEMP` | `pcb_instances.refdes` |
| pad (incl. a generated array cell) | `U_TEMP.3`, `ARR1.R3C4` | refdes + pin |
| net (track / via / pour) | `net:HV_RAIL` | `pcb_nets.name` |
| board feature | `feature:outline` | `pcb_features.ftype` |
| nothing (whole-design argument) | *(no handle)* | the design |

## Left

- **Acceptance criterion 1 — manual, not automatable here.** The
  cross-document click on `/pcb/ewod-dogfood-1` (1280×800) has no code
  home: the repo has no JS test runner, so `static/pcb-argue.js`'s
  `<object>`-document listener has no unit test. Server-side coverage
  stands in for the handle contract (`tests/precis_web/
  test_pcb_object_handles.py`, `tests/test_pcb_selection_objects.py`, and
  `test_selection_and_argue_scripts_share_the_attach_event`). Manual check:
  open the page, click an electrode pad, the HV507 body and a track — the
  box must read e.g. `ARR1.R3C4 U1 net:HV_RAIL`. File a gripe if a JS
  runner ever lands so this becomes a real test.
- **`service_config` row — decided: not owed.** `pcb_argue` is a *job_type*,
  not a worker *pass*; `service_config` gates the `job_inproc` lane it rides
  (`workers/registry.py`), never a per-job_type name — the same as every
  other job_type. Enablement is the mint door (`put(kind='job',
  job_type='pcb_argue', parent_id=<pcb ref>, params={'pcb_ref_id': N,
  'limit'?, 'question'?})`) plus the live lane; its console record is the
  `precis-job-help` listing (`DESCRIPTION` + `known_job_types()`). A
  per-job_type kill switch would be new machinery — gripe it if operators
  ever want one.

The gr335242 se/nm viewer remainder (argue-with-points picker, notes layer,
pre-solve stick figure, and the `precis.cad.tessellate` per-triangle
facet-id blocker) is homed in `pcb-argue-backport-se.md`, not duplicated
here. Mirror `src/precis/pcb/argue.py` for the se/nm handle grammar.

**Non-goal, explicit (Reto):** no place or route button on the page.
Actions stay in the MCP/job surface; the page is for arguing, not driving.
