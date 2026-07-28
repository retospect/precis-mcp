---
status: draft
title: Bundle the pathway plugin in-tree (like precis_bio); make autocatpath precis-free
model: opus
---

# Bundle the pathway plugin in-tree (like precis_bio); make autocatpath precis-free

> Supersedes the withdrawn `precis-interface-slim-sdk` proposal. That one kept
> the glue in the autocatpath repo behind a thin contract package; this one
> deletes the cross-repo seam entirely by moving the glue into precis-mcp,
> matching how `precis_bio` and `precis_chem` already work.
>
> **Revised after ADR-0048 readiness review** — the review caught that
> "bundle the code in-tree" was wrongly conflated with "own the schema in
> precis's sealed core migration chain." Corrected below: the bundled plugin
> keeps a **plugin-namespace migration**, exactly like the siblings. That
> single correction also collapses the review's split signal — the proposal is
> now one consistent deliverable.

## Motivation / why

`autocatpath` is the **odd one out** among precis's three science plugins. Its
reaction-pathway glue (handler, job, views, migration, skill) lives in the
*autocatpath* repo and depends on `precis-mcp`; precis-mcp discovers it via
entry-points from a separately git-installed package. The two siblings invert
that — the glue lives inside precis-mcp and imports the science engine:

| Plugin | Glue lives in | Heavy compute | Cross-repo pin? |
|---|---|---|---|
| `precis_bio` (protein/fold) | precis-mcp `src/precis_bio/` | **in-process** (AlphaFold, ansible role) | none |
| `precis_chem` (route/retrosynth) | precis-mcp `src/precis_chem/` | container (aizynth podman) | none |
| `autocatpath` (pathway) | autocatpath repo `src/autocatpath/precis/` | in-process (ase+mace) | **yes** |

**`precis_bio` is the exact precedent**: a bundled plugin whose heavy compute
(AlphaFold) runs *in-process* on a GPU node via an ansible role — the same
shape as pathway's ase+mace, not chem's container. So bundling pathway is not
novel; it's applying the bio pattern to the one plugin that skipped it.

The external seam is the source of concrete pain: the unresolvable
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
   `src/precis_bio/`): the `pathway` handler, the `autocatpath_explore` job
   type, the toon/text views, persist, ingest bridge, and the
   `precis-pathway-help` skill. This code imports the **pure** autocatpath
   engine (`autocatpath.structures`, `.neb`, `.network`, `.uncertainty`,
   `.provenance`, …) and precis's own types directly (same tree — no seam).
2. **Register via precis-mcp's own entry-points**, exactly like `precis_bio`:
   add `pathway`/`autocatpath_explore`/`precis-pathway-help` and the migration
   namespace to precis-mcp's `pyproject.toml` `[project.entry-points."precis.*"]`
   tables. `src/precis/dispatch.py::_load_plugins` (`PLUGIN_GROUP =
   "precis.handlers"`) discovers them from precis-mcp's own dist — there is no
   separate built-in-list path for plugin-style kinds.
3. **Keep the kind migration as a plugin-namespace migration**, now shipped
   from precis-mcp's tree: `src/precis_pathway/migrations/0001_pathway_kind.sql`,
   discovered via precis-mcp's `[project.entry-points."precis.migrations"]` and
   applied by `Migrator.discover_sources` into the `_migrations` ledger's
   `plugin` column — **not** the sealed `src/precis/migrations/` core chain, and
   **no** `scripts/bump` baseline regeneration (baseline bakes only
   `PRECIS_PLUGIN_NAME` core migrations). Keep the ledger namespace key
   `autocatpath` (the value already applied in prod) so the migration is a
   no-op on the live DB, not a re-apply under a new namespace. `pathway`/
   `pathway_body` stay the slugs.
4. **Declare autocatpath as a precis-mcp dependency via extras:**
   - `precis-mcp[catalyst]` → `autocatpath` (pure) — the **kind surface**
     (gateway: handler + views + migration, no ML backend).
   - `precis-mcp[catalyst-gpu]` → `autocatpath[mace]` — the **compute**
     (spark: in-process NEB/relax on torch/MACE).
