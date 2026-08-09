# MCP session ergonomics

Shipped portion: see `docs/conventions/kind-enablement.md` and the
`src/precis/kind_gate.py` / `src/precis/default_tags.py` /
`src/precis/startup_skills.py` module docstrings; full six-phase plan
in git history. Live: trimmed verb docstrings + per-verb skills
(`precis-search-help` et al.), banner discovery CTA +
`Kinds loaded/unavailable` summary, `PRECIS_STARTUP_SKILLS`,
`PRECIS_KINDS_DISABLED` + declarative resource gating,
`PRECIS_DEFAULT_TAGS` note-like injection, and the tools-list /
instructions byte-ceiling regression guards.

## Open scope

- **OQ-11 (verification only; the shipped design stands either
  way).** Does MCP 2025-06-18 + FastMCP 1.x let a server flag a
  `prompts/list` entry as "render at session start", or is that tag
  client-side only? Read the FastMCP prompts/list handler + MCP
  §prompts. The answer decides whether the redundant
  `Pinned skills:` banner line can be dropped. Owner:
  `src/precis/mcp_modalities.py::register_skill_prompts`.

## Decided constraints

- Tool names stay seven-verb — skills carry the reference detail;
  every trimmed docstring paragraph must exist in a skill before the
  trim (discoverability invariant).
- `PRECIS_DEFAULT_TAGS` stays a flat list, not a structured
  per-axis env-var schema; project-prefix conventions live in skill
  docs.
- Default-tag injection lives at the dispatch boundary
  (`precis.tools.core.put`/`.edit`), never per-handler.
