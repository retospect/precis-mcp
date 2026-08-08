# context-quality-eval

## Residuals (from OPEN-ITEMS)

Round-2 agent-facing render bugs, precisely root-caused, unfixed:
- Attention view drops a halted todo's reason —
  `src/precis/handlers/_todo_views.py::render_attention` builds h['reasons']
  from halt:<reason> tags but only prints id+title (the sibling child-failed
  loop shows its reason). test: a halt-tagged leaf shows the reason inline.
- Cross-kind / view='keywords' TOON tables drop the universal handle for
  numeric-ref kinds (bare integer) —
  `src/precis/handlers/_numeric_ref.py::_body_search_hits` missing uhandle=;
  `src/precis/utils/search_merge.py` table renderers fall back to
  str(ref_id); handle_registry CHUNK_CODES missing "orcid". test: renders
  m<id>/oi<id>, not bare ints.
- Quest frontier shows the default "objective: energy (min)" for
  non-materials quests — suppress/qualify when no candidates and
  meta.rubric_objectives unset (`quest.py::_render_frontier`).
- sort='recency' source-search omits the N-of-K total + per-kind breakdown —
  `src/precis/runtime/search.py::_dispatch_source_search`.
- view='strategic' has no scoping/pagination (deferred — possibly an
  intentional dashboard). The quest-domain classifier gap is gr170252.
