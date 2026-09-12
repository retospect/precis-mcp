---
status: draft
title: pose/rot vector unit ingest — awaiting Reto ruling
prio: normal
---

# pose/rot vector unit ingest

Carved out of `units-policy-cutover.md` (shipped 2026-09-12, deleted) —
its one OPEN question. Pose/rot vectors are today bare numbers meaning
SI (metres / radians) through the shared `precis.blocktree.ops._as_vec3`
path se and nm both use. That is the zero-counting hazard
(`pose: [0.000000003, 0, 0]`) on the most-used arg, deliberately left
out of the cutover because tightening it changes se's op surface too.

**Recommended (pending Reto):** vectors join the unit-required set with
a single unit per vector — accept `"3 0 0 nm"` (compact) and
`["3 nm", "0 nm", "0 nm"]` (componentwise); bare numeric vectors reject
with the standard two-reading hint. No data migration (stored pose/rot
already SI after the cutover: metres since se's origin, radians via
`0006_units_se_pose_rot_rad.sql`). Alternative: document
bare-SI-by-convention permanently and accept the hazard.

If ruled strict: one small round — extend `_as_vec3` ingest with the
unit grammar (reuse `precis/utils/units.py` parse + hint machinery),
sweep se/nm op docstrings + skills examples, fixture sweep for authored
pose literals. The angle components follow the shipped angle ruling
(`deg`/`rad` explicit).
