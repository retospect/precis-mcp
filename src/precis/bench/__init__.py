"""Memory-ablation benchmark: fixture corpus, planted facts, ingest, (later) harness.

The bench measures what each memory layer (keywords, summaries, categorizer
tags, fisheye, recall, hybrid search, chunking) buys an agent in tokens and
correctness, on a public corpus anyone can re-fetch: a pinned, hash-verified
subset of QASPER (AI2, CC BY 4.0). :mod:`precis.bench.corpus` holds the
committed-manifest selection, the cached fetch and the loader that mints one
``kind='paper'`` ref per QASPER paper into a fresh dev database. The loader
writes text, cards and chunk offsets only; embeddings and derived passes are
the worker's job (``fill_embeddings``). Entry point: ``scripts/bench``; design
in the memory graph, hub ``me621608``; slices under todo ``td604839``.
"""
