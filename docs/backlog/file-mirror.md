---
status: draft
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
`term-taxonomy` thread carried this as "Claude Code memory/skills mesh
pilot — NOT filed" at Horizon 10. This item files it.

The file-backed kinds already do most of the work: `markdown` refs have
slugs from the path, section chunks, and links accept `markdown:<slug>~
<section>` selectors. What blocks reuse is `PRECIS_ROOT` being a single
root that refuses symlink escapes (`config.py`).

## In scope

1. **Multi-root file kinds.** `PRECIS_ROOTS=alias:/abs/path[:ro],…` in the
   shape `PRECIS_PYTHON_ROOTS` already uses; slugs become `alias/rel-path`.
   A root flagged `ro` refuses every write verb with `Unsupported`. The
   single `PRECIS_ROOT` keeps working as alias `workspace`.
2. **Two roots registered on the session MCP container:** `skills` →
   `src/precis/data/skills` (ro) and `memory` → the harness memory dir
   (ro). The cluster does not see the memory root; that is by design
   (Mac-local, same as the sandbox).
3. **Links from the files.** An idempotent pass (`precis mirror-links`,
   runnable by the same worker that embeds file chunks) mints
   `related-to` links from `[[slug]]` references in memory files and from
   skill frontmatter (`kinds:` → the kind's overview skill, `applies-to`
   verbs, `[[precis-…]]` cross-refs). Tree edges: `part-of` from a
   `parent:` frontmatter key (skills) and from the `##` section a bullet
   sits under in `MEMORY.md` (memory). Links carry `meta.source='mirror'`
   so the pass can remove what a file no longer says.
4. **Fisheye rings** for the two roots land via `fisheye-everywhere.md`'s
   ring table (`related-to`, `part-of`).
5. **Recall measurement** (the AC that makes the later migration
   decidable): a fixture of ten questions whose answers live in one topic
   file each; `search(kind='markdown', q=…, folder='memory')` must return
   the right file top-3 for at least eight.
6. **Runtime doc.** `precis-memory-help` gains one paragraph pointing at
   the mirrored root; `precis-overview` lists the two roots.

## Explicitly NOT in scope

- Authoring skills or memories in the graph (native nodes, files
  generated from them). Reto's ruling is pending; if "native" wins this
  item becomes the migration's read-only stage, not wasted work.
- Writing to the harness memory dir from precis, ever.
- Replacing auto-memory or `MEMORY.md`; the harness stays the owner.
- Session transcripts (`session-history-into-precis.md`).
- Embedding budget beyond the one-off pass over ~200 files.

## Acceptance criteria

1. With `PRECIS_ROOTS` set, `get(kind='markdown', id='skills/precis-
   relations')` and `get(kind='markdown', id='memory/MEMORY')` render;
   `edit`/`put`/`delete` on either raises `Unsupported` naming the
   read-only root.
2. `link(kind='finding', id=F, rel='related-to', target='markdown:memory/
   <topic>~<section>')` succeeds and appears in the finding's `view=
   'links'`.
3. After `precis mirror-links`, a memory file with three `[[slug]]`
   references has three outbound `related-to` links; deleting one
   reference and re-running removes exactly that link.
4. `get(kind='markdown', id='skills/precis-fisheye-help', view=
   'fisheye+1hop')` shows its `part-of` parent and `related-to`
   neighbours.
5. The recall fixture passes 8/10.
6. `tests/test_deploy_tree_no_secrets.py` stays green (the memory root path
   is configuration, never a tracked literal).

## Target + blast radius

- `src/precis/config.py` (roots parsing), the file-kind handlers'
  root resolution (`markdown`, `plaintext`, `tex`), `handlers/skill.py`
  (unchanged: skills stay served as skills; the mirror is a second
  address)
- `src/precis/cli/` (mirror-links command), one worker hook
- `deploy/` compose for the session MCP container (mount + env; no
  addresses)
- `src/precis/data/skills/precis-memory-help.md`, `precis-overview.md`
- tests: config roots parsing, read-only root, mirror-links idempotence,
  recall fixture

## Open questions / decisions log

- **[waiting on Reto]** Mirror first, or native from the start (todo in
  Reto's queue, 2026-09-30).
- **[decided 2026-09-30]** Reuse the `markdown` kind over a new kind: the
  goal is reachability, and a new kind would re-implement slugs, chunks
  and selectors that already exist.
- **[open]** Whether the memory root is mounted at all on a cluster node
  (no: the harness memory is per machine; each developer's session
  container mounts their own).
