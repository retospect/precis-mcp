# The doc system — how to read it, how to keep it true

One contract, referenced by `CLAUDE.md`, `.windsurfrules`, and `AGENTS.md`
(tool-specific rules stay in those files; the doc system is defined once,
here). The main reader and writer is an LLM: keep prose compact, use
glossary terms, and prefer deleting to archiving — git is the history,
`docs/` is the present.

## Where truth lives

| What | Where |
|---|---|
| Orientation + package map | `docs/codebase.md` (map is generated — see below) |
| Subsystem architecture + why | the owning package's `__init__.py` **module docstring** |
| Cluster topology / what runs where | `deploy/README.md` |
| Cross-cutting invariants | `docs/conventions/` |
| Controlled vocabulary | `docs/glossary.md` (hand-written) |
| Work items (idea → ready) | `docs/backlog/` — one file per item |
| Operational procedures | `docs/runbooks/` |
| Generated reference (schema, config catalog) | `docs/reference/` |
| Mission / pitch narrative | `docs/mission.md` (positioning, not architecture) |
| History, shipped plans, old decisions | `git log` — nothing else; no CHANGELOG, no archive dirs |

There is no `docs/design/`, `docs/proposals/`, `docs/decisions/`, or
`OPEN-ITEMS.md` — plans and decisions either live as backlog items (future),
package docstrings (present truth + rationale), or git history (past).

## Reading order

1. `docs/codebase.md` — shape, lifecycle, seams, package map.
2. The owning package's `__init__.py` docstring — subsystem detail.
3. `docs/glossary.md` — when a term is unfamiliar or overloaded.
4. `docs/backlog/README.md` — what's planned (index is generated).

## Rules that keep it true

- **Package docstrings are the architecture record.** A subsystem change
  updates the owning `__init__.py` docstring in the same commit. Compact
  them freely; never strip them in a refactor. Their "why it's this way"
  paragraphs hold rejected-alternatives rationale — condense, don't delete.
- **Code-true names.** Docs use the names the code uses, always — retrieval
  breaks otherwise. A pending rename is declared in the glossary and tracked
  as a backlog item; the doc flips in the same commit as the code rename.
- **Backlog lifecycle.** An item is a few lines of prose (`# title` + what
  and why). When it grows a real spec it stays in the same file; when it is
  buildable, add `status: ready` front-matter (the fixer's pick signal;
  branch `fix/<slug>`, optional `model:` and `blocked-by:` as before). On
  ship: fold any surviving truth into the owning docstring, **delete the
  file** in the same commit.
- **Delete on ship.** Applies to everything: backlog items, scratch notes,
  superseded prose. A doc that describes shipped work is a lie waiting to
  happen.
- **Cite code by durable anchor** (`path/file.py::Sym`), never by line
  number. → `docs/conventions/code-anchors.md`.

## Generated indexes

`scripts/docs-index` rewrites the blocks between `<!-- docs-index:begin -->`
/ `<!-- docs-index:end -->` markers; `scripts/ship` runs it before the WIP
commit (same pattern as the Tailwind prebuild). Never hand-edit inside the
markers.

- `docs/backlog/README.md` — slug, `status:`, first prose line.
- `docs/runbooks/README.md` — slug, first prose line.
- `docs/codebase.md` package map — import path + docstring first line
  (PEP 257). A package listed as *(no package docstring yet)* is the nudge
  to write one.
