"""Typed edit-op catalog + apply pipeline.

The ``edit(kind='structure_draft', ops=[...])`` verb takes a list of
typed op records. ``validate`` checks each op against the structure's
current views; ``apply`` runs them via ASE and pymatgen; ``enumerate``
expands combinatorial ops (``substitute(... enumerate='all')``) into
batches of sibling structures.

See ``catalog.py`` for the closed list of op kinds and their
schemas; ``apply.py`` for the ASE-side machinery; ``enumerate.py``
for symmetry-distinct expansion; ``validate.py`` for the per-op
checks.
"""
