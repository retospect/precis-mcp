# Poster/display typography — genre-independent TeX rules

Rules for TeX meant to be read at a distance (posters, decks, display
print). Each was a bug on a shipped A0 board first — provenance and the
rest of the poster spec:
`docs/backlog/draft-poster-genre-and-themes.md` §3. They bind the generic
template (`src/precis/data/templates/draft/poster.tex`) and any hand-tuned
`.tex` alike.

- **Citations may not wrap away from the word they cite.**
  `\newcommand{\src}[1]{\unskip\nobreak\ {\footnotesize\textcolor{citation}{[#1]}}}`
  — `\unskip` eats the space the call site typed, `\nobreak\ ` puts back
  an unbreakable one.
- **A colour band sizes to the glyph bounding box, not to the line.**
  Block titles with descenders get visibly taller bands than titles
  without; `\vphantom{Ay}` before the title text gives every line a cap
  ascender and a descender so all bands match. `\strut` is the reflex fix
  and is **wrong** — it is keyed to `\baselineskip` and grows every band
  past the old maximum.
- **A band is symmetric about the text's bounding box, so equal vskips
  look wrong.** 1.0ex above / 0.45ex below reads as even; 0.7/0.7 reads
  as tight on top and loose below.
- **Units upright, quantities italic** (ISO 80000-2). `upgreek`'s
  `\upmu` for µV K⁻¹; plain `\mu` italicises the prefix and nothing else
  in the unit.
- **`\boldmath` is a switch, not an argument.**
  `\newcommand{\hl}[1]{\boldmath\textbf{#1}}` looks scoped and is not —
  it bolds every subsequent formula to the end of the paragraph. Wrap it
  in its own group: `{{\boldmath\textbf{#1}}}`.
- **Ragged-right for narrow measure.** Justified `p{}` columns at poster
  column width open rivers; `\hyphenpenalty=10000` additionally stops
  table labels breaking mid-word.
- **Sans for posters, not serif.** Serifs pay off at 10–12 pt read at
  40 cm; at poster sizes read at ~2 m under gallery lighting the
  reinforcement is below the eye's resolving limit and the stroke
  contrast thins. Readability at poster distance is x-height, counter
  size and tracking.
- **A range written with a slash must not break.** `$-3.3/{-3.8}$` —
  the braces also keep the second minus unary rather than binary.
