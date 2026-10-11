---
status: idea
title: pcb view='drc' — errors first, filter by severity/rule, summary header
pillar: 3d-design
---

# pcb DRC view filters

From gr477797 (2026-10-10 gripe triage). On a board with 182 findings,
`get(kind='pcb', view='drc')` returns about 25 KB over two pages. It lists
warnings before errors and takes no arguments to filter.

- Order the output errors first, then warnings, then info.
- Open with a header of counts by severity × rule.
- Add `args={'severity': 'error', 'rule': 'via_pad_keepout'}` filters.

Owner: `src/precis/handlers/pcb.py::PcbHandler._render_drc` — it takes
only `ref_id` (no `args`), emits findings in run order, and its header is
one line (`# DRC — run … — N error(s), M warn(s)`) with no per-rule
breakdown. The view dispatch passes `args` to `_render_svg` but not here.
Who: agent-code.

## Acceptance

`view='drc'` on the 182-finding board fits one page with
`args={'severity':'error'}`, and its first line gives the counts.
