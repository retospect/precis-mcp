"""Shared paper cite-key replacement and best-effort corpus PDF relocation."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from precis.corpus_layout import (
    corpus_pdf_dest,
    corpus_roots_from_env,
    resolve_local_pdf,
)
from precis.errors import BadInput, NotFound
from precis.identity import is_junk_cite_key
from precis.store._identifiers_ops import DROPPED_IDENTIFIERS_META_KEY
from precis.utils.handle_registry import try_format

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: Reason recorded on every automatic re-mint (gr521515) — the
#: ``cite_key_changed`` ref_event payload and the rekey receipt agree.
AUTO_REMINT_REASON = "auto re-mint after metadata repair"

#: Wall-clock bound every caller wraps the re-key in: the corpus-mount
#: probes (``is_file`` / ``mkdir`` / ``rename`` — a wedged NFS mount hangs
#: them unbounded) and the identifier-write lock waits share it. The web
#: route applies it via ``asyncio.wait_for`` + ``to_thread`` (its
#: ``_RENAME_SLUG_TIMEOUT_S`` is this constant); the MCP door via
#: :func:`precis.utils.deadline.call_with_deadline`. 2026-08-17: an
#: unbounded probe held a request 12+ minutes.
REKEY_DEADLINE_S = 30.0


def rekey_paper(
    store: Store,
    ref_id: int,
    new_key: str,
    *,
    reason: str,
    dry_run: bool = False,
    source: str = "mcp-edit",
    corpus_dirs: tuple[Path, ...] | None = None,
    old_pdf: Path | None = None,
    kinds: tuple[str, ...] = ("paper",),
) -> str:
    """Replace a paper's key, audit the change and move its local PDF.

    Identifier and audit writes share a transaction. As in the web rename,
    filesystem moves are best-effort after that commit: a failed move keeps
    the authoritative storage path and is named in the receipt. ``old_pdf``
    lets the web adapter supply its already-resolved, timeout-bounded probe.
    Dry runs validate ownership but never write or move anything.

    ``kinds`` names the ref kinds the door accepts — the cite_key handle is
    shared across the document family (the web meta panel and the
    ``PaperHandler`` subclasses both re-key cfp/datasheet refs), so callers
    pass their own kind set rather than hard-coding ``'paper'``.
    """
    next_call = "get(kind='skill', id='precis-paper-ref-help')"
    reason_call = (
        f"edit(kind='{kinds[0]}', id='{ref_id}', "
        f"args={{'cite_key': {new_key!r}, 'reason': '<why>'}})"
    )
    if not isinstance(new_key, str) or not re.fullmatch(r"[a-z0-9]+", new_key):
        raise BadInput(
            f"cite_key={new_key!r} is invalid — use lowercase letters and digits only",
            next=next_call,
        )
    if not reason.strip():
        raise BadInput("cite_key needs a reason for its ref_event", next=reason_call)
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or ref.kind not in kinds:
        raise NotFound(
            f"no live {'/'.join(kinds)} with ref_id={ref_id}",
            next=f"search(kind='{kinds[0]}', q='...')",
        )
    old_key = ref.slug or ""
    handle = try_format(ref.kind, ref_id) or f"{ref.kind}:{ref_id}"
    owner = store.find_ref_by_identifier("cite_key", new_key)
    if owner is not None and owner != ref_id:
        raise BadInput(
            f"cite_key={new_key!r} already belongs to ref id={owner}",
            next=next_call,
        )
    # A displaced key is RESERVED (gr521515): a value another ref holds
    # as a cite_key_alias redirect is never handed out again — taking it
    # would silently retarget every ``\cite{<old>}`` onto this ref.
    with store.pool.connection() as conn:
        alias_owner = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'cite_key_alias' AND id_value = %s",
            (new_key,),
        ).fetchone()
    if alias_owner is not None and int(alias_owner[0]) != ref_id:
        raise BadInput(
            f"cite_key={new_key!r} is held as a cite_key_alias redirect "
            f"by ref id={int(alias_owner[0])} — a displaced key stays "
            "reserved, it is never re-minted",
            next=next_call,
        )
    if new_key == old_key:
        return f"no change: {handle} already has cite_key={new_key!r}."
    # A ref can hold several live cite_key rows (a .bib import registers
    # the entry's own key additively, gr517275); the rekey deletes them
    # ALL and re-homes each displaced value as a cite_key_alias — name
    # every one so the preview and receipt never silently demote a key
    # (gr763051).
    extras = [k for k in store.ref_cite_keys(ref_id) if k != old_key]
    change = f"{handle} cite_key: {old_key!r} → {new_key!r}"
    if dry_run:
        suppressed = _suppressed_dropped_values(ref)
        displaced = _displaced_clause(
            [k for k in extras if k not in suppressed],
            [k for k in extras if k in suppressed],
        )
        return (
            f"dry run: {change}{displaced}"
            "; would move its local PDF and log the reason."
        )

    roots = corpus_roots_from_env() if corpus_dirs is None else corpus_dirs
    sha = ref.pdf_sha256
    stored_path = store.pdf_storage_path(sha) if sha else None
    # The on-disk file may sit under ANY of the ref's live keys (the
    # fetcher names it by whichever key it saw), so probe them all.
    old_pdf = resolve_local_pdf(roots, stored_path, [old_key, *extras]) or old_pdf
    try:
        with store.pool.connection() as conn:
            extras = [k for k in store.ref_cite_keys(ref_id, conn=conn) if k != old_key]
            changed = store.set_ref_identifier(
                ref_id, "cite_key", new_key, source=source, conn=conn
            )
            demoted: list[str] = []
            lost: list[str] = []
            if changed and extras:
                landed = {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT id_value FROM ref_identifiers "
                        "WHERE ref_id = %s AND id_kind = 'cite_key_alias' "
                        "AND id_value = ANY(%s)",
                        (ref_id, extras),
                    ).fetchall()
                }
                demoted = [k for k in extras if k in landed]
                lost = [k for k in extras if k not in landed]
            if changed:
                payload: dict[str, Any] = {
                    "old": old_key,
                    "new": new_key,
                    "reason": reason.strip(),
                }
                if demoted:
                    payload["demoted"] = demoted
                if lost:
                    payload["suppressed"] = lost
                store.append_event(
                    ref_id,
                    source=source,
                    event="cite_key_changed",
                    payload=payload,
                    conn=conn,
                )
    except BadInput as exc:
        # The setter also guards ownership if a key changed after the probe.
        raise BadInput(exc.cause, next=next_call) from exc
    except UniqueViolation as exc:
        # A concurrent write claimed the key between the probe above and
        # this tx's INSERT — report it like the probe's collision refusal.
        raise BadInput(
            f"cite_key={new_key!r} was claimed by another ref mid-rekey",
            next=next_call,
        ) from exc
    if not changed:
        # Nothing was written — e.g. this ref already holds new_key among
        # several live cite_key rows. Say so: the other rows stay live.
        still = [old_key, *[k for k in extras if k != new_key]]
        others = (
            "; still live as cite_key: " + ", ".join(repr(k) for k in still)
            if still
            else ""
        )
        return f"no change: {handle} already has cite_key={new_key!r}.{others}"
    note = ""
    if old_pdf is not None:
        # The destination lives under the corpus root holding the file —
        # an off-convention storage_path (old_pdf.parent.parent ≠ a
        # configured root) must not push the move outside it.
        root = next(
            (r for r in roots if old_pdf.is_relative_to(r)),
            old_pdf.parent.parent,
        )
        new_pdf = corpus_pdf_dest(new_key, root)
        try:
            new_pdf.parent.mkdir(parents=True, exist_ok=True)
            if new_pdf != old_pdf and new_pdf.exists():
                # Never redirect the attachment to an unrelated existing file.
                note = " PDF not moved: destination already exists; old path kept."
            else:
                moved = new_pdf != old_pdf
                if moved:
                    old_pdf.rename(new_pdf)
                try:
                    if sha:
                        store.set_pdf_storage_path(sha, str(new_pdf))
                except Exception as exc:
                    # The file moved but the storage-path write failed —
                    # put it back so the recorded (old) path still finds
                    # it; otherwise a "no change" retry leaves the file
                    # orphaned off its authoritative location.
                    restored = not moved
                    if moved:
                        try:
                            new_pdf.rename(old_pdf)
                            restored = True
                        except OSError:
                            log.warning(
                                "rekey_paper: could not restore %s to %s "
                                "after set_pdf_storage_path failed",
                                new_pdf,
                                old_pdf,
                                exc_info=True,
                            )
                    note = (
                        f" PDF not moved: {type(exc).__name__}; old storage path kept."
                        if restored
                        else f" PDF stranded at {new_pdf}: "
                        f"{type(exc).__name__} recording the new storage "
                        "path, then the rollback rename failed too — the "
                        f"file sits at {new_pdf} while storage_path still "
                        f"names {old_pdf}."
                    )
                else:
                    note = (
                        " Stored PDF moved; storage path updated."
                        if sha
                        else " PDF moved."
                    )
        except OSError:
            note = " PDF not moved: filesystem unavailable; old storage path kept."
    else:
        note = " No local PDF found; not moved."
    return (
        f"updated {change}{_displaced_clause(demoted, lost)} "
        f"(ref_event 'cite_key_changed' logged).{note}"
    )


def _suppressed_dropped_values(ref: Any) -> set[str]:
    """cite_key-ish values the drop-door marker keeps suppressed on ``ref``.

    ``refs.meta.dropped_identifiers`` (gr517591) is ``{scheme: {value:
    {...}}}``: a displaced live cite_key row whose ``cite_key`` or
    ``cite_key_alias`` pair is marked is NOT re-homed as an alias — the
    operator's suppression stands. Lets the dry run predict exactly what
    :meth:`Store.set_ref_identifier` will skip.
    """
    meta = ref.meta if isinstance(getattr(ref, "meta", None), dict) else {}
    dropped = meta.get(DROPPED_IDENTIFIERS_META_KEY)
    out: set[str] = set()
    if isinstance(dropped, dict):
        for scheme in ("cite_key", "cite_key_alias"):
            values = dropped.get(scheme)
            if isinstance(values, dict):
                out.update(str(v) for v in values)
    return out


def _displaced_clause(demoted: list[str], suppressed: list[str]) -> str:
    """``;``-clauses naming every OTHER live key a rekey displaces (gr763051).

    ``demoted`` re-homes to ``cite_key_alias``; ``suppressed`` loses its
    live row with no redirect because its drop-door mark stands.
    """
    out = ""
    if demoted:
        out += (
            "; "
            + ", ".join(repr(k) for k in demoted)
            + " also demoted to cite_key_alias"
        )
    if suppressed:
        out += (
            "; "
            + ", ".join(repr(k) for k in suppressed)
            + " also displaced — drop-door suppression stands, no cite_key_alias"
        )
    return out


def remint_junk_cite_key(
    store: Store,
    ref_id: int,
    *,
    source: str,
    conn: Connection | None = None,
    adopt_from_ref_id: int | None = None,
) -> str | None:
    """Re-mint a junk-shaped cite_key after a metadata repair (gr521515).

    Fires only when the paper's **current** key matches a junk mint
    (:func:`~precis.identity.is_junk_cite_key` — ``anon*``/``user*``/
    ``the*``) AND :meth:`Store.suggest_cite_key` on the repaired
    authors+year yields a *different, non-junk* key: a repair that still
    produces ``anon`` (no usable byline) leaves the key alone, and a
    collision lands on ``suggest_cite_key``'s letter disambiguation, never
    a stolen key. The displaced key stays resolvable — the
    ``cite_key_alias`` redirect :meth:`Store.set_ref_identifier` writes.

    Two entry shapes:

    * ``conn=None`` — post-commit path for a caller whose write already
      landed (the ``edit`` door, ``apply_resolution``): re-reads the ref
      and runs the full :func:`rekey_paper`, PDF move included.
    * ``conn=<open transaction>`` — in-transaction path for a caller
      mid-write (stub reconcile / merge): reads and flips the identifier
      on the caller's conn and audits the same event. The PDF is *not*
      moved (a filesystem rename can't join the caller's commit); the
      file stays findable through ``pdfs.storage_path``, the
      authoritative lookup. The leg runs inside a savepoint
      (``conn.transaction()``) so a mid-flight SQL error — e.g. a
      concurrent claim aborting the setter's INSERT — rolls back only
      the remint, leaving the caller's transaction usable.

    ``adopt_from_ref_id`` names a merge/stub-reconcile loser that is
    about to retire: when its own cite_key is non-junk, the survivor
    *adopts* it (the value moves off the loser and becomes the
    survivor's live key) instead of minting a suffix — drafts citing
    the loser's key keep resolving, which a fresh ``<base><letter>``
    would silently break. The loser's other cite_key values re-home as
    ``cite_key_alias`` redirects on the survivor. Falls back to the
    suggester when the loser has no non-junk key to adopt.

    ``source`` must be a machine-class source (not ``web-edit``/
    ``mcp-edit``/``edit``): a deliberate ``drop_identifier`` suppression
    must still refuse the remint. Returns the receipt line, or ``None``
    when no re-mint applied (never raises — a sweep is not sunk by one
    key it cannot fix).
    """
    try:
        if conn is not None:
            with conn.transaction():
                return _remint_in_tx(
                    store,
                    ref_id,
                    source=source,
                    conn=conn,
                    adopt_from_ref_id=adopt_from_ref_id,
                )
        ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
        if ref is None or ref.kind != "paper":
            return None
        new_key = _remint_target(store, ref.id, ref.slug or "", ref.authors, ref.year)
        if new_key is None:
            return None
        return rekey_paper(
            store, ref_id, new_key, reason=AUTO_REMINT_REASON, source=source
        )
    except Exception:
        log.warning(
            "remint_junk_cite_key: ref %d re-mint failed", ref_id, exc_info=True
        )
        return None


def record_rehomed_cite_keys(
    conn: Connection,
    loser_ref_id: int,
    values: list[str],
    *,
    adopted: str | None = None,
    survivor_prior_key: str | None = None,
    rehomed_aliases: list[str] | None = None,
) -> None:
    """Stamp on the merge loser exactly which cite_key values were
    re-homed to the survivor (``meta.rehomed_cite_keys``), which of its
    own ``cite_key_alias`` redirects moved with them
    (``meta.rehomed_cite_key_aliases`` — the loser's earlier rekey
    demotions), plus, when the survivor *adopted* a value outright as
    its live cite_key, which value and which survivor key it displaced
    (gr524022 restore fix).

    The merge's re-home is a plain identifier move — nothing else records
    that a survivor's ``cite_key_alias`` came from THIS loser rather than
    being survivor-owned, so a restore that chased any alias could give
    the loser the survivor's own displaced key, or a wrong loser's key.
    :meth:`Store.restore_superseded_ref` moves back exactly this recorded
    set (and, for an adopted value, first puts the survivor's displaced
    key back), then clears the markers."""
    updates: dict[str, Any] = {}
    if values:
        updates["rehomed_cite_keys"] = [str(v) for v in values]
    if rehomed_aliases:
        updates["rehomed_cite_key_aliases"] = [str(v) for v in rehomed_aliases]
    if adopted is not None:
        updates["adopted_cite_key"] = str(adopted)
        if survivor_prior_key:
            updates["adopted_displaced_key"] = str(survivor_prior_key)
    if not updates:
        return
    conn.execute(
        "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
        (Jsonb(updates), loser_ref_id),
    )


def _remint_target(
    store: Store,
    ref_id: int,
    old_key: str,
    authors: Any,
    year: int | None,
    *,
    conn: Connection | None = None,
) -> str | None:
    """The suggested replacement key, or ``None`` when no re-mint applies.

    A record with no publication year never re-mints: the suggested key
    would carry the ``00`` placeholder — digits indistinguishable from
    the junk mint's own (gr521515 dry-run: ``boys00``). The key stays
    junk until a repair supplies the year; the sweep reports the paper
    as skipped instead of guessing.
    """
    if not is_junk_cite_key(old_key) or year is None:
        return None
    new_key = store.suggest_cite_key(authors, year, exclude_ref_id=ref_id, conn=conn)
    if not new_key or is_junk_cite_key(new_key) or new_key == old_key:
        return None
    return new_key


def _remint_in_tx(
    store: Store,
    ref_id: int,
    *,
    source: str,
    conn: Connection,
    adopt_from_ref_id: int | None = None,
) -> str | None:
    """In-transaction remint leg of :func:`remint_junk_cite_key`."""
    row = conn.execute(
        "SELECT (SELECT id_value FROM ref_identifiers "
        " WHERE ref_id = refs.ref_id AND id_kind = 'cite_key' "
        " ORDER BY created_at, id_value LIMIT 1), authors, year "
        "FROM refs WHERE ref_id = %s AND retired_at IS NULL AND kind = 'paper'",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    old_key = str(row[0] or "")
    adopt_key: str | None = None
    if adopt_from_ref_id is not None and is_junk_cite_key(old_key):
        # The merge loser's live key is still claimed when the suggester
        # runs — but the loser retires right after, so its (often
        # bib-cited) key would die with it while the survivor mints a
        # suffix. Adopt the loser's non-junk key instead.
        loser_row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'cite_key' "
            "ORDER BY created_at, id_value LIMIT 1",
            (adopt_from_ref_id,),
        ).fetchone()
        if loser_row is not None and not is_junk_cite_key(str(loser_row[0])):
            adopt_key = str(loser_row[0])
    adopted_from: int | None = None
    new_key: str | None
    if adopt_key is not None:
        # Free the loser's cite_key rows; every value re-homes on the
        # survivor as a redirect first — the adopted value included, so
        # a refused promote still leaves it resolving (a successful
        # promote drops the survivor's own alias row for it anyway).
        # The re-homed set is stamped on the loser NOW (before the
        # promote): even a refused promote leaves the aliases moved, and
        # the restore door needs the record either way. adopt_key is only
        # set when adopt_from_ref_id was given — it's an int here.
        assert adopt_from_ref_id is not None
        rehomed_values = [
            str(value_row[0])
            for value_row in conn.execute(
                "DELETE FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'cite_key' RETURNING id_value",
                (adopt_from_ref_id,),
            ).fetchall()
        ]
        for value in rehomed_values:
            conn.execute(
                "INSERT INTO ref_identifiers "
                "(id_kind, id_value, ref_id, source) "
                "VALUES ('cite_key_alias', %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (value, ref_id, source),
            )
        record_rehomed_cite_keys(conn, adopt_from_ref_id, rehomed_values)
        new_key = adopt_key
        adopted_from = adopt_from_ref_id
    else:
        new_key = _remint_target(store, ref_id, old_key, row[1], row[2], conn=conn)
    if new_key is None:
        return None
    extras = [k for k in store.ref_cite_keys(ref_id, conn=conn) if k != old_key]
    changed = store.set_ref_identifier(
        ref_id, "cite_key", new_key, source=source, conn=conn
    )
    if not changed:
        return None
    demoted: list[str] = []
    lost: list[str] = []
    if extras:
        landed = {
            str(r[0])
            for r in conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'cite_key_alias' "
                "AND id_value = ANY(%s)",
                (ref_id, extras),
            ).fetchall()
        }
        demoted = [k for k in extras if k in landed]
        lost = [k for k in extras if k not in landed]
    if adopted_from is not None:
        record_rehomed_cite_keys(
            conn,
            adopted_from,
            [],
            adopted=new_key,
            survivor_prior_key=old_key or None,
        )
    store.append_event(
        ref_id,
        source=source,
        event="cite_key_changed",
        payload={
            "old": old_key,
            "new": new_key,
            "reason": AUTO_REMINT_REASON,
            **({"adopted_from": adopted_from} if adopted_from else {}),
            **({"demoted": demoted} if demoted else {}),
            **({"suppressed": lost} if lost else {}),
        },
        conn=conn,
    )
    return (
        f"pa{ref_id} cite_key: {old_key!r} → {new_key!r}"
        f"{_displaced_clause(demoted, lost)} (auto re-mint)"
    )
