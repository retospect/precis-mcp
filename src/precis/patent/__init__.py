"""``precis.patent`` — patent parsing/ingest helpers shared by handlers,
jobs and workers (moved out of ``handlers/_patent_*``; see the import-linter
contract in ``pyproject.toml``).

The claims and TOC views classify stored text with the current dependency
heuristic so parser fixes apply to existing patents without a re-ingest.
"""
