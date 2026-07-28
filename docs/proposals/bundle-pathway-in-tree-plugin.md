---
status: draft
title: Bundle the pathway plugin in-tree (like precis_chem/bio); make autocatpath precis-free
model: opus
---

# Bundle the pathway plugin in-tree (like precis_chem/bio); make autocatpath precis-free

> Supersedes the withdrawn `precis-interface-slim-sdk` proposal. That one kept
> the glue in the autocatpath repo behind a thin contract package; this one
> deletes the cross-repo seam entirely by moving the glue into precis-mcp,
> matching how `precis_chem` and `precis_bio` already work.

## Motivation / why

`autocatpath` is the **odd one out** among precis's three science plugins. Its
reaction-pathway glue (handler, job, views, migration, skills) lives in the
*autocatpath* repo and depends on `precis-mcp`; precis-mcp discovers it via
entry-points from a separately-installed package. The two siblings invert that:

| Plugin | Glue lives in | Dep direction | Cross-repo pin? |
|---|---|---|---|
| `precis_chem` (route/retrosynth) | precis-mcp `src/precis_chem/` | precis → science | none |
| `precis_bio` (protein/fold) | precis-mcp `src/precis_bio/` | precis → science | none |
| `autocatpath` (pathway) | autocatpath repo `src/autocatpath/precis/` | science → precis | **yes** |

That external seam is the source of concrete pain: the unresolvable
`precis-mcp>=8.21,<9` pin + the `[tool.uv] override-dependencies` hack in
`autocatpath/pyproject.toml` (there *only* to keep the repo lockable), a
cross-repo version dance every time precis's plugin ABI moves (the pin comment
literally reads "needs `KindSpec.can_own_jobs`"), and a bespoke cluster install
path (`roles/autocatpath` git-installs `autocatpath[precis]` into every precis
venv, gated on `PRECIS_AUTOCATPATH_ENABLED`). catpath is external for
*historical* reasons (standalone tool first, bridged later), not architectural
ones — the same author owns both repos, so "plugin ownership" is not a
consideration.

Moving the glue in-tree makes ABI changes atomic inside precis-mcp, deletes the
pin/override hack, and leaves `autocatpath` as what it actually is: a pure
`numpy`/`scipy`/`ase`/`rdkit` reaction-pathway science library with **zero
precis dependency**.

## In scope

1. **Move the glue into precis-mcp** as `src/precis_pathway/` (mirrors
   `src/precis_chem/`): the `pathway` handler, the `autocatpath_explore` job
   type, the toon/text views, persist, ingest bridge, and the
   `precis-pathway-help` skill. This code imports the **pure** autocatpath
   engine (`autocatpath.structures`, `.neb`, `.network`, `.uncertainty`,
   `.provenance`, …) and precis's own types directly (same tree — no seam).
2. **Move the kind migration into precis-mcp's forward chain.** The
   `pathway`/`pathway_body` kinds become precis-core-owned schema: a new sealed
   `src/precis/migrations/00NN_pathway_kind.sql` (forward-only, ADR 0005),
   replacing the plugin-namespace migration. `pathway` stays the kind slug.
3. **Declare autocatpath as a precis-mcp dependency via extras:**
   - `precis-mcp[catalyst]` → `autocatpath` (pure) — the **kind surface**
     (gateway: handler + views + migration, no ML backend).
   - `precis-mcp[catalyst-gpu]` → `autocatpath[mace]` — the **compute**
     (spark: in-process NEB/relax on torch/MACE).
4. **Make autocatpath precis-free.** Delete `src/autocatpath/precis/`, the
   `precis` optional-dependency, all four `precis.*` entry-points, the
   `>=8.21` pin, and the `[tool.uv] override-dependencies`/`conflicts`-for-
   precis wiring from `autocatpath/pyproject.toml`. The engine, CLI, and tests
   that don't touch the bridge stay exactly as they are.
5. **Fold the cluster install.** `roles/autocatpath` collapses into installing
   the right precis-mcp extra on each node (`catalyst` on the gateway,
   `catalyst-gpu` on the compute node) through the existing `precis_worker`/
   `precis_web` extras mechanism, gated by the existing capability topology
   (`autocatpath`/`autocatpath_mace`/`autocatpath_plugin`). `PRECIS_AUTOCATPATH_*`
   env semantics are preserved (enable flag, route node, wall seconds) but now
   read by in-tree code.

## Explicitly NOT in scope

- **No change to the pathway science.** The engine (structures/NEB/network/
  uncertainty) is untouched; only the precis *glue* relocates.
- **No change to the `pathway` kind's public shape** — same slug, same views
  (`network`/`profile`/…), same `autocatpath_explore` job semantics. This is a
  code-home + dependency-direction move, not a redesign.
- **Compute is NOT being containerized.** Unlike chem/bio (whose heavy compute
  is a container image), pathway compute stays **in-process Python** on the GPU
  node. B re-homes the glue and the *declaration* of the heavy dep; it does not
  eliminate the need for `autocatpath[mace]` in the compute venv (see wrinkle 1).
