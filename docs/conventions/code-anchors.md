# Convention — durable code anchors in docs

**Cite code by symbol, not by line.** In `docs/` and the memory index, a
reference like `workers/review.py:308` rots the moment anyone edits above line
308. Instead write a **durable anchor** — the repo-relative file plus the
symbol's dotted qualified name:

```
path/to/file.py::Qual.name
```

`Qual.name` is Python's `__qualname__` shape: a module-level `function`, a
`Class`, or a `Class.method` (nested: `outer.inner`, `Class.Inner.method`).
Examples:

```
src/precis/workers/review.py::run_review_pass
src/precis/dispatch.py::Hub.register_ability
src/precis/store/types.py::Tag.open
```

## The tool — `scripts/coderef` (three verbs, one `ast` pass)

```sh
scripts/coderef resolve <anchor>…        # anchor -> clickable file:line
scripts/coderef anchor  <file.py:LINE>   # a line you have -> the durable anchor
scripts/coderef check   <paths…>         # DRIFT: anchors whose symbol no longer resolves (tree-wide, high-signal)
scripts/coderef check --bare <file>      # + UPGRADE nudges: bare file.py:line refs, with the anchor to use (point-of-use)
```

- **Authoring:** grab the durable form from a line you're looking at —
  `scripts/coderef anchor src/precis/dispatch.py:174` → `…::Hub.register_ability`.
- **Reading/clicking:** `scripts/coderef resolve …::Hub.register_ability`
  → `src/precis/dispatch.py:174` (jump to it). A bare `method` resolves too if it's
  unambiguous; otherwise the tool lists the candidates to qualify.
- **Keeping it true:** `scripts/coderef check docs` (drift-only, tree-wide) flags
  any written anchor whose symbol no longer resolves — a renamed/removed symbol is
  drift to fix, or a deliberate cite of since-removed code you leave. `--bare` on a
  single file additionally lists bare `file.py:line` refs with the anchor to replace
  each (the authoring nudge — point-of-use, kept out of the tree-wide run so it isn't
  a firehose). Advisory — never a gate — wired into `/whatneedsdoing`'s hygiene wave
  next to `memory-lint` / `docs-orphans`.

## Rules that keep it from rotting — or nagging

- **Anchor in citation surfaces only:** `docs/` (design, ADRs) and the memory
  index. **Not** inline code comments — a `:line` there sits next to its target
  and moves with it; nagging those is pure noise.
- **Python only (v1).** The repo is ~all Python; `ast` gives exact, dependency-
  free resolution. A non-`.py` reference stays a bare path (no symbol). Resolvable
  symbols: functions, methods, classes, and **module-/class-level constants**
  (`ingest_oracles.py::_BUILTIN_TAG`) — locals inside a function body are not
  addressable. Paths are **repo-relative** (`src/precis/…`), not `src/precis`-
  relative shorthand — that's what `check` validates.
- **`ast`, not the semantic index.** `scripts/coderef` is deterministic exact
  resolution. The claude-context/Milvus index is for *discovery* — finding the
  symbol the first time you write an anchor — not resolution; don't couple a
  citation check to a running vector DB.
- Line numbers are fine in throwaway chat and terminal output (they're clickable
  there and die with the message). This convention governs what gets *written
  down* to be read later.
