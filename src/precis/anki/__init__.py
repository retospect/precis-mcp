"""Anki integration — headless AnkiWeb sync for the `anki` cloze kind.

precis is the Anki *client*: it holds a single local `.anki2` mirror and drives
Anki's own sync via the official `anki` pylib (lazy-imported; installed on the
one designated sync runner by ansible, gated behind PRECIS_ANKI_ENABLED). Sync
is add-only-own-notes by deterministic guid (``precis:<ref_id>`` — a text
edit updates the note in place, never re-guids, so Anki's scheduling history
survives); the guard allows FULL_DOWNLOAD but **refuses FULL_UPLOAD**, so
precis can never clobber the account. Media never syncs (cards are text).
Soft-deleted refs retire their notes on the next tick — own-guid lookups
only, 90-day window, ``--no-retire`` opts out. Decay stats read back into
``meta.anki_stats``; a ``deck-<topic>`` tag maps to a ``Precis::<topic>``
sub-deck.

- `notes` — pure, anki-free helpers (guid/deck/tag conventions, ref→card spec,
  stats aggregation). Safe to import anywhere.
- `sync` — the engine (lazy-imports `anki`): upsert our authored notes, the
  guarded sync (bootstrap-download / incremental / abort-on-lossy-upload), and
  the stats read-back.
- `fix` — the `precis anki-sync --fix` flow: a card tagged ``precis-fix`` in
  Anki + a comment → LLM rewrite → written back (per-card opt-in widening of
  own-notes-only).
- `project` — read-only PG projection of *foreign* Anki cards (any notetype)
  as `anki` refs (``meta.source=anki-foreign``), content-hash-gated re-embeds,
  vanished cards soft-deleted.

The leech-finder read (``get(kind='anki', id='/leeches')``) lives in
:mod:`precis.handlers.anki`.
"""

from __future__ import annotations
