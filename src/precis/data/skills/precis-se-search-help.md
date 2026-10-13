---
id: precis-se-search-help
family: se
title: precis — finding and composing se library blocks (search wants=, compose=, the lever family)
summary: search(kind='se', wants={...}) ranks every library block by its declared capabilities (never a strict filter); search(kind='se', compose={'delta': [...], 'span': [...]}) proposes a rotary unit + arms composition that meets the ask and runs DRC on the composed tree; the lever family is the worked example — read precis-se-help first for the op grammar
answers:
  - how do I find a library block that does what I need (a light-driven bistable switch, say)?
  - how do I get precis to propose a composition that meets a delta/span requirement?
  - what is the lever family and how does compose swing= use it?
  - why did the composition proposer return a weaker match instead of nothing?
applies-to: search(kind='se'); read precis-se-help first for the op grammar
status: active
tags: verbs, design
kinds: se
---
# Finding and composing se library blocks

Split from `precis-se-help` 2026-10-13 (size cap). The op grammar, units
and the checking views are there; this skill is the search half.

## Ranked library search — search(kind='se', wants=…)

`search(kind='se', wants={...})` ranks every non-instance block in the
whole library against a per-attribute wishlist — **never a strict
filter**: results are always ranked, every row shows each attribute's
✓/✗ and its actual value, and an empty result is impossible unless the
library itself has no blocks. `q=` is then optional — with it, the card
search narrows which *designs* are considered; a narrow to zero designs
falls back to the whole library and says so in the header.

Each `wants` value is one of three shapes: a bare scalar (`'light'`,
`True`, `1.0`) is a *target* — numeric targets match within 10% relative
tolerance, str/bool match exactly; a `[lo, hi]` list is an *interval* —
matches when the value's band overlaps it; a dict
`{'target'?, 'min'?, 'max'?, 'tol'?, 'weight'?}` (weight default 1.0,
tol default 0.1) spells out both the comparison and its human-set
importance — an agent never tunes its own weight.

Three keys are built in, read off the block/tree itself: `stimulus`
(matches if the wanted value is among the block's transitions'
`driver_kind`s); `bistable` (True iff ≥2 declared states AND no
`thermal` transition — a thermal path is a T-type reverse); `joining`
(matches a port `roles` entry, or either half of a named chemistry —
`'CuAAC'` matches an `azide` or `alkyne` port). Every other key is a
**star-schema lookup**, never a fact stored on the block: a
`component`-bound block's own spec values, then that component's
`made-of` material's property values, then the design's own `made-of`
material (optionally scoped to one block via the link's
`meta.block`) — first hit wins, and the row names the entity + source
+ conditions it came from. When a material property carries several
sourced rows, the pick is a band row (the spread summary) if exactly one
survives, else the newest — never a silent, unexplained newest — and the
row's provenance says which and lists the samples passed over; a dict
want `{'target'?, ..., 'conditions'?: {...}}` narrows to the row(s)
whose own `conditions` match every given key/value first (falling back,
and saying so, when nothing matches). An unrecognised key (not a spec,
not a property, not a built-in) still scores as a plain miss; the header
notes it once, never a refusal.

```python
search(kind='se', wants={'stimulus': 'light', 'bistable': True,
       'delta_length': 1.0, 'joining': 'CuAAC'})
# 3 library block(s) ranked for wants={...}
# 1. switch1#dye  3/4  ✓stimulus: light  ✓joining: azide (CuAAC)
#    ✗bistable: T-type (thermal reverse)  ✓delta_length: 1 nm (component:azo1)
```

The winning row instances directly: `edit(kind='se', id=<yours>,
ops=[{'op':'instance_block','name':'<new>','template':'switch1#dye'}])`
— `template` takes the qualified `<design>#<block>` spelling cross-design
instancing already supports (slice 1).

## Composition proposer — search(kind='se', compose=…)

