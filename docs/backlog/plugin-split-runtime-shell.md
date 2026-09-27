---
status: draft
title: plugin split, top-down — precis-util runtime shell + precis-geom build layer + precis-xxx models, for agent use over MCP
prio: normal
model: opus
snooze-until: 2026-10-16
---

# Plugin split, top-down: `precis-util` + `precis-geom` + `precis-xxx` models

Design sessions 2026-09-27 (Reto + agents, `curried-pondering-starlight-39`
handing off to `iridescent-watching-reef`). Inverts
[`package-split.md`](./package-split.md), which is the same lineage (Reto
2026-09-16) approached bottom-up as kernel extraction. **One of the two
should be deleted at 10-16, not merged** — see the open calls. That item's
30-day no-changes rule governs both: `snooze-until` above is the earliest
day a file moves, and until then only behaviour-neutral prep is fair game.

## Motivation / why

Reto, 2026-09-27: the goal is **agent use over MCP**. `pip install
precis-util precis-hexfold` should yield a working MCP server whose tool
surface an agent already understands. Secondary and falling out of it: a
model can live in a private repo until its paper, then publish as a
citable standalone — *"a standalone thing with the paper, and also
integrated."*

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

1. **Import-boundary test.** Encode the lattice before anything moves:
   core must not import a plugin; a plugin's SQL must not reference another
   plugin's tables; core SQL must not reference a plugin's (already stated
   as a comment at `0162_design_core.sql:30`). Also measures util's exact
   size.
2. **Move `precis_pathway/analysis.py` into core or geom.** It is "pure,
   precis-free" by its own docstring — functions over node-link dicts.
   Three call sites, all already function-local: `quest/figures.py:374,396`
   and `quest/results_table.py:269`. This removes the *only* core→plugin
   import in the tree.
3. **Declare `precis.skills` from one in-tree plugin** (pathway or se) and
   move its skills under that package. Exercises a built-but-never-used
   path in production, and is the cheapest possible test of the
   agent-facing seam.
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
   `precis.cli.taproot`. Ship it as its own item with its own
   justification; it fixes that outage class whether or not the split
   happens.
5. **Compatibility contract, with a test.** A model asserts the util
   contract it needs at registration, and one that cannot satisfy it darks
   **legibly to the agent** via `kind_gate` — an agent handles "this kind
   is not installed, here is why" fine and handles a silently missing kind
   badly, by hallucinating or retrying. A pip floor alone is not enough:
   the `autocatpath>=0.22.0` floor in this very repo let a hand-ported copy
   drift behind it, caught by nothing until
   `tests/precis_web/test_pathway_kinetics_parity.py` was written
   (2026-09-27). se, at 180 edges, is the case that decides whether
   whatever we adopt is adequate.

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
* **CI speed.** `package-split.md` already measured this: 59% of commits
  touch core, so a split would not shorten the gate. The 6-way shard
  already did.

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
`src/precis_pathway/analysis.py` (moves) · `pyproject.toml` entry-point
groups · `deploy/` extras lists once wheels split.

## Open questions / decisions log

* **`se`: inside `precis-geom`, or a plugin that hard-depends on it?**
  Registration shape says plugin (handler + 3 job_types + migrations);
  coupling says same wheel. They move together either way.
* **"Private until the paper" — invisible, or quarantined?** The repo split
  + `/opt/precis/wheels` gives invisibility (catpath precedent). It does
  not give isolation: content sits in core tables gated only by
  `PRECIS_KINDS_DISABLED`. Quarantine needs a separate database. Reto to
  say which is meant.
* **`package-split.md`: superseded or merged at 10-16?** Recommended:
  superseded and deleted — bottom-up kernel extraction and top-down runtime
  shell disagree about what the first artifact is, and keeping both invites
  half of each.
* **Orphan tables on uninstall.** No down-migrations, so removing a model
  leaves its tables. Mechanically detectable via the `<plugin>_` prefix
  (cf. `artifact-kinds-orphan-tables.md`). Needs an owner, not necessarily
  a fix.
