---
status: draft
title: Trace frame keys from structure, not line numbers
pillar: 3d-design
prio: high
---

# Trace frame keys from structure, not line numbers

Meant to be linked from the structure-workspaces spec (section Construction
traces — who made this atom) and the pcb-workspace-code spec (section
Construction traces for boards) by their owners. gr513662; unblocks gr513671.

## Motivation / why

`precis_geom.trace_schema::frame_key` hashes the absolute call-site `line`.
Insert one comment above a call and that frame's key changes; every
descendant key changes with it (children hash the parent key). Entity
`maker` stamps then cite nothing, though the construction is unchanged.
This breaks the edit-rebuild loop and the run-to-run diff (gr513671).
Parameter edits (rows 8 -> 10) already keep keys; only line shifts break them.

## Design

**Key.** `sha8(parent_key, qual, occ)`. The parent key already carries the
caller's qual, and a frame's `qual` is the callee, so this is the
`(caller, callee, occurrence)` of the gripe. `occ` = 0-based ordinal of the
callee `qual` among that parent's children, across all call sites (today
`occ` counts per `(qual, line)`; `Tracer._push` drops `line` from `occ_key`).
No args in the base key: an edit to a literal must keep the key. A canonical
`args` digest enters the key only when two siblings would otherwise collide
(see Collisions).

**Non-key location fields.** Keep `line`; add `line_in_def` (call-site line
minus the enclosing def's `co_firstlineno`; 0 for the root and for calls
from outside `/work`) and `src` (stripped call-site source line, capped at
120 chars). Frames outside user code carry `src` = null.

### Stability requirements (each is a test)

1. A blank line, comment or import inserted above a call: every key
   unchanged; `line` moves, `line_in_def` moves only for calls below the
   insertion inside the same def.
2. Renaming a local variable: keys unchanged.
3. Changing a literal argument: keys unchanged.
4. Reordering two calls to the same callee in one caller: their keys swap.
   Accepted — rarer than line shifts, and `src` still locates each.
5. Renaming the caller or callee: keys change (intended; the qual is the
   identity).

### Collisions

- Same callee, same caller, several call sites or loop iterations: `occ`
  order is the disambiguator; no arg digest needed.
- Loop count change (range(8) -> range(10)): the first 8 keys are kept, new
  frames are tail `occ` values. Shrinking drops tail frames only. Inserting a
  new call to the same callee before existing ones shifts later `occ`
  (accepted, same as requirement 4).
- Collapse: past `COLLAPSE_SIBLINGS` the group is `(qual)` under one parent
  (was `(qual, file, line)`); the collapsed frame keeps
  `frame_key(parent, qual, COLLAPSE_SIBLINGS)`, the group key, unchanged by
  the loop growing past 64.
- Recursion: parent key chains per level, so equal quals at different depths
  never collide; `occ` is per parent.
- A key can still repeat only through a `_sync`-minted `user` frame
  recreated under the same parent with the same qual (re-entered generator).
  In a single run this is in fact unreachable: `occ` is drawn from the
  parent's `_occ_next`, which lives on the parent's `_Frame` in the tree
  even after the stack pops, so the re-mint takes `occ+1`, not a repeat —
  and a duplicate higher up would need the same impossible re-mint at its
  own parent. The tie-break stays as defence for a writer that merges
  runs or mints frames some other way; the test forces the collision by
  cloning a `_Frame` into the tree
  (`test_geom_trace.py::test_forced_duplicate_key_mints_a_tiebreak`) —
  there is no natural repro to name.
  Tie-break: the later frame mints `sha8(key, "#", args_digest, n)`, n = its
  rank among equal keys in emit order. `dump` counts these in a new
  `run.key_collisions` (0 expected); `loads()` rejects duplicate keys not
  counted there. Never a silent merge.

### Re-anchor rule (LLM / importer)

Given a stored frame and the current script: (1) `qual` -> the def (file stem
+ co_qualname); (2) seek `line_in_def`; (3) if the line there does not match
`src`, take the nearest line inside the def whose text fuzzy-matches `src`
(difflib ratio >= 0.8, else none); (4) no match: report the def and say the
call moved or was removed; exact history via `run.script_sha` + `git show`
when that version is stored. Level promised: **def granularity always,
line granularity when the edited def still contains a near-identical
call line**. That is close enough for an LLM; no AST diffing.