5. **Make autocatpath precis-free.** Delete `src/autocatpath/precis/`, the
   `precis` optional-dependency, all four `precis.*` entry-points, the
   `>=8.21` pin, and the `[tool.uv] override-dependencies` precis wiring from
   `autocatpath/pyproject.toml`. The engine, CLI, and tests that don't touch the
   bridge stay exactly as they are.
6. **Fold the cluster install.** `roles/autocatpath` collapses into installing
   the right precis-mcp extra on each node (`catalyst` on the gateway,
   `catalyst-gpu` on the compute node) through the existing `precis_worker`/
   `precis_web` extras mechanism, gated by the existing capability topology
   (`autocatpath`/`autocatpath_mace`/`autocatpath_plugin`). `PRECIS_AUTOCATPATH_*`
   env semantics are preserved (enable flag, route node, wall seconds) but now
   read by in-tree code.

## Explicitly NOT in scope

- **The `pathway` kind does NOT become core-owned schema.** It stays a
  plugin-namespace migration (see in-scope 3). "Should any plugin kind graduate
  to core schema?" is a separate ADR-worthy question, deliberately out of scope.
- **No change to the pathway science.** The engine (structures/NEB/network/
  uncertainty) is untouched; only the precis *glue* relocates.
- **No change to the `pathway` kind's public shape** — same slug, same views
  (`network`/`profile`/…), same `autocatpath_explore` job semantics. Code-home +
  dependency-direction move, not a redesign.
