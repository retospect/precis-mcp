# ADR 0032 — `precis.skills` entry-point group for plugin skill docs

**Status:** Accepted (2026-06-20)
**Context:** Third-party handler packages (e.g. `precis-chain`) plug
kinds in via the `precis.handlers` entry-point group
(`dispatch._load_plugins`), and ship migrations / job_types via the
sibling groups added in `docs/design/dft-phase-0-pr-1-plugin-registries.md`.
But a plugin's LLM-facing **skill docs** had no discovery path: the
skill index read only the built-in `precis.data.skills` package, so a
plugin's `get(kind='skill')` / `search(kind='skill')` help was invisible.

## Decision

Add a `precis.skills` entry-point group, mirroring `precis.handlers`.
Each entry-point value is a package holding `*.md` skill files
(e.g. `precis_chain = "precis_chain.data.skills"`).

`handlers/skill.py:_load_skills_map` is the single chokepoint — both
`get(kind='skill')` (via `_load_skill`) and `search(kind='skill')` (via
`_get_index` → `FileCorpusIndex`) derive their corpus from it. It now
walks the built-in package first, then every plugin root resolved from
the group. Discovery (`_plugin_skill_roots`) and the walk
(`_walk_skill_root`) are factored out; the walk is duck-typed on
`iterdir`/`is_dir`/`name`/`read_text` so both a `pathlib.Path` and an
importlib `Traversable` work.

## Consequences

- **Built-ins win slug collisions** — built-in skills load first and
  `_walk_skill_root` skips a stem already present, so a plugin cannot
  shadow a core skill.
- **Failure isolation** — a broken/missing plugin root is logged and
  skipped, never raised (same contract as `dispatch._load_plugins`); the
  core skill surface always loads.
- **No registration call needed** — plugins declare the group in their
  own `pyproject.toml`; the host stays unaware, consistent with the
  reverse-dependency plugin model.
- `hub.register_skill` is unchanged — it remains the MCP-*prompt*
  surface and is deliberately distinct from the searchable skill kind.
- Process-wide cache (`_SKILLS_MAP_CACHE`) means a plugin installed
  after boot needs a restart to appear — acceptable; plugins ship with
  the deployment image.
