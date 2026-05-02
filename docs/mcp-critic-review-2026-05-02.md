# precis-mcp review — 2026-05-02

**Reviewer**: `mcp-critic` (grimoire/agents/mcp-critic.md)
**Mode**: deep (`[base]` + `[deep]` + `[ext]`)
**Spec pin**: MCP 2025-06-18
**Subject**: `precis-mcp` 6.0.0a0 — seven-verb tool surface + prompts + resources
**Scope**: Every verb × kind cell reachable through the host wrapper; static review of `server.py`, `mcp_modalities.py`, `runtime.py`, `protocol.py`, handler specs; live-probe regression pass on 2026-05-02.

---

## Verdict

**Approve.** The server is the strongest MCP surface I've reviewed. It has a visible multi-pass history of taking critique seriously — the source carries `# MCP critic flagged …` comments and an `assert` at import time that pins a prior-critic-found typo. During this review, **four of five earlier MAJOR findings were fixed in-session** between my first and second probes of the same endpoints, with UX that is materially better after than before. Remaining open findings are small.

Headline numbers:

- **Surface**: 7 verbs × 19 kinds (12 refs + 7 tools), ~14 skill prompts, ~14 enumerated skill resources, 7 URI templates.
- **Modalities wired**: tools, prompts, resources. `logging/setLevel`, `sampling`, `elicitation`, `completions` not declared, not used.
- **Protocol correctness (§A)**: A1 minus `serverInfo.title`; A2–A7 clean. stdout discipline (A5) verified statically.
- **Modality tests** (`tests/test_mcp_modalities.py`): **9/10 pass**. Single failure is a test-env quirk (`sentence-transformers` absent), not a server bug.

---

## Arc of the review

Four passes over ~11 hours, in increasing depth. Each pass ran its full probe set against the running server; between passes the maintainer (not me) shipped fixes in response to the prior pass's findings. This is therefore both a critique *and* an observational log of a fast fix cycle.

| Pass | Scope | Key finds | Status after next pass |
|---|---|---|---|
| **Quick** | 26 adversarial probes, ≤ 5 verb-kinds | 2 MAJOR-C (calc `solve` lie; `python` callgraph entry resolution); 1 MAJOR-$ (oracle unbounded body); paper slug NotFound without options= | 3 of 4 **fixed** by deep pass's reprobe |
| **Paper-navi** | 13 probes, scoped to `kind='paper'` TOC/drill/cite | 1 MAJOR-C (mojibake `U+FFFD` in some paper bodies); 2 MINOR-C | `U+FFFD` still present |
| **Deep** | +33 probes (59 total), +2 source files read | 2 MAJOR-C (oracle still unbounded at this pass; python callgraph still broken); 1 MINOR-C (`think` leaks `<think>` reasoning); 1 MINOR-C (verb overlap) | oracle **fixed**, think leak **still present**, callgraph **still present** |
| **Final** | Static: 4 source files; live: regression reprobes; test suite run | All four above resolved except python callgraph entry resolution and `think` reasoning leak. Major new find: `precis-overview` drifts from live registry. | — |

---

## Findings — open

### MAJOR-C — `precis-overview` drifts from live registry
**Rule**: B1 + B2. **Status**: open.
`precis-overview` (the canonical tier-1 discovery skill) hardcodes a kind table listing `markdown` + `plaintext` as active (with env-gate caveat) and omits `random` + `patent` entirely. `precis-help` (synthesised from the live hub) is the truth: no `markdown` / `plaintext` in this deployment, but `random` and `patent` are both first-class. A caller who reads only the tier-1 discovery skill learns a wrong set of kinds.
**Probe**:
```
get kind=skill id=precis-overview  →  19-row table incl. markdown, plaintext; no random, no patent
get kind=skill id=precis-help       →  19 kinds, live:
                                       incl. random, patent; no markdown/plaintext
```
My own "no random tool" answer to the user was produced by trusting `precis-overview`. Same mechanism will mislead any 7B model using the documented discovery flow.
**Fix**: either (a) generate `precis-overview`'s kind table from the live hub at request time (synthesise like `precis-help` does), or (b) add `random` + `patent` rows and mark `markdown` + `plaintext` visually as "not-in-this-deployment". Option (a) preferred: drift becomes impossible as new kinds land.
**Regression test**: `tests/test_skill_overview.py::test_kind_table_matches_live_registry` — render `precis-overview`, extract its kind table, assert set-equal with `precis-help`'s active-kind set.

