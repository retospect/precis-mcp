# precis-mcp

A [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that gives LLMs compressed, structured context for `.docx` and `.tex` documents. It maintains a heading tree with RAKE keyword summaries so the LLM can navigate and edit documents without flooding context.

## Features

- **5 tools** — `activate`, `toc`, `get`, `put`, `move`
- **Dual addressing** — 5-char content-hash slugs + positional heading paths
- **RAKE keyword extraction** — stateless, zero-dependency precis generation (<5ms)
- **DOCX citations** — `[@key]` round-trip with styled hyperlinks and bibliography entries
- **Track changes** — `put()` writes Word revision markup by default
- **LaTeX support** — `\input`/`\include` resolution, `\label{}` aliases, `.bib` parsing
- **Atomic I/O** — every call reads fresh from disk, no stale state

## Installation

```bash
uv pip install -e ".[dev]"
```

## Usage

Run as an MCP server:

```bash
precis
```

Or use with an MCP client:

```python
from precis.tools import Session, activate, toc, get, put

session = Session()
await activate(session, "paper.docx")
await toc(session)
await get(session, id="KR8M2")
await put(session, id="KR8M2", text="Updated text.", mode="replace")
```

## Configuration

`~/.config/precis/precis.toml`:

```toml
[precis]
author = "precis"   # track-changes author name (DOCX only)
```

## Testing

```bash
uv run python -m pytest tests/ -v
```

## License

MIT — see [LICENSE](LICENSE).
