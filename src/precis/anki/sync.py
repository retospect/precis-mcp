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

from dataclasses import dataclass
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


class AnkiSyncError(Exception):
    """Base for sync failures."""


class AnkiNotInstalled(AnkiSyncError):
    """The `anki` pylib is not importable on this runner."""


def _import_anki() -> tuple[Any, Any]:
    try:
        import anki.sync_pb2 as sp  # type: ignore[import-not-found]
        from anki.collection import Collection  # type: ignore[import-not-found]
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
    stats_written: int = 0
    fix_requested: int = 0
    fix_applied: int = 0
    all_cards: list[ForeignCard] | None = None  # populated when project=True
    aborted: str | None = None

    def summary(self) -> str:
        if self.aborted:
            return f"ABORTED: {self.aborted}"
        boot = " (bootstrap download)" if self.bootstrapped else ""
        fix = (
            f", {self.fix_applied}/{self.fix_requested} precis-fix"
            if self.fix_requested
            else ""
        )
        return (
            f"synced{boot}: {self.pushed} new, {self.updated} updated, "
            f"{self.stats_written} stat rows read back{fix}"
        )


# ── collection-level ops (no network — testable against a local .anki2) ──


def upsert_notes(
    col: Any, specs: list[AnkiCardSpec], deck: str = PRECIS_DECK
) -> tuple[int, int]:
    """Insert-or-update our authored notes by deterministic guid. Returns
    ``(pushed, updated)``. Add-only-own-notes: only touches notes whose guid we
    minted, so a foreign note is unreachable here."""
    cloze = col.models.by_name("Cloze")
    if cloze is None:
        raise AnkiSyncError("stock Cloze notetype missing from the collection")
    pushed = updated = 0
    for spec in specs:
        guid = guid_for(spec.ref_id)
        existing = col.db.list("select id from notes where guid = ?", guid)
        if existing:
            note = col.get_note(existing[0])
            changed = False
            for name, val in spec.fields.items():
                if name in note.keys() and note[name] != val:  # noqa: SIM118 (anki Note, not a dict)
                    note[name] = val
                    changed = True
            if changed:
                col.update_note(note)
                updated += 1
        else:
            note = col.new_note(cloze)
            note.guid = guid
            for name, val in spec.fields.items():
                if name in note.keys():  # noqa: SIM118 (anki Note, not a dict)
                    note[name] = val
            note.tags = [precis_tag(spec.ref_id), MANAGED_TAG]
            # decks.id() creates the (sub-)deck if absent — `Precis::chinese`.
            col.add_note(note, col.decks.id(spec.deck or deck))
            pushed += 1
    return pushed, updated


def read_precis_stats(col: Any) -> dict[int, dict[str, Any]]:
    """Decay signal (interval/ease/reps/lapses/due) for our cards, keyed by
    ref_id. A cloze note's N cards are folded via `aggregate_stats`."""
    rows = col.db.all(
        "select n.guid, c.ivl, c.factor, c.reps, c.lapses, c.due, c.queue "
        "from notes n join cards c on c.nid = n.id "
        f"where n.guid like '{GUID_PREFIX}%'"
    )
    by_ref: dict[int, list[tuple[int, int, int, int, int, int]]] = {}
    for guid, ivl, factor, reps, lapses, due, queue in rows:
        rid = ref_id_from_guid(guid)
        if rid is None:
            continue
        by_ref.setdefault(rid, []).append((ivl, factor, reps, lapses, due, queue))
    return {rid: aggregate_stats(cards) for rid, cards in by_ref.items()}


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
) -> tuple[SyncResult, dict[int, dict[str, Any]]]:
    """One full tick against AnkiWeb. Account-safe by construction: a required
    full sync is resolved by *downloading* (only our regenerable mirror is at
    risk); a required full *upload* is refused.

    When ``fix`` is set, runs the precis-fix pass (LLM rewrites of cards the user
    tagged `precis-fix` in Anki) after the download and before the push, so the
    fixes ride the same sync up."""
    Collection, sp = _import_anki()
    CR = sp.SyncCollectionResponse.ChangesRequired
    col = Collection(mirror_path)
    result = SyncResult()
    try:
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
            col.full_upload_or_download(
                auth=auth, server_usn=out.server_media_usn, upload=False
            )
            col.reopen(after_full_sync=True)
            result.bootstrapped = True

        if fix:
            from precis.anki.fix import run_fix_pass

            fix_res = run_fix_pass(col, model=fix_model)
            result.fix_requested = fix_res.requested
            result.fix_applied = fix_res.fixed

        result.pushed, result.updated = upsert_notes(col, specs, deck)

        out2 = col.sync_collection(auth, False)
        if out2.required == CR.FULL_UPLOAD:
            # Our incremental add somehow escalated — do NOT overwrite the
            # account. Our notes stay in the mirror; the next tick retries.
            result.aborted = (
                "incremental push escalated to FULL_UPLOAD — refused; notes "
                "held locally, will retry next tick"
            )

        stats = read_precis_stats(col)
        result.stats_written = len(stats)
        if project:
            # Read the whole collection off the mirror for the read-only PG
            # projection (the CLI, which holds the store, does the upsert).
            result.all_cards = read_all_cards(col)
        return result, stats
    finally:
        col.close()
