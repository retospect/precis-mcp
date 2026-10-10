# Draft chunk gists carry hallucinated arXiv ids and LLM scratchpad text

**what**: Draft chunk gists (auto-generated summaries) currently carry hallucinated arXiv identifiers and stray LLM scratchpad text (e.g., reasoning, intermediate states) in the gist body. Requires a regeneration pass of existing gists plus a guard in gist generation to block scratchpad leakage.

**why**: Gists are user-facing summaries; hallucinated arXiv ids mislead readers, and scratchpad text exposes internal reasoning that should not be persisted. Found while retiring stale repo-dev memory threads on 2026-10-10.

**owner anchor**: `src/precis/workers/job_types/` · gist generation workers · `src/precis/ingest/` (gist generation entry points)

**test**: n/a — requires manual audit of existing gists in draft chunks and verification that scratchpad text does not appear in newly generated gists.
