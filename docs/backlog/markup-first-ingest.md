# Markup-first ingest — JATS/LaTeX/HTML before PDF+OCR

Status: draft (built, dark)
Owner: reto

Shipped portion: see the `src/precis/ingest/markup.py` module
docstring; full design in git history. Built: the pure markup
producers (JATS / arXiv HTML / Elsevier XML / flattened LaTeX) →
Marker-shaped blocks → the existing `PaperToWrite` downstream, watcher
routing, the attach-only upgrade guard in `db_writer`, the `fetch_oa`
markup cascade behind `PRECIS_FETCH_MARKUP` (default-off), and
provenance (`source_format`). Fallback contract: any markup parse
failure falls back to Marker OCR — markup-first must never lose a
paper we could have OCR'd.

## Open scope

- **Decide the PDF-race before flipping `PRECIS_FETCH_MARKUP`** (the
  blocking residual): per-stub the markup pass runs first
  (best-effort, swallows its own errors), then the PDF cascade runs
  unconditionally after — which body wins when both succeed is
  undecided. Decide before enabling on any host. Owner:
  `src/precis/workers/fetch_oa.py::_run_markup_cascade` /
  `_markup_fetch_enabled`.
- **Rollout tail:** flip the flag default-on once the stub backlog
  has been exercised; ADR documenting the append-only punt for
  existing refs (no retro re-ingest — refs keep their OCR body until
  a natural re-ingest).
- **Surface `source_format` in paper views** (lean: yes, in the
  existing meta block) so the operator can see which refs got the
  good path.

## Decided constraints

- Heavier structural `.tex` parsing is out of scope — v1 flattens and
  chunks (the `.bbl`/`anc/` conventions make it robust; it cannot
  fail to parse).
- Springer leg lands silently no-op when `PRECIS_SPRINGER_API_KEY` is
  unset (same pattern as the Elsevier/Wiley PDF legs).
- The PDF is always kept as the printable; Marker is simply never
  invoked when markup succeeds.
