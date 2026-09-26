---
status: draft
---

# Vocab compaction

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Vocabulary compaction — staged full-consistency migration

_Grouped 2026-09-26; was `vocab-compaction-stages`._

Decision (Reto, 2026-08-30): one word = one meaning, enforced at EVERY level —
files, classes, functions, vars, DB columns, tag axes, meta keys, MCP kwargs,
CLI commands, skill ids. Persisted names migrate (forward-only migrations +
backfills); no legacy names retained once a stage lands. Glossary keeps a
`(legacy: X)` note only while the old name is still encounterable, then drops
it. Stages ship independently, each internally consistent.

Evidence base: the two survey matrices (homonym + synonym, 2026-08-30 session);
sizes/cost classes verified there.

### Stage A — code-internal cheap renames (LANDED — enforced by tests/test_vocab_lint.py)

Tier→SourceGrade · nursery Finding→Symptom · validate Finding→ValidationIssue ·
reaper Candidate→DeadHold · PassBand→PassPriority · router dispatch()→route() ·
KindSpec.role→placement · reading/cards.py→flashcards.py · review lens→persona ·
citation_lens→citation_recall · cast Source→CitedRef · lane kwargs→sim_kind/
cadence · addr→handle · gloss→summary · excerpt consolidation · export cite
verbs→render_ · block→chunk surface lies (types.py docstring, tools/core.py,
4 skills) · wall_seconds nested unification (read-both shim) · local hub→claim_hub.

### Stage B — store.blocks→chunks full facade rename (LANDED)

Code-only (table was already `chunks`). `Block`→`ChunkRow` (renamed off
`workers/base.py::ChunkRow`, a pre-existing unrelated worker-claim type, now
`ClaimedChunk`), `BlockInsert`→`ChunkInsert`, `Store.blocks`→`Store.chunks`,
`BlockStore`→`ChunkStore` + the whole method surface (incl. the bare
`search_blocks`→`search_chunks` dispatcher, not originally enumerated),
`pos`→`ord` field alignment on `ChunkRow`/`ChunkInsert` (and
`Link.src_pos`/`dst_pos`→`src_ord`/`dst_ord`, confirmed 1:1 via
`sc.ord AS src_pos` in `_links_ops.py`), parser `*Block` types renamed
(`MdBlock`/`PlaintextBlock`/`TexBlock`→`*Chunk`, keeping their own `pos`
field — a deliberate, separate parse-fragment concept), `block_ingest.py`→
`chunk_ingest.py`, `block_slug.py`→`chunk_slug.py`, protocol accessors,
~477 test sites (mypy-driven convergence pass caught the cross-module
fallout). `add_link(src_pos=…)`/`LinkTarget.pos`/`HeadingHit.pos`/
`SearchHit.pos`/`LogbookLine.pos`/`block_pos=` kwargs are a deliberate,
pre-existing "pos" agent-facing convention, untouched. Out of scope (left
alone, different concept): `ingest/blocks.py` (parse-stage dicts),
`md_index`'s `MdBlockEntry`/`BlockKind` (its own index-entry type), the real
persisted `chunks.pos` TEXT column (draft-tree lexicographic ordering, via
`DraftChunk.pos`) — do not confuse with `ChunkRow.ord`. Glossary block
legacy note shrunk to just the prompt-assembly `Block` type.

### Stage C — persisted low-blast migrations (LANDED c6c386a3, deployed 2026-08-30)

- `email_scan.tier` (INT col) → `depth` — forward migration + code.
- `claim_embeddings.claim_ref_id` → `hub_ref_id` — migration + code (its own
  COMMENT already says the rows are claim hubs).
- Quest fidelity meta keys: `meta.tier_ladder`→`fidelity_ladder`,
  `tier_promote_neb/_verify`→`fidelity_promote_*`, candidate
  `flags/measures.barrier_tier`→`barrier_fidelity` — migration UPDATE backfill
  of quest/candidate meta + code + skills (`precis-quest-help`) + glossary
  (**tier ladder**→**fidelity ladder** entry).
- Review digest `tier_tag` open-tag strings on memories — backfill UPDATE of
  ref_tags + code field rename (`digest_tag`).
- wall_seconds cleanup: backfill in-flight job rows flat→nested, then remove
  the read-both shim.

### Stage D — surface renames (LANDED c6c386a3, deployed 2026-08-30)

