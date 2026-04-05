# Precis

[![PyPI](https://img.shields.io/pypi/v/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/precis-mcp)](https://pypi.org/project/precis-mcp/)
[![License](https://img.shields.io/pypi/l/precis-mcp)](LICENSE)

Unified document MCP — read, write, search, and annotate any structured document.

## Four tools

```
search(query, top_k?, scope?)
get(id, grep?, depth?)
put(id, text?, mode?, tracked?, note?, link?)
move(id, after)
```

## URI grammar

```
scheme:path[›selector][/view[/subview]]
```

### Schemes

- `file:` — on-disk files, extension determines handler (`.docx`, `.tex`, `.md`, `.txt`)
- `paper:` — acatome paper store (pre-ingested PDFs)
- `todo:` — task management with state machine

### Examples

```python
read('paper:')                              # list all papers
read('paper:miller2023foo')                 # overview + abstract
read('paper:miller2023foo/toc')             # structure
read('paper:miller2023foo›38')              # chunk 38
read('paper:miller2023foo/cite/bib')        # BibTeX citation
read('file:planning.docx')                  # table of contents
read('file:planning.docx›KR8M2')            # node by slug
put('file:planning.docx›KR8M2', text='Revised text.', mode='replace')
```

## Output markers

```
=  verbatim (safe to quote)
~  derived (keywords/summary — not quotable)
%  annotation (user note/comment)
```

## Installation

```bash
pip install precis-mcp            # core + markdown + plaintext + latex
pip install precis-mcp[word]       # + DOCX support
pip install precis-mcp[paper]      # + paper store support
pip install precis-mcp[all]        # everything
```

## Plugin system

Register new schemes or file types via entry points:

```toml
[project.entry-points."precis.schemes"]
chem = "my_plugin:ChemHandler"

[project.entry-points."precis.file_types"]
".sdf" = "my_plugin:SDFParser"
```
