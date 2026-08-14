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
