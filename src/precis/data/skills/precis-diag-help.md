---
id: precis-diag-help
family: se
title: precis — Diag/RunResult: the structured failure record anchored to a trace frame
summary: precis_diag is the domain-free diagnostics core (stdlib-only, in the precis-geom wheel). A Diag is one structured check/failure record — status ok|info|not_checked|warn|error, anchor{frame_key|op_id, file, line, chain}, refs, message, next, data — a superset of hexfold.report.Finding with tested conversion both ways. A RunResult is the run-level record (diags + a trace.json pointer). The first adapter is the CAD run failure (precis_geom.run_diag.failed_run_result): a failed out.run's manifest+trace becomes one error Diag anchored on the user-facing traced call under the nearest user ancestor — or one Diag per finding when the exception carried a serialized report (hexfold BuildError) — so a failure names its fix site by stable frame key instead of prose.
answers:
  - how do I get a machine-readable record of a failed workspace script run?
  - what is a Diag / RunResult and how do they map to hexfold Findings?
  - how does a run failure name the failing op's trace frame?
  - how do I read the failure the import_workspace refusal carried?
  - how do I ask what a selector resolves to without running anything?
applies-to: workspace script runs that fail (out.run / import_workspace), and any adapter turning domain failures into Diags
status: active
tags: [design]
kinds: [se, cad]
---

# precis-diag-help — structured failure records

`precis_diag` holds the shared diagnostic value types. One `Diag`
answers "what failed, where, and what next" for any domain that used to
report prose; a `RunResult` is the set of Diags one run produced plus a
pointer at the run's `trace.json`.

- `Diag.status`: `ok` < `info` < `not_checked` < `warn` < `error`.
  `not_checked` is a coverage gap — a declared check that could not
  fire — never a silent clean.
- `Diag.anchor`: `{frame_key | op_id, file, line, chain}`. `frame_key` is the
  trace frame's stable key (`trace_schema.frame_key`); `op_id` covers
  scriptless ops. `chain` lists inner traced frames in outer-to-inner
  order, each with `{frame_key, file, line}`; it is empty without nesting.
- `Diag.code` uses a lowercase domain namespace: `<domain>.<code>` (for example,
  `cad.run-failed`).
- `Diag` is a superset of `hexfold.report.Finding` —
  `precis_diag.diag_from_finding` / `hexfold.diag.finding_from_diag`
  convert both ways
  (`ok`/`not_checked` have no Severity; the Finding direction refuses
  them by name).
- `render(diags)` is the one text renderer — the MCP error path's
  shape; `"ok"` on an empty list.
- `schema_version`: readers accept older versions, refuse newer with a
  next step — the laptop wheel lags the server.

## The CAD run-failure adapter

`precis_geom.run_diag.failed_run_result(manifest, trace_doc)` turns a
`status="failed"` manifest plus the trace into a one-Diag `RunResult`:

- the Diag's code is `cad.run-failed`. Its anchor is the user-facing
  traced call directly under the nearest `kind=user` ancestor:
  `anchor.frame_key` names that op, and `file`/`line` name its call
  site in the user's script. `anchor.chain` lists the inner traced
  frames in outer-to-inner order, excluding the anchor itself;
- when no traced call failed (including a bare raise after an earlier
  traced call), the anchor uses the manifest's `error.at` script site;
- `message` is the import refusal's prose verbatim
  (`manifest.json: run failed — <type>: <message>`), `next` the same
  fix-and-rerun hint;
- `Run.fail` stashes it as `run.result` and prints the frame key on
  the stderr line; `import_workspace_run` raises `BadInput` and
  attaches the RunResult as `exc.result`. `RunResult.run` points to
  the run's `trace.json`.

In-process Python callers can read `exc.result.diags[0].anchor`.
An MCP caller receives the `BadInput` text: the error type and message,
user-facing op frame key when available, script file/line, and the fix-and-rerun next
step. The Python exception attribute is not an MCP response field.

## Report-carrying failures: one Diag per finding

When the run's exception carried a structured report, the RunResult is
one Diag per finding instead of the single `cad.run-failed`.

- `precis_geom.out._error_entry` serializes an exception's `.report`
  via `to_dict()` into `manifest.error.report`. The report is found
  through a bounded `__cause__`/`__context__` walk, so a
  `GeneratorError`-wrapped `hexfold.BuildError` still lands; a report
  that raises or will not serialize reads as absent.
- Each serialized finding (`code`, `severity`, `message`, `where`,
  `span`, `data`, `fix`) becomes its own Diag, verbatim — `fix` maps to
  `next`, and codes stay the domain's API (e.g. `port.unknown`), not
  `cad.*`. Every finding shares the one anchor: the op that raised.
- A malformed report or finding — not a dict, a `findings` list that
  is not dicts, a field that will not convert — loses the whole
  conversion and falls back to the generic `cad.run-failed` Diag;
  the adapter never emits a partial record.
- `hexfold.build`/`check` mint trace frames through a soft
  `precis_geom.trace` import (a no-op without the wheel), so a
  spec-compile `BuildError` is anchored on the `build` call frame.
- The live-object direction is `hexfold.diag.diags_from_report(report,
  *, anchor)` — the mirror of `finding_from_diag` for callers holding
  the `Report` object rather than the manifest.

## Probes (S1c)

`precis_diag.probe(selector, label=None)` answers "what does this
selector resolve to" — nothing runs. It returns
`{"count", "digest", "sample"}`; the digest is
`{"count", "kind", "ids" | "bbox"}` and a per-domain resolver
(`can_resolve` / `resolve` / `digest`) does the work. In a workspace
script, `o.probe(scene_or_selector, label=...)` resolves a pg object
(structure scene, flattened `pga.Geom`, hexfold `Net`) or a
`frame:<key>` selector against the live trace, prints one
`probe <label>: resolves to N <kind>` line and records
`{label, count, kind, digest}` on the calling trace frame — the
`view='trace'` frame card lists it there. On a board design,
`pcb.local.probe(board, "pads_of('U3')")` or a `nets:` pattern resolves
through `precis.pcb.probe`, also with no op run. An unresolvable
selector raises `precis_diag.ProbeError`, which carries the refusal
Diag (`core.probe-unresolved`, `next` names the fix); inside
`out.run` the failed run's record picks the Diag's code up.

## See also

- [[precis-workspace-help]] — the script loop that produces the runs this layer reports on
- [[precis-se-help]] — the se call surface the import refusal lands on
