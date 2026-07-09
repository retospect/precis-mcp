"""Draft-document machinery shared by the store, handlers, and web reader.

Currently: the structured ``term`` **registry** (ADR 0052) — glossary,
patent drawings/parts, and manufacturing components/BOM as one abstraction
over the ``chunk_kind='term'`` leaf, distinguished by ``meta.registry`` and a
per-registry numbering policy. See :mod:`precis.draft.registry`.
"""
