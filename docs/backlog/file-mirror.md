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

## In scope

1. **Multi-root file kinds.** `PRECIS_ROOTS=alias:/abs/path[:ro],…` in the
   shape `PRECIS_PYTHON_ROOTS` and `PRECIS_MD_ROOTS` already use
   (`parse_alias_roots`); slugs become `alias/<flattened rel-path>`, where
   the rel-path part keeps the existing `--` flattening of
   `file_slug_from_path` (`work/foo.md` under alias `memory` →
   `memory/work--foo`), so `canonicalize_path_id` gains one `/` split and
   nothing else. A root flagged `ro` refuses every write verb with
   `Unsupported`. The single `PRECIS_ROOT` keeps working as alias
   `workspace`. Every ref under a mirrored root carries the open tag
   `mirror:<alias>`, which is how searches scope to one root
   (`tags=['mirror:memory']`; `folder=` is the `folder` kind's placement
   tree and has nothing to do with roots).
2. **Two roots registered on the session MCP container:** `skills` →
   `src/precis/data/skills` (ro) and `memory` → the harness memory dir
   (ro). The cluster does not see the memory root; that is by design
   (Mac-local, same as the sandbox).
3. **Links from the files.** An idempotent pass (`precis mirror-links`,
   runnable by the same worker that embeds file chunks) mints
   `related-to` links from two reference forms in memory files — relative
   markdown links to a sibling file (the `[Title]` + `(<slug>.md)` form
   `MEMORY.md`'s ~100 index bullets use) and `[[slug]]` (rare in the live
   corpus: two uses, both in `MEMORY.md`) — and from skill frontmatter
   (`kinds:` → the kind's overview skill, `applies_to` verbs,
   `[[precis-…]]` cross-refs). Tree edges: `part-of` from the `##` section
   a bullet sits under in `MEMORY.md` (memory), and for skills from a
   **new optional `parent:` frontmatter key** on `SkillFrontmatter`
   (`handlers/_skill_common.py`; today no skill file has one) with a
   default rule so the tree exists without hand-editing 176 files: a
   `precis-<kind>-help` skill is `part-of` `precis-overview`, every other
   skill is `part-of` the `-help` skill whose `kinds:` it shares, else
   `precis-overview`. `parent:` overrides the rule; the seed authors it for
   the index skills only. Links carry `meta.source='mirror'` so the pass
   can remove what a file no longer says.
4. **Fisheye rings** for the two roots land via `fisheye-everywhere.md`'s
   ring table (`related-to`, `part-of`).
5. **Recall measurement** (the AC that makes the later migration
   decidable), in two halves that must not be confused. Gate half: a
   synthetic memory tree checked in under `tests/fixtures/file_mirror/`
   (a `MEMORY.md` index with sectioned bullets, ten topic files in the
   memory frontmatter shape, three of them cross-linked) and ten
   questions whose answers live in one topic file each;
   `search(kind='markdown', q=…, tags=['mirror:memory'])` must return the
   right file top-3 for at least eight. Report half: the same ten
   questions run once by hand against Reto's live memory root from his
   session, the score written into this item's decisions log — that is
   the number the mirror-vs-native decision reads; it never runs in the
   gate, because the gate container has no memory root and the live
   files churn (landed threads are deleted).
6. **Runtime doc.** `precis-memory-help` gains one paragraph pointing at
   the mirrored root; `precis-overview` lists the two roots.

## Explicitly NOT in scope

- Authoring skills or memories in the graph (native nodes, files
  generated from them). Ruled out for now (2026-09-30); judged after the
  recall report, as a separate item.
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
3. After `precis mirror-links` over the synthetic fixture, a topic file
   with three references (two of the `[Title]` + `(<slug>.md)` form, one
   `[[slug]]`) has
   three outbound `related-to` links; deleting one reference and
   re-running removes exactly that link.
4. `get(kind='markdown', id='skills/precis-fisheye-help', view=
   'fisheye+1hop')` shows its `part-of` parent and `related-to`
   neighbours.
5. The synthetic recall fixture passes 8/10 in the gate; the live-root
   score is logged in the decisions log (report half, not a test).
6. `tests/test_deploy_tree_no_secrets.py` stays green (the memory root path
   is configuration, never a tracked literal).

## Target + blast radius

- `src/precis/config.py` (roots env var, via `handlers/_roots.py::
  parse_alias_roots`), the file-kind handlers' root resolution and slug
  composition (`markdown`, `plaintext`, `tex`; `canonicalize_path_id`,
  `file_slug_from_path`), `handlers/_skill_common.py::SkillFrontmatter`
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
   gripe reaches the convention it violates.
3. **A retirement condition per file class**, which `docs/roadmap.md`
   §Retirement points at: the harness memory index retires when session
   start can load the same bullets from
   `search(kind='markdown', tags=['mirror:memory', 'SPACE:repo-dev'])`
   and the recall fixture (in-scope 5) passes; the skill listing retires
   when `precis-overview` is served from the mirrored tree; a convention
   file retires when its rule is a `finding` with a `tests` edge. None of
   these fires while the mirror is read-only — they are the acceptance
   for the native stage td458720 decides.
