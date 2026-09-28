---
status: idea
title: precis-se-help is over the 32KB skill hard cap and is allowlisted — split the FRET/optical and discrete-states domains into their own skills
---

# `precis-se-help` needs the same split its companions already got

## What

`precis-se-help.md` crossed `FAIL_BYTES` (32768) at 33072 B on `1a547543`
and now sits in `_ALLOWLIST` in `tests/test_skill_size.py`. That entry is a
short-term escape, not the fix — the test says so itself, and the file still
counts toward the `WARN_BYTES` drift warning so its shrink-back stays
visible.

It is the largest skill in the corpus by a wide margin (next is
`precis-se-print-help` at 27300 B), and it is served in ~14 KB paginated
frames, so an agent asking about *one* op pays two or three round-trips
before reaching it.

## Why prose trimming is the wrong lever

The over-cap mass is not verbosity. The file is the single call surface for
the whole `se` kind: blocks and ports, connects and joints, loads, measures
and datums, modes, binding and realize, BOM and order, notes, formfind,
discrete states and transitions, kinematics, FRET optics, ranked library
search, and the composition proposer. Compressing it loses ops
documentation and buys a few hundred bytes that the next `se` feature
spends again — which is what happened here.

## Fix

The pattern is already established: `precis-se-design-help`,
`precis-se-fasten-help`, `precis-se-print-help` and `precis-se-atomic-help`
were all split out of this file. Two domains are ready for the same
treatment:

1. **`## Optical (FRET) ops — energy transfer as a comm channel`** (~2.5 KB)
   — `set_chromophore` / `set_optical_link` / `set_optics`, `view='fret'`,
   the dead-link diagnostics, and the required-efficiency check. A
   self-contained sub-domain with its own vocabulary; three of the twelve
   `answers:` entries are FRET questions.
2. **`## Discrete states + transitions`** (~3.6 KB, the largest section) —
   `declare_states` / `declare_transitions` / `set_current_state`,
   `args={'state': …}` transient posing, and `view='sweep'`.

Either one alone brings the file under the cap with headroom; both together
take it to roughly 27 KB, in line with its siblings. Each needs the usual
skill-split work: new file with frontmatter and `answers:`, pointers added
to this skill's intro and `## See also`, and the moved `answers:` entries
relocated so retrieval still finds them.

When that lands, delete the `precis-se-help` entry from `_ALLOWLIST`.

## Already done

The chunk-budget half of this was fixed in the same ship: the
`## Ops — loads, prose, measures, modes, fabrication, BOM, notes, formfind`
section was 4286 chars against a 4000-char chunk budget and is now split
into `## Ops — loads, prose, measures`, `## Ops — modes, binding,
fabrication`, and `## Ops — BOM, order, notes, formfind`.
