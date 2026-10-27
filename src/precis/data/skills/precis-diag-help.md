---
id: precis-diag-help
family: se
title: precis — Diag/RunResult: the structured failure record anchored to a trace frame
summary: precis_diag is the domain-free diagnostics core (stdlib-only, in the precis-geom wheel). A Diag is one structured check/failure record — status ok|info|not_checked|warn|error, anchor{frame_key|op_id, file, line, chain}, refs, message, next, data — a superset of hexfold.report.Finding with tested conversion both ways. A RunResult is the run-level record (diags + a trace.json pointer). The first adapter is the CAD run failure (precis_geom.run_diag.failed_run_result): a failed out.run's manifest+trace becomes one error Diag anchored on the user-facing traced call under the nearest user ancestor, so a failure names its fix site by stable frame key instead of prose.
answers:
  - how do I get a machine-readable record of a failed workspace script run?
  - what is a Diag / RunResult and how do they map to hexfold Findings?
  - how does a run failure name the failing op's trace frame?
  - how do I read the failure the import_workspace refusal carried?
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

## See also

- [[precis-workspace-help]] — the script loop that produces the runs this layer reports on
- [[precis-se-help]] — the se call surface the import refusal lands on
