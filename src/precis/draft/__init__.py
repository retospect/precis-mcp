"""Draft-document machinery shared by the store, handlers, and web reader.

- The structured ``term`` **registry** (ADR 0052) — glossary, patent
  drawings/parts, and manufacturing components/BOM as one abstraction over
  the ``chunk_kind='term'`` leaf, distinguished by ``meta.registry`` and a
  per-registry numbering policy. See :mod:`precis.draft.registry`.
- Document-class **scaffolding** — genre briefs + section styles + the
  standard section skeleton laid down per ``doc_type`` (ADR 0037 step 4,
  paper-writing pipeline rung 4). See :mod:`precis.draft.scaffolds`.
"""
