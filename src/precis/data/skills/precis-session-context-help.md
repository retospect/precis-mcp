---
title: Session-context env vars (PRECIS_STARTUP_SKILLS, PRECIS_KINDS_DISABLED, PRECIS_DEFAULT_TAGS)
tier: 2
applies-to: skill
---

# Session-context env vars

Three operator-facing env vars shape every connecting agent's
session context without inflating the cold-start banner for
deployments that don't opt in. Each one is independent; they
compose cleanly.

## Quick reference

| Env var                       | Effect                                 | Banner footprint          |
|-------------------------------|----------------------------------------|---------------------------|
| `PRECIS_STARTUP_SKILLS`       | Pin specific skill ids to surface at boot | One line per pinned set    |
| `PRECIS_KINDS_DISABLED`       | Prohibit named kinds from loading      | One line if any prohibited |
| `PRECIS_DEFAULT_TAGS`         | Merge tags into every put on note-like kinds | None (per-call hint)        |

The default for all three is empty — every kind whose resources
are available loads, no skills are pre-pinned, no tags are merged.

## `PRECIS_STARTUP_SKILLS` — pin operator-curated skills

```
PRECIS_STARTUP_SKILLS=precis-search-help,precis-paper-help
```

Pinned skills land in the banner so the agent can pre-fetch their
bodies via `prompts/get` or `get(kind='skill', id=...)` on the
first message. Modern MCP clients see a `pinned` tag in
`prompts/list` and can prioritise these in their picker.

Cap: `PRECIS_STARTUP_SKILLS_CAP_KB` (default 50). Drop-tail when
exceeded; first-violation slug and every slug after it are
dropped with a banner notice citing the cap.

Cross-checks:

- Unknown slugs (typo, removed skill) → banner notice listing
  them.
- Pinned skills targeting a kind that's prohibited or missing
  resources → banner notice listing them. The skill body still
  loads, but its recipes would fail on the unavailable kind, so
  the operator sees the mismatch.

Full reference: `get(kind='skill', id='precis-startup-skills-help')`.

## `PRECIS_KINDS_DISABLED` — prohibit kinds

```
PRECIS_KINDS_DISABLED=patent,web,youtube
```

A prohibited kind is **not constructed** at boot — handler module
isn't imported, no sockets open, no env vars consumed. The kind
surfaces on the cold-start `Kinds unavailable: <kind> (prohibited),
...` banner line so the agent can skip recipes that target it.

The kind-enablement predicate is:

```
loaded(kind) = NOT prohibited(kind) AND resources_present(kind)
```

Prohibition wins over resource availability — operator intent
surfaces over incidental env presence.

Full reference: `get(kind='skill', id='precis-kinds-disabled-help')`
plus the handler-author convention at
`docs/conventions/kind-enablement.md`.

## `PRECIS_DEFAULT_TAGS` — auto-merge session tags

```
PRECIS_DEFAULT_TAGS=fbproj,2026-q2,team-research
```

A `put` on a **note-like** kind has these tags merged into the
caller's `tags=` (preserving the caller's stated order; defaults
appended in operator-stated order). The dispatcher emits a hint
listing the additions:

```
[info] Added PRECIS_DEFAULT_TAGS to put: fbproj, 2026-q2.
```

A `tag(kind=..., id=..., add=[...])` call on a note-like kind
**does not** mutate the operator's stated set. Instead the
dispatcher emits a suggestion hint listing the missing defaults:

```
[info] PRECIS_DEFAULT_TAGS suggested for tag add: fbproj.
```

The agent decides whether to re-issue the call with the merged
set.

### Note-like kinds

`KindSpec.note_like=True` is set on user-authored kinds:

- **Numeric refs**: `memory`, `gripe`, `conversation`,
  `flashcard`, `quest`, `todo`.
- **File-rooted authored content**: `markdown`, `plaintext`,
  `tex`.

Not note-like (no merge / no suggestion):

- **Ingested kinds**: `paper`, `patent` — bibliographic metadata
  is canonical; auto-tagging an ingested paper with `fbproj`
  would corrupt the corpus across deployments sharing the same
  store.
- **Fetched caches**: `web`, `wolfram` (`math`), `youtube` —
  the cache key is the URL / query; tagging is per-bookmark not
  per-session.
- **Generators / read-only**: `oracle`, `random`, `skill`,
  `calc`, `python` — no `put` surface to tag, or no agent-stable
  identity to tag.

### Interaction with the `workspace` auto-tag

File-rooted kinds (`markdown`, `plaintext`, `tex`) auto-stamp a
`workspace` flag tag on every ref under `PRECIS_ROOT`. This is
applied INSIDE the handler after ingest (not via the dispatch
hook) and uses `set_by='system'` to distinguish from
agent-authored tags.

`PRECIS_DEFAULT_TAGS` and `workspace` **layer**: workspace
identifies file-rooted-ness, defaults identify deployment intent.
Both apply, neither supersedes the other.

Full reference: `get(kind='skill', id='precis-tags')` for the tag
axis matrix and the closed-prefix rules.

## Composition

The three env vars compose without surprises:

1. **Pinned skill targets a prohibited kind** → banner notice on
   the startup-skills line. Pin still resolves; suggest the agent
   skip its recipes.
2. **Default tag is a closed-prefix tag** (`STATUS:open`) — it's
   accepted as long as the kind allows that axis. The standard
   tag axis matrix (`precis-tags`) still applies.
3. **Default tag conflicts with a `tag` `remove=` call** — the
   default set isn't applied to `tag` (only to `put`); the remove
   wins. The next `put` on a note-like kind re-applies the
   defaults though, so a default tag explicitly removed via `tag`
   will be re-added on the next create. Operators who want a
   permanent removal should remove the tag from
   `PRECIS_DEFAULT_TAGS` and restart.

## Operator workflow

1. `get(kind='skill', id='precis-overview')` for the live kind
   set and seven-verb surface.
2. Decide the deployment axes:
   - Which kinds to disable (security / scope).
   - Which skills to pre-surface (workflow shape).
   - Which session tags to default (project / cohort / quarter).
3. Set the env vars in the deployment manifest.
4. Restart the server.
5. Verify the banner: a connecting agent sees `Kinds loaded: ...`,
   `Kinds unavailable: ... (reason)`, `Pinned skills (load via
   prompts/get): ...`, and any warning lines on the very first
   message. Per-call hints from `PRECIS_DEFAULT_TAGS` show up
   inline on each `put` / `tag` response.

## Related

- `precis-startup-skills-help` — operator-facing skill for the
  pinning env var.
- `precis-kinds-disabled-help` — operator-facing skill for the
  prohibition env var.
- `precis-tags` — agent-facing skill for the tag axis matrix and
  closed-prefix rules.
- `docs/conventions/kind-enablement.md` — handler-author
  contract for declarative resource gating.
- `docs/design/mcp-cold-start-token-budget.md` — design context
  for all three env vars.
