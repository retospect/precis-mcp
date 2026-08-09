---
status: draft
title: Patent-evidence parity — residual watch items
model: opus
---

# Patent-evidence parity — residual watch items

All five phases shipped. Phases 1–3: hub-refine patent discovery leg with
legal-claim-block grounding filter + patent-aware verify prompt;
publication-date seniority incl. `refs.year` at patent ingest; family
mechanism (`family_id`, simple-family stubbing, `same-family-as`,
representative helper); citation kind accepts patent handles + patent
bibliography format + family collapse in the hub evidence view. Phase 4:
`patent_example` chunk-level classifier axis (worked/prophetic/none,
tense-of-performance test) + deterministic prophetic caveat injected at
`attach_evidence()` (the single evidence-edge choke point). Phase 5:
dream's patent eye-draw was already live (ADR-0051); the residual gap —
a family stub wasting the draw slot — closed with a stub filter in
`_recent_ref_ids`. Shipped behavior lives in the owning docstrings:
`src/precis/workers/hub_refine.py`, `src/precis/handlers/_patent_ingest.py`,
`_patent_family.py`, `citation.py`, `src/precis/taproot/hub.py`,
`src/precis/workers/dream_agent.py`. Terminology ("world-claim" vs "legal
claim", "worked vs prophetic example") → `docs/glossary.md`.

Open (watch items only — no build work):

- **Enable the axis in prod.** `axis:patent_example` auto-registers but is
  default-OFF like every axis service; until a `service_config` row turns
  it on (`precis service prio '*' axis:patent_example 1`), patent chunks
  stay unclassified and the prophetic caveat never fires (untagged chunks
  get no caveat by design). Operator step, needs the prod-write key.
- **Watch on first prod use:** priority-claims extraction
  (`_patent_xml.py`) was built from the ST.36 shape without a live-OPS
  sample; a shape mismatch degrades safely to full ingest (never stubs),
  but the first real stub decision deserves a glance.
- **Watch axis precision:** the tense-heuristic lives in the small-tier
  model prompt (no regex pre-stage exists in `axis_pass.py`); if prod
  precision on `prophetic` disappoints, the escalation lever is a
  confident-pattern regex stage or a `role3`-style local model.

Explicitly not in scope (unchanged from the shipped design): patent
authoring/FTO (→ `patent-authoring-loop.md`), new patent sources beyond
EPO OPS, patent citation-following, any hand-set mixed-warrant tag
(derive warrant profile from edges at render time).
