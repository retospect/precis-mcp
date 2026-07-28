---
status: draft
title: Carve a slim `precis-interface` plugin-SDK package out of precis-mcp
model: opus
---

# Carve a slim `precis-interface` plugin-SDK package out of precis-mcp

## Motivation / why

Out-of-tree plugins (`autocatpath`'s `pathway` kind today; the ADR 0056
chemistry/protein tool-packs tomorrow) declare a dependency on the **whole**
`precis-mcp` distribution just to *define a handler and declare entry points*.
That pulls the full server stack — `mcp[cli]`, `psycopg[binary,pool]`,
`pgvector`, `marker-pdf`'s transitive Pillow pins, `prompt-toolkit`,
`watchdog`, … — into any environment that wants to type-check, unit-test, or
import the plugin's surface. `autocatpath`'s `pyproject` already carries a
multi-paragraph `[tool.uv]` override and a `precis-mcp>=8.21,<9` pin it can't
even resolve against PyPI, purely to keep the plugin lockable. The plugin
authors' own note calls the surface "frozen since precis v6" — it is a stable
contract wearing a fast-moving package's dependency footprint.

The surface a plugin actually imports at module-load time is tiny, and — the
load-bearing finding — **nearly dependency-free**:

| Symbol(s) plugin imports        | precis module              | Real deps of that module |
|---------------------------------|----------------------------|--------------------------|
| `Handler`, `KindSpec`           | `precis.protocol`          | stdlib (`Hub` already `TYPE_CHECKING`) |
| `Response`                      | `precis.response`          | stdlib only |
| `BadInput`, `InitError`, `Unsupported` | `precis.errors`     | stdlib only |
| `BlockInsert`, `Tag`            | `precis.store.types`       | stdlib (+ `precis.errors`) |
| `toon` (encode/decode)          | `precis.format.toon`       | stdlib only |
| `try_format`                    | `precis.utils.handle_registry` | (verify) |
| `Cell`, `Scene`, `Atom`, `ImageOffset` | `precis.structure.{cell,scene}` | **numpy** |
| `JobTypeSpec`                   | `precis.workers.job_types` | stdlib (the spec dataclass; the registry lazy-imports) |

Everything else the plugin touches — `Hub`, `Store`, `JobHandler` — it imports
**lazily, inside methods**, and only ever exercises when running *inside* the
precis server, where the full `precis-mcp` is present by construction. So the
import/type/entry-point surface can live in a package whose only third-party
dependency is **numpy**.

## In scope

1. **New distribution `precis-interface`** (import name `precis_interface`),
   numpy-only, versioned independently, holding the contract types:
   `protocol` (`Handler`, `KindSpec`), `response` (`Response`), `errors`,
   `store_types` (`BlockInsert`, `Tag` — the value objects, **not** `Store`),
   `format.toon`, `structure` (`Cell`, `Scene`, `Atom`, `ImageOffset`),
   `job_types` (`JobTypeSpec`).
2. **precis-mcp depends on `precis-interface` and re-exports** from every
   existing path (`precis.protocol`, `precis.response`, `precis.errors`,
   `precis.store.types`, `precis.format.toon`, `precis.structure`,
   `precis.workers.job_types`) so no in-tree code, no existing plugin, and no
   pickle/entry-point path breaks. The old import sites keep working verbatim.
3. **`autocatpath`'s `precis` extra** depends on `precis-interface` (light) for
   the import/type surface; the server runtime it needs to actually *execute* a
   handler is supplied by the `precis-mcp` install already on the node, not by
   the plugin's declared deps. Drops the unresolvable `precis-mcp>=8.21` pin and
   the `[tool.uv]` override from `autocatpath/pyproject.toml`.
4. **Plugin import hygiene**: move any remaining top-level runtime-only imports
   in the plugin (e.g. `from precis.dispatch import Hub`) behind `TYPE_CHECKING`
   / local import, mirroring what `precis.protocol` already does for `Hub`, so
   the plugin's module-load surface resolves against `precis-interface` alone.

