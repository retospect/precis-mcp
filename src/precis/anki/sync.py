"""The Anki sync engine — lazy-imports the `anki` pylib.

Three testable-in-isolation pieces that take an open `Collection` (so they run
against a throwaway *local* collection with no network), plus the network
orchestrator `sync_tick`:

- `upsert_notes(col, specs)` — add-only-own-notes: insert-or-update our notes by
  deterministic guid. Never touches a foreign note.
- `read_precis_stats(col)` — the decay signal for our cards, keyed by ref_id.
- `read_all_cards(col)` — every note incl. foreign (for accessibility / the
  precis-fix loop); pure read, so it can never corrupt.
- `sync_tick(...)` — login → guarded sync (bootstrap-download / incremental /
  abort-on-lossy-upload) → upsert → push → read-back.

The guard (design floor: never corrupt the account): a `FULL_DOWNLOAD` only
risks our *regenerable* local mirror, so it is allowed. A `FULL_UPLOAD` would
overwrite AnkiWeb, so it is **refused** — the tick aborts and reports.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any

from precis.anki.notes import (
    GUID_PREFIX,
    MANAGED_TAG,
    PRECIS_DECK,
    AnkiCardSpec,
    aggregate_stats,
    guid_for,
    precis_tag,
    ref_id_from_guid,
)

log = logging.getLogger(__name__)


class AnkiSyncError(Exception):
    """Base for sync failures."""


class AnkiNotInstalled(AnkiSyncError):
    """The `anki` pylib is not importable on this runner."""


def _import_anki() -> tuple[Any, Any]:
    try:
        import anki.sync_pb2 as sp
        from anki.collection import Collection
    except ModuleNotFoundError as e:  # pragma: no cover - env-dependent
        raise AnkiNotInstalled(
            "the `anki` package is not installed on this runner. Install it with "
            "`pip install anki` (a prebuilt wheel — no Rust toolchain needed). "
            "Only the designated sync runner needs it; the pass is gated behind "
            "PRECIS_ANKI_ENABLED and merges dark elsewhere."
        ) from e
    return Collection, sp


@dataclass
class SyncResult:
    bootstrapped: bool = False
    pushed: int = 0
    updated: int = 0
    removed: int = 0
    stats_written: int = 0
    review_ids: list[int] = field(default_factory=list)
    fix_requested: int = 0
    fix_applied: int = 0
    all_cards: list[ForeignCard] | None = None  # populated when project=True
    aborted: str | None = None
    deleted_ref_ids: list[int] = field(default_factory=list)
    boosted_ref_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.aborted:
            return f"ABORTED: {self.aborted}"
        boot = " (bootstrap download)" if self.bootstrapped else ""
        rem = f", {self.removed} retired" if self.removed else ""
        fix = (
            f", {self.fix_applied}/{self.fix_requested} precis-fix"
            if self.fix_requested
            else ""
        )
        boost = f", {len(self.boosted_ref_ids)} boosted" if self.boosted_ref_ids else ""
        return (
            f"synced{boot}: {self.pushed} new, {self.updated} updated{rem}, "
            f"{self.stats_written} stat rows read back{fix}{boost}"
        )


# ── collection-level ops (no network — testable against a local .anki2) ──


def upsert_notes(
    col: Any, specs: list[AnkiCardSpec], deck: str = PRECIS_DECK
) -> tuple[int, int]:
    """Insert-or-update our authored notes by deterministic guid. Returns
    ``(pushed, updated)``. Add-only-own-notes: only touches notes whose guid we
    minted, so a foreign note is unreachable here. A pending deck retag
    relocates the existing note's cards in place. Otherwise the cards keep
    their Anki deck, including manual moves made before retag tracking.
    Attachments are written to the mirror's media collection and an
    image tag with Anki's returned filename is appended to ``Text``."""
    cloze = col.models.by_name("Cloze")
    if cloze is None:
        raise AnkiSyncError("stock Cloze notetype missing from the collection")
    pushed = updated = 0
    for spec in specs:
        fields = spec.fields
        if spec.image is not None:
            filename = col.media.write_data(*spec.image)
            fields = {
                **fields,
                "Text": f'{fields["Text"]}<br><img src="{escape(filename)}">',
            }
        guid = guid_for(spec.ref_id)
        existing = col.db.list("select id from notes where guid = ?", guid)
        if existing:
            note = col.get_note(existing[0])
            changed = False
            for name, val in fields.items():
                if name in note.keys() and note[name] != val:  # noqa: SIM118 (anki Note, not a dict)
                    note[name] = val
                    changed = True
            legacy = f"precis::{spec.ref_id}"
            if legacy in note.tags:  # fold into the collapsible precis::id tree
                note.tags = [t for t in note.tags if t != legacy]
                note.tags.append(precis_tag(spec.ref_id))
                changed = True
            if changed:
                col.update_note(note)
            if spec.pending_deck_revision is not None:
                target_did = col.decks.id(spec.deck or deck)
                cids = [c.id for c in note.cards() if c.did != target_did]
                if cids:
                    col.set_deck(cids, target_did)
                    changed = True
            if changed:
                updated += 1
        else:
            note = col.new_note(cloze)
            note.guid = guid
            for name, val in fields.items():
                if name in note.keys():  # noqa: SIM118 (anki Note, not a dict)
                    note[name] = val
            note.tags = [precis_tag(spec.ref_id), MANAGED_TAG]
            # decks.id() creates the (sub-)deck if absent — `Precis::chinese`.
            col.add_note(note, col.decks.id(spec.deck or deck))
            pushed += 1
    return pushed, updated