`search(kind='se', compose={'delta': [10, 12], 'span': [40, 50]})`
enumerates **n switches in series + m spacers** over the ranked-search
library and scores each composition against the box exactly like a
`wants=` row — never a strict filter, never empty while one switch
exists (the nearest misses show with their distances). `delta` (Å,
port-to-port stroke) and `span` (nm, long-state length) take the same
scalar / `[lo, hi]` / dict shapes as `wants`; at least one is required.
`n_max`/`m_max` (default 6/4) bound the enumeration (cap 2 000
compositions). Left unset, each switch/spacer derives its own from the
box's `delta`/`span` hi edge (clamped to 200) — the header names the
bound and fix when that still misses. `wants=` may ride along: its keys
score on the **switch** block; `q=` narrows designs. A `conditions` box
key (same shape as `wants[key]['conditions']`) filters every per-unit
fact read — e.g. pick the row logged at one wavelength over a newer
one.

(Scoring and ranking rules: the module docstring of
`precis/src/precis_se/compose.py`.) Facts are star-schema rows, never on
the block — five ordinary
`material`/`component` properties (an unknown one mints `proposed`-tier
on first `put(kind='material', …, property=…)`, no migration):
`delta_length` (Å; a block with a row is a *switch*), `unit_length`
(nm; with no `delta_length` the block is a *spacer*),
`pss_short_fraction` (0–1, photostationary conversion; conditions carry
the wavelength), `thermal_half_life` (s), `persistence_length` (nm).
Blocks with neither length row are skipped and counted in the header.

Every row surfaces what a stroke estimate must not hide: the PSS-scaled
Δ beside the ideal one (`Δ 10.2 Å (8.16 Å at PSS 80 % short)`, or
`PSS unknown`), the bistability verdict with τ½ (`bistable ✗ (T-type
(thermal reverse), τ½ 2 d)`), `floppy: span 23 nm > Lp 15 nm (rod#u)`
when the span exceeds the spacer's (or, without one, the switch's)
persistence length — `stiffness unknown` when there is no row — and
the switch↔spacer port complementarity (`azide↔alkyne (CuAAC)`).

```python
search(kind='se', compose={'delta': [8, 9], 'span': [20, 30]},
       wants={'stimulus': 'light'})
# 1. 3 × azo#u + 2 × rod#u  3/3  ✓delta: 10.2 Å (8.16 Å at PSS 80 % short)
#    ✓span: 23 nm  ✓stimulus: light  · bistable ✗ (…, τ½ 2 d)
#    · floppy: span 23 nm > Lp 15 nm (rod#u) · joining: azide↔alkyne (CuAAC)
```

The Next line is the top row's ops script: `instance_block` × n +
spacers, alternating, joined by `connect` through the complementary
ports — paste it into `edit(kind='se', id=<yours>, ops=[…])` and run
DRC on the composed tree. `compose='<design>#<block>'` (or
`'<design>#<block>/<from>-><to>'`) reads the box off that block's own
declared transition `requires=` instead of a literal dict — its
`stimulus` comes from `driver_kind`. Exactly one transition may carry
`requires=`; none → `declare_transitions … requires=`; several → add the
`/<from>-><to>` selector.

## Lever family — rotary unit + arms (compose swing=)

A third box key, `swing` (°, total rotary angle), adds a second family.
A **rotary unit** is a block with a non-zero `view='kinematics'` swing
(else a sourced `step_angle`); one with no usable envelope (no `arm₀`)
is skipped and counted in the header. With
`delta`, lever rows — one rotary unit + k arm units (ordinary spacers)
at its rotating port, tip stroke `2·(arm₀ + k·unit_length)·sin(angle/2)`
— rank in ONE merged list beside switch chains (`family: lever` /
`family: chain` on each row). With `swing` instead, `n` rotary units
chained (angles summed) rank alone (`family: series`). `delta` and
`swing` together is refused — one stroke measure. A lever row's `span`
is its arm reach (`arm₀ + k·unit_length`, nm), scored like a chain's and
labelled `(arm reach)`; a series row's `span` is its units' summed
`unit_length`. Each lever row shows the arm breakdown, the angle and its origin (`derived
from trans → cis on port p1`, or a sourced `step_angle` beside a
disagreeing derived one), the bistable/τ½ verdict, and the
rotating-port↔arm joining — `joining: none (…)` when no role is
complementary, and the ops script then omits that connect rather than
guessing a port.
