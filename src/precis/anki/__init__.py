"""Anki integration — per-user headless AnkiWeb sync for the `anki` cloze kind.

precis is the Anki *client*, one relationship per web user: each account holds
its own AnkiWeb credentials (`creds`, vaulted, self-service from ``/account``)
and its own local `.anki2` mirror (`<PRECIS_ANKI_MIRROR_DIR>/<login>/`), and
every `anki` ref carries `refs.owner_login` (migration 0164) — the login it
belongs to. `anki` wheel sync is lazy-imported; installed on the one
designated sync runner by ansible, gated behind PRECIS_ANKI_ENABLED. Sync is
add-only-own-notes by deterministic guid (``precis:<ref_id>`` — a text edit
updates the note in place, never re-guids, so Anki's scheduling history
survives); the guard allows FULL_DOWNLOAD but **refuses FULL_UPLOAD**, so
precis can never clobber an account. Media never syncs (cards are text).
Soft-deleted refs retire their notes on the next tick, scoped to their
owner — own-guid lookups only, 90-day window, ``--no-retire`` opts out. Decay
stats read back into ``meta.anki_stats``; a ``deck-<topic>`` tag maps to a
``Precis::<topic>`` sub-deck.

- `creds` — per-user AnkiWeb credentials in the secrets vault; also
  `anki_logins()`, the roster the sync fans out over.
- `notes` — pure, anki-free helpers (guid/deck/tag conventions, ref→card spec,
  stats aggregation). Safe to import anywhere.
- `sync` — the engine (lazy-imports `anki`): upsert our authored notes, the
  guarded sync (bootstrap-download / incremental / abort-on-lossy-upload), and
  the stats read-back.
- `fix` — the `precis anki-sync --fix` flow: a card tagged ``precis-fix`` in
  Anki + a comment → LLM rewrite → written back (per-card opt-in widening of
  own-notes-only).
- `project` — read-only PG projection of *foreign* Anki cards (any notetype)
  as `anki` refs (``meta.source=anki-foreign``) owned by the syncing login,
  content-hash-gated re-embeds, vanished cards soft-deleted.

The leech-finder read (``get(kind='anki', id='/leeches')``) lives in
:mod:`precis.handlers.anki`.
"""

from __future__ import annotations
