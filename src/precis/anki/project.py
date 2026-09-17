"""Slice 3 — read-only PG projection of foreign Anki cards.

Every card the user made *in Anki* (any notetype) is mirrored into Postgres as a
**read-only** `anki` ref so the whole collection is searchable in precis and
feeds the retention knowledge-model. The `.anki2` mirror / AnkiWeb stays the
source of truth; PG holds a derived, disposable, re-syncable index (like paper
chunks vs the PDF on disk) — so it can never corrupt the account.

`project_cards(store, cards, owner_login=)` takes plain `ForeignCard`s (from
`sync.read_all_cards`) — no `anki` import — so it tests against a real PG store
with no wheel and no network. Idempotent + cheap on re-sync: a per-card content
hash means only *changed* cards re-embed; vanished cards are soft-deleted.
Each projected ref carries `owner_login` (migration 0164) — the web user whose
AnkiWeb account the card came from; a pre-existing unowned row (projected
before per-user ownership existed) is claimed for `owner_login` the next time
its content changes, belt-and-braces alongside the sync's one-shot
`claim_unowned_refs` catch-up.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from precis.anki.notes import ref_id_from_guid
from precis.store import Tag

#: `meta.source` marking a projected foreign card, and its human-findable flag.
FOREIGN_SOURCE = "anki-foreign"
FOREIGN_FLAG = "anki-foreign"

_CLOZE_RE = re.compile(r"\{\{c\d+::(.+?)(?:::.+?)?\}\}", re.DOTALL)
_HTML_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Anki fields hold HTML + cloze markup; reduce to plain search text."""
    t = _CLOZE_RE.sub(r"\1", text)
    t = _HTML_RE.sub(" ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(t.split())


def searchable_text(fields: dict[str, str]) -> str:
    return "\n".join(_plain(v) for v in fields.values() if v and v.strip())


def title_for(fields: dict[str, str]) -> str:
    for v in fields.values():
        p = _plain(v)
        if p:
            return p[:120]
    return "(empty card)"


def content_sha(fields: dict[str, str], notetype: str) -> str:
    payload = json.dumps(
        {"nt": notetype, "f": fields}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProjectResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_own: int = 0

    def summary(self) -> str:
        return (
            f"projected: {self.inserted} new, {self.updated} changed, "
            f"{self.unchanged} unchanged, {self.deleted} removed "
            f"({self.skipped_own} own skipped)"
        )


def _foreign_index(
    store: Any, owner_login: str
) -> dict[str, tuple[int, str | None, str | None]]:
    """guid → (ref_id, content_sha, owner_login) for every live projected
    foreign ref that's either already ``owner_login``'s or unowned (a legacy
    row projected before per-user ownership existed) — an unowned hit is how
    a legacy card gets claimed on its next content change."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "select ref_id, meta->'anki'->>'guid', meta->'anki'->>'content_sha', "
            "owner_login "
            "from refs where kind = 'anki' and retired_at is null "
            "and meta->>'source' = %s and (owner_login = %s or owner_login is null)",
            (FOREIGN_SOURCE, owner_login),
        ).fetchall()
    return {g: (r, sha, owner) for r, g, sha, owner in rows if g}


def project_cards(store: Any, cards: list[Any], *, owner_login: str) -> ProjectResult:
    """Upsert ``owner_login``'s foreign cards into PG as read-only `anki`
    refs; soft-delete any whose guid vanished from the mirror. precis-authored
    notes (guid `precis:<id>`) are skipped — they're already authoritative
    refs."""
    res = ProjectResult()
    existing = _foreign_index(store, owner_login)
    seen: set[str] = set()

    for c in cards:
        if c.ref_id is not None or ref_id_from_guid(c.guid) is not None:
            res.skipped_own += 1
            continue
        seen.add(c.guid)
        sha = content_sha(c.fields, c.notetype)
        stats = c.stats or {}
        prior = existing.get(c.guid)
        if prior is not None and prior[1] == sha:
            # Content unchanged → refresh only the recall stats (they move on
            # every review). A cheap meta-only patch — NO card_combined re-emit,
            # so no re-embed. Keeps the leech-finder current for free.
            if stats:
                store.update_ref(prior[0], meta_patch={"anki_stats": stats})
            if prior[2] is None:
                store.set_ref_owner(prior[0], owner_login)
            res.unchanged += 1
            continue

        meta = {
            "source": FOREIGN_SOURCE,
            "readonly": True,
            "notetype": c.notetype,
            "deck": c.deck,
            "fields": c.fields,
            "anki_stats": stats,
            "anki": {"guid": c.guid, "note_id": c.note_id, "content_sha": sha},
        }
        text = searchable_text(c.fields)
        title = title_for(c.fields)
        if prior is None:
            with store.tx() as conn:
                ref = store.insert_ref(
                    kind="anki",
                    slug=None,
                    title=title,
                    meta=meta,
                    owner_login=owner_login,
                    conn=conn,
                )
                store.add_tag(ref.id, Tag.flag(FOREIGN_FLAG), conn=conn)
                store.chunks.upsert_card_combined(ref.id, text, conn=conn)
            res.inserted += 1
        else:
            with store.tx() as conn:
                store.update_ref(prior[0], title=title, meta_patch=meta, conn=conn)
                store.chunks.upsert_card_combined(prior[0], text, conn=conn)
                if prior[2] is None:
                    store.set_ref_owner(prior[0], owner_login, conn=conn)
            res.updated += 1

    for guid, (ref_id, _sha, _owner) in existing.items():
        if guid not in seen:
            store.retire_ref(ref_id)
            res.deleted += 1
    return res
