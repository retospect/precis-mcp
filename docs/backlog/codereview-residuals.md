# Codereview residuals

Grouped 2026-09-26 from 8 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## codereview: handler/mixin size cleanups — residuals

_Grouped 2026-09-26; was `codereview-handler-size-cleanups`._

Done (shipped): DraftStore review surface lifted to `DraftReviewStore`
(`store.drafts.review.*`, 12 methods, transitional delegations kept);
draft-lint hint generators extracted to `handlers/_draft_lint.py`;
finding.py's store-only state machines extracted to
`handlers/_finding_acquire.py`/`_finding_edit.py`/`_finding_evidence.py`
(FindingHandler stays on NumericRefHandler — it genuinely uses the
shared CRUD contract); perplexity tiers collapsed to a `_SonarTier`
config dataclass + `cost_per_call_usd` declared on `CacheBackedHandler`.

REMAINING:

- Migrate the review-surface call sites (`handlers/draft.py`,
  `quest/review_fanout.py`, `handlers/_review_view.py`,
  `precis_web/routes/drafts.py`) to `store.drafts.review.*` and delete
  the 12 transitional delegations on DraftStore
  (`tests/test_store_drafts_facade.py` pins their signature parity
  meanwhile).
- `precis_web/item_view.py::ItemPresenter` — 10-method hierarchy with
  one subclass overriding one method; watch-item only, keep an eye on
  whether it earns its ceremony (module docstring already tracks
  promotion honestly).

## codereview: DB row mapping — positional-mapper residuals

_Grouped 2026-09-26; was `codereview-row-mapping-layer`._

The three worst flows are shipped: draft review surface returns
frozen dataclasses (`_draft_ops.py::ReviewableChunk`/`ChunkReviewEntry`/
`DraftReviewRow`, consumed via attributes through `quest/review_fanout`
→ `handlers/_review_view` → `precis_web/routes/drafts`); component +
structure ops read via psycopg `dict_row` with TypedDict rows
(`_component_ops.py::ComponentValueRow` union family,
`_structure_ops.py::StructRunRow`/`StructForcesRow`); pathway payloads
typed (`precis_pathway/types.py::PathwayArtifact`/`NetworkTopology`/
`SeedPartialResult`/…). Pattern for new code: named column access
(`dict_row` + `cast` to a TypedDict, or explicit named unpack) — never
positional indexing over a long SELECT list.

REMAINING (convert opportunistically, when the file is next touched):

- `store/_mappers.py` — the original refs/blocks/links mappers are
  still positional over up to 30 columns with defensive `len(row) > N`
  probing, and no pool-level `row_factory` exists (`store/pool.py`).
  Converting is a big, mechanical, test-heavy diff; do it per-mapper
  when a mapper's SELECT next changes, not as a big bang.
- `store/_material_ops.py` — identical `_row_to_property`/
  `_row_to_value` positional mappers + the same 5-way tagged-union
  values shape as component ops; same `dict_row` + TypedDict recipe
  applies directly.
- `store/_structure_ops.py::structure_load` — atom/bond/measure rows
  use positional multi-variable unpacking (self-documenting but
  drift-prone); larger Scene-construction diff, low urgency.
- `store/_pcb_ops.py` — dict-returning sigs never audited in detail.

## codereview: `store: Any` epidemic — convert remaining call sites

_Grouped 2026-09-26; was `codereview-store-typing-seam`._

Seam is typed (shipped): `Hub.store: Store | None` + `Hub.live_store`
narrowing property; central `precis/store/protocols.py` (role
protocols); the four ad-hoc `_StoreLike`/`_StoreProto` Protocols
consolidated/deleted; the two MRO-luck runtime stubs in
`store/_refs_ops.py` / `_cache_ops.py` retired to `TYPE_CHECKING`;
`ActorSlug` widened to include the seeded `chase` actor.

Wave 1 shipped: taproot/reading/backfill packages fully converted
(~77 sites; new role protocols `PoolStore`/`ClaimTrustStore`/
`SettingsStore`; `refeye._Chunk` → `@property` members). Conversion
recipe that worked: role protocol where tests pass a fake
(`test_export_latex._FindingStore`, `test_briefing_cast._NudgeStore`/
`_AlertLaneStore`), plain `Store` under `TYPE_CHECKING` elsewhere;
`briefing_cast._lane_quest`/`_lane_system_activity` stay `Any` (their
fakes are narrower than the `Store`-typed callees they forward into —
commented in place).

