---
status: draft
title: Workspace code cites its sources by db handle; export expands handles to self-contained excerpts
pillar: 3d-design
prio: normal
---

# Workspace code cites its sources by db handle

Source: gr514049 (Reto, 2026-10-24: annotate workspace code with db
references, or references plus excerpts, or add the excerpts from the
references on export, "for those who read this later and may have a
different db"). First emitter: the .epro2 -> script writer (gr514048,
[pcb-epro-mcp-roundtrip](pcb-epro-mcp-roundtrip.md), thread
[pcb-easyeda-round-trip](threads/pcb-easyeda-round-trip.md)). Consumers:
[pcb-workspace-code](pcb-workspace-code.md) and
[structure-workspaces](structure-workspaces.md) (§Code inside the design).

## Motivation / why

A workspace script (`main.py` for a board or an se structure) holds
numbers whose reason lives in the db: a dropout voltage from a datasheet,
a creepage from a standard, a seam angle from a paper. Today the reason
is in the author's head or a chat. Two readers lose it: a later session
(it can resolve handles, but nothing tells it which decisions have one)
and anyone outside the instance (a board package, a public git push,
paper SI), who has no db at all. Handles are cheap to write and resolve
only here; excerpts are portable but bloat the source. So: write handles,
expand to excerpts at the boundary.

## Design

Three layers, one convention.

1. **Write.** The script cites a source with a handle in a comment.
2. **Check.** A lint resolves every handle in the script; unresolved ones
   are reported, never dropped.
3. **Export.** An expansion pass turns each handle into a self-contained
   citation with a verbatim excerpt. Idempotent; reversible on import.

### Annotation syntax

Handles are the existing universal handles (`src/precis/utils/handle_registry.py`:
2-char type code + decimal id, `pa5`, `pt12`, `st503005`, `gr513665`;
chunk handles for body chunks, `pc10`). No new address form.

**v1 (what the .epro2 writer emits first): a trailing comment whose whole
content is one or more handles.**

```python
b.part("U3", lcsc="C6186")      # pa4474653
r = b.part("R7", value="5.1k")  # pc88213 pa4474653
```

Grammar: `# ` + handle (`[a-z]{2}[0-9]+`), optionally space-separated
further handles, nothing else on the comment. A comment holding a handle
anywhere inside free text is NOT an annotation in v1 (too many false
hits: `# see gr513665 for history` is prose). A line may carry at most one
annotation comment; it binds to the statement on that line (for a
multi-line call, to the line it sits on, the lint reports the AST node).
A standalone `# <handles>` line with no code binds to the next statement.

**Designed-for extension (not v1, must stay parseable by v1 readers as
"unknown tail, ignored"):** after the handles, an optional ` | ` and a
free-text gloss, then optional `key=value` fields.

```python
b.rule("dropout", min_v=1.1)    # pc88213~p12 | AMS1117 dropout 1.1 V @ 1 A
```

- `~<selector>` after a handle (page `p12`, section, chunk range) uses the
  selector syntax of the kind; v1 readers keep the whole token.
- ` | gloss`: the author's why, kept through export.
- Later fields (`quote="..."`, `role=derived|chosen|taken`) are reserved;
  a reader that does not know a field keeps it verbatim.

Rationale for a trailing comment (not a decorator or a `cite()` call): it
costs nothing at run time, cannot change the trace or script_sha
semantics beyond the text, survives formatters, and the .epro2 writer can
emit it without importing anything. The trace re-anchor rule
([trace-frame-structural-keys](trace-frame-structural-keys.md)) already
maps frames to source lines, so an annotation can be joined to its frame
by line and `src`.

What gets annotated (skill text, not enforced): part choice, values
derived from a datasheet, constraint numbers (clearance, creepage, trace
width), seam angles and lattice choices, anything taken from a paper or
finding, optimisation weights (gr513747 skill: why the weights are what
they are).

### Export expansion hook

Neither workspace has a code-export path yet; the hook goes where each
is specced to leave the system.

