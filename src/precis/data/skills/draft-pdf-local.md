---
id: draft-pdf-local
family: work
title: precis — compile a draft export to PDF locally (texlive-nanobud image)
summary: export a draft's LaTeX project with the prod CLI and compile it inside the pinned texlive-nanobud container — the Mac's tectonic cannot run biber, and the image carries the extra packages the export preamble loads
answers:
  - how do I build a draft's PDF locally instead of through the prod job?
  - which container image compiles a draft export, and how do I build it?
  - why does the containerised compile fail with 'makeglossaries: not found'?
  - does the CLI export check figure licensing like the prod PDF job?
applies-to: repo-dev on the operator Mac — a local PDF from a prod draft export
status: active
tags: [workflow]
---

# draft-pdf-local — compile a draft export to PDF locally

The Mac has only tectonic, which cannot run biber — and the export needs
biber plus makeglossaries. Compile inside the `texlive-nanobud` container
image instead. Use this when you want the PDF without a deploy, or when
the prod PDF job's figure-licensing gate would refuse the export.

## Build the compile image once

`texlive-nanobud:latest` is the upstream TeX Live medium image plus the
packages the draft preamble loads (glossaries, glossaries-extra,
biblatex-chem, datetime2, marginnote, authblk and relatives):

```dockerfile
FROM texlive/texlive:latest-medium
RUN tlmgr install glossaries glossaries-extra biblatex-chem pdfcomment \
    preprint cleveref csquotes datetime2 datetime2-english zref marginnote
```

(`preprint` is the CTAN bundle that carries authblk.) Build with
`docker build -t texlive-nanobud:latest - < Dockerfile`.

## Export the LaTeX project from prod

```bash
scripts/prod-precis draft export <numeric id> --bib-style rsc --out <dir>
```

- Needs the numeric id — the `dr…` slug answers "no draft".
- Counts as a write under prod-precis's stale-tree guard: it refuses
  unless HEAD descends from origin/prod. Rebase, or run from a tree
  that does.
- Runs longer than five minutes on a large draft — background it and
  watch the log.
- Writes `main.tex`, `refs.bib`, `preamble.tex` and `.latexmkrc` into
  `<dir>`; the rc wires biber + makeglossaries, so one latexmk call runs
  every pass.

## Compile inside the container

```bash
docker run --rm -v <dir>:/work -w /work texlive-nanobud:latest \
    latexmk -lualatex -interaction=nonstopmode -halt-on-error main.tex
```

The TeX Live bin directory must be on PATH or `makeglossaries` is not
found and the acronym/glossary lists never build. Inside the image it
sits at `/usr/local/texlive/<year>/bin/<arch>-linux` (e.g.
`.../2026/bin/aarch64-linux` on Apple Silicon); the base image puts it
there by default, so a "makeglossaries: not found" means a `-e` env or a
custom entrypoint clobbered PATH. The deliverable is `<dir>/main.pdf`.

## The CLI export has no figure-licensing gate

`draft export` never checks figure licensing — a locally compiled PDF
embeds every image the draft references. The prod PDF job refuses
third-party figures whose permission is not granted. Fine for an
internal read; a local PDF is not a cleared-for-circulation copy.

## See also

- [[precis-draft-export-help]] — the prod export surface: the job form,
  audiences, bibliography styles, and the figure gate the CLI skips.
- [[quiet-output]] — background the long export instead of watching a
  silent foreground run.
