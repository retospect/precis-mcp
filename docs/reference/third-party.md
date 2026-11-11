# Third-party register — vendored & adapted code

One row per piece of code in `src/` that came from outside this repo:
copied in, ported, or adapted. The repo is public, so licence
obligations live here, not in scattered "Adapted from" sentences.
Runtime deps in `pyproject.toml` are not rows — their licences travel
with the package.

## The gate

`tests/test_third_party_register.py` scans `src/` (skills included) for
the provenance idioms `adapted from` / `vendored from` / `ported from`
(word-bounded — `imported`/`exported`/`reported from` do not match).
Every hit must be either:

- **covered** — a row's *Landed* entry names the file, a directory above
  it (trailing `/`), `path::Symbol`, or `skill:<slug>`; or
- **exempted** — the line or the one above it carries
  `third-party: n/a — <reason>`, for idioms that are not vendoring
  claims: the figure-caption pointer vocabulary in
  `src/precis/taproot/backfill.py`, same-repo ports (the retired classic
  `/drafts` reader, `asa_bot.claude_invoke`), a ported *ruling*.

Each row's *Licence* must sit in the allowlist (Reto 2026-11-11):
`MIT`, `BSD`, `BSD-N-Clause`, `Apache-2.0`, `LGPL-* (dynamic)`, or
`first-party` — our own code crossing a repo boundary; the upstream
licence is noted in parentheses where it matters (catpath is
GPL-3.0-or-later upstream and ours to relicense — a separate decision
when it bites, gr608720). Anything else fails the gate: vendoring
copyleft code is a policy decision, not a drive-by.

*Landed* entries must resolve to a file or dir that exists — a stale row
fails the gate. `tests/` is deliberately out of scope (it carries
parallel vendored suites: `tests/ingest/` ← acatome-\*,
`tests/test_precis_flow.py` ← cfdtest); `src/precis/thermo/` already
self-registers third-party *data* in its own `NOTICE`.

## Register

| Piece | Source | Licence | Landed | Date | Notice |
|---|---|---|---|---|---|
| feynman recipe skill | github.com/companion-inc/feynman | MIT | `skill:precis-ml-recipe` | 2026-08-16 | "Adapted from" sentence in the skill body |
| feynman audit skill | github.com/companion-inc/feynman | MIT | `skill:precis-paper-code-audit` | 2026-08-16 | "Adapted from" sentence in the skill body |
| feynman comparison skill | github.com/companion-inc/feynman | MIT | `skill:precis-source-comparison` | 2026-08-16 | "Adapted from" sentence in the skill body |
| feynman replication skill | github.com/companion-inc/feynman | MIT | `skill:precis-replication` | 2026-08-16 | "Adapted from" sentence in the skill body |
| feynman PaperRank port | github.com/companion-inc/feynman | MIT | `src/precis/workers/paper_rank.py` | 2026-08-16 | module docstring |
| mermaid bundle | github.com/mermaid-js/mermaid | MIT | `src/precis_web/static/mermaid/` | 2026-09-03 | none vendored; upstream LICENSE |
| Framer.js spring physics (inside mermaid bundle) | Framer.js (Koen Bok) | MIT | `src/precis_web/static/mermaid/mermaid.min.js` | 2026-09-03 | embedded `/*! Adapted from Framer.js … MIT */` banner |
| three.js r160 | github.com/mrdoob/three.js | MIT | `src/precis_web/static/three/` | 2026-09-03 | `@license` banner in the bundle |
| three.js 0.184.0 | github.com/mrdoob/three.js | MIT | `src/precis_web/static/three-r184/` | 2026-10-03 | `three-r184/LICENSE` |
| three-cad-viewer 5.0.6, rebuilt with external `three` | github.com/bernhard-42/three-cad-viewer | MIT | `src/precis_web/static/three-cad-viewer/` | 2026-09-12 | `three-cad-viewer/README.md` |
| 3Dmol.js | github.com/3dmol/3Dmol.js | BSD-3-Clause | `src/precis_web/static/3dmol/` | 2026-09-03 | bundle banner points to `3Dmol-min.js.LICENSE.txt` — not vendored (gap) |
| KaTeX | github.com/KaTeX/KaTeX | MIT | `src/precis_web/static/katex/` | 2026-08-04 | licence header in `katex.min.js` |
| pdf.js | github.com/mozilla/pdf.js | Apache-2.0 | `src/precis_web/static/pdfjs/` | 2026-06-24 | `pdfjs/LICENSE`; bundled fonts carry their own LICENSEs under `web/standard_fonts/` |
| htmx 2.0.3 | github.com/bigskysoftware/htmx | BSD-2-Clause | `src/precis_web/static/htmx.min.js` | 2026-09-03 | none vendored; upstream LICENSE |
| Alpine.js | github.com/alpinejs/alpine | MIT | `src/precis_web/static/alpine.min.js` | 2026-09-03 | none vendored; upstream LICENSE |
| catpath kinetics panel | catpath `autocatpath/report.py` | first-party (GPL-3.0-or-later upstream) | `src/precis_web/static/pathway-kinetics.js` | 2026-08-25 | file header |
| cfdtest flow kernel | cfdtest `breakthrough_annealer` scratch repo | first-party | `src/precis_flow/` | 2026-10-20 | package `__init__` docstring |
| acatome pdf_metadata | `acatome_extract.pdf_metadata` | first-party | `src/precis/ingest/pdf_metadata.py` | 2026-05-22 | module docstring |
| precis v1 ports | precis v1 (`url_canonical`, math handler) | first-party | `src/precis/utils/url.py`<br>`src/precis/handlers/math.py` | 2026-04-22 | module docstrings |
| dream-lens seeds | dream-lens persona seeds | first-party | `src/precis/data/oracle/scientists.yaml` | 2026-07-05 | yaml `description` |
