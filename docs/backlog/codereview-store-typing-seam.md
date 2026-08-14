# codereview: `store: Any` epidemic — convert remaining call sites

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

REMAINING — the long tail, convertible incrementally (each batch its
own ship):

- ~180 `store: Any` parameter sites: `src/precis/utils` (11 files),
  `handlers` (11), `export` (6), `reading` (5), `taproot` (4),
  `precis_pathway` (4), `cli` (3), `backfill` (3), `pcb` (2) —
  same recipe, deleting dead defensive `getattr` fallbacks as they go
  (e.g. `precis_web/routes/refs.py::_row` null-guarding non-optional
  `datetime` fields; `diagram/doc_context.py`'s `getattr(store,
  "figure_owning_draft"/"get_ref"/"block_views", …)` variance shims).
- `upsert_stub_paper(set_by: str)` and friends take plain `str` for
  actor slugs that land in FK-checked columns elsewhere — audit which
  `set_by` params should be `ActorSlug` (wave 1 already widened the
  taproot write doors: `mint_hub`/`apply_placement`/`seed_claim_hub`/
  `apply_chunk`/`_file_review_todo`). Note: `dream`/`weave`/`orcid`
  are used as `set_by=` strings but are NOT seeded actors; they only
  work because those params never hit the `actors` FK — decide seed vs
  rename before typing them.

Related: [codereview-store-decomposition] (the facade work will keep
these protocols as its public role surface).
