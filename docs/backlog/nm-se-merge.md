---
status: ready
title: Merge the nm kind into se as atomic mode — one design kind, one exclusive window
prio: high
model: opus
---

# Merge the nm kind into se as atomic mode — one design kind, one exclusive window

Design session 2026-09-14 (Reto + agent, glowing-zooming-glade worktree).
Vetted clean through two rounds same day; Reto greenlit execution
("do the merge") 2026-09-14 — status flipped to ready on that word.
Window opens when the last in-flight se/nm sibling tree lands.

## Motivation / why

nm and se were built as siblings on the symmetry `se : cad :: nm :
structure` — the same six-level IR, the same cad kernel, a `precis_nm`
scaffold copied verbatim into `precis_se`. The units-policy cutover
removed the last substantive difference: both kinds now store SI metres
(nm's Å columns were converted in the cutover window; the map records
"cross-kind seams are trivial now the internal rep is shared"). What
remains of the kind split is duplicated scaffold (handler/ops/persist/
migrations ×2), an Å↔m seam (`_envelope_A_to_m`) that no longer guards a
storage difference, a which-kind-do-I-use decision point for every
agent (the LLM-confusion signal counts these), and — most expensively —
a build-order dependency: blocktree slice 2 is specced as the *nm
adoption* of the shared states schema, and design-state-core is being
co-designed around two renters. Merging first means the step-2
foundations land once, in se's atomic mode, with one renter.

se has the receiving *vocabulary*, not a working landing zone (vet
finding, 2026-09-14): `modes.py` declares an `atomic` mode family but
only `purchase` has an implementer, and `set_binding(kind='nm')` is
wired independently of `mode` — nothing couples the two. The merge
therefore *builds* the mode↔binding coupling (an explicit in-scope
item below), it does not merely promote an existing one.

Timing is the cheap moment and it closes monotonically: prod nm designs
are dogfood and deletable (Reto 2026-09-14: photonic-arm-3c may go), and
step-2 has not dispatched. After step-2, everything the campaign builds
on the split must be migrated instead.

## In scope

- **Fold `precis_nm`'s domain layer into `precis_se` as atomic mode**:
  generators, `mechanics.py` (moved, signatures untouched), the single
  `nm_propose` job type (its `literature` view and `envelope_fit`
  validate rule are handler/validate code and move with it — they are
  not job types), `structure` bindings, nm's validate findings. Carve
  as submodules — the handlers are already ~71 KB and ~85 KB; the merge
  must not produce one file.
- **Mode↔binding coupling** (new build, per the vet): mark the
  `atomic` mode family implemented; define and enforce the relation
  between `mode=='atomic'` and `bound_kind` (DRC finding on mismatch,
  not write-time rejection — the house posture). This is the substrate
  the mode-scoped help below stands on; today no guard exists.
- **Generators emit unit-suffixed Å text** (new build, per the vet):
  today generators emit *bare-number* Å configs and
  `_envelope_A_to_m` does the ×1e-10; rewrite them to emit genuine
  `Å`-suffixed DSL strings so `units.py`'s generic parse does the
  multiply at the one ingest boundary, then delete
  `_envelope_A_to_m`. The enclave rule (generator math stays Å)
  survives as a code-local convention.
- **Retire the `nm` kind**: remove its `pyproject.toml` entry-point
  registrations (handler, job type, migration namespace, handles) —
  dispatch/kind_gate are entry-point-driven and need no direct edit;
  the kind then vanishes from the MCP kind list and skills TOC. No
  alias handle — dogfood deletion makes dangling `nm:` references moot.
- **Delete prod nm designs** (dogfood; user-sanctioned 2026-09-14).
  Prod mutation goes through the usual user-runs-it handoff.
- **Job/service rename**: the one `nm_propose` `JobTypeSpec` → its se
  atomic-mode name; the service_config row updated; workers pick up
  the new type.
- **Mode-scoped help**: se kind-help shows atomic affordances only in
  atomic mode and fastener/BOM vocabulary only outside it (the kind-help
  footer + graph machinery is the mechanism).
- **Migrations**: forward-only; `precis_nm`'s migration namespace is
  sealed history — new se migrations move/drop the nm tables.
- **Surface unification**: web viewer's se/nm envelope reader routes
  become one; `precis-nm-help` skill folds into the se help; package
  docstrings and the map's symmetry line updated.
- **Exclusive window** across `precis_se` + `precis_nm` (the units-
  cutover precedent): nothing else lands on those packages while this
  is in flight, and the window does not open until in-flight sibling
  se/nm worktrees have landed.

## Explicitly NOT in scope

- **pcb** — different IR (nets/footprints/traces/layers is not the
  block graph); it binds, not merges → `pcb-se-binding.md`.
- **cad-the-kind** — the kernel stays a library regardless; folding
  stored cad designs into se needs its own consumer survey (exports,
  part-catalog derivations, the enclosure bridge) first.
