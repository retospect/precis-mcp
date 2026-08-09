# Split the giant modules

`handlers/draft.py` (2,877 lines): extract the ~9 hint methods →
`_draft_hints.py`, table CRUD → `_draft_tables.py` (the
`paper.py`/`_paper_*.py` precedent). Same medicine later for
`precis_web/routes/drafts.py`, `status.py` (promote its `_*_ctx`
seams), `refs.py`, `tasks.py`. Sonnet-shaped.
