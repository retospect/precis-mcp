---
name: embedding-worker
description: Embeddings are filled by the worker, never by ingest
metadata:
  type: reference
---

Ingest stores chunks with a NULL embedding vector; the embed worker fills them later. Calling fill_embeddings from ingest double-bills the GPU.
