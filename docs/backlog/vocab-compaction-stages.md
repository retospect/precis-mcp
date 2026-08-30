# Vocabulary compaction — staged full-consistency migration

Decision (Reto, 2026-08-30): one word = one meaning, enforced at EVERY level —
files, classes, functions, vars, DB columns, tag axes, meta keys, MCP kwargs,
CLI commands, skill ids. Persisted names migrate (forward-only migrations +
backfills); no legacy names retained once a stage lands. Glossary keeps a
`(legacy: X)` note only while the old name is still encounterable, then drops
it. Stages ship independently, each internally consistent.

Evidence base: the two survey matrices (homonym + synonym, 2026-08-30 session);
sizes/cost classes verified there.

## Stage A — code-internal cheap renames (LANDED — enforced by tests/test_vocab_lint.py)

Tier→SourceGrade · nursery Finding→Symptom · validate Finding→ValidationIssue ·
reaper Candidate→DeadHold · PassBand→PassPriority · router dispatch()→route() ·
KindSpec.role→placement · reading/cards.py→flashcards.py · review lens→persona ·
citation_lens→citation_recall · cast Source→CitedRef · lane kwargs→sim_kind/
cadence · addr→handle · gloss→summary · excerpt consolidation · export cite
verbs→render_ · block→chunk surface lies (types.py docstring, tools/core.py,
4 skills) · wall_seconds nested unification (read-both shim) · local hub→claim_hub.

## Stage B — store.blocks→chunks full facade rename (LANDED)

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

## Stage C — persisted low-blast migrations (one ship each or bundled)

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

## Stage D — surface renames (agents/CLI/skills re-learn from shipped docs)

- Worker `dispatch` → `minter`: registry name, `precis worker --only minter`,
  service_config row UPDATE migration, skill `precis-dispatch-help`→
  `precis-minter-help` + all cross-refs. Historical `ref_events.source=
  'dispatch'` rows: DECIDED (Reto 2026-08-30) — rewrite history too
  (migration UPDATE → 'minter'); full consistency wins over provenance.
- CLI `precis watch` → `precis ingest --watch` (+ deploy launchd plist).
  `WATCH:` tag axis and recurring-todo watches KEEP the word (they are the
  canonical senses); patent_watch is semantically a watch — keeps it.
- MCP `source=` kwarg split: search(kind='patent', source=…) vs
  put(source='paper:<slug>') are unrelated. DECIDE the winner/renames.
- Review persona externals (Stage A renamed code only): web HTTP `"lens"`
  JSON field + CLI `--lenses` flag → persona vocabulary.
- Write-ack string `"<verb> block <N> '<slug>' …"` (`utils/file_id.py::
  format_write_result`, pinned by test_files_write.py) still says "block" to
  agents → "chunk", together with its `block_pos=`/`block_slug=` kwargs and
  the wider agent-facing `pos`/`block_pos=` convention (`add_link`,
  `LinkTarget.pos`, …) — one coordinated surface rename, agents re-learn.

## Stage E — task→todo + retire/soft-delete unification (largest)

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

## Stage F — dispatch Hub→Registry (PARKED)

After Stages A–E ship AND the in-flight sibling worktrees land (242-file diff
conflicts with every dirty tree). Class Hub→Registry, hub=→registry=,
self.hub→self.registry, module dispatch.py→registry.py (~356 import sites).

## Naming criterion — token efficiency (Reto 2026-08-30)

When a stage picks a NEW name, prefer a common 1-token English word,
unambiguous first (depth, route, minter, placement conform). Never rename
solely for token count unless measured as a top emitter: cost = tokens/occurrence
× occurrences/LLM-call × call volume — dominated by the runtime surface
(kind/view names, tag axes, MCP trailer + skill vocabulary), not dev code.
Optional measurement pass: tokenize the skills corpus + sampled MCP responses,
rank terms by aggregate token cost, shortlist the top emitters for renaming.

## Ordering constraints

- A ships first (in flight). B on the clean tree after A. C/D/E each need a
  deploy window (migrations auto-apply on redeploy). F last.
- Skills + glossary update in the SAME commit as each stage's rename.
- Prod DB writes only via migrations; dev-DB testing first (scripts/dev).

## Deploy protocol for persisted stages (Reto 2026-08-30)

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
