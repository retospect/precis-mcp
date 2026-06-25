"""Import external LaTeX documents into the ``draft`` kind.

Phase 1 is a read-only *dry run* (:mod:`precis.draftimport.tex`): flatten
``\\input`` trees, extract the citation family, build the section tree, and
report bibliography coverage — no database writes. The DB resolution pass
(cite key -> precis paper slug, via the ``ref_identifiers`` alias table) and
the writing pass (mint project + ``create_draft`` + ``add_chunks``) layer on
top once the dry-run map looks right.
"""
