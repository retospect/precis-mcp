---
status: draft
pillar: memory-graph
title: Poster/deck drafts with a generic template and per-user branding profiles
prio: normal
---

# Poster/deck genre for drafts, with per-user branding

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

Shipped 2026-10-15 (slices 1–5): the generic `poster.tex` scaffold; the
`poster` entry in `DOC_TYPES` with the §6 prose-register brief, a
section skeleton and `meta.poster_column` hints; the typography rules as
`docs/conventions/poster-typography.md`; `precis.export.poster_profile`
(schema, resolution order, validators emitting `poster_role_missing` /
`poster_role_unknown` / `poster_logo_missing` /
`poster_logo_resolution_low` / `poster_contrast_insufficient` plus the
size-limit codes, 4.5:1 WCAG floor, 10 MiB / 40 MP / 64 KiB / 20-logo
caps); and export dispatch — `doc_type='poster'` assembles the template
with role `\definecolor`s, the logo strip and `poster_profile.json`, then
compiles via the existing `latexmk` path.

Shipped 2026-10-15 (slice 6, profile storage + surfaces): `kind='profile'`
owner-scoped refs (payload in `refs.meta['profile']`, `refs.owner_login`
the ACL column — no migration; kinds boot-upsert), `ProfileHandler`
put/edit/get/delete validated through the poster validators; resolution
wired in `precis.profiles.draft_user_default`
(`meta.workspace.poster.profile` selector → the draft owner's `default`
profile → product palette), drafts stamped `owner_login` at create; the
`/account` poster-branding form and the `/drafts/new` profile preselect;
skill `precis-profile-help`.

**Still open:**

- Post-deploy manual check (ruling 5): Reto exports a poster draft under
  the private `ul-bernal` profile and eyeballs the result.
- `slides` stays out (ruling 6).

## Explicitly NOT in scope

- Web poster rendering, WYSIWYG layout editing or interactive balancing.
- Authoring through `pres`, which remains the finished-slide ingest path.
- Institution-specific templates, arbitrary user `.tex`, or checking brand
  compliance/licences. Generic chrome and typography belong to the template;
  profiles change only allowed fonts, hues and the logo strip.
- A new theme kind: branding profiles are data. A discoverable template
  catalogue can revisit kind-vs-data later; it is unnecessary for one template.

## Acceptance criteria

Done in slices 1–5: two-profile branding diff (hues/font/logo strip only),
sponsor re-flow, the named validation failures, `docs/conventions/`
typography rules. Remaining:

- A poster draft with the private `ul-bernal` profile exports an A0 portrait
  poster through the generic template — **manual post-deploy check by Reto**
  (ruling 5).
- MCP/profile settings agree; `/drafts/new` preselects the owner's profile.
- Adding one sponsor per poster leaves the user's profile unchanged;
  co-author institution logos are per-poster additions; the owning user's
  default wins unless the poster overrides it.

## Target + blast radius

Slice 6 lands on: a new `kind='profile'` kind + owner-scoped storage ·
`precis.handlers` (the put/edit surface, owner-checked writes) ·
`precis.export.latex` (`resolve_poster_profile`'s `user_default` seam —
the owning author's stored default) · `precis_web.routes.account`
(settings) and `precis_web.routes.drafts` (`/drafts/new` preselect).

---

# Worked example — the `ul-bernal` profile, from the nanobud poster

The historical A0 board is the worked example: its palette/fonts/logos seed
the private `ul-bernal` profile; its chrome, typography and columns inform
the generic template. The TeX below records the original technique, not a
tracked branding file or a separate institutional template to ship.

## 1. Palette — roles, not hues

The institutional palette is declared once, then **every downstream rule names
a role**. A rebrand is then a handful of `\colorlet` lines and no other edit —
which motivates the two-profile acceptance criterion above.

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
| Type scale | `\setbeamerfont{block title}` + explicit `\fontsize` for refs | Template owns sizes; profile chooses an allowed font |
| Columns | `columns` / `column` + fixed-height stretch `minipage` | See §4 — the `\colheight` loop |

Extra packages the genre needs and the current `draft` preamble does not
carry: `upgreek` (upright unit prefixes, §3), `booktabs` + `array` (the
`>{\colorlet{gfill}{…}}` per-column trick, §2), `multicol` (the 3-column
reference list), `graphicx` for the logo strip.

**Historical build: `tectonic -X compile poster.tex`.** That authoring
machine had no `latexmk` or `pdflatex`; this is evidence from the example,
not a change to the product compile engine. Two consequences worth recording
before anyone automates the layout loop:

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

Reto, 2026-10-15 20:35Z: (1) user profiles are a new owner-scoped
kind='profile'; (2) logos live in the figure blob store, referenced by
figure id; (3) owner-only read plus owner-checked write is enough ACL;
(4) the MCP surface is put/edit on kind='profile' with fields name,
palette (role->hex per spec section 1), fonts, logos (figure ids),
sponsor line, owner; (5) the ul-bernal private-palette acceptance becomes
a manual post-deploy check by Reto; (6) slides waits; only poster goes in
DOC_TYPES.

Resolved 2026-10-06: Reto supplied `fleet-state/scratch/poster-template-src/poster.tex`
and `README.md`. Only the stripped generic scaffold enters the public package;
the supplied manuscript and branding stay private.

Reto, 2026-10-05: per-user branding profiles are data. This settles the
branding part of “theme as kind or data”; a future template catalogue remains
separate. Existing column-hint, layout-probe and citation-pipeline details
remain implementation work within the template/export seams, not additional
questions for this backlog amendment.
