---
status: ready
title: units policy cutover — ingest-any → SI internal → neat formatter; relative-tolerance audit
prio: high
model: opus
---

# Units policy cutover

Implements the map's §Units policy (multiscale-design-architecture.md,
DECIDED) with the 2026-09-12 display revision: flexible ingest (any
declared unit, hogsheads included), one canonical internal SI float64
representation, and one shared **neat formatter** that renders
scale-appropriate SI units (`2.3 nm`, `1.2 kN`, `350 ml`) with
e-notation as out-of-prefix/debug fallback. Runs as the programme's
**exclusive window** on se + nm + cad, enforced by `blocked-by:
units-policy-cutover` on the sibling items that touch those packages
(`blocktree-library-build-plan`, `design-state-core`,
`box-full-dims-cutover`) — not by prose.

## Motivation / why

The current state — cad DSL docstring says mm, se stores m, nm stores Å,
one shared grammar — is the anti-pattern the map names: implicit
per-kind conventions an agent must guess. The two observed LLM failure
modes (zero-counting `box:w0.000000003`, silent 10× exponent slips that
validate cannot distinguish from intent) are both input-side and both
die with explicit units. Internal float64 relative precision is
scale-free, so SI base loses nothing down to sub-fm; the *real* hazard
is absolute epsilons tuned for metre-ish magnitudes, garbage at 1e-10.

## In scope

1. **Ingest boundary.** All se/nm/cad ops and the cad DSL accept an
   explicit unit on every length/force/mass/volume quantity (`3 mm`,
   `1.4 Å`, `12 N`; grammar-level in the DSL, arg-level in ops).
   Conversion happens exactly once, inbound.
2. **Internal representation.** SI base (m, N, kg), float64, everywhere:
   handler state, plugin tables, kernel calls. nm's Å storage columns
   convert to metres via a forward-only data migration (see decisions
   log). The cad kernel is declared unit-agnostic; its docstrings drop
   the mm convention.
3. **Neat formatter.** One shared utility (`precis/utils/units.py`):
   quantity → best SI prefix/unit string; headers name units; e-notation
   fallback outside prefix range; used by se/nm/cad text views and
   exposed to precis_web. No second prefix table anywhere.
4. **Relative-tolerance audit.** Every *system-supplied* absolute
   LENGTH epsilon in cad/se/nm/structsolve (SDF comparison, clearance
   "touching" bands, convergence thresholds — e.g. `LINEAR_EPS`,
   `CONTACT_TOL_MM`) becomes relative to a governing length (feature
   size, else bbox diagonal), following the shipped `kernel_scale` /
   `_GRAD_REL_EPS` precedent; views report which default they applied.
   Author-stated tolerances accepted as `%` or absolute-with-unit,
   never silently relativized. Ladder shipped HERE: stated →
   scale-relative fallback. The capability-row middle rung (absolute
   process tolerances trumping the relative default once `set_mode` is
   known) is a declared FORWARD HOOK — it inserts when
   `se-feasibility-and-cost` / `se-off-the-shelf-fabrication` ship the
   rows (build-order step 3); nothing here builds or stubs that schema.

## Explicitly NOT in scope

- Interaction physics (kT thresholds, π-stack energies, nm mechanics
  capacities) — the map's stated boundary; native quantities stay in
  their modules, never routed through the unit ladder. Concretely
  (dossier finding): `structure` designs stay Å-native (crystallography
  module boundary — atom coords, Cell, CIF/DFT glue untouched) and
  `precis_nm/mechanics.py` keeps its Å/nN/eV signatures; the nm handler
  converts m↔Å explicitly at those seams (`envelope_fit`, bind
  preflight, `_generated_cell`, mechanics calls). The full touchpoint
  inventory is `units-cutover-dossier.md` (working doc, delete with
  this item).
- Web-UI-side pretty rendering beyond calling the shared formatter.
- Currency/cost units; time units beyond what service-environment later
  needs.
