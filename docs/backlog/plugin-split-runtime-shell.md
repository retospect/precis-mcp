---
status: draft
title: plugin split, top-down — precis-util runtime shell + precis-geom build layer + precis-xxx models, for agent use over MCP
prio: normal
model: opus
snooze-until: 2026-10-16
pillar: platform
---

# Plugin split, top-down: `precis-util` + `precis-geom` + `precis-xxx` models

Design sessions 2026-09-27 (Reto + agents, `curried-pondering-starlight-39`
handing off to `iridescent-watching-reef`). It **supersedes**
`package-split.md` (Reto 2026-09-16, the same lineage approached bottom-up
as kernel extraction): Reto ruled 2026-10-01 to delete that item, and its
prep decisions not covered here were lifted into §In scope, steps 6–8.
Its 30-day no-changes rule still governs: `snooze-until` above is the
earliest day a file moves, and until then only behaviour-neutral prep is
fair game.

## Motivation / why

Reto, 2026-09-27: the goal is **agent use over MCP**. `pip install
precis-util precis-hexfold` should yield a working MCP server whose tool
surface an agent already understands. Secondary and falling out of it: a
model can live in a private repo until its paper, then publish as a
citable standalone — *"a standalone thing with the paper, and also
integrated."*

Reto, 2026-10-01, restating the target: release a **minimal package**
publicly, *and* have the same code fully useful inside precis — hold it
back until later. He also hopes the modularization shrinks the deploy jam
("deploy should not take hours for simple changes"), while keeping
everything in one repo. That second hope is only partly supported; see
§Explicitly NOT in scope, *Gate and deploy speed*.

Two properties already in the tree make this cheap, and they are the reason
this is worth doing rather than merely tidy:

* **The tool surface is constant.** Precis dispatches 7 verbs with a
  `kind=` discriminator, not a tool per capability. Installing a model adds
  `kind` values, never tools. The usual MCP plugin failure — four plugins
  and the agent's tool budget is gone — cannot happen here.
* **Skills already plug in.** `handlers/skill.py:2696` defines
  `SKILL_PLUGIN_GROUP = "precis.skills"`; `_plugin_skill_roots()` resolves
  it, built-ins win slug collisions, a broken plugin cannot brick the
  surface, and `tests/test_skill_plugin_group.py` covers it. Nothing
  declares it yet — in one wheel there has never been a reason. The
  mechanism is **built and idle**, not missing.

So a model contributes kinds (`precis.handlers`) *and* its affordance
documentation (`precis.skills`) into a fixed 7-tool surface the agent
already knows, and `get(kind='skill', id='toc')` picks it up.

**Simplification is NOT a goal.** A pluggable runtime is more total
machinery than a monolith — version skew across models, orphan tables with
no down-migrations, several release cadences. It buys modularity and
agent-composability. Saying otherwise sets up extraction three to feel like
failure when it is just the cost.

## End state

```
precis-util    runtime shell — 7 verbs · Hub · Store · JobHandler ·
               job_types · entry-point discovery (handlers · job_types ·
               migrations · handle_codes · skills) · serve + ~6 CLI
               commands (serve migrate db repl settings secret) · ~2 kinds
precis-geom    cad / structure / design / blocktree  + se  — the BUILD layer
precis-mcp     default kit — precis_web · full CLI · taproot/nanopub · quest
models         catpath · bio · chem · hexfold · surface · <dna> · …
               attach via entry points + Scene/extxyz, never by import
```

`precis-util` cannot be kind-free or DB-free: `JobHandler` is a base class
four plugins subclass, and plugins hold `Store` and ship DDL. Roughly two
kinds, not zero. Scope is the full runtime shell (option A) because
anything less cannot host `precis_se`, which registers three job_types.

## The measurements this rests on

Core import sites per package, measured against `9aa05bfa`:

| package | into core | size |
|---|---|---|
| `precis_se` | **101 cad**, 19 utils, 17 structure, 11 store, 10 design, 10 blocktree, 7 errors, 5 workers | 1.7 MB |
| `precis_web` | 74 utils, 42 store, **24 cad**, 24 handlers, 21 workers, … | — |
| `precis_bio` | **3 structure** (Cell, Scene, probe) + 6 runtime | — |
| `precis_pathway` | **2 structure** + 7 store, 3 workers | — |
| `precis_chem` | 6 runtime, **0 geometry** | — |
| `precis_surface` | **1 cad** (the marching-cubes tables) | 116 KB |
| `hexfold` | **nothing** | — |

Geometry kernel outbound: `cad` → `errors`×1 + `utils`×2; `structure` → 0;
`design` → 0; `blocktree` → `cad`×1. Total 948 KB.

`server.py` is already a clean shell over Hub — runtime, tools, utils,
mcp_modalities, kind_gate, install_watchdog, errors, config, plus one lazy
`handlers.skill`. No store, no workers, no taproot/nanopub.

### Two conclusions the numbers force

**1. `se` is not a domain model — it is the geometry application layer.**
Every other model attaches through two narrow seams: registration (entry
points) and geometry (`Scene`/`Cell` + extxyz), at 0–3 import sites. se is
at 138 geometry imports, and it *builds* the structures the other models
consume. It belongs with geom. It is also the hottest tree in the repo —
70 of the last 30 days' 661 commits, ~11%, with cad at 28 and structure at
22 — while absorbing the nm-kind merge, surface's smooth→tiling round and
the hexfold fold-in. Treating it as a peer of catpath and hexfold would
produce a boundary worth a year of regret.

**2. The build→simulate seam is data, and it already exists.** A catalyst
must be built before it is simulated, but that handoff crosses as text:
`quest/compute.py:1543` does `export.to_extxyz(scene, constraints=True)`,
passes it as the job parameter `slab_extxyz` (`precis_pathway/job.py:49`),
and results return through `precis_pathway/ingest.py` as `Cell`/`Scene`.
`runner.py:279` names it "the precis `structure` seam"; `job.py:88` records
which path ran. Crucially `quest/compute.py` carries an explicit, repeated,
enforced **no-autocatpath-import rule** (lines 1158, 1250-51, 1263, 1286,
1482, 1664, 1824) — core orchestrates the plugin by job_type *name* and
plain dicts, never by import. The discipline the split needs is already
practiced in the hottest cross-domain path in the tree.

`precis-geom` therefore cannot live inside `precis-se` (precis_web's 24 cad
imports would make the web surface depend on se) nor inside `precis-util`
(geometry has nothing to do with verbs/Hub/Store, and 948 KB of CAD in the
wheel every agent installs is wrong). It is a third **mandatory** layer:
precis-mcp requires it.

## In scope

Ordered. Steps 1–4 are behaviour-neutral and move no files between
packages, so they are fair game before 10-16.

1. ~~**Import-boundary test.**~~ **DONE 2026-10-01** —
   `tests/test_plugin_import_boundary.py`, four assertions: core must not
   import a plugin; core SQL must not reference a plugin's table; a
   plugin's SQL must not reference another plugin's; and the allowlist must
   not go stale. The plugin set is read from `pyproject.toml`'s entry-point
   groups, so a newly registered model is covered without editing the test.
   AST-walked, so a function-local import counts — that is the flavour both
   real violations had. `_GRANDFATHERED` holds one entry, gr459054
   (`quest/roadmap_tick.py` → `precis_se.handler`), which keeps the suite
   green while leaving the breach counted and attributed; the staleness
   assertion forces the entry out when the gripe lands. SQL matching strips
   `--` comments and single-quoted literals, because core migrations
   deliberately *discuss* the plugin tables they must not touch
   (`0162_design_core.sql` on `se_blocks`, `0158_checklist_kind.sql` on
   `se_notes`) — a positive control confirms an executable FK reference
   still trips it. **Not done:** the "measures util's exact size" half; the
   table in §The measurements this rests on is still the only sizing, and
   it is per-package, not a util total.