Wave 2 shipped: workers tree + precis_web + quest (~270 sites; new
role protocols `LinksStore`/`RefsByIdStore`/`RefMetaStore`; 3 latent
None-deref fixes typing surfaced: `factory._quests`,
`anki_sync` lock-row, `claude_inproc._build_job_result_text` ×3).
Gate lesson: coders' *targeted* host mypy runs miss cross-file
test-fake incompatibilities — the container gate type-checks ALL
files including every test, so a helper newly typed `Store` whose
test passes a hand-rolled fake fails only there. Before shipping a
typing batch, run full-tree mypy (or pre-check the fake-passing
tests). Nine precis_web helpers stayed `Any` with the standard
comment for exactly this reason (`smartdraft._cited_sources`/
`_needs_items`/`_ref_connection_groups`, `asks._chunk_context`/
`_ask_value`, `drafts._paper_pdf_missing`, `items._folder_options`,
`status._budget_tote`, `nav._gripes_count`). Protocol structural
matching needs matching parameter NAMES too (a fake's
`ids: list[int]` vs protocol `ref_ids: Iterable[int]` fails on both
name and variance).

Wave 3 (batch 3) shipped: the full remaining file list — `utils`,
`handlers`, `export`, `reading`, `taproot`, `precis_pathway`, `cli`,
`backfill`, `pcb` (~120 sites, 6 new role protocols in
`store/protocols.py`: `BlockListingStore`/`RefLookupStore`/
`PdfLookupStore`/`DraftsSubStore`/`BlockSearchStore`/`PinStore`); also
fixed several `precis_pathway/handler.py` sites that read `self.hub.store`
where `self.hub.live_store` was meant. A handful of sites stayed `Any`
with the standard fake-mismatch comment (`claude_quota.refresh_snapshot`,
`mentions.*`, `eye_render.*`), matching the wave-1/2 convention.

REMAINING — convertible incrementally (each batch its own ship):

- `precis_web` / `workers`-adjacent stragglers not yet swept (check via
  `grep -rn 'store: Any' src/precis src/precis_web src/precis_pathway`
  before starting the next batch — the count above is stale the moment
  a new file lands).
- set_by audit SHIPPED: `dream`/`weave`/`orcid` seeded (migration
  0127), `ActorSlug` widened, `upsert_stub_paper`/`mint_citation`
  retyped. Residual coverage gap: `draftimport/{resolve,build}.py`
  pass `set_by="tex-import"` into `upsert_stub_paper` through an
  `Any`-typed `store` param, so mypy can't see the Literal mismatch —
  benign at runtime (lands only in `refs.meta` JSON +
  `ref_identifiers.source`, no FK), but whoever tightens
  `draftimport`'s `store: Any` must add `"tex-import"` to `ActorSlug`
  (or seed an actor) at that point.

Related: [codereview-store-decomposition] (the facade work will keep
these protocols as its public role surface).

## codereview: verb-surface flat signatures — needs a proposal, not a patch

_Grouped 2026-09-26; was `codereview-verb-surface-proposal`._

> 2026-08-21: the demanded proposal's surface half SHIPPED — the frontier
> one-string profile (`PRECIS_MCP_PROFILE=command`,
> `tools/command_parser.py`); the typed surface stays for generic clients.
> This item keeps the *internal* half: args= migration of kind-specific
> params, typed per-verb signatures, retiring the `[override]` wall.

`tools/core.py` exposes `put` with 72 params (search 30, edit 37) — a flat
union of every kind's kwargs, widened by each new kind, shadowing builtins
(`property`, `min`, `max`, `id`). Downstream: `Handler` declares verbs as
`**kw: Any`, all ~55 subclasses narrow (181 `type: ignore[override]`), and
`runtime/dispatch.py::_accepted_kwargs` reconciles by reflection — so the
whole verb boundary is invisible to mypy.

Constraints (Reto, 2026-08-11): the narrow MCP surface is deliberate —
token-efficient minimal tool schema that "explodes with skills"; MCP means
backward compat is NOT the blocker, but any migration of existing
kind-specific params into the `args=` extras channel touches MCP schema +
associated skills together and needs a full proposal first.

Interim (can ship without the proposal): freeze the surface — a guard test
pinning the param count/roster of each verb so new kinds must use `args=`;
new kind-specific params don't widen the top-level verbs.

Proposal to write: migrate existing kind-specific params (material's
`property`/`min`/`max`/`maturity`, etc.) to `args=`; typed per-verb
signatures (Protocol or per-kind TypedDict kwargs) to retire the
`[override]` wall; skill-doc updates in the same change.