- Worker `dispatch` → `minter`: registry name, `precis worker --only minter`,
  service_config row UPDATE migration, skill `precis-dispatch-help`→
  `precis-minter-help` + all cross-refs. Historical `ref_events.source=
  'dispatch'` rows: DECIDED (Reto 2026-08-30) — rewrite history too
  (migration UPDATE → 'minter'); full consistency wins over provenance.
- CLI `precis watch` → `precis ingest --watch` (+ deploy launchd plist).
  `WATCH:` tag axis and recurring-todo watches KEEP the word (they are the
  canonical senses); patent_watch is semantically a watch — keeps it.
- MCP `source=` kwarg split — DECIDED (Reto 2026-08-30): the provenance
  family wins the word (`put(source='paper:<slug>')`, `source_handle`,
  `source_quote` — "the artifact a datum comes from"). The patent-search
  `source='both'|'local'|'remote'` is a search-LEG selector, not a source
  → rename to `reach=` (1 token, reads naturally: reach='remote'). Sites:
  tools/core.py verb kwarg + handlers/patent.py + patent skills. The
  `SRC:primary/secondary` tag axis is bibliographic source-grade (Stage A's
  SourceGrade) — standard literature term, KEEP.
- Review persona externals (Stage A renamed code only): web HTTP `"lens"`
  JSON field + CLI `--lenses` flag → persona vocabulary.
- Write-ack string `"<verb> block <N> '<slug>' …"` (`utils/file_id.py::
  format_write_result`, pinned by test_files_write.py) still says "block" to
  agents → "chunk", together with its `block_pos=`/`block_slug=` kwargs and
  the wider agent-facing `pos`/`block_pos=` convention (`add_link`,
  `LinkTarget.pos`, …) — one coordinated surface rename, agents re-learn.

#### Stage D residuals (found during the D build — fold into Stage E's ship)

- Code-internal review "lens" survives in `store/_draft_ops.py`,
  `store/_draft_review_ops.py`, `handlers/draft.py`,
  `workers/executors/claude_inproc.py` (incl. the persisted todo
  `meta.review=<persona>` write path) — Stage A's persona rename only
  reached quest/planner. Cheap-class but sizeable; touches the executor
  state machine, do deliberately.
- `PRECIS_BACKFILL_CITATION_LENS` env var → `_RECALL` (not `_PERSONA` — see
  the Stage E build note; it follows Stage A's citation_lens→citation_recall
  vocabulary). No deploy reference exists — code+docs only.
- Scope expansions D already made (recorded so the spec reads true): edgar's
  identical leg selector renamed to `reach=` in lockstep (shared verb
  kwarg); `SearchHit.source`→`reach` in `utils/search_merge.py`;
  `smartdraft.py` `_MACHINE_LENSES`→`_MACHINE_PERSONAS`. The
  `precis_watch` deploy role/label/log filenames keep their names (only the
  invocation changed).

### Stage E — task→todo + retire/soft-delete unification (LANDED c6c386a3, deployed 2026-08-30)

- task→todo: DECIDED (Reto 2026-08-30) — full rename. Web /tasks routes +
  tab label → todo, `precis-tasks-help`/`precis-auto-tasks-help` skill ids →
  todo names + all cross-refs (handlers/todo.py hints), prose ("task line"→
  "title"). Ends "task" as a synonym. Conceptual split stays kinds-level:
  todo = organizer node (human or rotation picks it), job = machine
  execution unit.
- retire vs soft-delete: DECIDED (Reto 2026-08-30) — standardize on
  **retired_at / retire_***. Migration renames refs.deleted_at→retired_at +
  Store.soft_delete_ref→retire_ref (+ partial indexes); the 8 retired_at
  tables already conform. Heaviest migration in the plan — schedule
  deliberately.

#### Stage E build notes (recorded so the spec reads true)

- `precis-tasks-help` collided with the existing flat-CRUD
  `precis-todo-help` skill id — renamed to `precis-todo-tree-help` instead
  (the skill is specifically the hierarchical-tree layer on top of the
  flat surface, so the extra word is accurate, not just disambiguating).
- The `refs.deleted_at`→`retired_at` migration (0149) checked the baseline
  for partial-index names mentioning "deleted": neither
  `refs_alive_idx` nor `uq_alert_open_source_fingerprint` does (only their
  *predicates* mention `deleted_at`, which Postgres rewrites automatically
  on column rename) — so no index rename was needed after all, just the
  column.
