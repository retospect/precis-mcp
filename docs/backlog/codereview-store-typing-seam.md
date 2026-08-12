# codereview: `store: Any` epidemic — convert remaining call sites

Seam is typed (shipped): `Hub.store: Store | None` + `Hub.live_store`
narrowing property; central `precis/store/protocols.py` (role
protocols); the four ad-hoc `_StoreLike`/`_StoreProto` Protocols
consolidated/deleted; the two MRO-luck runtime stubs in
`store/_refs_ops.py` / `_cache_ops.py` retired to `TYPE_CHECKING`;
`ActorSlug` widened to include the seeded `chase` actor.

REMAINING — the long tail, convertible incrementally (each batch its
own ship):

- ~600 `store: Any` parameter sites across handlers/workers/web —
  convert to `Store` or the narrowest role Protocol in
  `store/protocols.py` (grow it as needed), deleting dead defensive
  `getattr` fallbacks as they go (e.g. `precis_web/routes/refs.py::_row`
  null-guarding non-optional `datetime` fields;
  `diagram/doc_context.py`'s `getattr(store, "figure_owning_draft"/
  "get_ref"/"block_views", …)` variance shims).
- `upsert_stub_paper(set_by: str)` and friends take plain `str` for
  actor slugs that land in FK-checked columns elsewhere — audit which
  `set_by` params should be `ActorSlug` (note: `dream`/`weave`/`orcid`
  are used as `set_by=` strings but are NOT seeded actors; they only
  work because those params never hit the `actors` FK — decide seed vs
  rename before typing them).
- `refeye._Chunk` Protocol declares mutable attributes, so frozen
  dataclasses (`DraftChunk`) can't satisfy it — convert to `@property`
  members (same fix as `utils/wordcount.py::_ChunkLike`, shipped) and
  then drop the `Any`-typed locals in `tests/test_refeye.py`.

Related: [codereview-store-decomposition] (the facade work will keep
these protocols as its public role surface).
