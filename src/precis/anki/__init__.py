"""Anki integration — headless AnkiWeb sync for the `anki` cloze kind (slice 2).

precis is the Anki *client*: it holds a single local `.anki2` mirror and drives
Anki's own sync via the official `anki` pylib (lazy-imported; installed on the
one designated sync runner by ansible, gated behind PRECIS_ANKI_ENABLED). See
`docs/design/anki-integration.md`.

- `notes` — pure, anki-free helpers (guid/deck/tag conventions, ref→card spec,
  stats aggregation). Safe to import anywhere.
- `sync` — the engine (lazy-imports `anki`): upsert our authored notes, the
  guarded sync (bootstrap-download / incremental / abort-on-lossy-upload), and
  the stats read-back.
"""

from __future__ import annotations
