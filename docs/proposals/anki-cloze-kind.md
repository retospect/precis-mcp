---
status: draft
title: anki kind (cloze cards) + retire flashcard — slice 1
---

# anki kind (cloze cards) + retire flashcard — slice 1

> Slice 1 of the Anki integration. Full picture + the sync tick and retention
> model (slices 2–3): `docs/design/anki-integration.md`. This slice is the
> **kind itself, with no Anki dependency** — cards author, store, and search in
> the corpus; syncing them to AnkiWeb is slice 2.

## Motivation / why

Reto authors spaced-repetition cards constantly and lives in Anki, but the
corpus and the retention layer are disconnected. First step: a first-class
`anki` kind that lets a cloze card be authored, stored, and searched inside
precis — the substrate the sync tick (slice 2) and the knowledge model (slice 3)
build on. The existing `flashcard` kind is thin, its SM-2 scheduler is half-wired
(never writes) and redundant with Anki, and prod holds **0 live flashcard refs** —
so this slice also retires it.

## In scope

- **`anki` kind** (numeric ref, reuse `_numeric_ref`/`flashcard` infra),
  `corpus_role='none'` (authored artifact, never cited as evidence).
- **Generic storage shape** so future notetypes need no migration:
  - `meta.notetype` (default `"Cloze"` — the only authored type in v1),
  - `meta.deck` (default `"Precis"`),
  - `meta.fields` — key→value dict; for Cloze: `{"Text": "<cloze markup>",
    "Back Extra": "<optional terse note>"}`.
- **Cloze body** is `{{cN::…}}` markup stored in `fields.Text`.
- **Optional terse `Back Extra`** — a short answer-side annotation (source,
  mnemonic, gotcha); empty by default, "terse or omit" by convention.
- **`card_combined` chunk** built from the *stripped* cloze text (markup removed)
  + `Back Extra`, so cards embed and appear in `search(kind='*')`.
- **Verbs**: `put` (author), `get` (+ `/recent`), `search`, `tag`, `link`,
  `delete`. `edit` follows append-only card discipline (delete + re-insert so the
  embedding/keyword cascade re-runs).
- **Migration `0060_anki_kind.sql`** registering the kind + `anki_note` chunk
  kind(s), plus web browse-list registration.
- **Skill `precis-anki-help.md`** — cloze authoring rules (one deletion = one
  fact, minimal/unambiguous deletions, `{{c1::}} {{c2::}}` vs same-index, hint
  syntax `{{c1::answer::hint}}`), the terse-`Back Extra` convention, good/bad
  exemplars. Discoverable via the skill index.
- **Retire `flashcard`** — DONE in this slice (true blast radius was **~45
  files**, far past "deprecate a KindSpec"). Deleted the handler + `test_flashcard`
  + `precis-flashcard-help`; remapped the `fc` handle prefix → `ak` for `anki`
  (`utils/handle_registry.py`); swapped `flashcard`→`anki` in the per-kind maps
  (`store/types.py` closed-axes, `utils/mentions.py`, `mcp_modalities.py`,
  `precis_web/routes/{refs,preview}.py`, `handlers/skill.py` pinned list) and in
  ~a dozen skill-doc / docstring examples; excised the flashcard `/due` tests
  from `test_state_kinds`, repointed `test_{default_tags,handle_registry,migrate,
  skill}`. The `flashcard` kinds-table row stays (forward-only; 0 live refs, no
  data migration) — it is simply handler-less, so `put/get(kind='flashcard')`
  now returns Unsupported.

## Explicitly NOT in scope

- **Any AnkiWeb / `anki`-pylib dependency, sync, or `.anki2` mirror** — that is
  slice 2. This slice adds no new runtime dependency.
- **Reading decay stats back** (`meta.anki_stats`) — populated by slice 2; the
  field may be reserved in the shape but nothing writes it here.
- **Retention-aware retrieval, gap/weakness pass, foreign-card ingest** — slice 3.
- **Non-cloze authoring** (Basic, structured types) — the storage shape *carries*
  them but v1 authors only Cloze.
- **Image occlusion** — Reto hand-authors those; never in scope.
- **SM-2 / any scheduler in precis** — Anki owns scheduling; deliberately dropped
  from the retired `flashcard`.

## Acceptance criteria

- `put(kind='anki', text='… {{c1::x}} …')` creates a ref with
  `meta.notetype='Cloze'`, `meta.deck='Precis'`, `meta.fields.Text` = the markup,
  and emits a `card_combined` chunk whose text is markup-stripped.
- `put(..., extra='terse note')` (or equivalent) populates `fields."Back Extra"`;
  omitting it leaves the field absent/empty.
- `get(kind='anki', id=N)` renders the card; `get(kind='anki', id='/recent')`
  lists recent cards.
- `search(kind='anki', q='…')` and cross-kind `search(kind='*', q='…')` both
  surface the card via its `card_combined` chunk (semantic + lexical).
- `tag` / `link` / `delete` work as for other numeric refs; `link` can attach
  `derived-from` a paper/draft.
- `flashcard` is retired: its KindSpec no longer registers as authorable, and the
  skill index points cloze authoring at `precis-anki-help`. No live flashcard ref
  is touched (there are none).
- Full container gate green (`scripts/dev pytest` — ruff + mypy + pytest),
  including a new test that a cloze `put` round-trips through search.

## Target + blast radius

New: `src/precis/handlers/anki.py`, `src/precis/migrations/0060_anki_kind.sql`,
`src/precis/data/skills/precis-anki-help.md`, a browse-list entry in
`src/precis_web/routes/refs.py` (`_REFS_BROWSABLE_KINDS` + `_NUMERIC_KINDS`).
Retires: `flashcard` KindSpec registration + `precis-flashcard-help.md`. Reuses:
`_numeric_ref` create/search path, `upsert_card_combined`, the `embed` +
`chunk_keywords` pipeline, `KindSpec`. No new runtime dependency; no worker
changes; no cluster/ops changes.

## Open questions / decisions log

- **Cloze-text → `card_combined` stripping**: strip `{{cN::…}}` to the plain
  sentence (keep the answer text, drop the markers) so the embedded/searchable
  form reads naturally. Decided: strip markers, keep answers + `Back Extra`.
- **`extra` surface on `put`**: expose as a dedicated `extra=` arg vs a
  `fields=` dict. Lean: a simple `extra=` arg for the terse `Back Extra`
  (cloze-only v1), with the generic `fields` dict reserved for slice-3 notetypes.
  Resolve during build.
- **`anki_stats` field presence in v1**: reserve the key in the documented shape
  but write nothing (slice 2 owns it), so slice 2 needs no migration. Decided:
  reserve, don't write.