def remove_notes_for_refs(col: Any, ref_ids: list[int]) -> int:
    """Delete our notes for retired (soft-deleted) precis refs. Returns how many
    notes were removed. Own-notes-only by construction: lookups go through the
    deterministic ``precis:<ref_id>`` guid, so a foreign note is unreachable —
    the same floor `upsert_notes` holds. Guids absent from the mirror are
    silently skipped (never pushed, or already gone)."""
    removed = 0
    for rid in ref_ids:
        nids = col.db.list("select id from notes where guid = ?", guid_for(rid))
        if nids:
            col.remove_notes(nids)
            removed += len(nids)
    return removed


def _collection_today(col: Any) -> int:
    """Anki's day number for right now — days since the collection's
    creation-day cutoff, which is what a review card's ``due`` compares
    against. ``sched.today`` is the cached scheduler value on every pylib
    version in play; the ``crt`` fallback covers the rest."""
    today = getattr(col.sched, "today", None)
    if isinstance(today, int):
        return today
    crt = getattr(col, "crt", None)
    if crt:
        return int((time.time() - crt) // 86400)
    return 0


def bring_due_today(col: Any, ref_ids: list[int]) -> list[int]:
    """Apply pending boosts (``meta.anki_boost_due``, gr664414): set our
    review-queue cards' due date to today so they surface at the next review.
    Returns the ref_ids that actually moved a card — only those boosts are
    consumed and have their marker cleared by the caller.

    Own-guid lookups only, so foreign notes are unreachable. Only queue 2/3
    (review / day-learning) cards carry a day-numbered due date; a note not
    yet in the mirror, or one whose cards are all still new or mid-step,
    keeps its marker queued — nothing moved, so a later tick applies the
    boost once a card can move. Everything else about the card — interval,
    ease, reps, lapses — is left untouched: a boost never rewrites review
    history. ``usn = -1`` marks the row locally-modified so the change rides
    the incremental push."""
    if not ref_ids:
        return []
    today = _collection_today(col)
    consumed: list[int] = []
    for rid in ref_ids:
        nids = col.db.list("select id from notes where guid = ?", guid_for(rid))
        if not nids:
            continue  # not in the mirror (yet) — leave the marker for a later tick
        cids = col.db.list(
            "select id from cards where nid = ? and queue in (2, 3)", nids[0]
        )
        if not cids:
            continue  # still new or mid-step: no day-numbered due to move
        marks = ", ".join("?" for _ in cids)
        col.db.execute(
            f"update cards set due = ?, usn = -1 where id in ({marks})",
            today,
            *cids,
        )
        consumed.append(rid)
    return consumed


def read_precis_stats(col: Any) -> dict[int, dict[str, Any]]:
    """Decay signal (interval/ease/reps/lapses/due) for our cards, keyed by
    ref_id. A cloze note's N cards are folded via `aggregate_stats`.
    Count due reviews and interday learning cards at the sync's Anki day
    boundary, using the original due for cards temporarily in filtered decks."""
    rows = col.db.all(
        "select n.guid, c.ivl, c.factor, c.reps, c.lapses, c.due, c.queue, c.odue "
        "from notes n join cards c on c.nid = n.id "
        f"where n.guid like '{GUID_PREFIX}%'"
    )
    today = _collection_today(col)
    by_ref: dict[int, list[tuple[int, int, int, int, int, int]]] = {}
    due_reviews: dict[int, int] = {}
    for guid, ivl, factor, reps, lapses, due, queue, original_due in rows:
        rid = ref_id_from_guid(guid)
        if rid is None:
            continue
        by_ref.setdefault(rid, []).append((ivl, factor, reps, lapses, due, queue))
        if queue in (2, 3) and (original_due or due) <= today:
            due_reviews[rid] = due_reviews.get(rid, 0) + 1
    return {
        rid: {**aggregate_stats(cards), "due_reviews": due_reviews.get(rid, 0)}
        for rid, cards in by_ref.items()
    }


@dataclass
class ForeignCard:
    """A note read from the mirror (any notetype) — for accessibility + the
    precis-fix loop. `ref_id` is set only for precis-owned notes. `stats` is the
    aggregated recall signal (interval/ease/reps/lapses/due) so the projection
    can carry it and the leech-finder can surface bad-recall cards."""

    note_id: int
    guid: str
    notetype: str
    deck: str
    tags: list[str]
    fields: dict[str, str]
    ref_id: int | None = None
    stats: dict[str, Any] | None = None


def read_all_cards(col: Any, *, tag: str | None = None) -> list[ForeignCard]:
    """Read every note (or those carrying ``tag``) off the mirror — pure read,
    so it can never corrupt. Powers 'keep clozes accessible in precis', the
    precis-fix loop (tag=`precis-fix`), and the retention model (each note's
    per-card stats aggregated for the leech-finder)."""
    out: list[ForeignCard] = []
    nids = col.find_notes(f"tag:{tag}") if tag else col.find_notes("")
    for nid in nids:
        note = col.get_note(nid)
        nt = note.note_type()
        cards = note.cards()
        deck_name = col.decks.name(cards[0].did) if cards else ""
        rows = [(c.ivl, c.factor, c.reps, c.lapses, c.due, c.queue) for c in cards]
        out.append(
            ForeignCard(
                note_id=int(nid),
                guid=note.guid,
                notetype=nt["name"] if nt else "?",
                deck=deck_name,
                tags=list(note.tags),
                fields={k: note[k] for k in note.keys()},  # noqa: SIM118 (anki Note)
                ref_id=ref_id_from_guid(note.guid),
                stats=aggregate_stats(rows),
            )
        )
    return out


# ── the network orchestrator ─────────────────────────────────────────────


def sync_tick(
    *,
    mirror_path: str,
    user: str,
    password: str,
    specs: list[AnkiCardSpec],
    deck: str = PRECIS_DECK,
    endpoint: str | None = None,
    fix: bool = False,
    fix_model: str | None = None,
    project: bool = False,
    retire_ref_ids: list[int] | None = None,
) -> tuple[SyncResult, dict[int, dict[str, Any]]]:
    """One full tick against AnkiWeb. Account-safe by construction: a required
    full sync is resolved by *downloading* (only our regenerable mirror is at
    risk); a required full *upload* is refused.

    When ``fix`` is set, runs the precis-fix pass (LLM rewrites of cards the user
    tagged `precis-fix` in Anki) after the download and before the push, so the
    fixes ride the same sync up.

    ``retire_ref_ids`` are soft-deleted authored refs (the card_forge
    retire/rewrite path) whose notes are removed from the mirror before the push
    — own-guid lookups only, so foreign notes stay unreachable.

    Specs flagged ``boost`` (``meta.anki_boost_due`` set — the /learning boost
    button, gr664414) get their mirror cards' due date brought forward to
    today, never touching review history. The returned ``boosted_ref_ids``
    (boosts that actually moved a mirror card) tell the caller which markers
    to clear; the rest stay queued for a later tick."""
    Collection, sp = _import_anki()
    CR = sp.SyncCollectionResponse.ChangesRequired
    col = Collection(mirror_path)
    result = SyncResult()
    try:
        # Remember our notes before pulling remote deletions. Persisted sync
        # receipts also cover a lost/rebuilt mirror and retries after a crash.
        # Locally dirty notes (usn = -1) can be unpushed adds from an aborted
        # tick. A full download dropping them is no evidence of remote deletion.
        known_refs = {
            rid
            for (guid,) in col.db.all("select guid from notes where usn != -1")
            if (rid := ref_id_from_guid(guid)) is not None
        }
        auth = col.sync_login(user, password, endpoint)
        out = col.sync_collection(auth, False)
        auth = sp.SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint or auth.endpoint)

        if out.required == CR.FULL_UPLOAD:
            result.aborted = (
                "server required FULL_UPLOAD before our writes — refusing to "
                "overwrite AnkiWeb; leaving both sides intact"
            )
            return result, {}
        if out.required in (CR.FULL_SYNC, CR.FULL_DOWNLOAD):
            # Account-safe: download replaces only our local mirror.
            col.close_for_full_sync()
            # A media USN starts background media sync. Defer that transfer
            # until after the note push, and only for accounts with attachments.
            col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
            col.reopen(after_full_sync=True)
            result.bootstrapped = True

        present_refs = {
            rid
            for (guid,) in col.db.all("select guid from notes")
            if (rid := ref_id_from_guid(guid)) is not None
        }
        result.deleted_ref_ids = [
            spec.ref_id
            for spec in specs
            if not spec.restore_pending
            and (spec.previously_synced or spec.ref_id in known_refs)
            and spec.ref_id not in present_refs
        ]
        deleted = set(result.deleted_ref_ids)
        specs = [spec for spec in specs if spec.ref_id not in deleted]

        if fix:
            from precis.anki.fix import run_fix_pass

            fix_res = run_fix_pass(col, model=fix_model)
            result.fix_requested = fix_res.requested
            result.fix_applied = fix_res.fixed

        if retire_ref_ids:
            result.removed = remove_notes_for_refs(col, retire_ref_ids)

        result.pushed, result.updated = upsert_notes(col, specs, deck)

        # Pending boosts ride this same push: bring the mirror card's due date
        # forward to today. Specs already had remotely-deleted refs removed, so
        # a boost can't resurrect one. The marker is cleared by the caller for
        # the returned (consumed) ref_ids only.
        result.boosted_ref_ids = bring_due_today(
            col, [spec.ref_id for spec in specs if spec.boost]
        )

        out2 = col.sync_collection(auth, False)
        if out2.required in (CR.FULL_UPLOAD, CR.FULL_SYNC, CR.FULL_DOWNLOAD):
            # The push didn't complete. Leave the mirror for the next tick's
            # guarded pull, and keep pending edits eligible to replay.
            result.aborted = (
                f"incremental push required {CR.Name(out2.required)}; "
                "will retry next tick without uploading the mirror"
            )
            return result, {}

        # Include the whole collection, even cards with no précis ref. Manual
        # schedule changes have ease 0. Keep the 90-day activity window plus
        # two days for local-date boundaries.
        cutoff_ms = (int(time.time()) - 92 * 86_400) * 1000
        result.review_ids = col.db.list(
            "SELECT id FROM revlog WHERE id >= ? AND ease > 0", cutoff_ms
        )

        if any(spec.image is not None for spec in specs):
            # Keep the mirror open for the background transfer. A timeout
            # leaves the completed collection push eligible for receipts.
            col.sync_media(
                sp.SyncAuth(hkey=auth.hkey, endpoint=out2.new_endpoint or auth.endpoint)
            )
            deadline = time.monotonic() + 600
            while col.media_sync_status().active:
                if time.monotonic() >= deadline:
                    col.abort_media_sync()
                    log.warning("Anki media sync timed out; will retry next tick")
                    break
                time.sleep(0.25)

        stats = read_precis_stats(col)
        result.stats_written = len(stats)
        if project:
            # Read the whole collection off the mirror for the read-only PG
            # projection (the CLI, which holds the store, does the upsert).
            result.all_cards = read_all_cards(col)
        return result, stats
    finally:
        col.close()
