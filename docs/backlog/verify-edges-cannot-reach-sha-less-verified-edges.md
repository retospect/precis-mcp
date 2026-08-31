---
status: draft
title: verify-edges has no cohort for a verified edge with no claim sha
prio: high
---

# A verified, sha-less edge is unreachable by either `verify-edges` cohort

Found 2026-08-31 on **fi189536** (link_id 993363).

`taproot/verify_edges.py` offers exactly two cohorts:

- default — edges with no verdict at all;
- `--unverified-stamped` — `support` set at mint time with **no**
  `verified_by` fingerprint.

fi189536's edge is in neither:

```
link_id 993363  corroborates  support=partial
verified_by=opus-5/retro-verify   verified_claim_sha=NULL
```

It has a `verified_by`, so `--unverified-stamped` excludes it
(`AND NOT (l.meta ? 'verified_by')`); it has a `support`, so the default
cohort excludes it. Both `--hub 189536` runs report **0 edges
processed** — silently, which reads exactly like "nothing to do".

## Why it matters

`nanopub/preflight.py::withheld_edges` withholds an edge whose
`meta.verified_claim_sha` does not equal `claim_sha(live refs.title)`. A
NULL sha never matches, so the edge is **permanently withheld** and no
CLI path can re-stamp it. The hub is invisibly stuck behind the publish
gate.

`opus-5/retro-verify` is not this sweep's fingerprint
(`VERIFIED_BY`), so these edges came from earlier passes that predate
the `verified_claim_sha` stamp. It is a cohort, not a single row —
measured on prod 2026-08-31:

```
opus-5/retro-verify              207
agent:ga3-grounding-audit-step3   66
opus-5/autoyes-pushback           38
                                 ---
                                 311 edges across 186 live claim hubs
```

Every one of those 186 hubs is permanently withheld and no CLI cohort
can reach it. Six of the seven nanobud hubs re-checked on 2026-08-31
were in this set, which is how it surfaced: a `verify-edges --hub <id>
--apply` run reported `0 edge(s) processed` for five hubs in a row.

## Options

1. Widen the default cohort to `support IS NOT NULL AND
   verified_claim_sha IS NULL`, i.e. treat a sha-less verdict as
   unverified. Costs a re-verification per edge but is honest: the old
   verdict was against an unknown sentence.
2. Backfill the sha from the current title. **Wrong** — it would assert
   that a verdict from an unknown earlier sentence applies to today's,
   which is exactly the staleness the sha exists to catch.
3. A third `--sha-less` flag, parallel to `--unverified-stamped`.

Option 1 is preferred: the sha is missing precisely because nobody
recorded what was verified, so re-verifying is the only way to earn the
stamp.

Whatever the fix, a 0-processed run against an explicit `--hub` should
say *why* it selected nothing rather than reporting a bare zero.

## Related

`docs/backlog/nanobud-claim-remediation.md` — fi189536 was reworded
2026-08-30, so its verdict is doubly stale.
