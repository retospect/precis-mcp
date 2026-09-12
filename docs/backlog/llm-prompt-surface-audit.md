---
status: draft
title: LLM prompt-surface audit — units-cutover residue
prio: normal
---

# LLM prompt-surface audit — units-cutover residue

Full audit: a read-only sweep of every prompt/hint/skill surface an LLM
worker or cluster agent sees, run against the units-cutover tree (main
@538b1399, 2026-09-12) to catch surfaces still teaching the dead
pre-cutover (unitless) cad grammar. The original report is not preserved
verbatim here — see the commit that added this file for that; this is the
**trimmed remainder**: what's still open after the fix pass.

## Done (this commit — the 6 HIGH findings + listed cheap MEDIUMs)

- `workers/job_types/structure_propose.py` `_OP_VOCAB`: dead op
  `cursor{…}` → real op `eye{…}`; guarded by a new test that dry-runs a
  concrete example of every vocab op against the real registry
  (`tests/test_structure_propose.py::test_op_vocab_examples_dry_run_against_real_registry`).
- `handlers/cad.py` retry/onboarding hints (`:646`, `:653-655`, empty-store
  hint) — unitless `cyl:r25h8`/bare `@0,0,-1` → unit-suffixed, parser
  round-trip test added
  (`tests/test_cad_handler.py::test_retry_and_onboarding_hints_parse_under_boundary_grammar`).
- `cad.dsl.DslError` is now `class DslError(BadInput, ValueError)` —
  surfaces through the dispatcher's `PrecisError` branch (cause text
  intact) instead of flattening to `internal error in put: DslError`;
  every existing `except (DslError, ValueError)` call site keeps working
  unchanged. Regression: `tests/test_runtime.py::test_cad_dsl_error_renders_grammar_teaching_not_internal_error`.
- `precis_web/routes/drive.py` `_NEW_STARTERS['cad']` — was `part add
  box:w40d40h10` (doubly broken: `part` is the reserved catalog-part
  keyword, and the config was unitless) → `plate add
  box:w40mmd40mmh10mm`. Test:
  `tests/precis_web/test_drive_new_starters.py`.
- `data/skills/precis-overview.md:64` cad row + `cad/dsl.py`'s
  malformed-config error's own `e.g.` examples — both now unit-suffixed;
  both guarded by round-trip tests
  (`tests/test_skills_examples.py::test_precis_overview_cad_examples_parse_under_boundary_grammar`,
  `tests/test_cad_dsl.py::test_no_colon_error_examples_parse_under_boundary_grammar`).
- `precis_nm/job.py` nm_propose prompt: one sentence now states the
  envelope line is bare canonical metres and the op-vocabulary
  coordinates are Å — guarded by a prompt-content assertion in
  `tests/test_nm_propose.py`.
- `cad/scene.py` error strings: `_coerce_state`'s "must be a number" /
  cylindrical-shape errors now name the boundary's unit-suffixed form;
  state-outside-limits and dim-interval echoes (`_fmt_interval`) now
  render with an explicit unit via `_fmt_len` instead of a bare SI float;
  the dim-not-pinned hint's `<mm>` placeholder is now a concrete
  `'dim {nm} = 20mm'`; the payload-config error's "use literal numbers"
  now says "unit-suffixed literal numbers".
- `handlers/cad.py` joint hints (`:1078-1079` no-joints hint, `:1107`
  prismatic-limits hint) — `limits:lo..hi` → concrete unit-suffixed
  examples (`limits:-30deg..30deg`, `limits:0mm..50mm`).
- `workers/planner_prompt.py` vs `handlers/_draft_lint.py` temperature
  notation — the planner contract taught unspaced `63°C`;
  `_draft_lint.py`'s *enforced* regex behaviour (and
  `precis-notation-canon.md`, `workers/llm_summarize.py`) is actually
  SI-spaced `63 °C`. Planner text fixed to match; `_draft_lint.py`'s own
  stale header comment (which *also* claimed unspaced was canonical,
  contradicting its own regex) fixed in passing. Regression:
  `tests/test_planner_prompt_temp_notation.py` (asserts the planner's own
  example passes the live lint function).
- `data/skills/precis-toolpath-help.md:128` — dropped the stale "dark,
  needs `nm.enabled`" claim (gate removed 2026-09-11); guarded by
  `tests/test_skills_examples.py::test_toolpath_help_does_not_claim_nm_is_gated`.
- `errors.py:14-15` + the `PrecisError` docstring — deleted the
  `ErrorModel.enrich()` promise (no such class exists anywhere in `src/`);
  replaced with an accurate note that `next=`/`options=` are hand-written
  at the raise site (naming the two narrow dispatcher-boundary hooks that
  *do* append an extra `next:` line for their one recognized signal).

## Still open (not touched by this commit — scoped out or deferred)

### se implicit-metre inventory (correct TODAY — future se-cutover worklist)

`precis_se/` has no `require_units`/`parse_quantity`/`UnitRequiredError`
anywhere (grep-verified) — its envelope/pitch/radius/measure/joint/load
params are bare SI metres by design, and `precis-se-help.md` documents
exactly that. **Do not "fix" these** until/unless se joins the units
cutover; they are the worklist for that future change:

