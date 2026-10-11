---
status: idea
pillar: 3d-design
title: structure search — systematic-name / synonym expansion on the card index
---

# structure search: systematic names reach trivial-name cards

From gr334774 (nm dogfood 2026-09-11). `search(kind='structure',
q='diazene phenyl')` returns unrelated phosphorene / borophene cards for a
molecule indexed as `azobenzene`. The card text
(`src/precis/handlers/structure.py::card_text`) carries the trivial name
only, so an agent searching by systematic fragment names finds nothing the
trivial-name indexing covers.

Direction: expand each card with synonyms / systematic names at index
time (e.g. via PubChem or an IUPAC-name lookup keyed on the structure's
canonical identity) so lexical and semantic legs both see them; keep the
expansion in the card text so no query-time change is needed. Distinct
from `search-notation-symmetric-fold-index.md`, which folds unit/symbol
notation, not chemical nomenclature.

Acceptance: `q='diazene phenyl'` ranks the azobenzene card first; an
unrelated 2-D material does not appear above it.
Who: agent-code; needs-Reto: synonym source (external lookup vs curated
table).
