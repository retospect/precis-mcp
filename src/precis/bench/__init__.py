"""Memory-ablation benchmark: fixture corpus, planted facts, ingest, (later) harness.

The bench measures what each memory layer (keywords, summaries, categorizer
tags, fisheye, recall, hybrid search, chunking) buys an agent in tokens and
correctness, on a public corpus anyone can re-fetch: a pinned, hash-verified
subset of QASPER (AI2, CC BY 4.0). :mod:`precis.bench.corpus` holds the
committed-manifest selection, the cached fetch and the loader that mints one
``kind='paper'`` ref per QASPER paper into a fresh dev database. The loader
writes text, cards and chunk offsets only; embeddings and derived passes are
the worker's job (``fill_embeddings``). Arms that change the corpus itself
(chunker, edges, size) re-ingest into their own throwaway databases —
``corpus.Variant`` / ``corpus.provision_variant``, one ``runner.World``
each. :mod:`precis.bench.reformulations` holds the committed per-task query
rephrasings the multi-query arm reads. Entry point: ``scripts/bench``; design
in the memory graph, hub ``me621608``; slices under todo ``td604839``.

A second, standalone benchmark lives here too: :mod:`precis.bench.ann_recall`
measures recall@k of the ``chunk_embeddings`` HNSW index against an exact
scan — the number the contextual-embedding re-embed sweep is gated on
(gr690288). Entry point: ``scripts/bench-ann-recall`` (prod through the
sanctioned ``scripts/prod-psql`` hop, or ``--url`` for any reachable DB);
the measured numbers and their interpretation are in the module docstring.
"""
