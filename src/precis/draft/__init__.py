"""Draft-document machinery shared by the store, handlers, and web reader.

- Cascade recovery emits typed puts in reading order. Child puts use new
  parent handles because the retired tree stays out of reading order.
- Pin advisories check each table row separately so a source column can
  cover values in its row. Captions and headers have their own scope.
  See :func:`precis.draft.lint.pin_hint`.
- The structured ``term`` **registry** — glossary, patent
  drawings/parts, and manufacturing components/BOM as one abstraction over
  the ``chunk_kind='term'`` leaf, distinguished by ``meta.registry`` and a
  per-registry numbering policy. See :mod:`precis.draft.registry`.
- Document-class **scaffolding** — genre briefs + section styles + the
  standard section skeleton laid down per ``doc_type`` (paper-writing pipeline rung 4). See :mod:`precis.draft.scaffolds`.
- Abbreviation hygiene warns about authored definitions of glossary terms:
  export defines first use across chunks, so a later authored definition
  can repeat it. Write hints cover newly introduced definitions; the full
  hygiene view also catches existing prose and terms registered later.
"""
