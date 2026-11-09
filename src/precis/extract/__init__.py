"""The survey/review numeric-extraction substrate.

The "Next slice" of docs/backlog/measures-substrate.md, built on the
qu202467 pilot (paper-extraction-pilot/, kept as the reference
implementation): a local reader proposes bindings chunk by chunk, the
deterministic checks prove the mechanical claims, and a frontier model
samples the batch at a 5% error bar — no per-value human sign-off.

* :mod:`precis.extract.anchors` — the versioned positional anchor
  machinery (sentence ids, runs, numeric atoms, raw offsets); the
  constant is append-only.
* :mod:`precis.extract.verify` — literal/unit/numeric-region checks.
* :mod:`precis.extract.proposals` — the reader's reply → checked
  ``MeasureSpec`` runs, or recorded drops.
* :mod:`precis.extract.prompt` — the reader and sampler prompts.
* :mod:`precis.extract.accept` — stratified sampling + the Wilson
  acceptance rule.
* :mod:`precis.extract.tables` — the deterministic pipe-table grid
  parse (header, rows, exact-offset cells, footnote/citation markers,
  caption lookup); the one definition of a table line and separator.
* :mod:`precis.extract.alchem` — the ALCHEM knowledge graph as an
  external comparison set: TriG parse, ALCHEM unit normalisation and
  the per-paper agreement diff (gr606461).

Pure functions only: the DB and LLM glue lives in
``precis.workers.job_types.survey_extract``.
"""
