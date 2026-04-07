# Precis — Structured Document MCP Server

[![PyPI](https://img.shields.io/pypi/v/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![License](https://img.shields.io/pypi/l/precis-mcp)](LICENSE)

**Stop burning tokens on raw documents.** Precis is an [MCP server](https://modelcontextprotocol.io/) that gives AI agents structured, token-efficient access to DOCX, LaTeX, Markdown, and scientific papers — read, write, search, and annotate through four simple tools.

Instead of dumping a 100k-token PDF into your context window, Precis lets your agent navigate to exactly the section it needs, read just that chunk, and move on. **One MCP server replaces the PDF-in-context anti-pattern** that burns through Claude, ChatGPT, and Cursor usage limits in minutes.

## Why Precis?

- **Slash context bloat** — Navigate by heading, grep for keywords, or read specific paragraphs by slug. No more feeding entire documents into the context window.
- **Write back, not just read** — Edit DOCX with tracked changes, insert LaTeX sections, append Markdown — all through the same `put()` tool. Your agent becomes a true co-author.
- **Semantic search over papers** — Vector search across thousands of ingested PDFs. Get the relevant chunk, not the whole paper.
- **One server, every format** — DOCX, LaTeX (multi-file projects), Markdown, plaintext, and scientific papers. No format-specific plugins to juggle.
- **Token-aware output** — Automatic RAKE keyword summaries for large documents. Depth filtering, pagination, and adaptive truncation keep responses lean.
- **Plugin architecture** — Add custom document types or URI schemes via Python entry points.

## Works with

Claude Desktop · Cursor · Windsurf · Cline · any MCP-compatible client

## Quick start

```bash
pip install precis-mcp          # core: Markdown, plaintext, LaTeX
pip install precis-mcp[word]     # + Word DOCX support
pip install precis-mcp[paper]    # + scientific paper library
pip install precis-mcp[all]      # everything
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

```python
get(id='report.docx')                    # table of contents with slugs
get(id='report.docx›PLXDX')             # read specific paragraph by slug
get(id='report.docx', grep='methods')    # find all nodes matching 'methods'
get(id='report.docx', depth=2)           # outline: H1 + H2 only
get(id='wang2020state')                  # paper overview + abstract
get(id='wang2020state›38')              # read chunk 38 of a paper
get(id='wang2020state›38..42')          # read chunk range
get(id='wang2020state/cite/bib')         # BibTeX citation
get(id='wang2020state/fig/3')            # figure 3 with caption
get(id='doi:10.1021/jacs.2c01234')       # lookup by DOI
get(id='arxiv:2301.12345')               # lookup by arXiv ID
get(grep='year:2020-2024')               # filter papers by date
```

### put — Write and annotate

```python
put(id='report.docx', text='## Methods\n\nWe used...', mode='append')
put(id='report.docx›PLXDX', text='Revised paragraph.', mode='replace')  # tracked changes
put(id='report.docx›PLXDX', text='Needs citation.', mode='comment')     # margin comment
put(id='report.docx›PLXDX', mode='delete')
put(id='wang2020state', note='Key finding about selectivity')            # paper annotation
put(id='wang2020state', link='jones2023surface:cites')                   # link papers
```

### search — Find what matters

```python
search(query='CO2 capture metal-organic frameworks')         # semantic search
search(query='selectivity', scope='wang2020state')           # search within one paper
search(query='thermal stability', scope='chapter3.tex')      # search within a doc
```

## Supported formats

| Format | Read | Write | Track changes | Comments | Extras |
|--------|------|-------|---------------|----------|--------|
| **DOCX** | ✓ | ✓ | ✓ | ✓ margin comments | Citations, tables, lists, bibliography |
| **LaTeX** | ✓ | ✓ | — | — | Multi-file projects, .bib parsing, equations, figures, raw file access |
| **Markdown** | ✓ | ✓ | — | — | Headings, code blocks, tables, lists. Zero deps |
| **Plaintext** | ✓ | ✓ | — | — | Paragraph-based. Zero deps |
| **Papers** | ✓ | notes | — | — | Semantic search, figures, citations, Semantic Scholar graph |

## URI grammar

```
id = path[›selector][/view[/subview]]
```

Precis auto-detects the scheme from the identifier:
- File extension (`.docx`, `.tex`, `.md`, `.txt`) → file handler
- `doi:`, `arxiv:` prefix → paper lookup
- Bare DOI pattern (`10.1234/...`) → auto-detected
- Everything else → paper slug

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

## Plugin system

Extend Precis with new document types or URI schemes:

```toml
[project.entry-points."precis.schemes"]
chem = "my_plugin:ChemHandler"

[project.entry-points."precis.file_types"]
".sdf" = "my_plugin:SDFParser"
```

Implement the `Handler` protocol (just a `read()` method) and register via entry points. Precis discovers plugins at startup.

## License

GPL-3.0-or-later
