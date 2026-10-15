---
status: draft
title: find-in-ref — q= on a concrete-id get searches one ref's chunks
pillar: memory-graph
---

# find-in-ref — q= on a concrete-id get searches one ref's chunks

## Motivation / why

gr486840: `get(kind='memory', id=483106, q='plaza packing')` silently
dropped `q` and served the whole body; agents worked around it by
grepping chunks through `prod-psql`. `q` meant ~6 different things
across kinds (search query, compute input, eye ring label), and
`NumericRefHandler.get` never looked at it. Sibling: gr484241 (agents
detouring to prod-psql for capability gaps).

## In scope (implemented)

- `src/precis/handlers/_find_in_ref.py` — shared matcher + renderer:
  case-insensitive literal `q` by default, `re:<pattern>` for a bounded
  regex (no backrefs/backslash escapes/nested quantifiers, anchored scan
  so a non-terminating pattern can't happen), ±3-line context windows
  (`args={'context': N}`, ≤25), match counts, per-chunk handle labels
  (`pc<N>`, `gc<N>`, `dc<N>`, `mc<N>`, `<ref>~<ord>` fallback), file-line
  numbers where chunk meta carries `line_start`.
- `Handler.supports_find_in_ref` + `Handler.find_in_ref(id, q, view,
  context)` in `protocol.py`; `runtime/dispatch.py` routes
  `verb='get' + q` there only on non-eye views. Stray top-level kwargs
  (`of_kind=`, `spec=`, …) and non-`context` `args=` keys now refuse
  rather than ride silently into the find.
- Kind wiring: NumericRefHandler covers memory, todo, gripe, quest,
  alert, job, fleet, finding, llm, taxon, anki, agentlog, citation,
  concept, folder, message (`_find_resolve_ref` hook for slug-id kinds).
  Plaintext → markdown/tex. Paper (+cfp, datasheet). Draft (whole,
  `dc<N>`, `dr<N>~dc<M>` subtree). Md index (`alias/file[~sel]`). Skill
  (served body incl. synthesized slugs).
- `q` is never silently ignored on get: kinds that can't support it
  refuse through the kwarg-strictness gate (`email.get` dropped its
  unused `q` param so it refuses instead of swallowing); kinds that
  already owned `q` (web/youtube/news fetches, calc/estimate/oracle/
  random computes, paper/skill bare-q search delegation, eye ring
  labels) keep their semantics.
- Refusals: `q` + `view=`, `q` + missing/`/`/`[]` id, bad/`re:` regex.
- Tests: `tests/test_find_in_ref.py` (17 tests incl. multi-chunk,
  comment tagging, regex refusal, unsupported-kind refusal via
  dispatch, more() paging). Skills: `precis-get-help` section + one
  line in memory/todo/gripe/quest/draft/paper/files/md help.

## Explicitly NOT in scope

- `put`/`edit`-time find, `search` inside a ref (already exists —
  `search(..., scope=...)` for drafts), conv/pres/patent/edgar chunks
  (their `get` never declared `q`; they refuse for now — same wiring
  applies if a gripe asks for it).
- Regex over the *body* of non-chunked file kinds beyond what the
  shared block scan gives; grep-exact byte offsets.

## Acceptance criteria

- `get(kind='memory', id=N, q='x')` returns matching passages only,
  marked `>`, with `mc`/`me~ord` handles + line numbers + match count.
- No matches → `no match` line naming what was scanned.
- `get(kind='email', id=N, q='x')` (and every non-text kind) errors
  with a hint, never the full body.
- `get(kind='paper', q='x')` and `get(kind='skill', q='x')` keep the
  pre-existing search-delegation behaviour.
- Find bodies page through the existing `more()` recipe cursor.
- `scripts/test tests/test_find_in_ref.py` green; ruff/mypy clean.

## Target + blast radius

`runtime/dispatch._invoke_handler` (one branch ahead of the strictness
gate), `protocol.Handler`, `_numeric_ref` (all numeric kinds),
`plaintext`/`markdown`/`tex`, `paper`/`cfp`/`datasheet`, `draft`, `md`,
`skill`, `email` (param removal). No schema change; no prod writes.

## Open questions / decisions log

- Kept `q` as the parameter name (gr486840's request) over a new
  `find=` — the find only fires on concrete ids, where `q` had no
  prior meaning on the affected kinds.
- Synthesized skills are scanned via their rendered body (file body
  doesn't exist); `~`/`/` selectors refused on skill ids.
- `precis-job-help` is 33279 B > 32 KiB cap on main as of 2026-10-15 —
  pre-existing red in `test_skill_size`, untouched by this change.