### Frame -> Part link (deferred from gr513666)

Today `b.part('U1')`'s frame has `made={}`, so "which script line made U1"
has no answer. Options:

- **A. `parts` in `ENTITY_TYPES`.** `made={"parts": 1}`; entity stamps
  resolve via the existing `maker` path. Uniform with atoms/blocks, no new
  field; but counts say nothing about *which* part, so the importer must
  pair frame order with creation order.
- **B. `Part.frame_key` attribute.** The Part records the key of the frame
  that created it; the board dump emits it. Direct, survives reordering,
  but is a second linkage path beside `maker`, and `Part` lives in the pcb
  module another tree is changing.

Recommendation: A (one mechanism; the key is now stable enough to carry it).
Option A shipped (gr521596): every entity-creating `Board` primitive
stamps `made=`, and `view='frames'` resolves `meta.frame` bindings to
maker chains.

## Migration

- `SCHEMA_VERSION = 2`. `loads()` accepts `{1, 2}`; a schema-1 doc keeps its
  line-bearing keys and lacks `line_in_def`/`src`/`key_collisions`.
- `workspace_import` and `traceview` read both; stored `maker` stamps on
  schema-1 runs are left alone (old runs are old runs). No re-keying.
- One-time churn on the first schema-2 run of a workspace. The run diff
  (`gr513671`, now matching by (qual-path, occurrence)) switches to
  matching by `key` when both runs are schema 2, and keeps the
  (qual-path, occurrence) match for 1-vs-2 pairs, so the first post-upgrade
  diff still works.
- `tests/workspaces/data/*/out/trace.json` (six goldens). Only
  `bracket` regenerates end to end —
  `uv run python tests/workspaces/data/bracket/make_fixture.py`
  (`--h2-dx MM` for the ray.changed variant). The other five are
  hand-authored with readable sequential keys that tests cite literally
  (`test_trace_view.py::STRIP1` …) — keep them, or re-mint onto the
  current recipe with the committed re-minter
  `uv run python tests/workspaces/data/rekey_trace.py <trace.json>…`
  (walks `parent`, rewrites `key`/`parent` per `frame_key`, re-counts
  `run.key_collisions`; idempotent on a live dump — pinned by
  `test_geom_trace.py::test_rekey_trace_is_idempotent_on_a_real_dump`).
  Re-minted keys then need the citing constants updated too.
  `pcb_goldens.json` holds no trace keys; untouched.
- `test_identical_keys_across_runs`, `test_same_site_occ_counter`
  (becomes per-qual occ) and `test_trace_keys_stable_across_two_builds`
  are updated; new acceptance test below.

## Slices (smallest first)

1. `frame_key`/`_push`/collapse grouping on `(qual, occ)`; add `line_in_def`,
   `src`; schema 2 with `loads()` accepting 1 and 2. Test: requirements 1-5
   on a tmp script, plus regenerated goldens.
2. `key_collisions` + tie-break + loop-count test (grow/shrink keeps prefix).
3. Acceptance golden: insert a blank line and a comment atop
   `examples/boards/nano_usb_pd/main.py` and one se example (`trace_demo`); rerun;
   every key equal, only `line` differs.
4. Re-anchor helper in `precis_se.traceview` (qual -> line_in_def -> fuzzy
   `src`) and the frame -> Part link (option A or B). Test: edit a script,
   resolve old frames to new lines.

Unblocks: run-to-run diff matched by key (gr513671); `maker` stamps that
survive an edit.

## Explicitly NOT in scope

Error-marker and root-frame fixes (gr513668/9); cross-schema re-keying of
stored runs; AST-level move detection.

## Open questions

- Frame -> Part link: option A or B (recommended A)?
- `src` cap (120 chars) and fuzzy threshold (0.8): fine as defaults?
- Should `args` digest ever enter the base key for `kind=op` frames whose
  callee qual is generic?
