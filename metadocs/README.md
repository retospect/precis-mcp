# metadocs — documents *about* precis

Conference abstracts, papers, posters, and other outward-facing writing
about the project — as opposed to `docs/` (how the system is built and
run) and `pres/` (slide decks). One subdirectory per venue/artifact.

Built PDFs are tracked (the PDF is the deliverable; most readers have no
TeX toolchain); LaTeX build intermediates are ignored via `.gitignore`.
Builds use `tectonic <file>.tex`.

When an artifact's prose is mirrored as an editable precis draft, the
subdirectory's .tex is the typeset source of record and the draft is the
collaborative editing surface — the draft's submission-notes chunk names
the pairing.

- `aixmat-2026/` — AIxMAT 2026 (Warsaw, Nov 24–26 2026) one-page
  abstract, two variants: `abstract.tex` (tool-forward; precis draft
  `aixmat2026-abstract`, todo td261219) and `abstract-litsim.tex`
  (lit–sim-loop focus, autocatpath unnamed; precis draft
  `aixmat2026-abstract-litsim`, todo td262315). Submit exactly one.
