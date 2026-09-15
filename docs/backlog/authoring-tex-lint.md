# Corpus garbled-math repair lane (Reto, 2026-09-14)

Remaining slice of the authoring-side TeX/math lint effort. Origin: the
2026-09-14 reMarkable-send saga's five exporter fatal classes (memory
`remarkable-prod-rollout`).

**Slices 1/1b/3/4 status (worktree radiant-marinating-crayon):** code +
tests complete, gate-green except the two fixes below; ship in flight.
If this session died mid-ship, re-run `scripts/ship --mutate` from that
worktree (idempotent) and then `scripts/deploy`. What landed there:
write-path `math_form_hint` + hard `BadInput` on newly-introduced
unresolvable `[…]` refs (`handlers/_draft_lint.py` /
`handlers/draft.py`, gated at put / edit-text / edit-find / `sub=`),
docx math parity via the shared `_MATH` + `_math_braces_balanced` /
`_math_plausible` imports, `utils/authors.py::_scrub_name` at both
author funnels, and `store/_chunks_ops.py::chunk_owner_kind` (splits
"typo handle" from "chunk whose source ref was retired" so the latter
stays advisory, matching the ref-level tombstone carve-out, gr265228).
Tests that intentionally seed dead handles now write via the store, not
the `put` verb — the handler refuses that content by design.

## Slice 2 — corpus body chunks: bring garbled text to an LLM lane

Corpus body chunks are faithful extraction (append-only, never edited in
place — CLAUDE.md invariant), so no write-time gate applies. Instead:
a detector + LLM-repair loop, like the claim-graduation content-repair
arm (memory `claim-graduation-campaign`):

- Detector: run `export/latex.py::lint_math_spans` (the exporter's own
  demotion predicates) over corpus chunks; base-rate check FIRST (memory
  `corpus-detector-measure-base-rate-first`) — sample before sweeping.
- Surface: mint low-prio jobs/todos (or a `derived` pass) handing each
  flagged chunk to an LLM to either (a) mark the span as
  garbled-extraction (a tag the exporter could quote-sanitize harder),
  or (b) propose a repaired chunk via the sanctioned DELETE+INSERT path
  (embedding/summary cascade re-runs; never in-place UPDATE).
- The known live example: chunk 194080 (paper 1931, the seven-`\sqrt{`
  ratio) — good first test case.

## Optional follow-ups

- One-off prod repair sweep for existing garbage author rows (refs 258,
  893, 933, 3023, ryder14's thin space) — the read-funnel scrub in
  `author_display` already renders them clean, so this is cosmetic DB
  hygiene only. Prod-mutation rules apply: prep the SQL, hand to Reto.
- `docs/backlog/export-glyph-allowlist.md` — export-side glyph
  allowlist + per-glyph warnings (complementary, not superseded).
- `docs/backlog/remarkable-pairing.md` — token-exchange pacing;
  melchior image still predates cdf23337's `--content-only` retry (one
  `ansible-playbook deploy/playbooks/47-remarkable.yml` re-run, needs
  Reto or a permission rule).
