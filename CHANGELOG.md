# Changelog

## 3.0.0 — 2026-04-01

### Breaking

- **URI selector separator changed from `#` to `~`** — all selectors now use
  `~` (e.g. `paper:slug~38`, `doc.docx~PLXDX`). The `#` separator is no longer
  accepted.

### Added

- **MarkdownHandler** — read/write `.md` and `.markdown` files. Parses headings,
  paragraphs, fenced code blocks, tables, and lists. Zero extra dependencies.
- **PlainTextHandler** — read/write `.txt` and `.text` files. Paragraph-based
  parser (blank-line separated). Zero extra dependencies.
- **TodoHandler** — corpus-backed task management with state machine
  (pending → in_progress → done, blocked, cancelled). Requires `acatome-store`.
- **RefHandler** base class — extracted common corpus-backed read operations
  from PaperHandler. Provides TOC, chunk reading, search, summaries, links,
  and notes for any corpus-backed reference type.
- `PathCounter` in protocol for consistent node path generation.
- Entry points for new handlers (`.md`, `.markdown`, `.txt`, `.text`, `todo:`).
- Auto-create empty `.md` and `.txt` files on first access.

### Changed

- **PaperHandler** refactored to extend RefHandler (no API change).
- Registry now registers MarkdownHandler, PlainTextHandler, and TodoHandler
  as built-in plugins.
- All hint strings, error messages, and docstrings updated for `~` separator.

### Fixed

- Bump requests 2.32.5 → 2.33.0 (security).

## 2.2.1 — 2026-03-19

- Figure handling: `get(id='slug/fig')`, export to file
- List and table roundtrip in DOCX
- Citation validation and malformed-reference warnings
- Tracked changes and comment support in DOCX

## 2.2.0 — 2026-03-19

- Plugin registry with entry-point discovery
- Multi-ID batch reads: `get(id='slug1~4,slug2~9')`
- Grep and depth filtering in file handlers

## 2.1.1 — 2026-03-19

- LaTeX handler improvements
- URI parser with subview tails

## 0.4.1 — 2026-03-13

- Initial public release