- **structure** — stays the Å-native crystallography enclave per the
  units policy.
- **`mechanics.py` signatures** — interaction physics keeps Å/nN/eV
  ("cost terms over the geometry, not lengths in it").
- **`validate.py`'s `_A_TO_M`/`_M_TO_A`** — the `envelope_fit`
  design(m)↔atomistic(Å) conversion against bound `structure` scenes
  is the *permanent* structure-enclave crossing, not a merge-obsoleted
  relic. It moves with the code, unchanged; it is not on the kill
  list.
- **`_CROSS_SCALE_RATIO` / `kernel_scale`** — the mixed-scale SDF query
  guard is numerical conditioning, not kind hygiene; it stays.

## Acceptance criteria

- `nm` absent from the dispatch surface; `get(kind='nm', ...)` fails
  with a kind-shaped hint pointing at se atomic mode.
- Every former nm op and view reachable through se; atomic-mode help is
  mode-scoped both ways.
- `_envelope_A_to_m` deleted; generators emit Å-suffixed DSL text and
  `generate()` still round-trips (an Å-suffixed envelope parses to
  metres via units.py). `1e-10`/`1e10` in `precis_se` confined to an
  explicit test-pinned allowlist: the moved `mechanics` module and the
  moved `envelope_fit` structure-boundary conversion — nothing else.
- The atomic mode family reports implemented; a `mode=='atomic'` /
  `bound_kind` mismatch yields a DRC finding (test-pinned both ways).
- The `nm_propose` job runs under its se name end-to-end on the dev
  DB; service_config rename script prepared for prod.
- nm's test suites ported with mutation coverage preserved (the recent
  link-ack pins included).
- `blocktree-library-build-plan.md` slice 2 retargeted from nm to se
  atomic mode; the multiscale map's build order reflects the amendment.
- Prod: nm designs deleted, nm tables dropped by migration, doctor
  green post-deploy.

## Target + blast radius

`precis_se` (handler/ops/persist/validate/modes + new migrations) ·
`precis_nm` (deleted) · **`pyproject.toml`** (the entry-point
registrations every "retire the kind" edit routes through —
dispatch/kind_gate are generic and untouched) · workers +
service_config (job rename) · `precis_web` viewer routes · skills
(`precis-nm-help`, se help, TOC) · docs (map, `nm-kind.md` folds in,
package docstrings) · prod nm tables + designs.

## Open questions / decisions log

- **Decided** (Reto 2026-09-14): merge direction is nm→se; prod nm
  designs deletable; window sits before the step-2 foundations
  dispatch; pcb stays a mm enclave (map §Units policy).
- Open: submodule carve of the merged package (proposal: `atomic/`
  subpackage mirroring nm's file split, one handler delegating by
  mode).
- **Decided** (agent call 2026-09-14 post-vet, Reto may veto):
  `nm-kind.md`'s unshipped round-2 content (objective verdicts +
  apply) **trails** as a follow-on se atomic-mode item — this window
  stays mechanical. `nm-kind.md` survives the merge renamed in place
  as that follow-on's spec.
- **Resolved 2026-09-14** (same session, post-vet): all four vet
  blockers folded back into the sections above — Motivation no longer
  claims a working landing zone (mode↔binding coupling is now an
  in-scope build item), the job surface is stated as the single
  `nm_propose` `JobTypeSpec`, the seam kill-list is exactly
  `_envelope_A_to_m` (replaced by generators emitting Å-suffixed text)
  while `validate.py`'s structure-boundary conversion is explicitly
  kept, and the `1e-10` acceptance grep now names its allowlist.
  Blast radius names `pyproject.toml`;
  `blocktree-library-build-plan.md` now carries
  `blocked-by: nm-se-merge` (was prose-only ordering).

### Readiness vet (glowing-zooming-glade, 2026-09-14)

- **blocker** — "atomic-mode blocks exist, and today they *bind* nm
  designs" (Motivation) is not what the code shows.
  `precis_se/modes.py`'s `atomic` `ModeFamily` has no `implemented=True`
  (only `purchase` does — the module docstring says so explicitly:
  "Only `purchase` has an implementer today"), and `set_binding`'s
  `kind='nm'` (`precis_se/ops.py::_op_set_binding`, `_BINDING_KINDS`) is
  wired independently of `mode` — nothing in `ops.py`/`drc.py`/
  `handler.py` requires `mode=='atomic'` when `bound_kind=='nm'`, or vice
  versa. There is no enforced coupling to promote; the "receiving
  structure" is two uncoupled, partly-unimplemented pieces, not a
  working bind-today landing zone. This understates the build (the
  mode↔binding coupling has to be designed, not just "promoted"), and
  the mode-scoped-help acceptance criterion has no existing DRC/guard to
  build on.
