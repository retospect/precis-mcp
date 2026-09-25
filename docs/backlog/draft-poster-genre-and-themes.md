---
status: draft
title: Poster/deck genre for drafts + a named theme system (palette, block chrome, typographic rules) carried as data, not as a hand-kept .tex
prio: normal
---

# Poster/deck genre for drafts, with named themes

## Motivation / why

The ÖPG-CMD Graz 2026 nanobud poster (`pres/poster-cmd2026-nanobuds/`,
untracked) was built as a hand-maintained `beamerposter` `.tex`. The
*content* decisions were the point and are recoverable; the *format*
decisions are worth as much and are currently recoverable only from one
file's comments, which will be deleted along with the poster.

Those format decisions are not poster-specific. A palette whose entries are
**roles rather than hues**, a semantic-colour rule that says what a colour is
allowed to mean, unbreakable citation glue, fixed-height stretch columns —
every one of them applies to the next poster, the next deck, and to
`draft` export generally. Today each of those would be re-derived from
scratch, badly, by whoever writes the next one.

Two seams already exist and neither is used for this:

- `precis.draft.scaffolds.DOC_TYPES` picks a genre at draft creation, stashes
  it as `meta.workspace.doc_type`, and its docstring already names "the future
  export documentclass switch" as the consumer. `poster` and `slides` are not
  in the table.
- `precis/data/templates/draft/preamble.tex` is a **single hardcoded
  preamble**. There is no way to say "render this draft in the UL theme" or
  "render it in the Maynooth theme".

`kind='pres'` is the other half and is read-only in the wrong direction:
`precis.ingest.pres` takes a finished slide PDF apart into blocks, and
`precis.handlers.presentation` serves them back. Nothing authors one. A poster
made in precis should be a draft with a genre and a theme, exported through the
same path that already produces docx/pdf.

## In scope

1. **`poster` and `slides` entries in `DOC_TYPES`**, each with its register
   brief and a section skeleton (a poster's skeleton is blocks with a declared
   column, not `## Introduction`).
2. **A `theme` kind, or themes as data under `precis/data/themes/<slug>/`** —
   whichever the schema review prefers. A theme owns: the role→hue table, the
   block chrome (title band, body tint, inter-block skip), the font stack and
   sizes, and the logo strip. `meta.workspace.theme` selects it.
3. **A documentclass switch in export** keyed on
   `(doc_type, theme)` → preamble + body template, replacing the single
   hardcoded `preamble.tex`.
4. **Semantic-emphasis macros as a theme contract, not ad-hoc `\textbf`.** The
   theme declares the emphasis roles a genre may use and what each means; the
   renderer emits them. This is the part with teeth — see the worked example.
5. **The nanobud poster carried in as the first theme (`ul-bernal`) and the
   first worked example**, so the theme system is validated against a document
   that actually went on a board.

## Explicitly NOT in scope

- Rendering a poster in the web UI. Export to PDF only.
- Authoring posters through the `pres` kind. `pres` stays the ingest side;
  this is `draft` + genre + theme. If the two want to converge later, that is a
  separate item.
- A WYSIWYG layout editor, or any interactive column-balancing. The layout
  numbers are measured by building and re-measuring (below) — automating that
  loop is a possible follow-up, not this.
- Brand compliance checking against an institution's published guidelines.

## Acceptance criteria

- `put(kind='draft', …)` with `doc_type='poster'` and `theme='ul-bernal'`
  yields a draft whose export is an A0 portrait poster with the UL block
  chrome, with no hand-written `.tex` anywhere in the path.
- Changing `theme` on an existing poster draft and re-exporting changes every
  hue and font and **nothing else** — no content edit, no layout edit. This is
  the test that the role indirection is real.
- A second theme exists (even a deliberately ugly one) purely so criterion 2
  is falsifiable.
- The semantic-emphasis contract is enforced: a draft that uses a role the
  theme does not declare fails export with a named error, rather than
  silently rendering as plain bold.
- `docs/conventions/` gains the typographic rules that are genre-independent
  (below), so they are cited rather than rediscovered.

## Target + blast radius

