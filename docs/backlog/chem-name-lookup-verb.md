---
status: idea
title: Chemistry name lookup — PubChem-backed formula/ID → common name
---

# Chemistry name lookup — PubChem-backed formula/ID → common name

Follow-on to gripe 168609's small slice (offline `speak_chemistry`
transform in the narrate/verbalize pronunciation path). The offline helper
covers curated common formulas, simple salt/hydrate parsing, and
acronym-number designators — anything outside that table stays verbatim.

The full version: a lookup — formula, IUPAC name, CAS, or registry ID in →
common/spoken name out — backed by PubChem (PUG REST) through `safe_fetch`,
with the offline table as first-tier cache/fallback and a persistent cache
so narration never blocks on the network (async enrich, speak-from-cache).
Surface question is open: internal helper for the pronunciation-lexicon
builder + compose prompt only, or an agent-facing verb (`get(kind='chem',
q=…)`) next to the `precis_chem` tooling — decide against
`chem-tools-integration.md` so we don't grow two chem surfaces.

Needs: rate-limit etiquette for PubChem, cache keying (canonicalized
formula), and a quality gate (PubChem "preferred" vs first synonym is often
wrong for spoken text).