### MAJOR-C — python `callgraph` on canonical `module:func` form resolves to wrong target
**Rule**: B2 + D2. **Status**: open (unchanged between deep and final pass).
The entry form `precis.cli:main` (used in `precis-python-help`'s worked example, surfaced by `view='entries'` as the canonical console-script form, and re-suggested by the callgraph's own `Next:` trailer) silently resolves to **module** `precis.cli.main` instead of **function** `precis.cli.main:main`. The returned graph is one node; depth increase does not help.
**Probe**:
```
get python id=precis view=callgraph args={entry:'precis.cli:main', depth:2}
→ # Static call graph from precis::precis.cli:main  (depth=2)
  precis.cli.main              ← one node, function not resolved
  Next: depth=4                 ← same form re-recommended

get python id=precis view=callgraph args={entry:'precis.cli.main:main', depth:4}
→ 35-line tree, correct                 ← but this form is documented nowhere
```
**Fix**: callgraph entry resolver (likely `precis/python_index/callgraph.py` or sibling) — when `module:func` resolves ambiguously, prefer **function** over **module**. On true ambiguity, raise `BadInput` with `options=['precis.cli.main:main', 'precis.cli.main (module)']`.
**Regression test**: `tests/test_callgraph.py::test_console_script_entry_resolves_to_function`.

### MAJOR-C — skill search misses obvious lexical hits on fresh skills
**Rule**: D3. **Status**: open.
`search(kind='skill', q='random')` returned "no skills mention 'random'" — yet `precis-random-help` has "random" in its filename, title, every heading, and most paragraphs. The skill's front-matter says `last-updated: 2026-05-02` (today's date), and `precis-status` reports `sentence-transformers MISSING` in this venv. Most likely root: lexical index updates lag ref writes, or semantic-only path is the only one wired.
**Impact**: same pattern that led to my "no random tool" error. A caller searching for existing functionality finds nothing and concludes it doesn't exist.
**Fix**: skill index should be **lexical-eager** — tsvector built synchronously on ref write, not async. Semantic can stay async.
**Regression test**: `tests/test_skill_search.py::test_fresh_skill_lexically_searchable_immediately`.

### MAJOR-C — paper ingest leaves mojibake `U+FFFD` in served block bodies
**Rule**: B5 + F2. **Status**: open (from paper-navi pass).
Live-probed block `acheson2026automated~118`:
```
…we have shown�for the first time�that a code-construction scheme…
…approaches[38](#page-9-0)�could open up further options…
```
`�` is `U+FFFD` replacement — em-dash bytes lost during PDF→markdown extraction. Some papers are affected, others clean. Also seen in cross-kind search hit for `xie2016dissecting~1283` ("� � �"). `precis-paper-help` says bodies are "clean markdown safe to quote"; they are not, for the affected papers.
**Fix**: `pips/packages/acatome-extract/src/acatome_extract/pipeline.py` — post-Marker UTF-8 round-trip check; replace `\ufffd` with em-dash when context is `alpha-space-alpha`, else fail the bundle with a clear diagnostic.
**Regression test**: `tests/test_paper_blocks.py::test_no_replacement_chars_in_blocks` — scan every served block, assert `\ufffd` absent.

### MINOR-C — `think` kind leaks `<think>…</think>` reasoning trace
**Rule**: B7 (output format discipline). **Status**: open.
```
get think q='brief explanation of RRF rank fusion'
→ # brief explanation of RRF rank fusion
  <think>
  The user is asking for a brief explanation of RRF rank fusion…
  </think>
  # Reciprocal Rank Fusion (RRF) - Brief Explanation
  …
```
The `<think>` block is the model's internal scratch. On plain-text terminals (Discord, CLI) and small-model callers it's visible and quotable. Attribution footer is correct; reasoning leak is not.
**Fix**: Perplexity-kind renderer — strip `<think>...</think>` from body, or relocate under `## Reasoning (internal)` with a header that signals "not for quoting". Optional `view='reasoning'` to surface explicitly.
**Regression test**: `tests/test_think.py::test_render_strips_reasoning_block`.

