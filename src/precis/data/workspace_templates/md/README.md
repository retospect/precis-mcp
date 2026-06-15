# Markdown workspace

Files here are produced by the precis planner cascade. Per-tick commits
land in git history; rollback with `git reset --hard <sha>`.

## Layout

- `main.md` — top-level assembly. Sections under `sections/` are
  stitched in via the pandoc include filter or appended here directly.
- `sections/` — section sources (one `.md` per section).
- `pics/` — figures referenced as `![alt](pics/foo.png)`.
- `data/` — raw CSV / JSON inputs the figures and tables are built from.
- `build/` — pandoc output. Gitignored.

## Build

```
pandoc main.md -o build/main.pdf
```
