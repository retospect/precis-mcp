---
name: tex-workflow
description: >
  LaTeX editing workflow via the tex: scheme — reading nodes, replacing
  content with tracked-changes-style edits, adding .bib citations, raw
  line-range file access for non-node edits, and figure/label management.
user-invocable: true
allowed-tools: [get, put, search]
applies-to: [tex]
kind-onboarding: tex
tags: [latex, editing, papers]
---

## When to Use

- User asks you to edit a `.tex` source (paper, thesis chapter, slides)
- You see a `file.tex` in the working directory and need to modify sections
- You're adding citations to a `.bib` file
- You need to edit non-node content (preamble, macro definitions, commented-out blocks)

## The tex: URI grammar

```
file:paper.tex                    # table of contents
file:paper.tex›SLUG               # node (section / paragraph / equation)
file:paper.tex›S2.1               # section by hierarchical path
file:paper.tex›sec:methods        # node by LaTeX label
file:paper.tex›@main.tex:120..140 # RAW lines 120-140 of included file
file:paper.tex/meta               # document metadata
```

`SLUG` is a stable 5-char id assigned to every node at parse time.  Re-read the toc whenever the document structure might have changed — slugs can shift when content is inserted/deleted.

## Common workflows

### Read a section

```
get(id='file:paper.tex')              # toc first, always
get(id='file:paper.tex›S2.1')         # the section
get(id='file:paper.tex›38..42')       # chunks 38 through 42
```

### Replace a node

```
put(id='file:paper.tex›PLXDX', mode='replace', text='new paragraph text.')
```

The handler re-parses and returns the new slug (which may differ from the old one if the content restructured significantly).

### Add content after / before a node

```
put(id='file:paper.tex›PLXDX', mode='after', text='New paragraph goes here.')
put(id='file:paper.tex›PLXDX', mode='before', text='Intro paragraph.')
```

### Delete a node

```
put(id='file:paper.tex›PLXDX', mode='delete')
```

Surface the deleted slug in your reply so the user can undo.

### Add a .bib entry

The tex handler understands `[@key]: reference text` as a bibliography definition — it appends to the first `.bib` file declared by `\bibliography{}` or `\addbibresource{}`:

```
put(type='file', id='paper.tex',
    text='[@smith2024jacs]: Smith et al., "Title", JACS 146:12345 (2024). doi:...',
    mode='append')
```

If no `.bib` file is declared in the `.tex`, the handler raises `PARAM_INVALID` with a hint to add `\bibliography{refs}` first.

### Cite an entry in the body

Cite with `[@key]` inside any `mode='replace'` / `mode='after'` / `mode='append'` text:

```
put(id='file:paper.tex›PLXDX', mode='replace',
    text='We follow the approach of [@smith2024jacs] with modifications.')
```

The tex handler maps `[@key]` to `\cite{key}` on write.

### Raw line-range access (when nodes fail you)

When you need to edit macro definitions, preamble, or `\begin{document}` itself — things the parser doesn't treat as nodes — use the `@` prefix in the selector:

```
get(id='file:paper.tex›@main.tex:120..140')   # read lines 120-140
put(id='file:paper.tex›@main.tex:125',        # replace single line 125
    text='\\newcommand{\\foo}{bar}',
    mode='replace')
put(id='file:paper.tex›@main.tex:$',          # append to file
    text='% trailing comment',
    mode='append')
```

Path security: raw paths must stay inside the project directory. `@../../../etc/passwd` is rejected with `DENIED`.

## Figure & label workflow

LaTeX labels (`\label{fig:mof-synthesis}`) become slugs automatically.  Use them as selectors:

```
get(id='file:paper.tex›fig:mof-synthesis')    # read the figure env
put(id='file:paper.tex›fig:mof-synthesis',
    mode='replace',
    text='\\begin{figure}...\\caption{Updated caption.}...\\end{figure}')
```

## Rules

- **Always `get(id='file:paper.tex')` first** to refresh slugs.  Stale slugs error out with `ID_NOT_FOUND`.
- **Never edit generated files** — `.aux`, `.bbl`, `.log`, `.toc`.  These are LaTeX output and regenerate on `pdflatex`.
- **Don't invent BibTeX keys.**  Use `search(type='paper', query='...')` to find the canonical key from the precis library first; fall back to quest if the paper isn't ingested.
- **Track-changes isn't supported on .tex** (it's a Word-only feature).  For collaborative editing, use git, not `mode='comment'`.
