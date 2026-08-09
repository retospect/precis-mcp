---
status: draft
title: Patent-evidence parity — remaining phases (prophetic-example flag, dream un-defer)
model: opus
---

# Patent-evidence parity — remaining phases

Phases 1–3 shipped (hub-refine patent discovery leg with legal-claim-block
grounding filter + patent-aware verify prompt; publication-date seniority
incl. `refs.year` at patent ingest + meta fallback; family mechanism —
`family_id`, simple-family stubbing, `same-family-as`, representative
helper; citation kind accepts patent handles + patent bibliography format +
family collapse in the hub evidence view). Shipped behavior lives in the
owning docstrings: `src/precis/workers/hub_refine.py`,
`src/precis/handlers/_patent_ingest.py`, `_patent_family.py`,
`citation.py`. Terminology ("world-claim" vs "legal claim") →
`docs/architecture/glossary.md`.

Open:

- **Phase 4 — prophetic-example flag.** Cheap classifier over patent
  example paragraphs (past-tense worked vs present-tense prophetic, US
  convention), stored as a chunk-level tag via the chunk-classifier
  cascade, surfaced as a verifier caveat and in cites ("worked example"
  vs "prophetic — corroborates at best"). Open call: regex tense-heuristic
  first, or straight to a `role3`-style local model?
- **Phase 5 — un-defer dream external reach for patents.** The eye-draw
  hook exists (`dream_agent.py` cross-pollination fuel); flip once a
  patent spark can land as a grounded, citable edge (it can, post 1–3 —
  gate on a little prod soak first).
- **Watch on first prod use:** priority-claims extraction
  (`_patent_xml.py`) was built from the ST.36 shape without a live-OPS
  sample; a shape mismatch degrades safely to full ingest (never stubs),
  but the first real stub decision deserves a glance.

Explicitly not in scope (unchanged from the shipped design): patent
authoring/FTO (→ `patent-authoring-loop.md`), new patent sources beyond
EPO OPS, patent citation-following, any hand-set mixed-warrant tag
(derive warrant profile from edges at render time).