`precis.draft.scaffolds` (DOC_TYPES) · `precis/data/templates/draft/`
(preamble → template set) · `precis.render.latex` and
`precis.workers.job_types.draft_export` (the documentclass switch) ·
`precis.handlers.draft` (`scaffold=` surface) · `precis_web.routes.drafts`
(the `/drafts/new` form gains genre + theme selects) · possibly a new kind,
which pulls in the kind-totality pincer (`test_kind_totality` +
`test_item_view` together).

---

# Worked example — the `ul-bernal` theme, from the nanobud poster

Everything below is in production use on one A0 board. Taken as the seed
content for the first theme, and as the evidence for the rules the theme
system has to be able to express.

## 1. Palette — roles, not hues

The institutional palette is declared once, then **every downstream rule names
a role**. A rebrand is then a handful of `\colorlet` lines and no other edit —
which is exactly acceptance criterion 2 above.

```latex
% University of Limerick brand palette. Green-led per UL guidance; the
% non-green hues are UL's own published secondary accents, used sparingly.
\definecolor{ulgreen}{HTML}{005335}     % UL Green        -- headings, block titles
\definecolor{ulmodern}{HTML}{00B140}    % UL Modern Green -- rules, highlights
\definecolor{ulheritage}{HTML}{003726}  % UL Heritage Gr. -- dark bands, reverse type
\definecolor{ulsky}{HTML}{007DBA}       % Sky blue        -- accent
\definecolor{ulpumpkin}{HTML}{D45D00}   % Pumpkin         -- accent
\definecolor{ulmunster}{HTML}{CB333B}   % Munster red     -- accent
\definecolor{ulslate}{HTML}{373A36}     % Slate neutral
\definecolor{softgrey}{HTML}{EFF2EF}    % block body -- a whisper of green off white

% Role aliases -- every rule below is a role, not a hue.
\colorlet{accent}{ulgreen}              % emphasis + MEASURED values
\colorlet{ulteal}{ulslate}              % citation brackets -- recede
\colorlet{gapempty}{ulslate!55}         % an UNFILLED slot -- a slot, not a signal
```

Chrome, also by role:

```latex
\setbeamercolor{block title}{fg=white,bg=ulgreen}
\setbeamercolor{banner}{fg=white,bg=ulheritage}
\setbeamercolor{logostrip}{fg=ulgreen,bg=white}
\setbeamercolor{block body}{fg=black,bg=softgrey}
```

## 2. Semantic colour — a colour may mean exactly one thing

The strongest format decision on the poster, and the one most worth making a
theme contract. Green initially did two jobs: "this is a measured value" (6
sites) and "this is emphasis" (14 sites). Splitting them made the *scarcity of
measurement* visible on the board, which was the poster's whole argument.
Emphasis kept the weight and lost the hue.

```latex
\newcommand{\hlm}[1]{\textcolor{accent}{\boldmath\textbf{#1}}}  % MEASURED only
\newcommand{\hlc}[1]{\textcolor{ulsky}{\boldmath\textbf{#1}}}   % COMPUTED only
\newcommand{\hl}[1]{{\boldmath\textbf{#1}}}                     % emphasis, no colour
```

Generalised rule for the theme system: **a genre declares its emphasis roles
and their meanings; a document may not invent a new one.** A poster arguing
"predicted vs observed" wants exactly this pair; a proposal wants something
else. The renderer refusing an undeclared role is what stops the drift back to
decorative bold.

The same axis is then reused for non-text signals — the computation/experiment
gap table draws three dots per cell, coloured by column so the glyph inherits
the meaning of its heading:

```latex
\colorlet{gapempty}{ulslate!55}
\newcommand{\gf}{\textcolor{gfill}{\bullet}}
\newcommand{\gemp}{\textcolor{gapempty}{\circ}}
\newcommand{\gdeep}{$\gf\mkern3mu\gf\mkern3mu\gf$}
\newcommand{\gsome}{$\gf\mkern3mu\gf\mkern3mu\gemp$}
% ... all three slots ALWAYS drawn: an empty slot is a slot, not an absence.
```

`\colorlet{gfill}{…}` goes **inside the `array` preamble**, which is how one
glyph macro renders green in the "Meas." column and sky in "Comp." with no
per-cell markup. Worth keeping as a technique note.