- **se.** The `workspaces` section of `precis_se.persist.tree_to_json`
  (structure-workspaces §Code inside the design, "Self-contained
  export"): the files at the last run's `script_sha`. Expansion runs on
  the file text as it is written into that section.
- **pcb.** The same design export (pcb-workspace-code §Q3 says a design
  export embeds cached footprint rows and rebuilds offline), plus the
  folder download. The existing board exports in `src/precis_web/routes/pcb.py`
  (`/pcb/{slug}/export.epro2`) are board data, not code, and are out of
  scope.

Both call one function, `expand_references(text, *, resolver, mode)`, in
a new `src/precis/provenance/refs.py` (pure: text in, text out; the
resolver is injected so tests and a db-less caller supply a stub). `mode`
is `inline` (a comment block under the line), `block` (a `REFERENCES`
docstring/comment section at the end of the file, entries numbered, the
line keeps `# [3]`) or `sidecar` (`REFERENCES.md` next to the script, the
line keeps `# [3]`). Default `block`: one file stays one file.

### What an excerpt contains

Per handle, from the resolver (kind-specific, all already in the db):

| kind | citation | excerpt |
|---|---|---|
| paper `pa`/`pc` | authors, year, title, DOI | verbatim chunk text (cap 400 chars), page if known |
| datasheet / part | manufacturer, part number, revision, page | verbatim chunk (cap 400 chars) |
| patent `pt` | number, title, assignee | claim/chunk text (cap 400 chars) |
| finding `fi` | statement, status, trust | the finding's evidence quote |
| structure `st` | slug, version, generator, atom count | none (a pointer; the data is in the design export) |
| gripe / todo | none exported | none: internal handles are replaced by `(internal ref, not exported)` |

Verbatim excerpts carry the licence risk of republishing chunk text; the
cap and a `--no-excerpt` switch keep citation-only export available, and
whether copyrighted chunks may leave at all is an open question below.

### Staleness

- Each expanded entry records `handle`, a content digest of the excerpt,
  and the export date (UTC `Z`).
- Re-export recomputes; if the chunk text changed, the entry is replaced
  and the export echo lists `3 references changed since last export`.
- Chunks are append-only (DELETE + INSERT), so a handle whose chunk was
  re-ingested can point at a different row: the digest mismatch is the
  signal, and a deleted row expands to `(unresolved: pc88213)` kept in
  place, never dropped.
- Import (round trip) strips expanded blocks back to bare handles using
  the recorded digests; text the user edited inside a block is kept as a
  gloss.

### Skill updates

Skills are runtime docs (`src/precis/data/skills/`). Add a short
"Citing sources in workspace code" section, one shared text, to:
`precis-pcb-help`, `precis-pcb-check-help`, `precis-pcb-review-help`,
`precis-pcb-route-help` (constraint numbers), `precis-pcb-fab-help`,
`precis-se-help`, `precis-se-design-help`, `precis-se-atomic-help`
(seam/lattice choices). The gr513747 optimisation skill gets the weights
rule when it lands. Each says: the v1 syntax, what to annotate, that
unresolved handles are lint errors, that export expands them.

## Slices (smallest first)

| # | slice | test |
|---|---|---|
| 1 | Parser: find v1 annotations in a script (AST + tokenize), return `(line, handles)` | tmp scripts: trailing, standalone, multi-line, prose false-positive |
| 2 | Lint: resolve handles via `Store.resolve_handle`; report unresolved with line | stub resolver; unresolved listed, none dropped |
| 3 | `expand_references` pure function, `block` mode, citations + capped excerpts, idempotent | golden file; expand twice == once; strip(expand(x)) == x |
| 4 | Staleness digests + re-export diff echo | change a chunk, re-export, entry replaced and echoed |
| 5 | Wire into se `tree_to_json` `workspaces` and the pcb design export (needs those export paths to exist) | exported design rebuilds offline with excerpts present |
| 6 | Skill text in the eight skills above | skill lint |
| 7 | Extension fields (`~selector`, ` | gloss`, `inline`/`sidecar` modes) | v1 reader keeps unknown tails verbatim |

Slices 1-4 need nothing from the workspace code; 5 blocks on the export
paths. Slice 1 lands before the .epro2 writer so its output is checked.

## Explicitly NOT in scope

The .epro2 writer itself (gr514048); a new handle form; citing from
prose files (the draft/export walker already does that); signing
excerpts.

## Open questions

- May verbatim chunk text of copyrighted papers leave the instance in an
  export, or is citation-only the default and excerpts opt-in?
- Default mode `block` vs `sidecar`: does a one-file script matter more
  than a clean source?
- Should the lint be part of `b.check()` / the run import (a failed run),
  or a separate command (warn only)?
- Internal handles (gr, td, fi of a private design) in a public export:
  replace by a placeholder (proposed) or refuse the export?
- v1 strictness: is "comment content is only handles" too narrow for the
  LLM, which will want to write a gloss from day one? (The extension
  syntax exists for this; moving it into v1 costs a few lines in slice 1.)