- Statistical tolerance modes (RSS etc. — feasibility-and-cost rung 4).

## Acceptance criteria

- Every se/nm/cad op that takes a dimensioned quantity accepts
  `<number> <unit>` for at least: m, mm, cm, km, nm, Å, in, ft; N, kN,
  lbf; kg, g; l, ml — plus pint's long tail (pint is decided).
- A round-trip property test: ingest in any supported unit → stored SI →
  formatter output re-parses to the same quantity (tolerance 1e-12
  relative).
- Zero-counting guard: the DSL/ops reject a bare dimensioned number
  where a unit is required, with a structured error naming the arg AND
  carrying the hint (the value echoed under 2–3 plausible unit
  readings); a test asserts the hint's presence and content shape.
- `grep`-verifiable: no absolute LENGTH epsilon constants remain in
  cad/se/nm/structsolve comparison paths (each is derived from a
  governing length); an AST-walk or grep test pins this, with an
  explicit exempt list for dimensionless/angular epsilons
  (`ANGULAR_EPS`, `_AM_EPS`, `_AM_SMIN_EPS`, `_FILTER_GAMMA`,
  `_SINGULAR_RTOL` — angles and 0–1 fractions have no length scale).
- nm data migration: post-migrate, every live nm design revalidates
  clean and `validate` output is numerically unchanged (Å→m is exact
  ×1e-10 in float64 within relative 1e-15); a pre/post checksum over
  poses proves it.
- Formatter goldens: `2.3e-9 m → "2.3 nm"`, `1.2e3 N → "1.2 kN"`,
  `3.5e-4 m³ → "350 ml"`, plus out-of-range → e-notation.
- Docs: cad DSL docstring, se/nm skills, and the map §Units display
  paragraph agree with the shipped behaviour.

## Target + blast radius

`precis/cad/dsl.py` + kernel docstrings · `precis_se/handler.py`+ops ·
`precis_nm/handler.py`+ops + one nm data migration · `precis/utils/units.py`
(new) · `precis/structsolve` (audit only; already unit-agnostic) ·
skills `precis-se-*`/`precis-nm-*`/cad · precis_web render helpers.
Post-deploy blast check: live nm designs (photonic-arm-3c et al.)
revalidate; se unicycle DRC unchanged.

## Open questions / decisions log

All resolved with Reto, 2026-09-12:

- **Parser:** `pint`, ingest boundary only — and it is ALREADY a core
  dep (calc kind, `pint>=0.23` in pyproject); no image rebuild.
- **nm Å storage:** DECIDED — full conversion to metres, no Å anywhere
  internally. Forward-only data migration with pre/post pose checksum;
  deploy still gets an explicit go from Reto (live prod designs).
- **Bare numbers:** DECIDED — reject where a unit is required, and the
  structured error carries a HINT (echo the value with two or three
  plausible unit readings: "3 — state units: '3 mm'? '3 m'?"), so the
  agent's retry is one edit, not a guess.

### `/ready` findings, 2026-09-12 — RESOLVED same day

- Exclusive-window collision with blocktree: the window is now enforced
  by the fixer's real lever — `blocktree-library-build-plan.md` and
  `design-state-core.md` both carry `blocked-by: units-policy-cutover`.
  Prose promise deleted; blocked-by is the gate.
- Box full-dims switch (gr334785, DECIDED in its comment 1: full dims
  at DSL/MCP surface, half-extent stays kernel-internal): carved out to
  its own item `box-full-dims-cutover.md` with its own acceptance
  criteria (incl. migrating the ~24 half-extent-authored unicycle
  envelopes); blocked-by this item, lands in the same window
  back-to-back.
- Capability-row rung: item 4 rewritten — ladder shipped here is
  stated → scale-relative; capability rows are a named forward hook
  owned by step 3, nothing stubbed here.
- Advisories: hint now in acceptance criteria; pint hedge removed;
  dimensionless/angular epsilon exempt list added.