- **Compute is NOT being containerized.** It stays in-process Python on the GPU
  node (bio's shape). The heavy dep is re-declared, not eliminated (wrinkle 1).
- **Not touching precis_chem/precis_bio.** They are the template, not the target.
- **Not deleting the `autocatpath` PyPI package or its `catpath` compat alias.**
  autocatpath remains an independently published, standalone science tool.

## Cutover sequence (resolves the "no half-state window" risk)

The failure mode to avoid is the one hit during the rename deploy: a venv
holding **both** the old external `autocatpath[precis]` entry-points **and** the
new bundled `precis_pathway` entry-points → duplicate `pathway` handler, winner
non-deterministic per host. Ordered cutover:

1. **Release pure autocatpath first** (in-scope 5) to `retospect/catpath` main /
   PyPI — the new version ships **without** the `autocatpath.precis` subpackage,
   so it registers **no** `precis.*` entry-points.
2. **Ship precis-mcp** with `src/precis_pathway/` + the `[catalyst]`/
   `[catalyst-gpu]` extras pinning that pure autocatpath version (floor pin).
3. **Quiesce pathway compute before deploy** — confirm no in-flight
   `autocatpath_explore` job (the open `ssh_node` redeploy-survival bug kills
   in-flight jobs on bounce; `OPEN-ITEMS.md`). This was a no-op during the
   rename deploy and is expected to be again while quest 164903 is blocked, but
   it is a required pre-check, not an assumption.
4. **Deploy**: each precis venv's `[catalyst*]` extra install **upgrades**
   autocatpath to the pure version — pip removes the old `.precis` files and its
   entry-points in the same step — and precis-mcp now supplies the bundled
   entry-points. Ansible installs across all venvs, *then* bounces daemons, so
   each daemon starts exactly once post-upgrade with exactly one `pathway`
   handler. No per-daemon double/none window.
5. **Verify** single-registration on gateway + compute (the check that caught
   the rename-deploy duplicate): `entry_points(group='precis.handlers')` yields
   one `pathway`, resolving into `precis_pathway`.

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
  `autocatpath_explore` job end-to-end (EMT smoke path, no GPU needed).
- The `pathway`/`pathway_body` kinds are created by the **plugin-namespace
  migration shipped from `src/precis_pathway/migrations/`**, discovered via
  precis-mcp's `precis.migrations` entry-point and recorded in `_migrations`
  under the `autocatpath` plugin namespace. Idempotent against the prod DB that
  already has the kinds (`ON CONFLICT DO NOTHING`); **no** baseline regeneration,
  **not** in the sealed core chain.
- Post-deploy, both gateway and compute venvs show exactly one `pathway`
  entry-point resolving into `precis_pathway` (cutover step 5).
- Full precis-mcp `scripts/test` gate green, including a new
  `tests/test_pathway_plugin.py` covering the in-tree handler/job (moved +
  adapted from autocatpath's `test_precis_bridge.py`).
- `precis-mcp[all]` still resolves — `catalyst-gpu` is quarantined out of
  `[all]` and the universal-lock default fork (wrinkle 2).

## Target + blast radius

- **precis-mcp (new):** `src/precis_pathway/` (handler/job/views/persist/ingest/
  skill/`migrations/0001_pathway_kind.sql`), `pyproject.toml` `[catalyst]`/
  `[catalyst-gpu]` extras + `precis.{handlers,job_types,skills,migrations}`
  entry-points, `tests/test_pathway_plugin.py`.
- **precis-mcp (edit):** `deploy/roles/autocatpath` shrinks to an extras-install
  shim (or folds into `precis_worker`/`precis_web`); `state-map.md` +
  `codebase.md` + topology comments; **`docs/design/autocatpath-integration.md`**
  (the design-of-record for the deleted cross-repo mechanism — lines ~529-603
  — must be rewritten to the bundled model, else it goes actively wrong);
  graduates to an ADR (the dep-direction decision is durable). Cross-reference
  `docs/proposals/catalyst-physical-realism.md` (same subsystem, no scope
  conflict — this preserves the `pathway` public shape it builds on).
- **autocatpath (edit):** delete `src/autocatpath/precis/` +
  `tests/test_precis_bridge.py` + `tests/test_precis_runner_slab.py`; strip the
  `precis` extra, entry-points, pin, and `[tool.uv]` precis wiring from
  `pyproject.toml`; regenerate lock.
- **Deploy:** the coordinated cluster change of the Cutover section; the
  just-shipped entry-point plumbing (`autocatpath[precis]` git install, the four
  entry-points, `roles/autocatpath`'s git-install task) is retired here.

## Open questions / decisions log

**Decisions resolved from the ADR-0048 readiness review:**
- **Migration home** → plugin-namespace migration in `src/precis_pathway/
  migrations/`, ledger namespace `autocatpath` kept stable; NOT the sealed core
  chain, NO baseline bump. (Was the item-2 blocker; corrected in-scope 3.)
- **Registration mechanism** → precis-mcp-owned `precis.handlers` (etc.)
  entry-points, like `precis_bio`; no built-in-list path exists to weigh.
- **Cutover order** → specified in the Cutover section (was the open
  blocker-severity sequencing question).
- **Closest precedent** → `precis_bio` (in-process GPU compute), not chem
  (containerized); wrinkle-1 contrast corrected.

**Genuinely open (non-blocking):**
- **Wrinkle 1 — compute re-homes, doesn't vanish.** `precis-mcp[catalyst-gpu]`
  still pulls `autocatpath[mace]` into the compute venv. Confirm the two-extra
  split (`catalyst` vs `catalyst-gpu`) is the right seam vs. one `catalyst`
  extra with the backend chosen per node. (bio's role-based install is the
  reference.)
- **Wrinkle 2 — conflicting ML backends + precis-mcp's own `dft-ml` extra.**
  autocatpath's `mace`/`chgnet`/`fairchem`/`grace` extras are mutually exclusive
  (its `[tool.uv] conflicts`); depending on `autocatpath[mace]` surfaces that in
  precis-mcp's resolver. Additionally, precis-mcp *already* declares a dormant
  torch/MACE extra (`dft-ml = ["ase>=3.22","mace-torch>=0.3"]`) aimed at the
  **same GPU node** (`topology.example.yml`: `dft` and `autocatpath*` both on
  the GPU box). `dft-ml` isn't role-wired yet, so no active break — but if both
  land in one venv their independently-pinned torch stacks could collide.
  Decision: keep `catalyst-gpu` out of `[all]`/the default lock fork; open
  whether precis-mcp needs its own `conflicts` mirror or a shared torch pin with
  `dft-ml`.
- **`autocatpath` version floor in precis-mcp `[catalyst]`** — floor vs
  compatible-release, and the release cadence now that the glue no longer forces
  an autocatpath bump. Ship with the repo's existing floor-pin convention;
  revisit later.
