# Draft section styles — catalogue & remaining drafts

> Companion to ADR 0037 (git-only). Defines the **section styles**
> (= skills) across four genres. The patent and research-paper style
> bodies have **migrated to shipped skill files**
> (`src/precis/data/skills/patent-*.md`, `sci-*.md`) — their drafts
> here are deleted; git history keeps them. This file keeps the
> catalogue, the schema constraints, and the genres whose styles are
> not yet authored as skills.

## How a style works (ADR 0037 recap — decided constraints)

- A draft heading carries `meta.style` = one skill slug; authoring
  that section surfaces that skill as the prompt.
- Styles are **self-contained** — no cascade; shared phrasing is
  repeated, not inherited (intentional — fixable per-section).
- Genre is `meta.workspace.doc_type`; it selects a thin **scaffold
  skill** listing the sections to create (as prose).
- **v1 is prose only.** Behavior render code + the numbering engine
  are additive expansions. Correctness is a **review pass**
  (ADR 0037 §3a) + the style prompt's own discipline — never
  per-style code. No `numbering:` on a section style (series bind to
  the leaf chunk_kind); no `validate:`.
- Section vs leaf test: has children → section (style); a single
  thing you point at → leaf (`chunk_kind`). Citation is an inline
  token (`[[pc…]]` / `\citequote`), not a style; references sections
  are generated at export from `rel='cites'` links.
- v1 needs **no new chunk_kinds** — `figure` (the umbrella kind:
  image and graph are one `figure` discriminated by
  `meta.figure.origin`) and `term` exist; `claim`/`part`/`character`/
  `setting` reuse `paragraph`/`term` until prose-over-`term` stops
  sufficing.

Frontmatter schema (provisional, ADR 0037 open-Q3):

```yaml
style: <slug>            # dispatch key; matches meta.style on a heading
role: root | section     # root = scaffold; section = a section prompt
archetype: prose | managed | separator
manages: [<chunk_kind>…] # managed only
silent: true             # separator only
# behavior: <module>     # EXPANSION ONLY; omit for v1
```

## Catalogue

| style | role | archetype | `manages` | genre(s) | status |
|---|---|---|---|---|---|
| `patent` root + `patent-description` / `patent-abstract` / `patent-claim` / `patent-image-part` / `patent-prior-art` | — | prose/managed | claim · figure,part · reference | patent | **skills shipped** |
| `sci-abstract` / `sci-introduction` / `sci-related-work` / `sci-methods` / `sci-results` / `sci-discussion` / `sci-conclusion` / `sci-survey-section` | section | prose | — | paper / review | **skills shipped** |
| `paper-research` / `paper-review` roots | root | prose | — | paper | **open** — scaffold skills not yet authored |
| `scene-break` | section | separator | — | book/script | **open** (draft below) |
| `animation-script` root, `script-logline`, `script-scene` | — | prose | — | script | **open** — bodies drafted in git history of this file |
| `book` root, `chapter`, `scene`, `book-front-matter` | — | prose | — | book | **open** — bodies drafted in git history of this file |

## Open scope

1. **Root scaffold skills** (`patent`, `paper-research`,
   `paper-review`) — the standard-section list is prose in the root's
   body, not a frontmatter field.
2. **Script + book genre skills** — migrate the drafted bodies (git
   history of this file, 2026-06-22 version) into
   `src/precis/data/skills/` when a script/book draft is first
   authored. The one shared style still needed:

   ```markdown
   ---
   style: scene-break
   role: section
   archetype: separator
   silent: true
   ---
   You are inserting a **scene break** — a silent structural divider
   between passages within a chapter (the `* * *` separator). A
   heading with no title text: it begins a new sibling passage but
   contributes nothing to the ToC. Use it for a hard cut in time,
   place, or POV that does not warrant a new chapter. It carries no
   prose; at render it becomes a centered break glyph. If you want to
   write in it, you want a `scene` section, not a break.
   ```

3. **Expansions (explicitly not v1):** managed render modules
   (FIG. n, numeral substitution, claim formatting), the numbering
   engine + `pinned`/lock, dedicated `claim`/`part`/`character`/
   `setting` chunk_kinds.
