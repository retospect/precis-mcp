---
id: precis-tex-help
title: precis — read and edit LaTeX files
status: phase-6a
tier: 1
floor: any
applies-to: get/search/put/edit/delete (kind='tex')
last-updated: 2026-05-02
---

# precis-tex-help — `.tex` files

For shared concepts (address grammar, two-track addressing, multi-
root config, write modes, reverse lookups), read `precis-files-help`
first. For block grammar specifics, read `precis-plaintext-help` —
the `tex` kind is currently a thin subclass of `plaintext` and shares
its parser.

> **Status:** shipped, gated on `PRECIS_TEX_ROOT`. **First-cut block
> grammar**: paragraphs separated by blank lines, identical to
> `plaintext`. LaTeX sectioning (`\section`, `\begin{...}`, etc.) is
> **not** parsed yet — the source is stored verbatim and embedded as-
> is. The agent reads LaTeX fluently, so this works for the common
> case (papers under edit). A future refinement can add sectioning
> awareness without changing the public surface.

## When to use this kind

- You're editing a paper / chapter / lab notebook in LaTeX.
- You want `find-replace` against literal LaTeX source (e.g. fix a
  typo inside `\citep{Smith2020}`).
- You want `search(kind='tex', q='...')` over your project's `.tex`
  files alongside your `.md` notes.

If you want **bibliography / citation-graph navigation**, that's the
`paper` kind — it knows about DOI / authors / abstracts. The `tex`
kind treats `\cite{}` as opaque source text.

## Block grammar (inherited from plaintext)

One block = one paragraph (a run of non-blank lines separated from
its neighbours by at least one blank line). LaTeX commands inside a
paragraph are preserved verbatim:

```latex
\section{Methods}            ← block 0 (one-line paragraph)

We measured \(k_{\text{cat}}\) using the protocol of \citep{Smith2020}.
The activation energy was $E_a = 42 \pm 3$ kJ/mol.
                              ← block 1 (multi-line paragraph)
```

Block slugs are content-derived (first ~5 words + 6-hex hash), so
edits to one paragraph leave other slugs stable.

## Address shapes

```python
get(kind='tex', id='chapters/intro')                 # overview
get(kind='tex', id='chapters/intro~0')               # one paragraph by pos
get(kind='tex', id='chapters/intro~SLUG')            # one paragraph by slug
get(kind='tex', id='chapters/intro/raw')             # full source
get(kind='tex')                                      # index of all .tex files
```

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

### Search across the project

```python
search(kind='tex', q='activation energy')
search(kind='tex', q='kcat', scope='chapters/intro')   # one file
```

Cross-kind search picks `tex` up automatically:

```python
search(q='activation energy')   # markdown + tex + paper + ...
```

## Limits (first cut)

- **No sectioning awareness** — the TOC view (`/toc`) is not
  available yet. `\section{...}` is just a one-line paragraph.
- **No `\input{}` resolution** — each `.tex` file is its own ref.
  Use the `book` kind (when it ships) to compose chapters into a
  whole-project view.
- **No bibliography integration** — `\cite{}` keys are opaque text.
  For citation-graph queries, use `kind='paper'`.
- **No environment grouping** — `\begin{equation}…\end{equation}` is
  treated as ordinary paragraphs split on blank lines, which usually
  keeps each environment in one block but isn't guaranteed.

## See also

- `precis-files-help` — shared addressing model for all file kinds
- `precis-plaintext-help` — block grammar (inherited 1:1)
- `precis-edit-protocol` — universal anchored-edit grammar
- `precis-paper-help` — citation-graph navigation for cited papers
- `precis-markdown-help` — markdown block grammar for prose notes