`precis_se/ops.py:410-423` (`array_block` pitch/radius "must be a number
(m)"), `:727-729` (measure values bare float + out-of-band unit key
defaulting to `m`); `precis_se/handler.py:103` ("envelopes in METRES"),
`:122` (spring params N/m/N by prose), `:138-139` ("unit m default"),
`:158` (`'cyl:r0.02h0.01' = a 2 cm-radius disc`), `:170-171`
("in metres"), `:255-257` (put retry hint valid for se but fatal fed to
cad/nm — cross-kind confusion), `:1057-1059` (a `Next:` hint hands the
agent bare `2e-4`/`5e-5` — the exponent-slip shape the cutover targets);
`precis_se/joints.py:211-220` ("'lead' … metres per revolution"),
`:238-262` (free_length/rate/capacities/preload), `:269-287`
(force/torque vectors); `precis_se/measures.py:128-137` (relation
offset/tol); `precis_se/fasten.py:441-455, 471-491` (read side renders
`"a lead of 1.000 mm/turn"` while `joints.py:212`'s write side demands
bare metres — a silent 1000× trap for an agent that reads one and writes
the other, netted only by `drc.py:431-434`'s order-of-magnitude lint).

### Other deferred MEDIUM/LOW (not in this pass's fix list)

- **`cad/scene.py` gear-coupling conflict messages (`:1992-2003`,
  `chain_q`)** — still echo bare SI floats (`{cur:g}`/`{want:g}`) instead
  of a unit-suffixed form; same family as the dim/state echoes fixed
  above, deferred because the joint's length-vs-angle domain isn't as
  directly in scope there.
- **`handlers/cad.py:82-92` + `:1423`** — `_fmt_interval_line` renders a
  dim as `"= 2.3 mm"` (spaced) inside `_interfaces_block`, whose own
  docstring says the reply "doubles as the text to hand back to `put`" —
  but `_DIM_RE` requires no space (`'dim a = 2.3mm'`), so the round-trip
  text as shown doesn't re-parse.
- **`precis_web/templates/cad/list.html.j2:14`** — the Drive empty-state
  template still shows `put(kind='cad', id='flange', text='plate add
  cyl:r25h8')`, the same stale string the `handlers/cad.py` hints carried
  before this commit's fix (human-facing, not an MCP prompt surface, so
  out of this pass's scope — but now the one place that string survives).
- **`cad_discuss.py:74`** — "`loc` translates the primitive" names a
  concept, not the actual source token (`@x,y,z`); imprecise, read-only
  surface.
- **`structure_propose.py` `_OP_VOCAB` under-teaching** —
  `constrain{...,kind:fixed-x|y|z|all}` shorthand invites a literal
  `kind:'all'`/`kind:'y'` (real kinds are `fixed-all`/`fixed-y`, plus an
  untaught `none`); `add_atom{element,frac:[x,y,z]}` omits `cart`/`label`/
  `charge` (present in nm_propose's own vocab for the same op — the two
  cribs disagree); the vocab omits `ring`/`attach`/`from_smiles`/`slab`/
  `add_atom_site` entirely (unclear if intentional scoping).
- **`utils/prompt/tables.py:81`** — "the seven verbs" table header names
  six (`delete` absent from `_TOOLS`).
- **`cad/scene.py:17`** module docstring's canonical grammar example has
  every token unit-suffixed except `rot:0,0,45`, contradicting the same
  docstring's unit-required claim two lines down.
- **`cad/scene.py:693` / `:704` / `:1205`** — `pitch:<mm-per-rev>` /
  `pitch:<mm>` placeholders half-teach bare mm (the value needs any
  `LENGTH_UNIT_TOKEN`, not literally mm); same in the joint parse-error
  message at `:1205`.
- **`cad/dsl.py:208-210`** canonical-mode chamfer error
  (`'chamfer:1x0.785'`) is reachable via a `{name}`-parametrized config's
  malformed remainder; bare radians are correct for that mode but the
  message never says which mode it's in.
- **`quest/tick.py:975-1159` / `:1258-1458` / `:1576-1622`** — the
  biggest structure-op crib in the tree, correctly Å-implied under the
  structure-kind exemption, but no line states "lengths are Å"; first
  crib to break if structure ever joins a future cutover.
- **`{x:g} m`-style cosmetic formatting** (LOW, `format_quantity` already
  imported in each file but unused for these) — `precis_se/validate.py:309`,
  `precis_se/drc.py:431-434`, `precis_nm/validate.py:326-327, 499-505`.

## Target + blast radius

Prompt-building code under `workers/job_types/`, `precis_nm/job.py`,
`handlers/cad.py`, `cad/dsl.py`, `cad/scene.py`, `errors.py`,
`runtime/dispatch.py` (indirectly, via `DslError`'s new base), skills
under `data/skills/`, and `precis_web/routes/drive.py`. No schema/DB
change; no behavior change other than: (1) a cad `DslError` now renders
as a structured `[error:BadInput]` instead of an opaque `Internal` —
callers parsing the *old* internal-error string (none found in `src/` or
`tests/`) would need updating.

## Open questions / decisions log

None blocking. The se-cutover worklist above has no owner/date yet —
raise it as its own backlog item (with a `blocked-by` on nothing, since
it's independent) if/when se joins the units cutover.
