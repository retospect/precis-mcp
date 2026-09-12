---
status: ready
title: pose/rot vector unit ingest — strict, three accepted forms, canonical hint
prio: high
---

# pose/rot vector unit ingest

Carved out of `units-policy-cutover.md` (shipped 2026-09-12, deleted) —
its one OPEN question. Pose/rot vectors are today bare numbers meaning
SI (metres / radians) through the shared `precis.blocktree.ops._as_vec3`
path se and nm both use. That is the zero-counting hazard
(`pose: [0.000000003, 0, 0]`) on the most-used arg, deliberately left
out of the cutover because tightening it changes se's op surface too.

**RULED (Reto 2026-09-12): strict — reject bare, accept three forms,
one canonical form in every hint.**

Accepted input forms (broadly accepting):
1. **Trailing/external unit** — `[3, 0, 0] nm` (string form
   `"[3 0 0] nm"` / `"3 0 0 nm"`); the unit distributes over all
   components. This is the CANONICAL form — every hint and every doc
   example uses it.
2. **Per-component units** — `["3nm", "0nm", "0nm"]`; components may
   mix units (`["3nm", "0nm", "2Å"]` — each converts independently).
3. **External unit arg** — the unit supplied beside the vector where
   an op's arg shape allows (e.g. `pose=[4,0,0]` + `(km)`;
   rot analogously with `deg`/`rad`).

Ergonomic carve-outs: a bare `0` component is always legal (zero
needs no unit); e-notation mantissas are fine in every form
(`[3e-9, 0, 0] m` ≡ `[3, 0, 0] nm` — Reto 2026-09-12; the
gr336344 awkward-mantissa warn may later nudge toward the neat
prefix, warn-only, pending the gr336349 notation eval). Mixing form 1
and form 2 in one vector (some components united, others bare-nonzero
relying on a trailing unit) rejects.

Rejection: a bare nonzero numeric vector rejects with the standard
structured hint, which ECHOES THE CALLER'S OWN VECTOR in canonical
form under 2–3 plausible unit readings: `pose=[3,0,0]` →
`state units — "[3 0 0] nm"? "[3 0 0] m"?`. Heavy hinting, one-edit
retry. `rot` follows the shipped angle ruling (`deg`/`rad`; radians
internal, degrees in display).

**DRY + echo + tables policy (Reto 2026-09-12, applies to the whole
bundled round):**
- ONE helper: `precis/utils/units.py` gains `parse_vector_quantity`
  beside `parse_quantity`, reusing it per component (pint owns unit
  words; only the bracket/trailing-unit/mixing/bare-zero rules are
  new, written once). `_as_vec3` calls it — se and nm identical by
  construction; calc and future vector args use the same helper. No
  surface parses units itself.
- ECHO ON WRITE, canonical form, throughout: every op that ingests
  quantities acknowledges with what it understood in neat canonical
  form (`understood: pose = [3 0 0] nm`) — per-turn reinforcement of
  the preferred syntax + an immediate mis-parse tripwire. Query
  ANSWERS follow gr336352's respond-in-kind rule instead (caller's
  unit, or both when ambiguous).
- TOON TABLES: a column-mode formatter (in units.py, written once)
  picks one common unit per homogeneous column such that every
  mantissa lands in ~0.001–10000 → unit in the HEADER (`gap (nm)`),
  bare aligned cells. If the spread can't fit one unit without
  zero-runs, per-value neat units in cells and no header unit. Never
  header unit + divergent cell suffixes.

- **prefunits (Reto 2026-09-12), per-call — IN SCOPE this round:**
  any op/view accepts `prefunits=nm,deg,kg` (one preferred unit per
  dimension); output renders in those units, ALWAYS still stated
  (`0.15 nm`, never bare). Implementation: a preferred-unit override
  threaded into `format_quantity`/the column formatter — display
  only, ingest unchanged, no second prefix table. Overrides the neat
  auto-prefix; respond-in-kind still wins for query answers when the
  caller's own unit is clear. WHY (Reto): this closes the
  no-model-side-conversion loop — unit-required input means the LLM
  never converts writing; prefunits means it never converts reading.
  It works in one unit end to end; every conversion happens once, in
  code.
- **Sticky prefunits — design-scoped, NOT session state (backlog
  consideration):** "set until disregard" lives as `meta.prefunits`
  ON THE DESIGN/REF (a crystal design sets Å once; every later view
  of it renders Å) — durable, survives sessions, no protocol state;
  per-call prefunits overrides it. This RESOLVES the parked
  "Å display hint for structure views" question. Weigh at this
  round's build whether design-meta ships now (small) or waits.

- **conv kind + never-convert rule (Reto 2026-09-12):** checked —
  `conv` (handlers/conversation.py) stores free-text chat turns and
  has NO quantity machinery; nothing to wire. The notation reaches
  conversations via convention only: agents writing prose use the
  canonical forms — covered by the skills directive below, no
  conv-handler work. And the skills get a
  standing directive, stated once in `precis-overview` and echoed in
  the cad/se/nm/structure/calc help skills: **never convert units
  yourself — always through the tools** (calc's conversion path
  `3 ft to m`, or better, prefunits so no conversion is needed at
  all). An LLM-performed unit conversion is a silent-error site the
  whole policy exists to eliminate; the skills must say so explicitly
  rather than assume it.

No data migration (stored pose/rot already SI: metres always, radians
via `0006_units_se_pose_rot_rad.sql`). Implementation: extend the
shared `_as_vec3` ingest path (ONE place — se and nm both ride it)
with the vector grammar from `precis/utils/units.py`'s parse + hint
machinery; sweep se/nm op docstrings + skills examples to the
canonical form; fixture sweep for authored pose literals. Natural
bundle: ship as one "interface explicitness" round together with
gr336352 (MCP-wide angle explicitness — calc's deg/rad triple
convention, pcb `rot` self-naming) and gr336344 (awkward-mantissa
warn). SEQUENCE AFTER the prompt-surface audit fixes land (they touch
the same hint machinery/files).
