# LaTeX workspace

Files here are produced by the precis planner cascade. Per-tick commits
land in git history; rollback with `git reset --hard <sha>`.

## Layout

- `main.tex` — top-level assembly. Sections are `\input{}`'d in as the
  planner writes them.
- `tex/` — section sources (one `.tex` per section).
- `pics/` — figures. `\graphicspath{{pics/}}` is set, so
  `\includegraphics{foo}` resolves to `pics/foo`.
- `data/` — raw CSV / JSON inputs the figures and tables are built from.
- `build/` — latexmk output. Gitignored.
- `refs.bib` — auto-generated from precis citation refs; do not hand-edit.

## Build

```
latexmk -pdf main.tex
```