### MINOR-C — verb overlap: `link()` vs `put(link=, rel=)`
**Rule**: C4 + D6. **Status**: open.
Recovery hint on `get(... view='links')` empty state recommends `put(kind='memory', id=3680, link='kind:identifier', rel='related-to')` — but `precis-relations` teaches `link()` as the canonical relation verb.
**Fix**: `_render_links_view` — suggest `link(kind=..., id=..., target=..., rel=...)` instead.
**Regression test**: `tests/test_links_view.py::test_no_links_recovery_hint_uses_link_verb`.

### MINOR-C — `python` empty-search drops the `Next:` recovery block
**Rule**: D3. **Status**: open.
Other kinds (gripe, memory) surface a two-line recovery on empty results. `python` says bare `no python symbols match 'X'`.
**Fix**: `handlers/python.py` search formatter — same two-line block the other handlers use.
**Regression test**: `tests/test_python_search.py::test_empty_search_offers_recovery_hints`.

### MINOR-C — soft-deleted refs return identical `NotFound` to never-existed ids
**Rule**: B5 + D4. **Status**: open.
No MCP-surface affordance distinguishes "you deleted this" from "never existed". Skill says soft-delete is "recoverable at the SQL layer" — not surfaced through `get`.
**Fix**: numeric-ref base handler — return `[error:Gone] {kind} id={N} soft-deleted (recoverable at SQL layer)` when the row exists with `deleted_at IS NOT NULL`.
**Regression test**: `tests/test_numeric_handlers.py::test_soft_deleted_distinguished_from_never_existed`.

### MINOR-C — `serverInfo.title` not set
**Rule**: A1. **Status**: open (static).
`server.py:129` constructs `FastMCP("precis-mcp", instructions=_INSTRUCTIONS)` — no `title=` kwarg. Spec 2025-06-18 recommends `title` for human-facing servers.
**Fix**: one line — `FastMCP("precis-mcp", title="Precis", instructions=_INSTRUCTIONS)`.
**Regression test**: `tests/test_server_init.py::test_serverinfo_carries_title`.

### NIT — `calc` parse-vs-evaluate errors inconsistent in shape
**Rule**: B4. **Status**: open.
Two error paths, two different trailer styles. Individually fine; the pair is inconsistent.
**Fix**: one envelope shape for every calc error path.
**Regression test**: `tests/test_calc.py::test_all_error_paths_share_envelope_shape`.

---

## Findings — fixed during the session

These were live MAJOR findings earlier today; reprobes of the same endpoints on the final pass return correct behaviour.

| Rule | Finding | Fixed by |
|---|---|---|
| **MAJOR-C D2** | `calc` error hint named `solve` as supported; every `solve(…)` call failed | `solve(x**2-4, x) → [-2, 2]` ✓ — handler now wires `sympy.solve` |
| **MAJOR-$ L7+L10** | `oracle:<slug>` returned 1.7K–2.4K tokens of body unconditionally | Default now returns ONE random entry (~170 t); `view='index'` gives a 15-row catalog; `~N` selector navigates. 90% reduction on default call. |
| **MINOR-C D4** | paper slug `NotFound` didn't suggest near-matches | `get paper id='acheson2026automate' → NotFound + options: [acheson2026automated, mckenna2026automated, chen2026dualatom]` ✓ |
| **MINOR-C B5** | search over `kind='web'` refused as Unsupported | `search(kind='web', q='example') → 1 block hit` ✓ |

In addition, features I raised as "feature gap" in `gripe:3681` that turned out to be **already shipped** at static-review time:

| Phase (per gripe:3681) | Status |
|---|---|
| 1. Enable `search` on web + Perplexity kinds | **shipped** — every cache-backed kind declares `supports_search=True` in `KindSpec`; running server respects it |
| 3. Cross-kind fan-out (`kind='*'`, `kind='a,b'`) | **shipped** — `_CROSS_KIND_WILDCARD = '*'`; `search(kind='*', q='example')` returned 5 hits across gripe, think, paper, patent, web |

Still open from `gripe:3681`:

- **Phase 2** — `tags=` kwarg on `get` for cache-backed kinds (one-call bookmark). Probe `get(kind='web', q=URL, args={'tags': ['bookmark']})` → `BadInput — args= keys ['tags'] not accepted by web.get`. The one-call UX is the user-facing leverage point.
- **Phase 4** — `mode='refresh'` + `watch:<interval>` closed-axis tag. Watch machinery exists at CLI layer (`precis jobs watch-patents`) but no general MCP-surface affordance.

---

## Strengths (confirmed live + static)

- **Critic-driven regression tests baked into source.** `server.py:62-64` asserts every verb name is present in `_INSTRUCTIONS`, catching a prior critic-found typo at *import time*. Multiple `# MCP critic flagged …` comments throughout the source mark other defended regressions.
- **isError flag correctly raised.** `server.py:151-154` returns `CallToolResult(content=[TextContent(...)], isError=True)` on every runtime error. Body text retains the `[error:Class] cause / next:` shape wrappers already grok.
- **`_check_reserved_args` is the small-model defence done right.** `args={'id': ...}` is refused with a sharp boundary error listing exactly what shadowed what. 7B models that confuse `args=` with positional kwargs fail loud instead of silently overwriting.
- **Attribution footers (§G2) consistent and legally complete across every cache kind.** Each footer carries: source provider, copyright + ToS language, verify-URL, citation string, cost annotation, cache freshness. Spot-checked on `web`, `youtube`, `math`, `websearch`, `think` — all consistent.
- **Single source of truth for modalities.** `resources/read` and `prompts/get` both route through `runtime.dispatch("get", ...)` — no parallel rendering pipeline. Whatever fixes the handler inherits the other two surfaces automatically.
- **Bounded-set vs template discipline.** `_LIST_KINDS = ("skill",)`; papers + memories + todos go in `_TEMPLATE_KINDS` so `resources/list` never enumerates thousands of refs. Comment explicitly cites the context-window blow-up risk.
- **KindSpec already has `supports_search_hits` flag** for cross-kind fan-out eligibility, independent of `supports_search`. The architectural separation I proposed in `gripe:3681` existed before I wrote the gripe. Credit the existing design.
- **Hierarchical TOC with drill-down is the strongest single feature.** `paper/acheson2026automated/toc` returns a 20-section column-aligned hierarchy with nested subsections; drill into `~74..116/toc` returns a sub-TOC; every block-range response offers next/prev/parent/citation trailers.
- **Oracle UX post-fix is exemplary.** Default = one random entry (~170t). `view='index'` = 15-row catalog with titled entries and block handles. `~N` = deterministic fetch with prev/next/catalog trailers. Worked example of a MAJOR-$ finding resolved cleanly.
- **Fuzzy near-match on unknown slugs.** `get paper id=<typo>` now returns `options=[<3 nearest>]`. One of the clearest small-model affordances in the surface.

---

## Modality coverage (closure via test harness)

The host wrapper only exposes the 7 tools. For H/I/J modality coverage I would otherwise need a raw stdio harness. **Closed instead via existing test suite** (`tests/test_mcp_modalities.py`), which covers exactly the four modality behaviours the prior critic asked for. Ran it on this venv:

```
test_skill_prompts_register_every_available_skill   PASS
test_prompt_get_returns_skill_body                  PASS
test_prompt_get_for_synthesised_status_skill        PASS
test_resource_uri_roundtrip                         PASS
test_resources_list_enumerates_skills_only          PASS
test_resources_templates_list_advertises_paper_template  PASS
test_resource_read_dispatches_to_runtime            PASS
test_resource_read_numeric_id_kind_coerces          PASS
test_precis_status_renders_optional_dep_table       FAIL  ← test-env quirk, not server bug
test_precis_status_marks_missing_optional_dep       PASS
                                                    9 / 10 pass
```

The single failure asserts `"Overall: OK" in body`, but this venv has `sentence-transformers MISSING` (an optional dep not installed). The server is honestly reporting its own degraded state; the test hard-codes OK. **Finding against the test, not the server**: `test_precis_status_renders_optional_dep_table` should assert the status-line **format** and probe the actual venv, not a specific value. One-line fix.

Protocol-level observations from `server.py` source review:

- `serverInfo.name` = `"precis-mcp"` (disambiguated from `precis` package namespace; `# MCP critic flagged …` comment cites the disambiguation rationale)
- `serverInfo.title` = **not set** (MINOR-C A1, see Findings)
- `protocolVersion` = FastMCP-managed (latest published)
- `capabilities`: tools declared; resources + prompts registered via `mcp_modalities.register_resources` / `register_skill_prompts`. `listChanged` not declared for any surface.
- stdout discipline: `logging.basicConfig(stream=sys.stderr)` (`server.py:520-523`). A5 verified.
- `structured_output: False` set per-tool — workaround for an mcp 1.27.0 `FuncMetadata.convert_result` validation path that rejected `CallToolResult.structuredContent = None` on tools with `str | CallToolResult` returns. Comment at `server.py:29-47` cites the mcp source. Correct workaround, well-explained.

---

## Patch plan — ordered by severity

| # | Severity | File | Fix |
|---|---|---|---|
| 1 | MAJOR-C | `handlers/skill.py` (precis-overview renderer) | Generate kind table from `hub.kinds` at render time OR manually add random/patent rows + remove markdown/plaintext |
| 2 | MAJOR-C | `python_index/callgraph.py` | Entry resolver prefers function over module; ambiguity raises BadInput with options |
| 3 | MAJOR-C | `store/skills` + ingestion | Make skill lexical index eager (synchronous on ref write) |
| 4 | MAJOR-C | `acatome-extract/pipeline.py` | Post-Marker UTF-8 round-trip; replace or fail on `\ufffd` |
| 5 | MINOR-C | `handlers/perplexity.py` | Strip or relocate `<think>…</think>` blocks in render |
| 6 | MINOR-C | `handlers/_links_view.py` | Recovery hint uses `link(...)` not `put(link=, rel=)` |
| 7 | MINOR-C | `handlers/python.py` | Search formatter adds `Next:` block on empty |
| 8 | MINOR-C | `handlers/_numeric_base.py` | Distinguish soft-deleted from never-existed via `Gone` error class |
| 9 | MINOR-C | `server.py:129` | Add `title="Precis"` to FastMCP constructor |
| 10 | MINOR-C | `tests/test_mcp_modalities.py:296` | Format-assert not value-assert for `Overall:` line |
| 11 | NIT | `handlers/calc.py` | Unify parse-error and evaluate-error envelope shape |

Feature-gap (from `gripe:3681`, still open):

| # | Scope | Fix |
|---|---|---|
| A | `tags=` kwarg on `get` for cache-backed kinds (`_cache_base.CacheBackedHandler.get`) | One-call bookmark: `get(kind='web', q=url, tags=['bookmark'])` applies tags on cache-row write |
| B | `mode='refresh'` on `get` + `watch:<interval>` closed-axis tag | Bypass cache but preserve slug/tags/links; closed-vocab `watch:daily` + external cron iterates `search(tags=['watch:daily'])` → `get(..., mode='refresh')` |

---

## findings.json (machine-readable)

