---
status: ready
title: Repo DRY + structure repair plan, and the python kind's reverse-lookup views
pillar: platform
prio: normal
---

# Repo DRY + structure repair plan, and the python kind's reverse-lookup views

## Motivation / why

A read-only audit on 2026-10-06 (three parallel agents: code duplication,
docs duplication, structure + `kind='python'`) scored the repo code DRY
4/5, docs 3/5, structure 3/5. The findings are concrete and cheap to fix
one lane at a time; this item is the plan so the lanes can be dispatched
to agents in parallel without re-deriving the audit. Reto, 2026-10-06:
"use agents in this session to fix the found issues, and add the features
to the python kind."

Audit facts (verified by spot-check, cite as anchors not line numbers):

- **Code.** A 10-line-window hash scan over ~580k lines of `src/` found
  133 duplicate windows, concentrated in six file pairs. Largest:
  `handlers/component.py::` and `handlers/material.py::` copy
  `_put_value`, `_route_value`, `_resolve_source`, `_check_unit`,
  `_validate_type_args` (76–90% line-similar, ~400 lines incl.
  `store/_component_ops.py` ↔ `store/_material_ops.py`). Then
  `workers/axis_pass.py` ↔ `workers/classify.py` (`_load_axis`,
  `_render_examples`, prompt builder; 7 bespoke `_claim*` SKIP LOCKED
  functions across workers), `precis_se/manufacture*` ↔ `simp*` twin
  wrappers, `structsolve/complementarity.py` ↔ `continuation.py` loop
  bodies. Long tail of helpers with no `utils/` home: `_esc` ×7,
  `_num` ×10, `_as_float` ×5, `_slugify` ×4 (though `utils/chunk_slug`
  exists), `_now_iso` variants ×8, `_render_table` ×5. Eight files build
  raw `httpx.Client` instead of `utils/http.py::http_client`
  (`workers/fetch_oa.py` seven times). Tests: `_seed_paper` redefined in
  ~36 files, `_seed` in 44, `FakeStore` ×11, `_make_handler` ×11.
- **Docs.** CLAUDE.md (1524 words) and AGENTS.md (2111) are parallel
  copies that have diverged: AGENTS.md describes a version-bump /
  `uv run` workflow and never mentions qland; both restate chunks
  append-only and safe_fetch without pointing at `docs/conventions/`.
  The gate shape is hand-copied into CLAUDE.md (×2),
  `docs/conventions/testing.md`, `scripts/ship` (×2), `.claude/commands/
  {land,qgo,round}.md` — and all carried a stale duration; the gate takes ~90 min
  today (Reto, 2026-10-06). The sealed-migration rule lives in
  `docs/codebase.md` prose and is restated in four `.claude/agents/*.md`
  files; there is no `docs/conventions/` page for it. `conventions/time.md`
  is the model: one page, one pointer line in CLAUDE.md.
- **Structure.** import-linter is configured and gated (`pyproject.toml`
  `[tool.importlinter]`, run by `scripts/ship` in both qland lint and
  the full gate) but its six contracts cover cross-package boundaries
  only; nothing enforces handlers/store/workers layering, and 29
  `workers/` modules import from `handlers/`. 39 loose top-level modules
  under `src/precis/` mix domain logic (`cad_resolve.py`,
  `fit_classes.py`, `thread_forming.py`) with server plumbing;
  `handlers/` is 124 flat files; 19 of 42 Handler subclasses use neither
  `_cache_base` nor `_numeric_ref`.
- **`kind='python'`** (`handlers/python.py::PythonHandler` over
  `python_index/`) has get/search/put/edit/delete, views toc / outline /
  source / callgraph / entries / runtrace, selectors `~Class.method` and
  `~L<a>-L<b>`, AST+ruff-gated writes. Search is lexical only. The
  indexer computes `CallEdge` and per-module `imports` but exposes them
  only forward via callgraph — "who calls / who imports X" had to be
  built outside the kind as `scripts/coderef`.
- **Settings.** `PRECIS_ROOT` (writable notes sandbox for
  markdown/plaintext/tex) and `PRECIS_PYTHON_ROOTS` / `PRECIS_MD_ROOTS`
  (aliased, read-only repo roots for the `python` / `md` kinds) are
  separate settings and stay separate — different semantics. The local
  HTTP container (`deploy/mcp-http/precis-mcp-http-ensure.sh`) already
  sets `PRECIS_PYTHON_ROOTS=precis:/app`, where `/app` is a tar copy of
  `SRC_REPO` = the `origin/prod` clone — so the python kind shows the
  deployed tree, not `main`.

## In scope — the lanes (each one coder/documenter dispatch, disjoint files)

4. **layering-contract** (coder). import-linter contract "workers import
   store and utils, never handlers", with the 29 current imports listed
   under `ignore_imports` as a ratchet (no new violations); migrate the
   cheapest third of them to store methods in the same lane.
7. **docstring-layer** (documenter). Audit result: 95 package docstrings,
   31.6k words, duplication low (2/5) — 8-word-window search found only
   scattered single-sentence twins (precis_se ↔ `precis-se-help` /
   `precis-se-print-help` skills; taproot ↔ nanopub ↔ `cli/nanopub.py`).
   The real defects: `src/precis_se/__init__.py` is 5.8k words (18% of
   all docstring text) — split by subpackage and make the two se skills
   point at it; `precis/utils`, `asa_bot` and top-level `precis` have
   9–18-word docstrings — write real ones; nothing lints
   docstring↔doc restatement — add an 8-word-window check and a
   ~1500-word ceiling to `scripts/docs-index` or `backlog-lint`.

## Explicitly NOT in scope

- The `src/precis/domains/` regrouping and `handlers/` namespacing. Both
  are mass renames across a repo with ~40 in-flight worktrees; they wait
  for a quiet window and their own item.
- Semantic (embedding) search for the python kind — needs a storage
  decision; file separately once reverse lookups land.
- Merging `axis_pass` into `classify` — behaviour change in a live
  worker pipeline; file separately with a dogfood plan.
- Type-aware call resolution (MRO walk) in `python_index`.

## Acceptance criteria

- `uv run lint-imports` passes with the new workers→handlers contract.

## Target + blast radius

CLAUDE.md, AGENTS.md, `.claude/agents/`, `docs/conventions/`,
`scripts/ship` (strings only), `handlers/component.py`,
`handlers/material.py`, `store/_component_ops.py`,
`store/_material_ops.py`, `utils/`, `workers/` (imports + claim helper),
`pyproject.toml` (importlinter), `handlers/python.py`, `python_index/`,
`src/precis/data/skills/precis-python-help.md`,
`deploy/mcp-http/precis-mcp-http-ensure.sh`. No migrations, no
`safe_fetch.py`, no prod data path.

## Open questions / decisions log

- Decided 2026-10-06: `PRECIS_ROOT` and `PRECIS_PYTHON_ROOTS` stay
  separate settings (writable sandbox vs aliased read-only repo roots).
- Decided 2026-10-06: the docstring layer is not a duplication problem
  (lane 7 audit); it is a bloat-and-gaps problem, handled in lane 7.
