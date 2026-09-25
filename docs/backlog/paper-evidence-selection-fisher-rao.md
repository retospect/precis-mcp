---
status: idea
title: Investigate task-relevant information-gain evidence selection
---

# Investigate task-relevant information-gain evidence selection

Research selection by expected task-relevant information gain per total token
cost. Shannon conditional entropy/mutual information describes the objective;
vector methods and Fisher–Rao geometry are possible tools, not interchangeable
with it. Preserve source-grounded scientific results and their conditions.
Follow-up to the local paper-extraction pilot; not implementation-ready.

Owner: paper enrichment in `src/precis/workers/`; retrieval through
`src/precis/store/_chunks_ops.py::ChunkStore.search_chunks`.

## Questions

- Define the uncertain target: result identities, quantity/condition bindings,
  provenance, and application-relevant qualifiers. Distinguish uncertainty
  reduction about these targets from entropy, novelty, or surprisal of the text
  itself. Knowing that necessary information is absent can also be useful.
- Calibrated posterior distributions are not currently established. Compare
  practical coverage/dependency proxies before claiming formal information gain;
  include the cost of estimating that gain.
- Compare lossless compact rendering and dependency-aware heuristic selection
  against maximal marginal relevance, facility-location/coverage selection,
  and Fisher–Rao-based selection where justified.
- Fisher–Rao requires a meaningful probability model. Investigate distributions
  over competing experiment/condition/evidence bindings and their calibration;
  arbitrary embedding similarities or their softmax are not calibrated beliefs.
- Prefer passages that resolve consequential uncertainties or unlock many
  supported records, not merely semantically novel passages. Preserve required
  methods, table headers, captions, reference conventions, and exceptions.
- Fit this around one capable document-level mapping/validation pass, reusable
  source bindings, script-generated records, and exception-focused review.

## Evaluation

Use frozen held-out papers/questions, separate from selector tuning. Measure
correct quantity/subject/condition bindings, omitted qualifications, justified
abstention, and grounded record coverage. Count total cost: preparation,
selection, mapping, expansion, extraction, retries, output, and verification;
label payload-token proxies separately from actual provider usage.

Never discard numerically distinct evidence because embeddings are close
(e.g. 20 mK versus 20 K), or treat model temperatures as physical operating
requirements. Retain provenance and unknown/inferred applicability explicitly.

Deliver a measured comparison and adopt/defer recommendation. A selector that
costs more than the reading it saves, or loses important context, has not won.
No production schema change or deployment is authorized by this research note.
Fisher information for curve-fit parameter identifiability is a separate question,
not a prerequisite for this investigation.
