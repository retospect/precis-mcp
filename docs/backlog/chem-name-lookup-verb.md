---
status: idea
pillar: 3d-design
title: Chemistry name lookup — spoken common name for narration
---

# Chemistry name lookup — spoken common name for narration

Follow-on to gripe 168609's small slice (offline `speak_chemistry`
transform in the narrate/verbalize pronunciation path). The offline helper
covers curated common formulas, simple salt/hydrate parsing, and
acronym-number designators — anything outside that table stays verbatim.

The agent-facing direction (name / CAS / abbreviation → SMILES + InChIKey)
shipped as `get(kind='chem', id=…)` (`precis.handlers.chem`,
`precis-chem-help`). That settles the surface question: one `chem` kind,
no second chem surface.

Open: the reverse direction for narration — formula, IUPAC name or
registry ID in, spoken common name out — reusing the `chem` kind's cached
PubChem record (CID → synonyms) with the offline table as first tier, so
narration never blocks on the network (async enrich, speak-from-cache).

Needs: PubChem synonym-picking rules (the "preferred" title vs the first
synonym is often wrong for spoken text) and a formula → CID path
(PUG REST `fastformula` returns many isomers, so it needs the same
ambiguity refusal the `chem` kind applies).
