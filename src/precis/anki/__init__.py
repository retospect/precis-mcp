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
precis can never overwrite the account's collection. Image attachments are
fetched at card creation and stored with the ref so retries and rebuilt
mirrors use the same bytes after the source changes or disappears. The
worker writes content-addressed files through Anki's media manager and
waits for separate, two-way media sync on accounts with attached cards.
The initial transfer can download the account's existing media library
to the runner. After a media timeout, the collection receipts still
advance and the next tick retries media. Card metadata views show the
attachment filename and byte size. The ``Text`` field uses the filename
returned by Anki, including when Anki renames a file to avoid a collision.
Soft-deleted refs retire their notes on the next tick, scoped to their
owner — own-guid lookups only, 90-day window, ``--no-retire`` opts out. Decay
stats read back into ``meta.anki_stats``. Recent graded revlog entries feed
the private learning calendar even for users with no précis-authored cards;
their timestamps, instead of cumulative card stats, preserve the review day. A ``deck-<topic>`` tag maps to a
``Precis::<topic>`` sub-deck. An effective deck retag stamps a revision
atomically with the desired deck. The sync worker acknowledges that exact
revision after a completed push, so full-sync interruptions and overlapping
retags leave work pending. Existing notes without a recorded retag keep
their Anki deck on rollout, preserving manual moves.

Anki-side deletions retire the authored ref rather than recreating its note.
The pre-download mirror and ``meta.anki_synced_at`` distinguish missing
previously synced notes from new adds, including after a mirror rebuild.
Undoing a web dismissal explicitly queues the original ref for re-push.
Sync honors that intent until a completed push confirms the note is back.

- `creds` — per-user AnkiWeb credentials in the secrets vault; also
  `anki_logins()`, the roster the sync fans out over.
- `notes` — pure, anki-free helpers (guid/deck/tag conventions, ref→card spec,
  stats aggregation). Safe to import anywhere.
- `cloze` — brace-aware scanning and acronym hint checks. Manual puts warn
  so the author can review the wording. Automated reading-card writes reject
  detected leaks before creating a note.
- `sync` — the engine (lazy-imports `anki`): upsert our authored notes, the
  guarded sync (bootstrap-download / incremental / abort-on-lossy-upload), and
  the card stats and dated review read-back.
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