2. ~~**Move `precis_pathway/analysis.py` into core or geom.**~~ **DONE
   2026-09-29** — now `src/precis/utils/reaction_graph.py`. The two core
   call sites in `quest/figures.py` and `quest/results_table.py` were
   function-local imports guarding a plugin dependency; they are module-scope
   now, since the dependency is gone. `precis_pathway` imports it back out of
   core (allowed direction) in `handler.py`, `toon_views.py` and
   `_dispatch_common.py`. Verified: `grep` for a core→plugin import across
   `src/precis/` returns nothing.
3. **DONE 2026-10-02** — `precis-pathway-help` ships from
   `precis_pathway.skills` via the `precis.skills` group. The skill corpus
   tests walk plugin roots read from pyproject (`tests/_skill_roots.py`), and
   `scripts/test`'s entry-point preflight self-heals a stale dev image per
   run (`--heal`), so a future entry-point change needs no rebuild round.
4. **CLI lazification** — a new `precis.cli` entry-point group, one-line
   help in entry-point metadata so top-level `--help` stays complete
   without importing. `cli/main.py:26` eagerly imports **55** subcommand
   modules (57 files, `taproot.py` 70 K, `quest.py` 25 K, `patent.py` 24 K)
   and `precis = "precis.cli:main"` makes every invocation pay it. This
   already caused a cluster outage — tenacity gated behind `[paper]` →
   `ModuleNotFoundError` → `serve` exits 1 → every node's embedder dead →
   embed worker crash-loop — which is why tenacity/pysbd/num2words/shapely
   were promoted to core deps as insurance. **Hard prerequisite**: without
   it `pip install precis-util precis-catpath` dies importing
   `precis.cli.taproot`. Filed as its own item with its own
   justification — `cli-lazy-subcommand-loading.md`; it fixes that outage
   class whether or not the split happens.
5. ~~**Compatibility contract, with a test.**~~ **DONE 2026-10-01** —
   `precis.protocol.PLUGIN_API` / `PLUGIN_API_MIN` plus
   `KindSpec.plugin_api`, checked by `kind_gate.gate`. Every in-tree
   plugin declares it, and a test enforces that. Doing it exposed the
   larger gap: `_load_plugins` bypassed the gate entirely — no
   `PRECIS_KINDS_DISABLED`, no `requires_*`, no banner verdict — so
   plugins now take the same gate as built-ins. One import-time case is
   named: a missing `precis.*` symbol reads as "built against a
   different precis". Tests are in `tests/test_dispatch.py`. Original
   text follows.
   **Compatibility contract, with a test.** A model asserts the util
   contract it needs at registration, and one that cannot satisfy it darks
   **legibly to the agent** via `kind_gate` — an agent handles "this kind
   is not installed, here is why" fine and handles a silently missing kind
   badly, by hallucinating or retrying. A pip floor alone is not enough:
   the `autocatpath>=0.22.0` floor in this very repo let a hand-ported copy
   drift behind it, caught by nothing until
   `tests/precis_web/test_pathway_kinetics_parity.py` was written
   (2026-09-27). se, at 180 edges, is the case that decides whether
   whatever we adopt is adequate.

Steps 6–8 were lifted from the deleted `package-split.md` (2026-10-01);
nothing else in this item covers them.

6. **Store-free vs store-backed test split**, so a member wheel can run its
   own suite without a database. As measured 2026-09-16, the store-free
   share is cad 9/25, pcb 13/47, structure 4/16.
7. **Deploy channel for member wheels.** Choose between a
   `#subdirectory=` source in the `deploy/redeploy-precis.yml` install line
   and the existing `/opt/precis/wheels` find-links (the catpath
   precedent). `scripts/deploy` pins one sha across three venvs, and a
   same-repo uv workspace keeps that invariant.
