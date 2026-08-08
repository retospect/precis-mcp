# Abstract pass — Crossref / Semantic Scholar fallback

A DOI stamped `meta.openalex.miss` never gets an abstract from anywhere. Add
a Crossref/S2 abstract fallback lane through safe_fetch; stamp its own miss
the same way so it isn't re-tried. Owner
`src/precis/workers/openalex_enrich.py` +
`src/precis/ingest/openalex_meta.py`. Polish; mechanical.