## 3. Typographic rules that are genre-independent

These are the ones that belong in `docs/conventions/` regardless of how the
theme system lands. Each was a bug on the board first.

- **Citations may not wrap away from the word they cite.**
  `\newcommand{\src}[1]{\unskip\nobreak\ {\footnotesize\textcolor{ulteal}{[#1]}}}`
  — `\unskip` eats the space the call site typed, `\nobreak\ ` puts back an
  unbreakable one.
- **A colour band sizes to the glyph bounding box, not to the line.** Block
  titles with descenders (`Topology sets the polygon census`) got visibly
  taller bars than titles without (`What has not been measured`).
  `\vphantom{Ay}` before `\insertblocktitle` gives every title line a cap
  ascender and a descender, so all bands match. `\strut` is the reflex fix and
  is **wrong** — it is keyed to `\baselineskip` and grows every band past the
  old maximum (cost 90 pt of page on first try).
- **A band is symmetric about the text's bounding box, so equal vskips look
  wrong.** 1.0ex above / 0.45ex below reads as even; 0.7/0.7 reads as tight on
  top and loose below.
- **Units upright, quantities italic** (ISO 80000-2). `upgreek`'s `\upmu` for
  µV K⁻¹; plain `\mu` italicises the prefix and nothing else in the unit.
- **`\boldmath` is a switch, not an argument.** `\newcommand{\hl}[1]{\boldmath\textbf{#1}}`
  looks scoped and is not — it bolded every subsequent formula to the end of
  the paragraph. Needs its own group: `{{\boldmath\textbf{#1}}}`.
- **Ragged-right for narrow measure.** Justified `p{}` columns at poster
  column width open rivers; `\hyphenpenalty=10000` additionally stops table
  labels breaking mid-word.
- **Sans for posters, not serif.** Serifs pay off at 10–12 pt read at 40 cm;
  at 25 pt read at 2 m under gallery lighting the reinforcement is below the
  eye's resolving limit and the stroke contrast thins. Readability at poster
  distance is x-height, counter size and tracking. (Asked and decided
  2026-09-20.)
- **A range written with a slash must not break.** `$-3.3/{-3.8}$` — braces
  also keep the second minus unary rather than binary.

## 4. Column layout — fixed-height stretch minipages

Poster columns are expected to bottom out on one line. The mechanism:

```latex
\newlength{\colheight}\setlength{\colheight}{2647pt}
...
\begin{column}{0.330\textwidth}
\begin{minipage}[t][\colheight][s]{\linewidth}
  <block>  \vfill  <block>  \vfill  <block>
\end{minipage}
\end{column}
```

`\colheight` is **measured, not derived** — and this is the part a tool should
own, because by hand it is a build-and-re-measure loop:

- probe natural per-column heights by setting `\colheight` to `1pt`, building,
  and reading the per-column bottoms out of `pdftotext -bbox-layout`;
- then set `\colheight` just under the page budget and check
  `grep 'Overfull \vbox'`, which reports *two different things* — the frame
  exceeding the page, and a column exceeding its own minipage.

The second is the one that silently prints content over the footer.
Automating this loop is the obvious follow-up item once the genre exists.

Edge alignment is likewise mechanical and worth generating: the logo strip,
the banner and the footer each share the columns' own wrapper, which is what
makes every edge line up —

```latex
\vspace*{-\topsep}\begin{center}\begin{minipage}{0.95\textwidth}%
  ...
\end{minipage}\end{center}\vspace*{-\topsep}
```

## 5. Tooling — what the stack actually is

The poster is a **`beamerposter`** document (the `beamer` class with the
`beamerposter` package, `size=a0,orientation=portrait,scale=1.13`). Everything
in §1–§4 above is expressed in beamer's own extension points rather than in
ad-hoc markup, which is what makes it themeable at all:

