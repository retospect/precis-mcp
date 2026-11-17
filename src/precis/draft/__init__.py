"""Draft-document machinery shared by the store, handlers, and web reader.

- The structured ``term`` **registry** — glossary, patent
  drawings/parts, and manufacturing components/BOM as one abstraction over
  the ``chunk_kind='term'`` leaf, distinguished by ``meta.registry`` and a
  per-registry numbering policy. See :mod:`precis.draft.registry`.
- Document-class **scaffolding** — genre briefs + section styles + the
  standard section skeleton laid down per ``doc_type`` (paper-writing pipeline rung 4). See :mod:`precis.draft.scaffolds`.

Explicit list writes accept one Markdown item because the requested kind
establishes list intent. Automatic paragraph conversion still requires two
bullets to avoid restructuring dash-led prose.
"""