- **Not touching precis_chem/precis_bio.** They are the template, not the target.
- **Not deleting the `autocatpath` PyPI package or its `catpath` compat alias.**
  autocatpath remains an independently published, standalone science tool.

## Acceptance criteria

- `pip install autocatpath` resolves with **no `precis*` in the tree** (asserted
  in autocatpath CI); `grep -ri precis src/autocatpath` returns nothing but
  incidental prose. The `>=8.21` pin and `[tool.uv]` precis override are gone.
- In a precis-mcp venv with **neither** catalyst extra, the server boots and the
  `pathway` kind is simply **absent** (dark), no ImportError — `precis_pathway`
  InitErrors cleanly when `autocatpath` isn't importable (same fail-dark
  contract as today's `PRECIS_AUTOCATPATH_ENABLED`).
- `precis-mcp[catalyst]` venv: `get(kind='pathway')` serves the kind surface;
  `precis-mcp[catalyst-gpu]` venv on the compute node runs an
  `autocatpath_explore` job end-to-end (EMT smoke path, no GPU needed for the
  test).
- The `pathway`/`pathway_body` kinds are created by the **precis-mcp forward
  migration** on a fresh DB (baseline snapshot regenerated via `scripts/bump`);
  the old plugin-namespace migration is retired. Idempotent against a prod DB
  that already has the kinds (`ON CONFLICT DO NOTHING`).
- Full precis-mcp `scripts/test` gate green, including a new
  `tests/test_pathway_plugin.py` covering the in-tree handler/job (moved +
  adapted from autocatpath's `test_precis_bridge.py`).
- `precis-mcp[all]` still resolves — the catalyst extras' conflicting ML
  backends are quarantined out of `[all]` (wrinkle 2).

## Target + blast radius

- **precis-mcp (new):** `src/precis_pathway/` (handler/job/views/persist/ingest/
  skill), one forward migration under `src/precis/migrations/`,
  `pyproject.toml` `[catalyst]`/`[catalyst-gpu]` extras + entry-point (or direct
  bundled registration, matching precis_chem), `tests/test_pathway_plugin.py`.
- **precis-mcp (edit):** `deploy/roles/autocatpath` shrinks to an extras-install
  shim (or folds into `precis_worker`/`precis_web`); `state-map.md` + `codebase.md`
  + the topology comments; ADR (this graduates to one — the dep-direction
  decision is durable).
- **autocatpath (edit):** delete `src/autocatpath/precis/` + `tests/test_precis_bridge.py`
  + `tests/test_precis_runner_slab.py`; strip the `precis` extra, entry-points,
  pin, and `[tool.uv]` precis wiring from `pyproject.toml`; regenerate lock.
- **Deploy:** a coordinated cluster change (comparable to the rename deploy) —
  precis-mcp ships the bundled plugin + migration, cluster venvs install the new
  extras, gateway + compute daemons bounce. The just-shipped entry-point plumbing
  (`autocatpath[precis]` git install, the four entry-points) is retired here.

## Open questions / decisions log

- **Wrinkle 1 — compute doesn't vanish, it re-homes.** chem/bio bundle fully
  because their compute is containerized; pathway compute is in-process ase+mace.
  So `precis-mcp[catalyst-gpu]` must still pull `autocatpath[mace]` into the
  compute venv. Confirm the extras split (`catalyst` vs `catalyst-gpu`) is the
  right seam vs. a single `catalyst` extra with the backend chosen per node.
- **Wrinkle 2 — conflicting ML backends leak into precis-mcp.** autocatpath's
  `mace`/`chgnet`/`fairchem`/`grace` extras are mutually exclusive (its
  `[tool.uv] conflicts`). Depending on `autocatpath[mace]` surfaces that in
  precis-mcp's resolver. Decision: keep `catalyst-gpu` OUT of `[all]` and out of
  the universal lock's default fork; does precis-mcp's `uv` config need its own
  `conflicts`/override mirror, or does gating the extra suffice?
- **Registration mechanism:** in-tree entry-point (like the `route`/`protein`
  handlers, which still use `precis.handlers` entry-points from precis-mcp's own
  dist) vs. direct registration in the dispatch built-in list. Match precis_chem
  exactly — confirm which it uses.
- **Migration cutover:** a fresh `00NN_pathway_kind.sql` in the sealed chain +
  baseline bump. The retired plugin-namespace `autocatpath` migration leaves
  historical rows in prod `schema_migrations` — harmless, but note it.
- **`autocatpath` version floor in precis-mcp:** how does `[catalyst]` pin
  autocatpath (floor vs compatible-release), and what's the release cadence now
  that the glue no longer forces an autocatpath bump?
- **Sequencing vs. the shipped rename:** this proposal assumes the
  catpath→autocatpath rename has landed (it has). It then *reverses* much of that
  rename's deployment plumbing — verify no half-state window where neither the
  entry-point plugin nor the bundled plugin serves `pathway`.