- **blocker** — "the `nm_propose` job family (propose / literature /
  envelope_fit)" doesn't exist as stated. `precis_nm/job.py` registers
  exactly one `JobTypeSpec` (`name="nm_propose"`, grep confirms a single
  `JobTypeSpec(` call in the package). `literature` is a
  `view='literature'` handler dispatch value (`handler.py` lines
  ~138-1213), and `envelope_fit` is a `validate.py` function/finding
  rule, not job types. There is one job to rename, not a family of
  three — a coder scoping "job/service rename" against this framing
  could go looking for job types that don't exist.
- **blocker** — the "Kill the Å↔m handler seams" bullet and the
  Motivation's framing of `_envelope_A_to_m` as a seam that "no longer
  guards a storage difference" conflate two different seams. (1)
  `precis_nm/validate.py`'s `_A_TO_M`/`_M_TO_A` (used by `envelope_fit`,
  called from `handler.py`'s bind preflight — squarely a handler-path
  call) convert at the design(m)↔atomistic(Å) boundary against a bound
  `structure` scene, which "Explicitly NOT in scope" says stays
  Å-native forever — this conversion is permanent, not a merge-obsoleted
  relic, and "siblings on handler/ops paths" doesn't say whether it's in
  the kill list. (2) `_envelope_A_to_m` itself (`handler.py`) is not
  dead weight either: it converts a generator's Å-valued params to a
  metres-valued, unit-suffixed string before it re-enters
  `_ingest_envelope` — today's generators emit *bare-number* Å configs
  (`cad_dsl.parse(config)`, no `require_units`), not the "Å-suffixed
  DSL strings" the in-scope bullet's parenthetical describes as already
  true. Deleting `_envelope_A_to_m` without either (a) keeping an
  equivalent conversion function under a new name, or (b) rewriting
  `precis_nm.generators` to emit genuine unit-suffixed Å text so
  `units.py`'s generic parse can do the multiply — a real code change
  not named as its own work item — breaks `generate()`. The acceptance
  criterion ("`1e-10` appears in `precis_se` only inside physics
  modules") doesn't say whether `validate.py`'s permanent conversion
  counts as a "physics module," so it's not clear what a green run of
  that grep is supposed to look like.
- **advisory** — nm's kind registration (handler, job type, migrations
  namespace, `handles`) lives in `pyproject.toml` entry points (lines
  ~463/476/483/489), not in `precis/kind_gate.py` or `precis/dispatch.py`
  (both are generic/entry-point-driven; grepping `"nm"` in either
  returns nothing). "Retire the nm kind: out of dispatch, kind_gate, the
  MCP kind list" describes the effect, but "Target + blast radius"
  doesn't name `pyproject.toml`, which is the actual file every one of
  those edits routes through.
- **advisory** — `docs/backlog/blocktree-library-build-plan.md` is
  `status: ready` right now and carries no `blocked-by` field; its own
  2026-09-14 amendment note says the merge should land "before this plan
  dispatches," and `multiscale-design-architecture.md`'s Build order
  section says the same, but neither is a machine-enforced gate — only
  prose. Given this item's own Motivation stakes ("After step-2,
  everything the campaign builds on the split must be migrated
  instead"), the ordering is currently vet-and-human-enforced only,
  not `blocked-by`-enforced.
- **confirmed, no issue** — claim 1 (both kinds SI metres post-cutover):
  verified. `precis_nm/migrations/0007_units_nm_wipe.sql` wipes
  pre-cutover Å-valued nm designs (dev/test only, ruled non-preserving);
  `precis_se/migrations/0001_se_kind.sql` declares `pose_xyz`/`envelope`
  metres from inception; `0006_units_se_pose_rot_rad.sql` only touches
  the deg→rad angle convention, not length. No remaining Å-valued
  columns in either kind's persist layer (`persist.py` for both has zero
  Å/1e-10/1e10 hits).

### Readiness vet round 2 (glowing-zooming-glade, 2026-09-14)

All 4 round-1 blockers resolved, both advisories addressed — (1)
Motivation now states the mode↔binding coupling as a build item with a
matching in-scope bullet + test-pinned acceptance criterion; (2) job
surface correctly stated as the single `nm_propose` `JobTypeSpec`
everywhere (in-scope, acceptance criteria, blast radius); (3) seam
kill-list is now exactly `_envelope_A_to_m` via the named
generators-emit-Å-suffixed-text build item, `validate.py`'s
`_A_TO_M`/`_M_TO_A` moved to Explicitly-NOT-in-scope as the permanent
structure-boundary crossing, and the `1e-10`/`1e10` acceptance grep
names its allowlist (mechanics + the moved envelope_fit conversion)
explicitly; (4) the round-2 fold-or-trail question is now a Decided
entry (trail, agent call, Reto may veto) rather than Open —
`pyproject.toml` is in blast radius, and
`blocktree-library-build-plan.md` carries `blocked-by: nm-se-merge`
(confirmed in its frontmatter). No new inaccuracy found in the
revision.
