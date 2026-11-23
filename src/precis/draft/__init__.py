"""Draft-document machinery shared by the store, handlers, and web reader.

- Cascade recovery emits typed puts in reading order. Child puts use new
  parent handles because the retired tree stays out of reading order.
- An outbound ``derived-from`` link starts at the draft chunk so readers
  can trace the passage to its source.
- Pin advisories check each table row separately so a source column can
  cover values in its row. Captions and headers have their own scope.
  See :func:`precis.draft.lint.pin_hint`.
- The structured ``term`` **registry** — glossary, patent
  drawings/parts, and manufacturing components/BOM as one abstraction over
  the ``chunk_kind='term'`` leaf, distinguished by ``meta.registry`` and a
  per-registry numbering policy. See :mod:`precis.draft.registry`.
- Document-class **scaffolding** — genre briefs + section styles + the
  standard section skeleton laid down per ``doc_type`` (paper-writing pipeline rung 4). See :mod:`precis.draft.scaffolds`.
- Draft-reference policy: a standalone one-to-four-digit bracketed number
  (``[001]``, ``[12]``) is prose and writes literally; five-plus digits
  (``[99999]``) still trip the bad-citation check. The cutoff is a lint
  heuristic, since handle parsing imposes no five-digit minimum. See
  :func:`precis.draft.lint._classify_chunk_ref_tokens`.
- Narration applies the curated chemistry lexicon before saved PubChem
  synonyms. The chem adapter enriches its records during lookup; render
  reads them locally so provider timeouts cannot delay audio.
- Abbreviation hygiene warns about authored definitions of glossary terms:
  export defines first use across chunks, so a later authored definition
  can repeat it. Write hints cover newly introduced definitions; the full
  hygiene view also catches existing prose and terms registered later.
- Self-plagiarism detection is text-level, not cite-level: a claim hub's
  canonical sentence carried verbatim by this draft AND another live draft
  is flagged at export (:func:`precis.draft.lint.find_verbatim_claim_reuse`,
  gr693801). A paraphrasing cite never trips it; an uncited paste does.
  The advisory uses the same live reading order as export and only names
  claim hubs and drafts visible to the export's reader.
"""