| Concern | Mechanism | Why it matters for a theme system |
|---|---|---|
| Palette | `\definecolor` + `\colorlet` role aliases | A theme is a colour table; a rebrand touches nothing else |
| Chrome | `\setbeamercolor{block title / block body / banner / logostrip}` | Named slots — a theme fills them, a document never names a hue |
| Block bands | `\setbeamertemplate{block begin / block end}` | Where the `\vphantom{Ay}` and the asymmetric vskips live |
| Type scale | `\setbeamerfont{block title}` + explicit `\fontsize` for refs | The one place genre and theme overlap; needs a decided owner |
| Columns | `columns` / `column` + fixed-height stretch `minipage` | See §4 — the `\colheight` loop |

Extra packages the genre needs and the current `draft` preamble does not
carry: `upgreek` (upright unit prefixes, §3), `booktabs` + `array` (the
`>{\colorlet{gfill}{…}}` per-column trick, §2), `multicol` (the 3-column
reference list), `graphicx` for the logo strip.

**Build is `tectonic -X compile poster.tex`.** This is a constraint, not a
preference — the authoring machine has no `latexmk` and no `pdflatex`, and
`precis/data/templates/draft/latexmkrc` assumes otherwise. Two consequences
worth recording before anyone automates the layout loop:

- Tectonic **does not write a `.log` by default.** Diagnostics have to be read
  off stdout (`2>&1 | grep 'Overfull \vbox'`) or measured out of the PDF with
  `pdftotext -bbox-layout` / `pdftoppm`. The `\colheight` probe in §4 depends
  on this.
- `Overfull \vbox` is emitted for **two distinct failures** that read
  identically — the frame exceeding the page, and one column exceeding its own
  fixed-height minipage. Only the second silently overprints the footer, and
  on this poster the two were confused for several rounds: cuts were made in
  column 2 while the binding column was 3, and the reported figure did not
  move. Any automated loop must attribute the overflow to a column before
  acting on it.

Figure generation sits alongside and is also worth carrying as tooling
precedent: headless **PyMOL** (`pymol.finish_launching(['pymol','-qc'])`,
explicit `cmd.set_view` camera) rendering per-structure panels, composited by
**PIL** with **one shared crop box** so a "common scale" caption stays true,
and **svglib + reportlab** for SVG→PDF logo conversion that keeps the vector.
Recipe and the traps are in the poster's own `figures/PYMOL-SETUP.md`.

## 6. Prose register for the genre

The poster's brief, which should become the `poster` entry's standing guidance
in `DOC_TYPES`. It overlaps `docs/conventions/llm-facing-prose.md` but is
stricter, because poster prose is read in glances:

- No em-dashes (already a house rule; the sweep on this poster found 26 sites
  and **left one sentence broken** where the dash had been carrying the clause
  join — so a genre lint has to re-read, not just delete).
- No negation-of-a-strawman openers ("That ring of heptagons is not
  decoration" — nobody suspected decoration).
- No metaphor doing a noun's work ("a reactive bullseye" → "the reactive
  site"; "the levers exist" → "the mechanism is understood").
- No instruction to the reader ("Read the morphology carefully:" — just state
  the morphology).
- **No more than 2 significant figures** unless the extra figure is
  load-bearing. A sweep on this poster cut ten numbers (3.53 GPa → 3.5,
  2.26 ppb → 2.3, 99.0 % → 99 %); graphite's canonical 372 mAh g⁻¹ was kept at
  three deliberately and flagged.

## Open questions / decisions log

- **Theme as a kind, or as data?** A kind buys search, tags and links (a theme
  could cite the institution's brand guideline paper); data under
  `precis/data/themes/` buys no schema work and no totality-pincer exposure.
  Leaning data-first, kind later if themes acquire provenance.
- **Does `poster` need a column model in the draft schema**, or is a column
  hint in block meta (`meta.column = 1|2|3`) enough? The latter keeps the
  draft linear and readable; the former is what a balancer would need.
- **Who owns the `\colheight` loop?** Doing it in the export worker means the
  export builds the document two or three times. Acceptable for a poster;
  check it against the export job's budget.
- **Whether the `\src` citation macro collides with the existing cite
  pipeline** (`cite_findings_only_policy` — prose cites are `[fi<id>]` hubs).
  A poster's numbered reference list is a rendering of those hubs, so this is
  probably a renderer concern only, but confirm before building.