8. **Decide whether members publish to PyPI before minting names.**
   Publishing lapsed: `publish.yml` fires on `v*` tags, the last tag is
   v8.4.4, and pyproject is at 8.35.x. Nobody noticed because the cluster
   installs from the repo. Reto's "release a minimal package" goal makes
   this live, no longer optional.

Then, after 10-16, in this order: `hexfold` out (imports nothing — proves
the mechanics at zero API risk) → `precis_surface` out (one import; fold
the marching-cubes tables into geom) → `precis-geom` wheel → formalize
catpath as the reference model.

## Explicitly NOT in scope

* **Per-plugin Postgres schemas.** Ruled out 2026-09-27: pgbouncer is
  `pool_mode = transaction`, so a dynamic `SET search_path` is a race by
  construction. Static qualification would work and has precedent (the
  `vault` schema) but buys nothing, because a plugin's refs/chunks/
  embeddings live in **core** tables by design — only auxiliary tables
  could move. A separate *database* is the answer if hard isolation is ever
  genuinely wanted.
* **Extracting `precis_se` as a peer plugin.** See conclusion 1.
* **`precis_web`.** It imports 20+ core subsystems; it rides with
  precis-mcp, never util.
* **Gate and deploy speed as a justification.** Reto hopes (2026-10-01)
  modularization cuts the hours-long deploy jam. The deleted
  `package-split.md` measured that 59% of commits touch core and only ~10%
  are se/kernel/web-only. So a split helps only that ~10%: a model-only
  change could gate on its own suite and redeploy one wheel. It does not
  help the majority. A plugin also loads in-process, so its change still
  bounces `serve`. If the jam is the goal, measure where its hours go
  (gate-slot queueing, re-gates when main moves, deploy itself) before
  crediting the split with fixing it.

## Acceptance criteria

* `pip install precis-util <one-model>` yields a `precis serve` that boots
  and exposes that model's kinds and skills — no precis-mcp installed.
* The import-boundary test fails on a core→plugin import and on
  cross-plugin SQL.
* `precis <subcommand> --help` and top-level `--help` work without
  importing the other 54 subcommand modules.
* A model built against an older util darks with a message an agent can act
  on, not a traceback and not silence.
* Migrations still apply in any install order (already structural:
  `_migrations` PK is `(plugin, version)`, core runs first, no cross-plugin
  FK exists, the baseline snapshot is core-only).

## Target + blast radius

`src/precis/cli/main.py` + all 57 `cli/` modules (step 4) ·
`src/precis/handlers/skill.py` (already supports step 3; only declarations
change) · `src/precis/quest/{figures,results_table,compute}.py` (step 2) ·
`pyproject.toml` entry-point
groups · `deploy/` extras lists once wheels split.

## Open questions / decisions log

* **`se`: inside `precis-geom`, or a plugin that hard-depends on it?**
  Registration shape says plugin (handler + 3 job_types + migrations);
  coupling says same wheel. They move together either way.
* ~~"Private until the paper" — invisible, or quarantined?~~ **Ruled
  2026-10-01: invisible** (Reto: "I don't want to deal with mad db
  migrates"). The catpath precedent stands: separate repo +
  `/opt/precis/wheels`, content in core tables, gated by
  `PRECIS_KINDS_DISABLED`. No separate database; the end-state diagram is
  unchanged.
* ~~`package-split.md`: superseded or merged?~~ **Ruled 2026-10-01:
  superseded**, deleted, prep lifted into steps 6–8. Reto's question on
  the ruling — "don't we need shared resources for all the packages?" — is
  answered by `precis-util` in §End state: it *is* the shared base every
  model depends on (verbs, Hub, Store, JobHandler, migrations runner,
  entry-point discovery).
* **Orphan tables on uninstall.** No down-migrations, so removing a model
  leaves its tables. Mechanically detectable via the `<plugin>_` prefix
  (cf. `artifact-kinds-orphan-tables.md`). Needs an owner, not necessarily
  a fix.
