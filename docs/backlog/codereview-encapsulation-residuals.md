# codereview: encapsulation residuals — small, independent

- `utils/rate_limit.py` — bare module-level `psycopg.Connection`
  (`_conn`, autocommit, never pooled/closed); the only connection outside
  the pool with no ownership story.
- `asa_bot/claude_invoke.py::_CHAIN_EXECUTOR` — ThreadPoolExecutor built
  at import time, no shutdown path.
- Unsynchronized module dict caches in request-handling web code:
  `precis_web/smartdraft.py::_NODE_CACHE`, `routes/cad.py::_ANALYSIS_CACHE`,
  `routes/drafts.py::_ABBREV_CACHE`/`_RO_CACHE` (contrast `secrets.py`,
  which pairs its cache with a lock).
- `_ensure_ingested` is documented public API for 5 external modules
  (one via `type: ignore[attr-defined]`) — rename public, same for
  `_sync_draft_links`.
- `precis_pathway/runner.py` injects `cfg._prebuilt_slab` onto a foreign
  autocatpath `Config` as a cache-key side-channel (3 writes, 1 read) —
  wrap instead of monkey-patch.
- Duplicated `Transport`/`HttpTransport` + `post_json` pair in
  `utils/llm/openai_tools.py` and `workers/llm_summarize.py` — unify.
- Repeated arg-cluster: executors thread `(store, ref_id, title, meta)`
  through 7 functions while `executors/_context.py::DispatchContext`
  already exists — pass the context object.