- `store/_refs_ops.py::soft_delete_todo_subtree` /
  `store/_draft_ops.py::soft_delete_draft` renamed to `retire_todo_subtree`
  / `retire_draft` alongside `soft_delete_ref`→`retire_ref` (same
  "soft_delete_*" family the stage note calls out) — not spelled out by
  name in the original bullet but the same rename.
- `include_deleted=` kwargs (`fetch_refs_by_ids`, `resolve_handle_ref`, the
  gripe CLI's `--include-deleted`/`--only-deleted`) were left alone: they
  don't match either grep target (`deleted_at` / `soft_delete_*`), and a
  bare-"deleted" sweep is out of scope per the stage note's own caution
  about false-positive-prone generic English.
- `PRECIS_BACKFILL_CITATION_LENS`→`_PERSONA` was correctly **refused** by
  the E build: this var's "lens" is the citation-graph-recall backfill
  sense, not a reviewer persona — `_PERSONA` would misdescribe it.
  RESOLVED in the same session: renamed to `PRECIS_BACKFILL_CITATION_RECALL`
  instead, following Stage A's own citation_lens→citation_recall rename
  (code + test + config-variables doc; nothing in deploy/ referenced it).

### Stage F — dispatch Hub→Registry (PARKED)

After Stages A–E ship AND the in-flight sibling worktrees land (242-file diff
conflicts with every dirty tree). Class Hub→Registry, hub=→registry=,
self.hub→self.registry, module dispatch.py→registry.py (~356 import sites).

### Naming criterion — token efficiency (Reto 2026-08-30)

When a stage picks a NEW name, prefer a common 1-token English word,
unambiguous first (depth, route, minter, placement conform). Never rename
solely for token count unless measured as a top emitter: cost = tokens/occurrence
× occurrences/LLM-call × call volume — dominated by the runtime surface
(kind/view names, tag axes, MCP trailer + skill vocabulary), not dev code.
Optional measurement pass: tokenize the skills corpus + sampled MCP responses,
rank terms by aggregate token cost, shortlist the top emitters for renaming.

### Ordering constraints

- A ships first (in flight). B on the clean tree after A. C/D/E each need a
  deploy window (migrations auto-apply on redeploy). F last.
- Skills + glossary update in the SAME commit as each stage's rename.
- Prod DB writes only via migrations; dev-DB testing first (scripts/dev).

### Deploy protocol for persisted stages (Reto 2026-08-30)

**Hold all tasks before the migration; deploy the new code at the same time.**
Per stage C/D/E window:

1. **Quiesce** — pause the factory fleet-wide: stop job claims (workers/
   dispatch) on every host, let running leases drain, verify no
   `STATUS:running` jobs remain against the keys/columns being renamed.
2. **Migrate + deploy together** — redeploy-precis.yml already auto-applies
   pending migrations; deploy to ALL hosts in one play (watchers race the
   shared inbox — never a partial fleet), so no old binary ever reads the
   renamed schema.
3. **Resume** — restart workers; watch the nursery/worker logs for the first
   claim cycle before walking away.

Old-code-vs-new-schema must never overlap; read-both shims (wall_seconds
pattern) are for keys only and don't excuse skipping the quiesce for column
renames.

## Vocab compaction C+D+E — post-ship residuals (c6c386a3)

_Grouped 2026-09-26; was `vocab-compaction-residuals`._

Harvested at the /go of stages C+D+E (2026-08-30). Delete items as they ship.

### Mutation survivors in quest/* (advisory, unverified)

`scripts/mutate-diff` on the C+D+E squash reported 9 SURVIVED, all in the
quest fidelity-key read paths (catalyst_seed.py:214 `is not→is`,
compute.py:1863 `or→and`, figures.py:292 `or→and`, frontier.py:1163
`in→not in`, gaps.py:119 `and→or`, rulings.py:206 `or→and`, + 3 more in the
run log). Mostly boolop flips on `meta.get('fidelity_*') or default`
fallback chains — plausible real assertion gaps on the renamed keys, but
mutate-diff SURVIVED can be a context-attribution artifact: apply the
mutation + run the module's tests before believing any of them
(auto-memory `mutate_diff_false_survivor`). If real → add the missing
assertions to the quest tests.

### vocab-lint markdown blind spot

`tests/test_vocab_lint.py` retired-name/phrase scans cover `src/**/*.py`
only. The C+D+E reviewer caught stale `precis-tasks-help` /
`precis-dispatch-help` references in README.md and docs/glossary.md that
the lint could not see — fixed by hand that time. Extend the lint: scan
`README.md`, `docs/*.md`, and `src/precis/data/skills/**/*.md` for retired
SKILL IDS at minimum (skill ids are deterministic, no false-positive risk —
unlike banning bare English words in prose).

### Quiesce protocol observation (for the next column-rename stage, if any)

The drain flag stops CLAIMS but not heartbeats: melchior's old binary's
heartbeat hit `column r.deleted_at does not exist` at 19:22:44 UTC — 23s
after 0149 applied, ~2min before that host's bounce. One failed local-llm
advertise, self-healed on restart. If a future stage renames a column read
by the heartbeat path, either bounce workers before the migration play or
accept this bounded blip knowingly.

### Migration fixture tests (accepted gap, optional)

None of 0143–0149 has a seed-legacy-row → assert-post-shape test; accepted
at ship time (matches repo convention; all 7 files line-reviewed + probed
against live pg17 by the reviewer). If a future stage adds jsonb-rewrite
migrations of similar shape (0145/0146/0147 were the risky ones), write the
fixture harness then and cover these retroactively.

## Setting the env var does nothing

_Grouped 2026-09-26; was `enable-env-flags-are-dead-switches`, status draft._

`cli/worker.py::_should_register` ends in `return bool(spec.enable_env)`: a
`ServiceSpec` carrying an `enable_env` registers **unconditionally**, and the
variable's *value* is never read. For `hub_refine` and `chase_trigger` that
makes `PRECIS_TAPROOT_REFINE_ENABLED` / `PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`
completely inert — neither pass is in the deploy-time seed loop
(`deploy/roles/precis_worker/tasks/provision.yml` seeds only `llm_summarize`,
`classify`, `llm_reconcile`, `job_claude_docker`, `cast_audio`), so the flag
is not even mirrored into a `service_config` row. `hub_refine_enabled()` and
`chase_trigger_enabled()` had no callers outside `__all__` and their own tests.

The real switch is a `service_config` prio row, live in both directions:

```
precis service prio <host> chase_trigger 1
precis service prio <host> hub_refine    1
```

Both on and off take effect within one cycle — registration is unconditional
and `pass_gate` re-reads `service_config` through a 5 s TTL cache
(`workers/service_config.py`). There is no daemon kick and no on/off
asymmetry.

### The scope is exactly two flags — do not generalize it

Measured 2026-08-20. Three other flags look like this one and are **live**;
"fixing" them would be a regression:

| Flag | Status |
|---|---|
| `PRECIS_TAPROOT_CHASE_ENABLED` | **live** — in-pass, `workers/chase.py` |
| `PRECIS_INBOUND_CHASE_ENABLED` | **live** — in-pass, `inbound_chase.py::inbound_chase_enabled`, also gating the citer sidecar in `handlers/paper.py` |
| `PRECIS_AXES_ENABLED` | **live** — seeds the `axis:<id>` gate default, an explicit documented exception to the §L cutover (`cli/worker.py::_gate_default_on`) |

The distinction is structural: a pass that is its **own service** flips via
`service_config`; a **sub-feature of another pass** has no `ServiceSpec` to
flip, so it keeps a genuine in-pass env flag.

### The trap: `enable_env` is load-bearing despite being unread

Deleting `enable_env` from these two `ServiceSpec`s — the obvious "remove the
vestige" fix — would **unregister both passes**, since its mere presence is
what makes a pass with empty `default_profiles` register at all. The field is
badly named, not vestigial. `ServiceSpec`'s own field comment
(`workers/registry.py`) already documents this correctly; the drift was
entirely in the narrative docs around it.

### Done 2026-08-20

Corrected in the same pass: the `precis.taproot` package docstring (canonical
statement of the two-mechanism model), `docs/runbooks/taproot-chase-enablement.md`
(the plist warning and the on/off-asymmetry paragraph, both stale), both worker
docstrings, and `precis-finding-help` (seed-vs-live wording). The dead
`hub_refine_enabled()` / `chase_trigger_enabled()` functions and their four
tests are deleted — a dead switch that tests green is worse than no switch.

### Left open

Rename `enable_env` to something that says what it does (a registration
marker, not an enablement flag). Cheap and mechanical, but it touches ~20
`ServiceSpec` rows plus `enable_env_for()`, so it wants its own change rather
than riding a docs pass. Until then the field comment is the guard.
