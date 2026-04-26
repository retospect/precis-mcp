# Phase 3.5 — Navigation parity for `paper`

> Status: **queued** (after phase 4). Phase 3 shipped a working `paper`
> kind; this mini-spike restores the v1 navigation experience that
> made precis distinctive in the first place.

## Why this is its own phase

Phase 3's `PaperHandler` exposes the data correctly (overview, chunks,
citations, RRF search) but the **navigation** feels flat compared to
v1: the `view='toc'` returns 177 lines of "block 0, block 1, …, block
176" instead of v1's 12-section hierarchical jump table. The "Next:"
hint trailer that taught agents how to drill down is mostly absent.

The user's framing: *"the core value (at long time ago) was to do this
excellent navigation"*. So this lands before any new kind.

## Three deliverables — ~150 LOC, ~20 tests, half a day

### 1. Hierarchical TOC view

`view='toc'` produces a section tree, not a flat list.

**Heading detection** (cheap regex on block text):

| Pattern | Level | Example block |
|---|---|---|
| `^■\s*\*\*([A-Z ]+)\*\*$` | H1 | `■ **METHODS**` |
| `^\*\*([A-Z][^*]+)\*\*$` | H2 | `**Heterodiatomic Molecules**` |
| `^#{1,6}\s+\S` | H1-6 | (markdown, fallback) |

**Output format** (matches v1's look):

```
# acheson2026automated — TOC (177 blocks, 12 sections)

  ~0..7    (8)   <untitled overview>
  ~8..20   (13)  ■ INTRODUCTION
  ~21..40  (20)  ■ THEORY
  ~41..73  (33)  ■ METHODS
    ~43..53 (11)   Physics-Informed Program Synthesis [PIPS]
    ~54..58 (5)    Calculation Details
    ~59..63 (5)    Heterodiatomic Molecules
    ~64..73 (10)   Alkanes
  ~74..116 (43)  ■ RESULTS & DISCUSSION
    ~76..96 (21)   Application to Heterodiatomic Molecules
    ~97..116(20)   Application to Alkanes
  ~117..123(7)   ■ CONCLUSIONS
  ~124..129(6)   ■ ASSOCIATED CONTENT
  ~130..139(10)  ■ AUTHOR INFORMATION
  ~140..177(38)  ■ REFERENCES

Next:
  get(kind='paper', id='<slug>~21..40/toc')   — drill into theory section
  get(kind='paper', id='<slug>~21..40')       — read theory section blocks
  get(kind='paper', id='<slug>', view='bibtex')
```

### 2. `_format_next_block` formatter + Next: trailers everywhere

V1's signature touch: every response ends with column-aligned
suggestions. Port `_format_next_block(calls: list[tuple[str, str]])`
from v1 (see `pips/packages/precis-mcp/src/precis/handlers/paper.py:203`)
into a shared helper at `src/precis/utils/next_block.py`.

Apply to every `PaperHandler` view:

- overview → `view='toc'`, `view='bibtex'`, `search(...)`
- chunks → adjacent chunks, parent section, citation
- toc → drill into largest section, switch to bibtex
- bibtex/ris/endnote → other formats, back to overview
- abstract → toc, citation, search

### 3. Section drill-down via `~A..B/toc`

`get(kind='paper', id='slug~46..105/toc')` re-renders the TOC scoped
to the given range. Recursive: each child section is itself
addressable. Composes from feature 1 + existing range slicing.

## Out of scope (defer to later phases)

- **Density skim views** (`view='representatives'`/`echoes`/`coverage`)
  — phase 5 polish; needs density-tag wiring first.
- **Figure handling** (`/fig/N`, `/fig/N/legend`, `/fig/N/image`)
  — phase 6 (file-handler land); requires acatome-extract figure
  surfacing in bundles.
- **Page-aware navigation** (`/page/N`) — phase 6.
- **Citation graph** (`/cites`, `/cited-by`) — phase 4 (cache-backed
  S2 lookups).
- **`put(kind='paper', tags=…)`** — phase 5 (ref-handler base class).

## Test plan

- `tests/test_paper_toc.py` — 8 tests:
  heading detection on synthetic block sets, range-scoped toc, deep
  nesting, no-headings fallback (returns flat list with note),
  unicode in heading names.
- `tests/test_next_block.py` — 4 tests: column alignment, empty list,
  long calls / short descs, edge cases.
- `tests/test_paper.py` extensions — 6 tests covering trailers on each
  PaperHandler view.

## Suggested commit sequence

1. `utils/next_block.py` + tests
2. `PaperHandler._render_toc` rewrite + tests
3. Range-scoped toc (tiny diff, big UX win)
4. Sprinkle Next: blocks across remaining views
5. Update `precis-paper-help.md` skill draft to highlight drill-down

## Done criteria

A fresh agent running `get(kind='paper', id='wang2020state')` then
`get(kind='paper', id='wang2020state', view='toc')` should be able to
navigate to a specific section and read it without ever asking the
user — purely from the Next: hints.