## codereview: Divergent duplicate helpers — pick canonical, consolidate

_Grouped 2026-09-26; was `codereview-dedup-helpers`._

**Cite-key minting, patent export, and provenance matching disagree on the same paper.**

### _first_author_surname ×5

Locations:
- `precis/identity.py` (full impl, **canonical**)
- `utils/short_cite.py`
- `export/_patent_cite.py` (skips ASCII folding)
- `ingest/provenance.py::_first_author_surname` (returns whole name when no comma; also `_from_authors`)

~60 LOC true duplication. Impact: cite-key minting fragmentation.

### Abbreviation short-form regex ×3

Locations:
- `export/docx.py::_Ctx.short_pattern` → `\b…(s?)\b`
- `export/latex.py` ~L379 → `(?<![\w-])…(s)?(?![\w-])`
- `precis_web/linkify.py` ~L952 → `(?<![\w-])…(?:s|es|'s|'s)?(?!\w)`

Manifestation: hyphenated compounds highlight in the web reader but not the PDF.

Canonical home: `utils/abbreviations.py` (has find/substitute, lacks a pattern builder).

### _lease_seconds ×2

Locations:
- `workers/executors/ssh_node.py` reads `params.resources.wall_seconds`, floor `_LEASE_FLOOR_S`
- `claude_docker.py` reads `params.wall_seconds`, floor `_LEASE_MARGIN_S`

Impact: a job carrying the other shape silently gets the floor lease. Needs a decision on the canonical meta shape.

**Decided + partially landed**: the key is unified on the nested spelling
(`params.resources.wall_seconds` — ssh_node/coordinator/quest.compute/
precis_pathway already wrote this shape). `sandbox_run.py`'s schema and
`claude_docker.py`'s three read sites (`_lease_seconds`, `_launch_build`,
`_launch_run`) were migrated to nested-write + a read-both shim
(`sandbox_run.resolve_wall_seconds`: prefer nested, fall back to legacy
flat `params.wall_seconds` so an in-flight row keeps leasing/launching).
The helper *consolidation* itself (one shared `_lease_seconds` instead of
per-executor copies) remains open.

### Module-kept copies by convention ×N

Documented in docstrings as "each module keeps its own copy" — causes real drift.

Examples:
- Taproot claim predicates: `_is_claim_hub` (byte-identical in `taproot/hub.py` + `taproot/seniority.py`); `_is_compound_hub` (`taproot/hub.py` + `workers/hub_refine.py`); conjunct-of relation string ×5 modules; no test pins copies together.
- `_cosine` ×5: `skill_index/index.py` · `utils/segmentation.py::zip(strict=True)` raises · `quest/gaps.py` silently truncates · `quest/placement.py` + `quest/tick.py` numpy

Re-decision needed: retire the convention or make it enforceable (e.g., via a code-sync test asserting copy parity).

### utils/llm/router.py::dispatch — inline gate chain

~15 sequential gate/filter steps (placement filter, cloud throttle, unserved-local-rung skip, failover wrap, breaker, admit, local-serving slot, hosted-small remap) each with a multi-line inline-comment block; several already have `_apply_*`/`_skip_*` sibling helpers. Extract each gate to a named helper so its comment lives once at the helper.

### `_label`/`_head` "handle — title" formatter ×2

`utils/refeye.py` and `utils/eye_render.py` duplicate a near-identical truncate-at-90/80-chars formatter — one shared formatter.

## material/component: whole-handler fork → shared typed-property core

_Grouped 2026-09-26; was `material-component-shared-core`._

`handlers/material.py` ↔ `handlers/component.py` share 20 symbols outright. ~500 LOC true duplication.

### Shared handler methods (20 symbols)

_check_type_consistency, _check_unit, _coerce_bool, _display_source, _display_value,
_fmt_conditions, _put_entity, _put_value, _render_table, _resolve_source, _route_value,
_search_entities, _search_values, _source_public_id, _validate_type_args, accepted_views,
get, put, search, __init__

### Store-layer duplication

`store/_material_ops.py` vs `store/_component_ops.py` — same op set, component adds containment.

### Deviation: _resolve_source

Byte-identical except material→component in one error string. Component copy has already lost an explanatory comment (documentation drift indicator).

### Right shape

A shared `TypedPropertyMixin` (or similar) — the two kinds parameterize a base for:
- Typed property with unit, conditions, maturity, source
- Value storage and search
- Rendering and entity resolution
- Source attribution and lineage