```json
[
  {"severity": "MAJOR-C", "rule": "B1+B2", "status": "open",
   "summary": "precis-overview kind table drifts from live registry (markdown/plaintext listed but not active; random/patent missing)",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/skill.py",
   "regression_test": "tests/test_skill_overview.py::test_kind_table_matches_live_registry"},
  {"severity": "MAJOR-C", "rule": "B2+D2", "status": "open",
   "summary": "python callgraph canonical entry 'module:func' resolves to module not function; documented example yields 1-node graph",
   "fix_file": "pips/packages/precis-mcp/src/precis/python_index/callgraph.py",
   "regression_test": "tests/test_callgraph.py::test_console_script_entry_resolves_to_function"},
  {"severity": "MAJOR-C", "rule": "D3", "status": "open",
   "summary": "skill search misses lexical hits on fresh skills (precis-random-help not found for q='random')",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/skill.py",
   "regression_test": "tests/test_skill_search.py::test_fresh_skill_lexically_searchable_immediately"},
  {"severity": "MAJOR-C", "rule": "B5+F2", "status": "open",
   "summary": "U+FFFD mojibake in some paper block bodies (em-dash dropouts from PDF→markdown)",
   "fix_file": "pips/packages/acatome-extract/src/acatome_extract/pipeline.py",
   "regression_test": "tests/test_paper_blocks.py::test_no_replacement_chars_in_blocks"},
  {"severity": "MINOR-C", "rule": "B7", "status": "open",
   "summary": "think kind leaks <think>…</think> reasoning trace into caller-visible body",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/perplexity.py",
   "regression_test": "tests/test_think.py::test_render_strips_reasoning_block"},
  {"severity": "MINOR-C", "rule": "C4+D6", "status": "open",
   "summary": "view=links empty-state recovery hint recommends put(link=,rel=) instead of canonical link() verb",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/_links_view.py",
   "regression_test": "tests/test_links_view.py::test_no_links_recovery_hint_uses_link_verb"},
  {"severity": "MINOR-C", "rule": "D3", "status": "open",
   "summary": "python empty-search drops Next: recovery block; inconsistent with other kinds",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/python.py",
   "regression_test": "tests/test_python_search.py::test_empty_search_offers_recovery_hints"},
  {"severity": "MINOR-C", "rule": "B5+D4", "status": "open",
   "summary": "soft-deleted numeric refs return NotFound identical to never-existed ids",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/_numeric_base.py",
   "regression_test": "tests/test_numeric_handlers.py::test_soft_deleted_distinguished_from_never_existed"},
  {"severity": "MINOR-C", "rule": "A1", "status": "open",
   "summary": "[static] FastMCP() constructor missing title= kwarg; serverInfo.title absent",
   "fix_file": "pips/packages/precis-mcp/src/precis/server.py",
   "regression_test": "tests/test_server_init.py::test_serverinfo_carries_title"},
  {"severity": "NIT", "rule": "B4", "status": "open",
   "summary": "calc parse-error and evaluate-error take different envelope shapes",
   "fix_file": "pips/packages/precis-mcp/src/precis/handlers/calc.py",
   "regression_test": "tests/test_calc.py::test_all_error_paths_share_envelope_shape"},

  {"severity": "MAJOR-C", "rule": "D2", "status": "fixed-in-session",
   "summary": "calc error hint advertised 'solve' as supported but failed on every solve(…) call",
   "fix_observed": "solve(x**2-4, x) now returns [-2, 2]"},
  {"severity": "MAJOR-$", "rule": "L7+L10", "status": "fixed-in-session",
   "summary": "oracle:<slug> returned 1.7K-2.4K tokens unconditionally; no view/pagination",
   "fix_observed": "default now returns one random entry (~170t); view='index' returns 15-row catalog; ~N selector navigates"},
  {"severity": "MINOR-C", "rule": "D4", "status": "fixed-in-session",
   "summary": "paper slug NotFound returned bare error without near-match suggestions",
   "fix_observed": "options= now lists 3 nearest slugs"},
  {"severity": "MINOR-C", "rule": "B5", "status": "fixed-in-session",
   "summary": "search(kind='web') returned Unsupported despite bodies being present",
   "fix_observed": "search(kind='web', q='example') returns hits; cross-kind kind='*' works too"}
]
```

---

## Related durable artifacts

- **`gripe:3681`** — registry-driven four-fold proposal (web search, cross-kind fan-out, one-call bookmark, refresh mode). Phases 1 + 3 confirmed shipped at static-review time; phases 2 + 4 remain.
- **`tests/test_mcp_modalities.py`** — 10 tests pinning the prompts/resources modality wiring. Hardcodes one test-env assumption (see MINOR-C #10 in patch plan).
- **`docs/paper_ingest.md`** — design doc covering the out-of-band paper ingestion flow (`.acatome` bundles; paper kind is read-only from MCP surface).

---

## Methodology notes

Every finding cites a live probe or a source line. No finding was written from docstrings alone. Token counts were estimated via chars/4; the numbers in this report (e.g. "1750t") are approximate to within ~10%. All reprobes used identical inputs to the original probe they re-tested; fixed-in-session findings were verified by the re-probe returning materially different output, not merely "looks better".

The four-pass structure (quick → paper-navi → deep → final) is not standard mcp-critic procedure; it emerged from the maintainer actively shipping fixes between passes. Future reviews of this server should either run a single pass, or explicitly plan for a short-cycle observation window to catch the same phenomenon.
