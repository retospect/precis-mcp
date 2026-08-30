# Structure/layering: utils god-package, hub_refine, web leakage, contract gaps

## utils/ god-package (26.5k LOC / 75 modules)

`utils/__init__.py` docstring: "Small pure utilities" — **false**.

Subsystems that belong elsewhere:
- `llm/router.py` (3,273 LOC)
- `claude_agent.py` (62KB)
- `search_merge.py`, `toc.py`, `refeye.py`, `workspace.py`

All are **non-pure** (29 reference Store). Candidates: move beside `src/precis/runtime/` (user has sanctioned package/file renames where helpful).

## workers/hub_refine.py (3,437 LOC) — mixed concerns

Five disjoint responsibilities:
- **Claim policy**: claim_depth_policy, judge_edge_strict, StrictVerdict, is_front_matter → move to `taproot/` (already imported)
- **Plan/apply data model**: Reground*/apply_reground_plan/verify_hub_intent → move to `taproot/`
- **~15 raw-SQL conn.execute helpers** → store op
- **External probes**: _probe_s2, _probe_perplexity → own module
- **Worker pass** (policy + probes applied to hubs)

Action: extract, re-home, keep the pass.

## Store ops leak into precis_web (195 sites across 22 modules)

195 `conn.execute` / `store.pool.connection()` calls — web should consume store interfaces, not execute SQL directly.

### Case study: smartdraft.py (1,281 LOC)

LLM-free relevance ranker importing:
- `precis.quest.review_fanout`
- `precis.store._draft_ops`
- `precis.utils.table_data`

Currently reachable **only** through FastAPI. **Belongs in `src/precis/`** as a first-class inference engine.

## precis_web/routes/refs.py (3,112 LOC) — flat file, 60 functions, 20 kind branches

Violates the "one per tab" rule. Examples of inlined pages:
- `_quest_detail` (~190 LOC)
- `_pathway_detail` (~192 LOC)

Action: split per routes/__init__.py's established convention (one file per tab/kind).

## importlinter contract gap

**Declared contract**: "store is the bottom layer" (bans store → workers/handlers/tools/jobs/ingest/cli/server).

**Reality**: store imports domain packages freely.

Examples:
- `store/_structure_ops.py:32–35` → `precis.structure.{cell,importers,measures,scene}` incl. `measures.evaluate` (domain computation from a store op)
- `store/_cad_ops.py` → `precis.cad`
- `store/_pcb_ops.py` → `precis.pcb`
- `store/_users_ops.py` → `precis.users`
- `store/core/store/_refs_ops` → `precis.hints`

Action: extend `forbidden_modules` to match reality, **or** push types down (store exports only the stable interface, domain packages provide the computation).

## precis_web imports underscore-private internals (7+ sites)

Breaks encapsulation:
- `routes/tags.py:18` + `routes/smartdraft.py:25` → `store._tags_ops._escape_like`
- `routes/cad.py:44` → `cad.bulk._expr_aabb`
- `routes/console.py:39` → `tools.cli_adapter._convert_value`
- `routes/datasheets.py:34` → `export.latex._DATASHEET_SUBTYPE_LABELS`
- `smartdraft.py:53` + `draft_eyes.py:106` → `store._draft_ops.content_sha`
- `_mappers.SEMANTIC_DISTANCE_FLOOR`
- `executors._common.{QUEUED,RUNNING,…}`
- `_prio_tag.PRIO_TAG_TO_INT`
- `_citations_view.draft_fetch_ref_ids`
- `_finding_hypothesis.PROPOSED_TAG`
- `export._data_package.collect_entry`

Action:
- **One-symbol cases**: promote to public in the owning module (add to __all__)
- **Multi-symbol clusters**: new public submodule homes (e.g., `export.public_constants` for DATASHEET labels)

## store/_tags_ops.py::TagsMixin.tags_for_with_expiry

Zero Python callers. Docstring claims "asa_bot reads it via the precis MCP" — **confirm consumer before deleting** (may be a stale claim).
