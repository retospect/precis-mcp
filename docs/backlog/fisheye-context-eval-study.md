---
status: idea
title: Fisheye-rail context-efficiency study — eval + paper (gr56588)
---

# Fisheye-rail context-efficiency study (gr56588)

Research study, not fixer-buildable. Consolidated from gripe 56588
(tags: adr-0051 fisheye research eval).

Once the fisheye rail (docs/proposals/draft-reader-fisheye-rail.md) is
real: for a medium-skill model writing/revising reports, measure CONTEXT
QUALITY and TOKEN EFFICIENCY under the fisheye-rail flow
(machine-proposed, right-sized, relevance-fisheye context) vs the
TRADITIONAL flow (whole-doc or naive-RAG context).

Hypothesis: the fisheye rail gets a mid-tier model to comparable output
quality at materially fewer tokens, because context is right-sized and
relevance-weighted rather than dumped.

Metrics: task success / edit quality (human or judge rubric), tokens-in
per accepted edit, hallucination/ungrounded-claim rate,
revisions-to-acceptance. Deliverable: a paper.

Ties to ADR 0051 (turn-taking + fisheye) and the per-tick job.meta
snapshots (a ready-made corpus of what-context-worked). Distinct from
`context-quality-eval.md` (that's the audit catalog/rubric over server-built
contexts; this is a controlled A/B study of the fisheye flow itself).
