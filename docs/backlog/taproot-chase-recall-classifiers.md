---
status: idea
pillar: memory-graph
title: Topic and axis classifiers skip finding and claim kinds, so chase recall cannot use them
---

# Classifiers for finding and claim kinds (gr182078)

Bundled in the 2026-10-11 gripe triage. The topic and ref-level axis passes
only cover papers and patents, so the taproot chase cannot filter or boost
by topic/axis on findings and claim hubs.

- `src/precis/workers/classify_topics.py::_claim` (driven by `run_classify_topics_pass`) still selects
  kinds `paper`/`patent`: extend it to finding and claim kinds.
- Ref-level axes: `src/precis/workers/axis_pass.py` plus the
  `src/precis/data/axes/*.yaml` `applies_to_kinds` lists. Add the finding and
  claim kinds to the axes that make sense for them, and check that the pass
  reads the right text for those kinds (claim sentence plus scope, not a body).
- Measure cost before enabling: claim hubs are numerous (~3.6k canonical),
  so run a dry count and a small-slice precision check first.

Who: agent-code (S).
