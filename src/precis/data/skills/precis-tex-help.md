---
id: precis-tex-help
title: precis — read and edit LaTeX files
status: active
tier: 1
floor: any
applies-to: get/search/put/edit/delete (kind='tex')
last-updated: 2026-05-02
---

# precis-tex-help — `.tex` files

For shared concepts (address grammar, two-track addressing, root
config, write modes, reverse lookups) read `precis-files-help`
first. The `tex` kind is a section-aware sibling of `plaintext` —
same edit / put / delete / tag / link surface, plus a sectioning-
aware block grammar and a recursive `/toc` view.

> **Status:** shipped, gated on `PRECIS_ROOT` (shared with `markdown`
> and `plaintext`). Section-aware block grammar: `\section`,
> `\subsection`, `\subsubsection`, `\paragraph`, `\chapter`, `\part`
> drive block boundaries. The `/toc` view recursively expands
> `\input{}` and `\include{}` so a TOC of `main.tex` shows sections
> from every included file inline at the inclusion point. Source
> text is preserved verbatim — no macro expansion, no environment
> grouping. Anchored edits work against the literal characters.

## When to use this kind

- You're editing a paper / chapter / lab notebook in LaTeX.
- You want `find-replace` against literal LaTeX source (e.g. fix a
  typo inside `\citep{Smith2020}`).
- You want `search(kind='tex', q='...')` over your project's `.tex`
  files alongside your `.md` notes.

If you want **bibliography / citation-graph navigation**, that's the
`paper` kind — it knows about DOI / authors / abstracts. The `tex`
kind treats `\cite{}` as opaque source text.

## Block grammar

A block boundary is created by **either**:

1. A blank line (paragraph break, like `plaintext`).
2. A sectioning command (`\part`, `\chapter`, `\section`,
   `\subsection`, `\subsubsection`, `\paragraph`, `\subparagraph`).

The sectioning command line is always its **own** one-line block —
so you can edit a heading without touching the body that follows.
Within a section, paragraphs are still split on blank lines, so
editing granularity stays paragraph-sized.

```latex
\section{Methods}                ← block 0 (heading, one line)

We measured \(k_{\text{cat}}\) using the protocol of \citep{Smith2020}.
The activation energy was $E_a = 42 \pm 3$ kJ/mol.
                                  ← block 1 (paragraph in Methods)

\subsection{Kinetics}            ← block 2 (heading)

Fitting Michaelis–Menten gave \(K_M = 1.2 \pm 0.3\) mM.
                                  ← block 3 (paragraph in Methods/Kinetics)
```

Block slugs are content-derived (first ~5 words + 6-hex hash), so
edits to one block leave other slugs stable. Each block also
records its **section ancestry** in `meta.section_path` (a list of
`[level, title]` pairs from outer to inner), and any
`\input{...}` / `\include{...}` arguments it contains in
`meta.inputs`. Search-result rendering can show "hit in Methods >
Kinetics" without re-parsing.

## Address shapes

```python
get(kind='tex', id='chapters--intro')                # overview
get(kind='tex', id='chapters--intro~0')              # one block by pos
get(kind='tex', id='chapters--intro~SLUG')           # one block by slug
get(kind='tex', id='chapters--intro/raw')            # full source
get(kind='tex', id='chapters--intro/toc')            # table of contents
get(kind='tex', id='chapters--intro', view='toc')    # equivalent
get(kind='tex')                                      # index of all .tex files
```

Path segments are encoded as `--` (so `chapters/intro.tex` becomes
`chapters--intro`), matching the rest of the prose-file kinds.

## Recipes

### Drop a chapter into the corpus

```python
put(kind='tex', id='chapters/discussion',
    text=r'''\section{Discussion}

Our results corroborate \citet{Smith2020}, but extend the operating
window from 5 to 25 bar.''',
    mode='create')
```

### Surgical edit against literal LaTeX

Same protocol as `plaintext` — see `precis-edit-protocol`. The `find=`
anchor is a literal substring match, so `\citep{...}` works.

```python
edit(kind='tex', id='chapters/intro',
     mode='find-replace',
     find=r'\citep{Smith2020}',
     text=r'\citep{Smith2020,Jones2021}')
```

### Delete a matched span in place

`text=''` is the canonical span-delete idiom. Use it to strip one
cite, one footnote, or one `\todo{...}` without rewriting the
surrounding paragraph.

```python
edit(kind='tex', id='chapters/intro',
     mode='find-replace',
     find=r'\todo{check this}',
     text='')   # empty text = delete
```

### Search across the project

```python
search(kind='tex', q='activation energy')
search(kind='tex', q='kcat', scope='chapters--intro')   # one file
```

Cross-kind search picks `tex` up automatically:

```python
search(q='activation energy')   # markdown + tex + paper + ...
```

### Inspect the project structure with `/toc`

```python
get(kind='tex', id='main/toc')
```

The TOC walks `main.tex`'s sections in source order and, whenever
it hits a `\input{path}` / `\include{path}`, fetches the target
`.tex` file (resolved relative to the parent's directory, gated by
`PRECIS_ROOT`), ingests it lazily, and inlines its sections at the
correct indent. Cycles (e.g. `a → b → a`) terminate with a `⇺`
marker rather than recursing forever; targets that resolve outside
`PRECIS_ROOT` are reported as `not found` so secret files can't
leak into the TOC.

Example output:

```
# TOC: main

- \section{Introduction}  (`main~introduction-...`)
  ⤷ \input{chapters/methods} → chapters--methods
    - \section{Methods}  (`chapters--methods~methods-...`)
      - \subsection{Kinetics}  (`chapters--methods~kinetics-...`)
  ⤷ \input{chapters/results} → chapters--results
    - \section{Results}  (`chapters--results~results-...`)
- \section{Conclusion}  (`main~conclusion-...`)
```

Each handle in backticks is a real address — paste it back to
`get(kind='tex', id='...')` to read that block.

## Limits

- **No macro expansion.** `\newcommand` definitions are opaque
  source. The agent reads the literal characters, which is what
  you want for surgical edits.
- **No environment grouping.** `\begin{equation}…\end{equation}`
  is split on blank lines and sectioning, not on environment
  boundaries. Most environments stay in one block by accident
  (no internal blank lines), but it isn't guaranteed.
- **No bibliography integration.** `\cite{}` keys are opaque text;
  for citation-graph queries use `kind='paper'`.
- **No `\subfile{...}` package** — only `\input{}` and `\include{}`
  are followed by `/toc`.

## See also

- `precis-files-help` — shared addressing model for all file kinds
- `precis-plaintext-help` — block grammar superset (tex extends it)
- `precis-edit-protocol` — universal anchored-edit grammar
- `precis-paper-help` — citation-graph navigation for cited papers
- `precis-markdown-help` — markdown block grammar for prose notes