Split the handler into `BaseTypedPropertyHandler` (shared) + per-kind subclasses (overrides). Do the same at the store layer with parameterized ops.

Prerequisite: clarify whether component's added containment belongs in the base or as a component-only override.

## Retire classify.py + classify_topics.py onto axis_pass

_Grouped 2026-09-26; was `classify-into-axis-pass`._

`workers/axis_pass.py` already parameterizes the claim → LLM-classify → tag-write shape over any axis definition.

### Current state

Two hardcoded workers coexist:
- `workers/classify.py` (464 LOC) — claim classification
- `workers/classify_topics.py` (394 LOC) — topic axis (similar shape)

Both duplicate:
- `_extract_json`
- `_load_axis`
- `_classify_one`
- `_build_prompt` / `_build_chunk_prompt`
- Verbatim `_render_examples` (axis_pass.py's docstring notes this copy was deliberate "per the additive-only brief")

Canonical: `workers/axis_pass.py` (~700 LOC true duplication).

### Migration path

1. Convert axis definitions to YAML under `data/axes/` (parametrize the axis spec, prompt instructions, examples, taxonomy)
2. Retire `classify.py` and `classify_topics.py` — ingest/worker dispatch registers them as `axis_pass` with the config'd axis from data/
3. Same verb surface and job kinds survive (backward compatible)

### Benefit

Axis definitions become self-serve (no Python changes to add/tweak an axis), axis_pass becomes the sole parameterized classifier, and the code-duplication bind breaks.

## Structure/layering: utils god-package, hub_refine, web leakage, contract gaps

_Grouped 2026-09-26; was `structure-layering`._

### utils/ god-package (26.5k LOC / 75 modules)

`utils/__init__.py` docstring: "Small pure utilities" — **false**.

Subsystems that belong elsewhere:
- `llm/router.py` (3,273 LOC)
- `claude_agent.py` (62KB)
- `search_merge.py`, `toc.py`, `refeye.py`, `workspace.py`

All are **non-pure** (29 reference Store). Candidates: move beside `src/precis/runtime/` (user has sanctioned package/file renames where helpful).

### workers/hub_refine.py (3,437 LOC) — mixed concerns

Five disjoint responsibilities:
- **Claim policy**: claim_depth_policy, judge_edge_strict, StrictVerdict, is_front_matter → move to `taproot/` (already imported)
- **Plan/apply data model**: Reground*/apply_reground_plan/verify_hub_intent → move to `taproot/`
- **~15 raw-SQL conn.execute helpers** → store op
- **External probes**: _probe_s2, _probe_perplexity → own module
- **Worker pass** (policy + probes applied to hubs)

Action: extract, re-home, keep the pass.

### Store ops leak into precis_web (195 sites across 22 modules)

195 `conn.execute` / `store.pool.connection()` calls — web should consume store interfaces, not execute SQL directly.

#### Case study: smartdraft.py (1,281 LOC)

LLM-free relevance ranker importing:
- `precis.quest.review_fanout`
- `precis.store._draft_ops`
- `precis.utils.table_data`

Currently reachable **only** through FastAPI. **Belongs in `src/precis/`** as a first-class inference engine.

### precis_web/routes/refs.py (3,112 LOC) — flat file, 60 functions, 20 kind branches

Violates the "one per tab" rule. Examples of inlined pages:
- `_quest_detail` (~190 LOC)
- `_pathway_detail` (~192 LOC)

Action: split per routes/__init__.py's established convention (one file per tab/kind).

### importlinter contract gap

**Declared contract**: "store is the bottom layer" (bans store → workers/handlers/tools/jobs/ingest/cli/server).

**Reality**: store imports domain packages freely.

Examples:
- `store/_structure_ops.py:32–35` → `precis.structure.{cell,importers,measures,scene}` incl. `measures.evaluate` (domain computation from a store op)
- `store/_cad_ops.py` → `precis.cad`
- `store/_pcb_ops.py` → `precis.pcb`
- `store/_users_ops.py` → `precis.users`
- `store/core/store/_refs_ops` → `precis.hints`

Action: extend `forbidden_modules` to match reality, **or** push types down (store exports only the stable interface, domain packages provide the computation).

### precis_web imports underscore-private internals (7+ sites)

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

### store/_tags_ops.py::TagsMixin.tags_for_with_expiry

Zero Python callers. Docstring claims "asa_bot reads it via the precis MCP" — **confirm consumer before deleting** (may be a stale claim).
