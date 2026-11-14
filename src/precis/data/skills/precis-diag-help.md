---
id: precis-diag-help
family: se
title: precis — failed-run reports and selector probes
summary: what a failed workspace-script run tells you (failing op, script file/line, fix hint) and o.probe() to check what a selector resolves to without running anything
answers:
  - what does a failed workspace script run report back?
  - how does a run failure name the failing op?
  - how do I read the failure an import_workspace refusal carried?
  - how do I ask what a selector resolves to without running anything?
applies-to: workspace script runs that fail (out.run / import_workspace); o.probe in workspace scripts
status: active
tags: [design]
kinds: [se, cad]
---

# precis-diag-help — failed runs and probes

## What a failed run reports

A failed workspace-script run surfaces as a `BadInput` refusal from
`import_workspace`. Its text carries:

- the error type and message (`manifest.json: run failed — <type>:
  <message>`);
- the failing op: the user-facing traced call directly under your
  script, named by its trace frame key, with the script file and line of
  its call site;
- a fix-and-rerun next step.

When no traced call failed (a bare raise after an earlier traced call),
the location is the script site where the error was raised. Inner traced
frames the failure passed through are listed outer-to-inner.

When the failure carried a structured report (a hexfold build/check
refusal), the refusal lists one entry per finding instead: the finding's
own code (e.g. `port.unknown`), message and fix hint, all anchored on
the op that raised.

Statuses a check can report: `ok` < `info` < `not_checked` < `warn` <
`error`. `not_checked` is a coverage gap — a declared check that could
not fire — never a silent clean.

## Probes — what does this selector resolve to

Nothing runs. In a workspace script,
`o.probe(scene_or_selector, label=...)` resolves a pg object (structure
scene, flattened `pga.Geom`, hexfold `Net`) or a `frame:<key>` selector
against the live trace, prints one `probe <label>: resolves to N <kind>`
line and records `{label, count, kind, digest}` on the calling trace
frame; the `view='trace'` frame card lists it there. On a board design,
`pcb.local.probe(board, "pads_of('U3')")` or a `nets:` pattern resolves
the same way, with no op run. An unresolvable selector raises a refusal
whose `next` names the fix; inside `out.run` the failed run's record
carries it.

## See also

- [[precis-workspace-help]] — the script loop that produces the runs this layer reports on
- [[precis-se-help]] — the se call surface the import refusal lands on
