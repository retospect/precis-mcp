# Chunk-tag classifier (ADR 0047) — corpus enablement

Cascade shipped + validated; the remaining steps are below.

- ~~Flip PRECIS_CLASSIFY_ENABLED=1 for the role3 corpus drain~~ — happening
  via the `derived_drain` classify band instead (materialize
  `PRECIS_SMALL_BAND_CLASSIFY`, live in prod): as of 2026-08-16, 1.98M
  chunks carry ROLE3 (own 795k / background 605k / furniture 586k), 163k
  remain, draining ~40k+/day since the 2026-08-15 SMALL cloud cutover.
  Still open from that bullet: optional tier-2 escalation
  (PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5, ~$200–400) for own-claim
  precision past 91%.
- The generic axis runner (`src/precis/workers/axis_pass.py`) has never been
  enabled for any of its ~10 axes. material/transport eval numbers are STALE
  (2026-07-25 vocab changes; material has no gold rows for its three new
  values) — re-run scripts/classify/eval-classifier + add gold rows first.
  The topic cascade has no gold at all (CLASSIFY_TOPICS_VERSION 3) —
  spot-check tier-1 precision on the new topics before a corpus sweep.
- BLOCKER before any chunk-level axis sweep (role, open-question): a per-axis
  failed-chunk_claims lease reaper — a failed LLM call leaves the lease, so
  the chunk never retries until a version bump. Ref-level axes self-retry.
- open-question on memory is a no-op until a ref-level path for a
  level:chunk axis exists (see data/axes/open-question.yaml note). Better
  table detection (pipe/tab/repeated-token heuristic) is polish.
