# Precis v2

Unified document MCP — read, write, search, and annotate any structured document.

## Two tools

```
read(uri, query?, summarize?, depth?, page?)
put(uri, text, mode?, tracked?)
```

## URI grammar

```
scheme:path[#selector][/view[/subview]]
```

### Schemes

- `file:` — on-disk files, extension determines handler (`.docx`, `.tex`)
- `paper:` — acatome paper store (pre-ingested PDFs)

### Examples

```python
read('paper:')                              # list all papers
read('paper:miller2023foo')                 # overview + abstract
read('paper:miller2023foo/toc')             # structure
read('paper:miller2023foo#38')              # chunk 38
read('paper:miller2023foo/cite/bib')        # BibTeX citation
read('file:planning.docx')                  # table of contents
read('file:planning.docx#KR8M2')            # node by slug
put('file:planning.docx#KR8M2', text='Revised text.', mode='replace')
```

## Output markers

```
=  verbatim (safe to quote)
~  derived (keywords/summary — not quotable)
%  annotation (user note/comment)
```

## Installation

```bash
pip install precis[all]       # everything
pip install precis[word]      # DOCX support only
pip install precis[paper]     # paper store support only
```

## Plugin system

Register new schemes or file types via entry points:

```toml
[project.entry-points."precis.schemes"]
chem = "my_plugin:ChemHandler"

[project.entry-points."precis.file_types"]
".sdf" = "my_plugin:SDFParser"
```
