# Export glyph allowlist + lint (Reto, 2026-09-14)

Invert the exporter's unicode handling from blacklist-the-failures to
allowlist-the-known-good. Tonight's remarkable_send saga fixed two fatal
classes case-by-case (brace-unbalanced math passthrough; pylatexenc emitting
preamble-undefined font-encoding commands like `\CYRT` for Cyrillic
homoglyphs — both in `export/latex.py`). The durable shape Reto asked for:

- **Allowlist** the input ranges known to render under LuaLaTeX + Latin
  Modern (ASCII, Latin-1/Extended, Greek, common punctuation/symbols — the
  set pylatexenc maps to core commands, plus the CJK path's `\cjktext`).
- **Everything else**: keep the raw glyph (compile-safe — missing-glyph box
  at worst, never a fatal), and **emit one `warn:` per distinct offending
  glyph** into the export's warning stream (`RenderResult.warnings` →
  job_events on the todo page), naming the chunk handle. Visibility is the
  point: corpus garbage (extraction homoglyphs, mojibake U+FFFD) gets fixed
  at the source instead of silently degrading PDFs.
- Structure-level garbage (the unbalanced-math class) stays a separate
  guard — char linting can't see it; `_math_braces_balanced` already
  degrades it to escaped prose. Consider warning there too (currently
  silent).

Starting points: `_U2L` / `_RAW_SCRIPT_RANGES` / `_keep_raw_scripts`,
`_encode_unicode`, `_render_gap` in `src/precis/export/latex.py`; the warn
channel is `RenderResult.warnings`. The docx exporter shares
`preprocess_draft_inline` but has its own glyph story — check whether it
needs the same lint.
