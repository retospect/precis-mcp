# Precis — Structured Document MCP Server

[![PyPI](https://img.shields.io/pypi/v/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![License](https://img.shields.io/pypi/l/precis-mcp)](LICENSE)

**Stop burning tokens on raw documents.** Precis is an [MCP server](https://modelcontextprotocol.io/) that gives AI agents structured, token-efficient access to DOCX, LaTeX, Markdown, scientific papers, a free local calculator (SymPy), and paid compute / web backends — read, write, search, and annotate through four simple tools.

Instead of dumping a 100k-token PDF into your context window, Precis lets your agent navigate to exactly the section it needs, read just that chunk, and move on. **One MCP server replaces the PDF-in-context anti-pattern** that burns through Claude, ChatGPT, and Cursor usage limits in minutes.

## Why Precis?

- **Slash context bloat** — Navigate by heading, grep for keywords, or read specific paragraphs by slug. No more feeding entire documents into the context window.
- **Write back, not just read** — Edit DOCX with tracked changes, insert LaTeX sections, append Markdown — all through the same `put()` tool. Your agent becomes a true co-author.
- **Semantic search over papers** — Vector search across thousands of ingested PDFs. Get the relevant chunk, not the whole paper.
- **One server, every format** — DOCX, LaTeX, Markdown, plaintext, papers, plus stateful kinds (todo, skill, memory, conversation, flashcard, quest), compute (calc, math), and external data (web, research, think, youtube). No format-specific plugins to juggle.
- **Token-aware output** — Automatic RAKE keyword summaries for large documents. Depth filtering, pagination, and adaptive truncation keep responses lean.
- **Cost-aware by default** — Every response ends with a `[cost: …]` footer and every failure uses the same `ERROR [<code>]:` envelope. Paid external calls (Perplexity, Wolfram) report per-call costs; free kinds say `free`.
- **Plugin architecture** — Add custom document types, URI schemes, or entire new kinds via Python entry points. Plugin protocol v2 with `KindSpec`, cost hints, and lifecycle hooks.

## Works with

Claude Desktop · Cursor · Windsurf · Cline · any MCP-compatible client

## Quick start

```bash
pip install precis-mcp           # core: Markdown, plaintext, LaTeX
pip install precis-mcp[word]     # + Word DOCX support
pip install precis-mcp[paper]    # + scientific paper library
pip install 'precis-mcp[calc]'   # + local SymPy calculator (offline)
pip install 'precis-mcp[all]'    # everything
```

Add to your MCP client config:

```json
{
  "mcpServers": {
    "precis": {
      "command": "precis"
    }
  }
}
```

That's it. Your agent now has structured document access.

## Four tools, zero sprawl

| Tool | What it does |
|------|-------------|
| `get(id)` | Read any document node — heading, paragraph, table, figure, chunk, citation |
| `put(id, text, mode)` | Write, replace, delete, annotate, or comment on any node |
| `search(query)` | Semantic search across your paper library or grep within a document |
| `move(id, after)` | Reorder sections and paragraphs within a document |

### get — Structured reading

Files auto-classify by extension; papers need either a `type='paper'`
hint, a scheme prefix (`paper:` / `doi:` / `arxiv:` / …), or a bare
DOI / arXiv id / PMCID / ISBN / ISSN that the classifier recognises.

```python
# Documents — extension auto-classifies
get(id='report.docx')                    # table of contents with slugs
get(id='report.docx›PLXDX')              # read paragraph by slug
get(id='report.docx', grep='methods')    # grep within document
get(id='report.docx', depth=2)           # outline: H1 + H2 only

# Papers — bare slugs need a routing hint
get(type='paper', id='wang2020state')            # overview + abstract
get(id='paper:wang2020state')                    # scheme prefix works too
get(type='paper', id='wang2020state›38')         # read chunk 38
get(type='paper', id='wang2020state›38..42')     # chunk range
get(type='paper', id='wang2020state/cite/bib')   # BibTeX citation
get(type='paper', id='wang2020state/fig/3')      # figure 3 with caption

# Structured identifiers — classifier routes without type=
get(id='doi:10.1021/jacs.2c01234')       # DOI prefix
get(id='10.1021/jacs.2c01234')           # bare DOI (auto-detected)
get(id='arxiv:2301.12345')               # arXiv id
get(id='pmcid:PMC1234567')               # PubMed Central
get(id='isbn:9780262533058')             # ISBN-13 with checksum validation

# Filter + list
get(type='paper', grep='year:2020-2024') # papers 2020–2024
get(type='paper', grep='tag:review')     # by tag

# Compute — calc is free local SymPy; math is paid Wolfram Alpha
get(type='calc', id='2+3*4')                    # exact arithmetic
get(type='calc', id='integrate(sin(x), x)')     # symbolic calculus
get(type='calc', id='0xff')                     # base conversion
get(type='math', id='population of Ireland')    # world-data lookups
get(type='math', id='orbital period of Jupiter')

# Other kinds
get(type='todo', id='/recent')           # recent todos
get(type='skill', id='find-paper')       # read a skill body
get(type='quest', id='/recent')          # paper-request backlog
```

### put — Write and annotate

```python
# Document writes (tracked changes on DOCX by default)
put(id='report.docx', text='## Methods\n\nWe used...', mode='append')
put(id='report.docx›PLXDX', text='Revised paragraph.', mode='replace')
put(id='report.docx›PLXDX', text='Needs citation.', mode='comment')
put(id='report.docx›PLXDX', mode='delete')

# Paper annotations + cross-kind links
put(type='paper', id='wang2020state', note='Key finding about selectivity')
put(type='paper', id='wang2020state', link='jones2023surface:cites')

# Create a new todo, memory, skill, …
put(type='todo', title='Review PR #432', text='priority: high')
put(type='memory', title='grimoire-rule-42', text='...')
put(type='skill', title='my-skill', text='# When to use\n...')
```

### search — Find what matters

```python
# Paper vector search
search(type='paper', query='CO2 capture metal-organic frameworks')
search(query='selectivity', scope='wang2020state')              # scope infers type
search(query='thermal stability', scope='chapter3.tex')         # search within a doc

# Grep-filtered vector search — metadata pre-filter + semantic ranking
search(type='paper', query='membrane', grep='tag:review')
search(type='paper', query='catalysis', grep='year:2020-2024')

# Compute
search(type='calc', query='integrate sin(x)*cos(x) dx')         # free, offline
search(type='math', query='speed of light in km/h')             # Wolfram Alpha

# External data
search(type='websearch', query='latest on perovskite solar cells')    # Perplexity Sonar
search(type='research', query='mechanistic review of …')        # deep research

# Other stateful kinds
search(type='skill', query='acquire paper')                     # find-paper skill
search(type='todo', query='priority:high')
search(type='memory', query='design decision')
```

## Supported kinds

Precis 4.1 ships 17 built-in kinds across four families.  Add the
`type=` kwarg to disambiguate when the identifier alone doesn't imply a
kind.

### Document kinds (file-backed)

| Kind | Read | Write | Track changes | Comments | Extras |
|------|------|-------|---------------|----------|--------|
| **DOCX** | ✓ | ✓ | ✓ | ✓ margin comments | Citations, tables, lists, bibliography |
| **LaTeX** | ✓ | ✓ | — | — | Multi-file projects, .bib parsing, equations, figures, raw file access |
| **Markdown** | ✓ | ✓ | — | — | Headings, code blocks, tables, lists. Zero deps |
| **Plaintext** | ✓ | ✓ | — | — | Paragraph-based. Zero deps |

### Stateful kinds (corpus-backed — `acatome-store`)

| Kind | What it is | Views |
|------|-----------|-------|
| **paper** | Scientific papers with chunks, figures, citations | `/toc`, `/abstract`, `/summary`, `/cite/<style>`, `/fig`, `/links` |
| **todo** | Task management with state machine | `/recent`, `/pending`, `/in-progress`, `/done`, `/blocked` |
| **skill** | Filesystem-indexed SKILL.md library | `/kind/<name>`, `/topic/<tag>`, `/recent`, `/help` |
| **memory** | Long-term verbatim agent drawers | `/recent`, `/topic/<tag>`, `/links` |
| **conversation** | Recorded agent conversations | `/recent`, `/agent/<id>`, `/links` |
| **flashcard** | SM-2 spaced-repetition cards | `/due`, `/new`, `/learning` |
| **quest** | Paper-request lifecycle (Postgres-backed) | `/recent`, `/queued`, `/needs-user`, `/failed`, `/agent/<id>` |

### Compute kinds

| Kind | Upstream | Cost hint | Env |
|------|----------|-----------|-----|
| **calc** | Local SymPy — exact arithmetic, calculus, linear algebra, units | free | — |
| **math** | Wolfram Alpha — natural-language math, world-data, fuzzy queries | `~$0.0001/call` | `WOLFRAM_APP_ID` |

`calc` is the default for pure computation (arithmetic, symbolic
algebra, calculus, base conversions, matrix ops, unit conversions).
It's free, offline, and deterministic.  Reach for `math` when the
query needs real-world data lookup or when `calc`'s parser can't
handle natural-language phrasing.

### External-data kinds (live APIs)

| Kind | Upstream | Cost hint | Env |
|------|----------|-----------|-----|
| **web** | Perplexity Sonar web search | `~$0.005/call` | `PERPLEXITY_API_KEY` |
| **research** | Perplexity Sonar deep-research | `~$0.04/call` | `PERPLEXITY_API_KEY` |
| **think** | Perplexity Sonar reasoning | `~$0.02/call` | `PERPLEXITY_API_KEY` |
| **youtube** | YouTube transcripts | free | — |

## URI grammar

```
id = [scheme:]path[›selector[,selector...]][/view[/subview]]
```

Precis classifies the identifier in this order:

1. **Explicit scheme prefix** (`paper:`, `doi:`, `arxiv:`, `pmcid:`,
   `isbn:`, `issn:`, `todo:`, `skill:`, `quest:`, `memory:`,
   `conversation:`, `flashcard:`, `websearch:`, `research:`, `think:`,
   `math:`, `calc:`, `youtube:`).
2. **File extension** (`.docx`, `.tex`, `.md`, `.txt`) — routes to the
   matching file handler.
3. **Structured identifier patterns** — bare DOI (`10.1234/…`), arXiv
   id (`2301.12345`), PMCID (`PMC1234567`), ISBN-10/13 (with full
   checksum validation), ISSN (mod-11 checksum).
4. **Otherwise** — the caller must supply a `type=` hint.  Bare slugs
   without any hint emit `KIND_UNKNOWN` with a list of registered
   kinds as `options:`.

## How it saves tokens

**The problem**: Feeding a raw 4,500-word PDF to Claude burns ~100,000 tokens. Every follow-up message resends the entire conversation history, compounding the waste. This is the #1 cause of hitting usage limits fast.

**The fix**: Precis parses documents into a navigable tree of headings, paragraphs, tables, and figures. Your agent reads the table of contents (tiny), drills into the section it needs (small), and gets exactly the paragraph it wants (minimal). Total tokens: a fraction of the raw dump.

| Approach | Tokens for a 30-page paper |
|----------|---------------------------|
| Raw PDF in context | ~100,000 |
| Precis: TOC → section → paragraph | ~2,000–5,000 |

For large documents (100+ nodes), Precis automatically returns headings-only and lets the agent drill in. RAKE keyword extraction provides compressed summaries for scanning without reading full text.

## Output markers

Every line of output is prefixed with a provenance marker:

| Marker | Meaning | Safe to quote? |
|--------|---------|----------------|
| `=` | Verbatim text from the document | ✓ |
| `~` | Derived (keywords, summary) | ✗ |
| `%` | Annotation (user note or comment) | context-dependent |

## Error envelope

Every failure emits the same shape — easy to pattern-match in agent
loops:

```
ERROR [<code>]: <one-line summary>
  where: type='…' verb='…' id='…'
  cause: <concrete reason>
  options: <comma-separated alternatives>
  next: <one concrete action>
```

Codes are the 16-entry `ErrorCode` enum (`KIND_UNKNOWN`,
`ID_NOT_FOUND`, `PARAM_INVALID`, `VIEW_UNKNOWN`, `UPSTREAM_ERROR`,
`UNAVAILABLE`, `TIMEOUT`, `RATE_LIMITED`, `DENIED`, `UNEXPECTED`, …).
Codes that aren't the agent's fault (`UNEXPECTED`, `TIMEOUT`,
`UPSTREAM_ERROR`, `RATE_LIMITED`, `UNAVAILABLE`) auto-append a
gripe-next-hint so agents know when to retry vs. when to escalate.

## Cost reporting

Every tool response ends with:

```
[cost: free]            # or ~$0.005/call, ~$0.04/call, etc.
```

The `stats()` tool exposes per-kind session stats (calls, errors,
last-cost) and startup warnings.  Cost resolution is three-level:
per-call `Handler.cost_of()` → static `KindSpec.cost_hint` → `"free"`.

## Agent-visibility masking

Set `PRECIS_KINDS` to restrict the kinds an agent can see:

```bash
PRECIS_KINDS='paper,todo[search,get]'   # paper (all verbs), todo (read-only)
PRECIS_KINDS='paper,skill'              # two kinds, all verbs
PRECIS_KINDS=''                         # empty — unset to see everything
```

Bracket grammar supports per-verb restriction.  Alias-in-config,
unknown-verb, duplicate-kind, and stray-bracket issues raise
`ConfigError` at startup; unknown kinds are dropped with a warning so
the server still boots.

## Plugin system

Extend Precis with new document types, URI schemes, or entire new
kinds:

```toml
[project.entry-points."precis.schemes"]
chem = "my_plugin:ChemHandler"

[project.entry-points."precis.file_types"]
".sdf" = "my_plugin:SDFParser"
```

Implement the `Handler` protocol (a `read()` method plus optional
`put()`/`move()`) and declare a `KindSpec` for agent-visible metadata
(description, aliases, required env vars, cost hint, examples).
Precis discovers plugins at startup and fails fast on kind-name
collisions.  See
[`docs/plugin-architecture.md`](docs/plugin-architecture.md) for the
full spec.

## License

GPL-3.0-or-later