## Explicitly NOT in scope

- **No behavior change, no schema change, no new kind.** Pure package topology.
- **Not moving `Store`, `Hub`, `dispatch`, `JobHandler`, or any DB/MCP code**
  into the interface — those stay in `precis-mcp`; they are runtime machinery,
  not contract.
- **Not making the plugin runnable without a server.** A handler still executes
  only inside `precis-mcp`; this decouples the *build/type/test* dependency, not
  the *runtime* one.
- **Not the catpath→autocatpath rename** (shipping separately; this proposal
  assumes it has landed).
- **Not auto-migrating the other in-tree consumers off the old import paths** —
  re-export keeps them working; a later cleanup can retarget them if desired.

## Acceptance criteria

- `pip install precis-interface` pulls **only** numpy (+ its own transitive
  numpy deps) — asserted by a resolved-tree check in the new package's CI.
- In a venv with `precis-interface` but **not** `precis-mcp`, `python -c "import
  autocatpath.precis.handler"` (and the `precis.job` / `toon_views` modules)
  imports cleanly — proves the module-load surface is interface-only.
- In a full `precis-mcp` venv, every legacy path still resolves:
  `from precis.protocol import Handler, KindSpec`, `from precis.store.types
  import BlockInsert`, `from precis.structure.scene import Scene`, etc. — and
  `Handler is precis_interface.protocol.Handler` (identity, not a copy), so
  `isinstance`/entry-point dispatch is unchanged.
- Full precis-mcp `scripts/test` gate stays green; the `autocatpath[precis]`
  bridge tests still pass against the re-exported symbols.
- `autocatpath/pyproject.toml` no longer needs the `precis-mcp>=8.21`
  override/pin; its lock resolves against published packages.

## Target + blast radius

- **precis-mcp**: `src/precis/protocol.py`, `response.py`, `errors.py`,
  `store/types.py`, `format/toon.py`, `structure/{cell,scene}.py`,
  `workers/job_types/__init__.py` become thin re-export shims over
  `precis_interface.*`; `pyproject.toml` gains the `precis-interface` dep.
  Everything importing those paths (dispatch, handlers, store, quest,
  precis_chem/bio, web) is insulated by the re-export — no call-site edits.
- **New repo/package `precis-interface`**: the extracted modules + numpy-only
  `pyproject` + minimal CI.
- **autocatpath**: `pyproject.toml` extra retargeted; optional import-hygiene
  tweak in `src/autocatpath/precis/handler.py`.
- Identity/pickle risk: re-export must `from precis_interface.x import Y` (bind
  the same class object), never redefine — otherwise `isinstance` and any
  entry-point-resolved `KindSpec` comparison silently diverge. Called out as the
  one correctness trap.

## Open questions / decisions log

- **Package boundary granularity**: one `precis-interface`, or split
  `precis-interface` (protocol/response/errors/store_types/job_types, stdlib-
  only) from `precis-structure` (the numpy IR)? A stdlib-only core lets
  non-structural plugins avoid even numpy. Leaning single package for now
  (numpy is ubiquitous); flag for `ready`.
- **Versioning/compat policy**: how does `precis-mcp` pin `precis-interface`
  (floor vs exact), and what's the deprecation stance on the re-export shims —
  keep forever, or sunset after N minor versions?
- **Repo home**: standalone `retospect/precis-interface`, or a
  `packages/precis-interface` path-dep inside this monorepo published as its own
  dist? Affects the forward-migration and release tooling (ADR 0031 dual-track).
- **`try_format` / `handle_registry`**: confirm its dep weight; if it reaches
  into runtime registries it may stay in precis-mcp with the plugin importing it
  lazily instead of via the interface.
- **`Tag`**: confirm it's a pure value type in `store/types` (safe to move) vs.
  carrying store-bound behavior (stays, re-export only).
