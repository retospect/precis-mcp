---
status: draft
pillar: memory-graph
title: file mirror — skills and the Claude Code memory files enter the graph as read-only refs with links, so search, link and fisheye reach them
prio: normal
model: opus
---

# file mirror — skills and memory files as graph nodes

## Motivation / why

Reto's goal (2026-09-30): "everything" goes into the graph; skills get
broken up into a tree; "the graph is the memory". Today two bodies of
knowledge sit outside it. The runtime skills are files under
`src/precis/data/skills/` served by `handlers/skill.py` with no `links`
rows, so a walk or a fisheye never reaches them (`knowledge-mesh.md`
records the ask for DRY reuse across "skills, memories" and leaves the
mirror unfiled). The Claude Code memory (`MEMORY.md` + ~155 topic files
under the harness's project memory dir) is a separate system precis
cannot see; the `precis-memory-help` skill says so explicitly. The
`knowledge-mesh` thread (then `term-taxonomy`) carried this as "Claude Code memory/skills mesh
pilot — NOT filed" at Horizon 10. This item files it.

The file-backed kinds already do most of the work: `markdown` refs have
slugs from the path, section chunks, and links accept `markdown:<slug>~
<section>` selectors. What blocks reuse is `PRECIS_ROOT` being a single
root that refuses symlink escapes (the `Path.resolve()` +
`relative_to(root)` check in `handlers/plaintext.py`).

Why `markdown` and not `md`: the `md` kind (`handlers/md.py`,
`PRECIS_MD_ROOTS`, parsed by `handlers/_roots.py::parse_alias_roots`) is
already a read-only multi-alias-root reader over prose trees, but it is
DB-free by design — its `KindSpec` declares only get/search, so nothing in
it can carry `links` rows, chunk selectors or a fisheye. This item reuses
`parse_alias_roots` for the env-var shape and extends `markdown` for the
storage, so there is one alias-root grammar and one DB-backed prose kind.

## In scope (left)

1. **Skills root on the session MCP container:** `PRECIS_ROOTS=skills:
   <main checkout>/src/precis/data/skills:ro` in
   `deploy/mcp-http/precis-mcp-http-ensure.sh`. Reto approved the skills
   root only (2026-10-17); golden installs it after the code rides a
   release. The first read or `precis mirror-links` run then ingests ~200
   skill refs into prod. No memory root (decisions log).
2. **Seed `parent:`** on the index skills where the default rule picks the
   wrong parent; nothing authors one yet.

## Explicitly NOT in scope

- Authoring skills or memories in the graph (native nodes, files
  generated from them). Ruled out for now (2026-09-30); judged after the
  recall report, as a separate item.
- Writing to the harness memory dir from precis, ever.
- Replacing auto-memory or `MEMORY.md`; the harness stays the owner.
- Session transcripts (`session-history-into-precis.md`).
- Embedding budget beyond the one-off pass over ~200 files.

## Acceptance criteria

1. With the skills root wired on the session MCP,
   `get(kind='markdown', id='skills/precis-relations')` renders on prod and
   `precis mirror-links` reports its links; the gate half of every other AC
   is green (decisions log, 2026-10-17).

## Target + blast radius

- `src/precis/config.py` (roots env var, via `handlers/_roots.py::
  parse_alias_roots`), the file-kind handlers' root resolution and slug
  composition (`markdown`, `plaintext`, `tex`; `canonicalize_path_id`,
  `file_slug_from_path`), `utils/skill_common.py::SkillFrontmatter`
  (optional `parent:`), `handlers/skill.py` otherwise unchanged (skills
  stay served as skills; the mirror is a second address)
- `src/precis/cli/` (mirror-links command), one worker hook
- `deploy/` compose for the session MCP container (mount + env; no
  addresses)
- `src/precis/data/skills/precis-memory-help.md`, `precis-overview.md`
- `tests/fixtures/file_mirror/` (synthetic memory tree)
- tests: config roots parsing, slug composition, read-only root, skill
  parent default rule, mirror-links idempotence, recall fixture

## Open questions / decisions log

- **[decided 2026-09-30, Reto]** Mirror first. Native authoring of
  skills or memories in the graph is judged after this item's recall AC,
  as a separate item.
- **[decided 2026-09-30]** Reuse the `markdown` kind over a new kind: the
  goal is reachability, and a new kind would re-implement slugs, chunks
  and selectors that already exist.
- **[decided 2026-09-30]** The memory root is never mounted on a cluster
  node or in the gate container: the harness memory is per machine; each
  developer's session container mounts their own. Hence the two-half
  recall AC.
- **[built 2026-10-17, in-scope 1]** `PRECIS_ROOTS=alias:/abs/path[:ro],…`
  (`handlers/_roots.py::parse_mirror_roots`) adds mirrored roots to the
  markdown/plaintext/tex handlers; slugs are `alias/<flat slug>`, a `:ro`
  root refuses put/edit/delete with `Unsupported`, and mirrored refs carry
  `mirror:<alias>` + `SPACE:repo-dev` instead of the `workspace` flag. Alias
  `workspace` stands in for an unset `PRECIS_ROOT`.
- **[built 2026-10-17, in-scope 3-6]** `precis mirror-links [--dry-run]`
  (`cli/mirror_links.py::sync_mirror_links`): `related-to` from relative
  `.md` links and `[[slug]]` (link regexes reused from `cli/memory_mirror.py`),
  `part-of` from a `MEMORY.md` bullet to its `##` section chunk; for alias
  `skills`, `part-of` from `parent:` (new optional `SkillFrontmatter.parent`)
  or the default rule (`skill_parents`), `related-to` from `[[precis-…]]`,
  `kinds:` and `applies-to` verbs. Links carry `meta.source='mirror'`; a
  re-run removes only those. Fisheye needed no change: `related-to` and
  `part-of` were already ring relations. `search(kind='markdown', tags=…)`
  now honours `tags=` (it was silently dropped). Gate tests:
  `tests/test_file_mirror_roots.py`, `test_file_mirror_links.py`,
  `test_file_mirror_recall.py` (fixture `tests/fixtures/file_mirror/`),
  `test_skill_listing_retired.py` (memory-native-authoring test 4b).
- **[decided 2026-10-17, branch-desk relaying Reto]** Skills root only. The
  harness memory root is not mounted: the memory policy keeps those files
  as a legacy import source, and the mesh is the record. This drops the
  live-root report half of in-scope 5; AC 5 of `memory-native-authoring.md`
  (native 9/10 vs mirror 9/10) already answers native-vs-mirror.
- **[decided 2026-10-17]** No worker hook for mirror-links: the cluster
  workers never see a mirrored root, so the pass runs as a CLI where the
  roots are mounted.
- **[readiness 2026-09-30, needs-work → folded in]** Four blockers from
  the vet, each now in the text above: `folder='memory'` was not a
  mechanism (→ `mirror:<alias>` tag); `parent:` did not exist on skills
  (→ new optional key + default rule); the recall fixture could not run
  in the gate (→ synthetic fixture + live report); `[[slug]]` barely
  exists in the corpus (→ relative `.md` links are the primary form).
  Advisories folded: slug composition stated; `md` kind acknowledged.
  Re-vet before flipping to ready.

## Pillar-review deltas (2026-09-30)

Folded in from the product-plan review's `harness-memory-into-graph`
(filed the same day in a sibling tree, deleted as a duplicate of this
item). Three additions this item now carries:

1. **A closed `SPACE:` tag axis** on every mirrored ref and every native
   memory node: `research | repo-dev | personal`. `repo-dev` is what the
   two roots above land as; `personal` is reserved for the held pillar 4
   (`docs/roadmap.md`) and the axis is designed with a per-user value in
   mind from the start so pillar 4 does not redo it. Segregation is by
   tag, never by a second store.
2. **More roots than two.** `CLAUDE.md`, `AGENTS.md`, `.claude/agents/*.md`
   and `docs/conventions/` are repo guidance in the same sense as the
   memory dir; they mount as a third `ro` root (`repo`) so a walk from a
   gripe reaches the convention it violates. `docs/backlog/` mounts as a
   fourth `ro` root (`backlog`): it is the largest body of repo guidance
   and is searchable today only through the md kind (`search(kind='md')`,
   DB-free); front-matter `pillar:` and `blocked-by:` become
   tags and `related-to` links in the same mirror-links pass.
3. **A retirement condition per file class**, which `docs/roadmap.md`
   §Retirement points at: the harness memory index retires when session
   start can load the same bullets from
   `search(kind='markdown', tags=['mirror:memory', 'SPACE:repo-dev'])`
   and the recall fixture (in-scope 5) passes; the skill listing retires
   when `precis-overview` is served from the mirrored tree; a convention
   file retires when its rule is a `finding` with a `tests` edge. None of
   these fires while the mirror is read-only — they are the acceptance
   for the native stage td458720 decides.
