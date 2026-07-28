# 0069 — Pathway plugin bundled in-tree; autocatpath made precis-free

- **Status**: accepted (2026-07-28) · **built + verified**, deploy pending the
  coordinated cutover. Graduated from
  [`docs/proposals/bundle-pathway-in-tree-plugin.md`](../proposals/bundle-pathway-in-tree-plugin.md)
  (which passed an ADR-0048 `ready` review). Packaging mechanics of record:
  [`docs/design/autocatpath-integration.md`](../design/autocatpath-integration.md) §3.7.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0056 — chemistry & protein tool-packs as in-tree plugins](./0056-chemistry-tool-packs-plugin-route-kind.md)
    — establishes the bundled-plugin pattern (`src/precis_chem`, `src/precis_bio`:
    glue in precis-mcp's tree, discovered via `precis.{handlers,job_types,migrations}`
    entry-points in precis-mcp's own `pyproject.toml`). This ADR applies that
    pattern to the one plugin that skipped it.
  - [ADR 0044 — the derived-job lane](./0044-derived-job-lane.md) — the
    `autocatpath_explore` compute job is unchanged; only its code home moves.

## Context

The `pathway` kind (catalyst reaction-network exploration) was the **odd one
out** among precis's three science plugins. Its glue (handler, job, views,
persist, migration, skill) lived in the **external `autocatpath` repo** at
`src/autocatpath/precis/` and depended on `precis-mcp`; precis-mcp discovered it
via entry-points from a separately git-installed `autocatpath[precis]`. The two
siblings invert that — the glue is in precis-mcp's tree and imports the science
engine; precis depends on the tool, not the reverse.

That external seam cost: an **unresolvable** `precis-mcp>=8.21,<9` pin plus a
`[tool.uv] override-dependencies` hack in `autocatpath/pyproject.toml` (present
only to keep the repo lockable); a cross-repo version dance on every precis
plugin-ABI change (`>=8.21` was literally "needs `KindSpec.can_own_jobs`"); and a
bespoke cluster install (`roles/autocatpath` git-installing `autocatpath[precis]`
into every precis venv). catpath was external for **historical** reasons
(standalone tool first, bridged later), not architectural ones — one author owns
both repos, so "plugin ownership" was never a real consideration.

## Decision

**Bundle the pathway glue in-tree as `src/precis_pathway/` (mirroring
`src/precis_bio`), and make `autocatpath` precis-free.** Concretely:

1. The glue lives at `src/precis_pathway/` and imports the **pure** `autocatpath`
   engine (numpy/scipy/ase/rdkit) directly. It registers through precis-mcp's own
   `precis.{handlers,job_types,migrations}` entry-points. The plugin migration
   ships from `src/precis_pathway/migrations/` under precis-mcp's `precis.migrations`
   entry-point — a **plugin-namespace** migration (ledger key `autocatpath`,
   preserved), **not** the sealed core chain, **no** baseline bump. (This
   correction came out of the `ready` review, which caught that "bundle the code"
   ≠ "own the schema in core"; bio/chem keep plugin-namespace migrations too.)
2. `autocatpath` becomes a precis-mcp dependency via two extras, both kept out of
   `[all]`: `precis-mcp[catalyst]` → pure `autocatpath` (gateway kind surface),
   `precis-mcp[catalyst-gpu]` → `autocatpath[mace]` (in-process NEB/relax on the
   GPU node — `precis_bio`'s in-process-compute shape, not `precis_chem`'s
   container).
3. `autocatpath` drops `src/autocatpath/precis/`, its four `precis.*`
   entry-points, the `>=8.21` pin, and the `[tool.uv]` precis override — a
   **breaking** removal of its `.precis` surface, so it bumps **0.3.0 → 0.4.0**.
   The catalyst extras pin `autocatpath>=0.4` (the old 0.3.0 still carries the
   bridge → would double-register `pathway`).
4. `roles/autocatpath` shrinks: it installs the `precis-mcp[catalyst*]` extra
   into the worker venv instead of git-installing autocatpath, keeping the env
   drop-in + MACE pre-seed. The `pathway`/`pathway_body` kinds, the
   `autocatpath_explore` job, and `PRECIS_AUTOCATPATH_*` env semantics are
   **unchanged**.

## Consequences

- **Positive**: plugin-ABI changes are now atomic inside precis-mcp (no cross-repo
  pin/lock dance); `autocatpath` is a clean, standalone numpy/ase/rdkit library
  with **zero** precis dependency; the plugin is consistent with its two siblings.
- **Cost re-homed, not removed**: pathway compute is in-process ase+mace, so the
  GPU node still installs `autocatpath[mace]` (via `[catalyst-gpu]`). What went
  away is the git-install + entry-point discovery + version pin.
- **Packaging watch (wrinkle 2)**: `autocatpath`'s mace/chgnet/fairchem/grace
  extras are mutually exclusive, and precis-mcp already declares a dormant
  `dft-ml` torch extra aimed at the same GPU node — so `catalyst-gpu` stays out
  of `[all]` and the default lock fork; a shared torch pin is an open follow-up.
- **Verification / CI**: `tests/test_pathway_plugin.py` (ported from
  autocatpath's `test_precis_bridge`) passes **18/18** against the pure engine
  (verified by mounting it into the dev container). It carries
  `importorskip("autocatpath")`, so it **skips** in the current baked dev image
  (which installs `precis-mcp[all]`, and `catalyst` is out of `[all]`) until that
  image bakes `autocatpath` — a documented, non-silent gap, not silent coverage.
- **Latent bug fixed in passing**: the port's first cross-env run surfaced that a
  non-finite EMT barrier (`NaN`) was persisted straight into JSONB → Postgres
  rejects it, crashing the in-process EMT slice-0 write path.
  `precis_pathway.persist` now coerces non-finite floats to JSON `null`, and the
  graph-math/renderers treat that null as "no value" (`_num`/`_nf`).
- **Deploy is a coordinated cutover** (see the proposal's Cutover section):
  release pure `autocatpath` 0.4.0 to PyPI **first**, then ship + deploy
  precis-mcp; the per-venv upgrade drops the old `.precis` entry-points as the
  bundled ones arrive, so no host ever double- or un-registers `pathway`.
