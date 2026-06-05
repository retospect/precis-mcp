---
id: precis-tex-help
title: precis — read and edit LaTeX files
applies-to: get/search/put/edit/delete (kind='tex')
status: active
---

# precis-tex-help — `.tex` files, section-aware

For shared address grammar, write modes, and root config, read
`precis-files-help` first. This skill covers what's tex-specific:
section-aware blocks, the recursive `/toc`, and literal-source edits
against LaTeX syntax.

## What does a tex id look like?
## How do I address a .tex file or one of its parts?
## Path form vs slug — what's the difference?

```python
get(kind='tex', id='chapters/intro.tex')        # path form
get(kind='tex', id='chapters--intro')           # slug form (path / → --)
get(kind='tex', id='chapters/intro.tex~3')      # block by pos
get(kind='tex', id='chapters/intro.tex~kinetics')   # block by name
get(kind='tex', id='chapters/intro.tex~L42-58') # block by line range
get(kind='tex', id='chapters/intro.tex/toc')    # TOC view
get(kind='tex', id='chapters/intro.tex/raw')    # full source
get(kind='tex')                                 # index of all .tex files
```

Path form and slug form are interchangeable. Block selectors come in
two tracks: durable names (`~kinetics`) survive edits above; line
coordinates (`~L42-58`) follow IDE/grep output.

## What counts as a block?

A block boundary is created by either:

1. A blank line (paragraph break).
2. A sectioning command: `\part`, `\chapter`, `\section`,
   `\subsection`, `\subsubsection`, `\paragraph`, `\subparagraph`.

The sectioning command is its own one-line block, so a heading can be
edited without touching the body. Each block records its section
ancestry — search results render "hit in Methods > Kinetics".

```latex
\section{Methods}                ← block 0 (heading)

We measured \(k_{\text{cat}}\) using the protocol of \citep{Smith2020}.
                                  ← block 1 (paragraph)

\subsection{Kinetics}            ← block 2 (heading)

Fitting Michaelis–Menten gave \(K_M = 1.2\) mM.
                                  ← block 3 (paragraph, ancestry: Methods > Kinetics)
```

Source is preserved verbatim — no macro expansion, no environment
grouping. `\begin{equation}...\end{equation}` stays in one block only
if it has no internal blank lines. `\cite{...}` keys are opaque text;
for citation-graph navigation use `kind='paper'`.

## Inspect a project's structure
## See the section hierarchy across included files
## What sections does main.tex contain?

```python
get(kind='tex', id='main.tex', view='toc')
get(kind='tex', id='main.tex', view='outline')   # headings only, no excerpts
get(kind='tex', id='main.tex/toc')               # path form
```

The TOC walks sections in source order. When it hits `\input{path}`
or `\include{path}`, it resolves the target relative to the parent's
directory, ingests it lazily, and inlines its sections at the right
indent. Cycles terminate with a `⇺` marker. Targets outside
`PRECIS_ROOT` show as `not found`. `\subfile{...}` is not followed.

```text
# TOC: main

- \section{Introduction}  (`main~introduction-...`)
  ⤷ \input{chapters/methods} → chapters--methods
    - \section{Methods}  (`chapters--methods~methods-...`)
      - \subsection{Kinetics}  (`chapters--methods~kinetics-...`)
  ⤷ \input{chapters/results} → chapters--results
    - \section{Results}  (`chapters--results~results-...`)
- \section{Conclusion}  (`main~conclusion-...`)
```

Each backticked handle is a real address. Paste it as `id=` to read
that block.

Views: `toc` (sections + excerpts, recursive across `\input`),
`outline` (headings only), `raw` (full source).

## Drill into part of a file with a sub-TOC

```python
get(kind='tex', id='<slug>~Methods', view='toc')   # TOC of one section
get(kind='tex', id='<slug>~L100-300', view='toc')  # TOC of a line range
```

Same shape as the file-level TOC, scoped to one section or range.

## Search across the project

```python
search(kind='tex', q='activation energy')
search(kind='tex', q='kcat', scope='chapters--intro')   # one file
search(q='activation energy')                           # cross-kind
```

Hybrid lexical + semantic. Results are `<slug>~<block>` handles;
order is the relevance signal.

## Edit literal LaTeX source

`find=` is a literal substring match, so LaTeX control sequences
work directly.

```python
edit(kind='tex', id='chapters/intro.tex',
     mode='find-replace',
     find=r'\citep{Smith2020}',
     text=r'\citep{Smith2020,Jones2021}')
```

## Strip a single command without rewriting the paragraph

`text=''` is the canonical span-delete.

```python
edit(kind='tex', id='chapters/intro.tex',
     mode='find-replace',
     find=r'\todo{check this}',
     text='')
```

## Create a new .tex file

```python
put(kind='tex', id='chapters/discussion.tex',
    text=r'''\section{Discussion}

Our results corroborate \citet{Smith2020}, but extend the operating
window from 5 to 25 bar.''',
    mode='create')
```

## See also

```python
get(kind='skill', id='precis-files-help')       # shared address grammar, write modes
get(kind='skill', id='precis-edit-help')        # find-replace + insert grammar
get(kind='skill', id='precis-plaintext-help')   # block grammar tex extends
get(kind='skill', id='precis-paper-help')       # citation-graph navigation
get(kind='skill', id='precis-markdown-help')    # .md block grammar for prose notes
```
