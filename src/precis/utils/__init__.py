"""Shared leaf helpers — everything handlers, workers, the store and the
web/bridge packages need that has no domain of its own.

**Shape.** Flat modules plus two subpackages (``llm``, ``prompt``). Meant as
the bottom layer: most modules are leaves, but a few that render or merge
across kinds (``kind_facts``, ``handle_registry``, ``search_merge``, the eye
renderers) import from ``handlers/`` — treat those as the exceptions, not the
pattern. Module docstrings own the detail; this one only groups them.

**Groups.**

- *Outbound network + LLM seams.* :mod:`~precis.utils.safe_fetch` (SSRF guard —
  every agent-supplied-URL fetch goes through ``safe_get``/``safe_stream``;
  raw redirect-following ``httpx`` is a vulnerability), :mod:`~precis.utils.http`
  (the one shared client seam for network-reaching kinds), :mod:`~precis.utils.rate_limit`,
  :mod:`~precis.utils.url`, :mod:`~precis.utils.inject_scan` (tier-0
  prompt-injection scan). :mod:`precis.utils.llm` is the LLM routing layer
  (tiers, chains, transports; every routed call goes through ``route``);
  :mod:`precis.utils.prompt` is the prompt-assembly model. The ``claude -p``
  wrappers are :mod:`~precis.utils.claude_p` (worker judge calls),
  :mod:`~precis.utils.claude_agent` (agentic calls),
  :mod:`~precis.utils.claude_oauth` / :mod:`~precis.utils.claude_quota`.
- *Text and parsing.* Block parsers for the prose kinds (``md_parse``,
  ``tex_parse``, ``plaintext_parse``), :mod:`~precis.utils.chunk_ingest` (the
  parsed-blocks to ``ChunkInsert`` pipeline — embeddings stay NULL, the worker
  fills them), :mod:`~precis.utils.chunk_slug`, :mod:`~precis.utils.fractional`
  (sibling-scoped chunk ordering), :mod:`~precis.utils.edit_resolve` (anchored
  edits), sentence/keyword/segmentation helpers (``sentences``, ``rake``,
  ``semantic_keywords``, ``segmentation``, ``abbreviations``), the LaTeX
  hardening/fix layers (``tex_hardening``, ``tex_mechanical_fix``,
  ``tex_llm_fix``, ``compile_guard``), and the draft helpers (``draft_markup``,
  ``draft_regex``, ``house_style``, ``wordcount``).
- *Identity and addressing.* :mod:`~precis.utils.handle_registry` (the single
  source of universal handles), :mod:`~precis.utils.handles` (opaque draft
  chunk handles), :mod:`~precis.utils.mentions`, :mod:`~precis.utils.slug`,
  :mod:`~precis.utils.authors`, :mod:`~precis.utils.short_cite`,
  :mod:`~precis.utils.file_id` / :mod:`~precis.utils.workspace` (prose-file
  kinds under ``PRECIS_ROOT``).
- *Search and render plumbing.* :mod:`~precis.utils.search_merge`,
  :mod:`~precis.utils.ref_hybrid`, :mod:`~precis.utils.embed_query` (degrades
  to lexical on a bad embedder), :mod:`~precis.utils.toc` / ``toc_db``,
  :mod:`~precis.utils.fisheye` / ``refeye`` / ``eye_render``,
  :mod:`~precis.utils.next_block` (the ``Next:`` hint block),
  :mod:`~precis.utils.search_header`.
- *Units, time, env.* :mod:`~precis.utils.units` (ingest-any, SI internally),
  :mod:`~precis.utils.timeutil` (coerce to tz-aware UTC),
  :mod:`~precis.utils.utc_logging`, :mod:`~precis.utils.env`.
- *Process plumbing.* :mod:`~precis.utils.db_log_handler` (centralised worker
  logs), :mod:`~precis.utils.db_retry`, :mod:`~precis.utils.load_gate`,
  :mod:`~precis.utils.container_limits`, :mod:`~precis.utils.optional_deps`
  (lazy-import guards for extras).

**Seams.** Most helpers are pure; the DB-touching ones (``db_log_handler``,
``rate_limit``, ``handle_registry``) take their connection or store from the
caller. A generic helper wanted by two or more packages belongs here — look for
an existing home before adding a private copy; a helper with one domain owner
stays with that owner. Conventions that bite: ``docs/conventions/``.
"""
