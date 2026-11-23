"""gr739333: record start/end markers, closed notices and body fences.

An LLM reader used to have to *infer* record boundaries and lifecycle
state: a ``get`` opened with ``# todo N`` but closed nothing, the
``STATUS:done`` tag sat at the bottom of the card, and link-table rows
carried no state at all — a closed todo read as live work (a voice
session cited td646972 as an open contradiction after it closed).

Ruled format (Reto + Claude, 2026-11-20): an XML-style open/close tag
pair whose attributes carry the record's handle, kind, status and a
short **per-response nonce** (``b=``, the MIME-boundary role) so text
stored inside a body can never forge a matching close tag — the nonce
is minted per response and lives nowhere else. Not strict XML: no
parser and no entity escaping, just a small fixed vocabulary used
identically by every kind:

- ``<record td646972 kind=todo status=done closed=2026-11-19 b=A486>``
  opens one authored record; ``</record td646972 status=done b=A486>``
  closes it, restating handle and status for a skimming reader.
- ``<body>`` … ``</body>`` fences the author's stored text inside a
  record, separating it from system metadata (tags, links, footers).
- ``<run b=A486>`` … ``</run b=A486>`` wraps a run of source text
  (a window of paper/draft chunks) once — the one-line chunk handle
  heads inside stay unchanged, no per-chunk close tag.
- ``<quote src=<handle>>`` … ``</quote <handle>>`` is the *write-side*
  convention a stored body may carry when it quotes another record;
  skills teach it. It is the only tag allowed to arrive inside stored
  text — every other tag spelled like the ones above is neutralised on
  the way out (:func:`neutralize`), so a stored ``</record …>`` can
  never end the real record early.

Tiering (ruled): full-body reads of authored records get the full
open/close pair; table rows (search hits, link tables, dependants) get
no tags — the row is the boundary, and lifecycle kinds now carry a
``status`` cell; a run of paper chunks gets one ``<run>`` wrapper.

Closed items get two human-readable notices as well as the attributes:
one at the very top of the record (``CLOSED <date> (<status>) —
historical record only, do not act on the text below``) and one just
above the close tag, so a reader that skims either end sees it. A
retired/redirected record says where it went (``re-pointed to
<handle>``).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING

from precis.utils import handle_registry
from precis.utils.timeutil import as_utc

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref, Tag

#: Nonce alphabet: unambiguous uppercase + digits (no 0/O, 1/I), the
#: ``b=A486`` shape the ruling's example carried. Four chars is ~29 bits
#: — plenty against accidental collision, and the value is only ever
#: compared *within* one response.
_NONCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_NONCE_LEN = 4

#: The ambient per-response nonce. ``nonce_scope`` mints one for a
#: multi-record response (a batch get, a combined eye) so every record
#: in the response shares it; a single record read mints fresh each
#: call and never sets it, so two sequential responses never share.
_NONCE: ContextVar[str | None] = ContextVar("precis_record_nonce", default=None)

#: Stored text that spells a response-side marker is neutralised by
#: backslash-escaping the ``<``: readable, ASCII-safe, and the escaped
#: form can never match the open/close grammar. ``<quote`` is absent on
#: purpose — it is the sanctioned write-side tag bodies may keep.
_FAKE_MARKER_RE = re.compile(r"</?(record|body|run)\b")

#: The :func:`open_tag` line, capturing the mirrored close attributes.
_OPEN_TAG_RE = re.compile(
    r"^<record (\S+) kind=\S+ status=(\S+)(?: closed=\S+)? b=([^\s>]+)>$"
)

#: Terminal ``STATUS:`` values per kind — the union of each kind's
#: browse-terminal set and (todo) its hard terminal constant, so a
#: legacy ``STATUS:abandoned`` todo row still announces itself closed.
#: Kinds not listed have no terminal concept: their live reads frame
#: with a plain ``status=`` and no CLOSED banners.
_TERMINAL_STATUS: dict[str, frozenset[str]] = {
    "todo": frozenset({"done", "won't-do", "auto-timeout", "abandoned"}),
    "gripe": frozenset({"done", "wontfix"}),
    "quest": frozenset({"abandoned"}),
    "alert": frozenset({"resolved"}),
}


def new_nonce() -> str:
    """A fresh 4-char marker nonce (the ``b=`` attribute value)."""
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LEN))


def response_nonce() -> str:
    """The ambient nonce, or a fresh one outside a :func:`nonce_scope`."""
    return _NONCE.get() or new_nonce()


@contextmanager
def nonce_scope() -> Iterator[str]:
    """Mint one nonce for the duration of a multi-record response."""
    token = _NONCE.set(new_nonce())
    try:
        yield _NONCE.get() or ""
    finally:
        _NONCE.reset(token)


def neutralize(text: str) -> str:
    """Backslash-escape stored text that imitates a response marker.

    ``</record …>`` inside a stored body would otherwise read as the
    real close tag to a reader that trusts markers over the nonce; the
    escape keeps the text legible while making the forgery impossible.
    ``<quote src=…>`` is left alone — it is the stored-text tag.
    """
    if "<" not in text:
        return text
    return _FAKE_MARKER_RE.sub(lambda m: "\\" + m.group(0), text)


def unframe(text: str, *, fences: bool = False) -> str:
    """Lift the outer record frame off a framed read (a person's view).

    Drops the ``<record …>`` open line and its matching close line (same
    handle and nonce) — the frame is agent chrome; a page shows status as
    chips. ``fences=True`` also drops the ``<body>``/``</body>`` fence
    lines: stored text spelling a fence is neutralised on the way out, so
    an exact fence line is always the server's. The CLOSED notices stay —
    a person needs them too. Unframed text is returned unchanged.
    """
    lines = text.split("\n")
    m = _OPEN_TAG_RE.match(lines[0])
    if m is None:
        return text
    close_head = f"</record {m.group(1)} status="
    close_tail = f" b={m.group(3)}>"
    out = lines[1:]
    for i in range(len(out) - 1, -1, -1):
        if out[i].startswith(close_head) and out[i].endswith(close_tail):
            del out[i]
            break
    if fences:
        out = [ln for ln in out if ln not in ("<body>", "</body>")]
    return "\n".join(out)


def record_close_suffix(text: str) -> str:
    """Return the final newline, close tag and any footer after it.

    Dispatch appends the trust/grounding footer *after* the frame's close
    tag (the trust line stays last so the web can lift it off the end), so
    a framed read's suffix is the close tag plus that trailing footer, and
    the fitter must re-append the whole tail, not just the close tag
    (gr768673). A body whose first record is followed by another framed
    record is not one frame: no suffix.
    """
    opening = _OPEN_TAG_RE.fullmatch(text.partition("\n")[0])
    if opening is None:
        return ""
    handle, status, nonce = opening.groups()
    close_line = close_tag(handle, status, nonce=nonce)
    lines = text.split("\n")
    for i in range(len(lines) - 1, 0, -1):
        if lines[i] == close_line:
            if any(ln.startswith("<record ") for ln in lines[i + 1 :]):
                return ""
            return "\n" + "\n".join(lines[i:])
    return ""


def fence_body(text: str) -> str:
    """``<body>`` … ``</body>`` around one authored body for a record.

    The stored text is neutralised first so the fence itself can't be
    forged from inside.
    """
    return f"<body>\n{neutralize(text)}\n</body>"


def status_of_tags(tags: list[Tag]) -> str | None:
    """The ref's live state token: its ``STATUS:`` value, else the
    ``alert-state:*`` open-tag value (alerts carry state there), else
    ``None`` for a kind with no lifecycle axis."""
    for t in tags:
        if t.prefix == "STATUS":
            return t.value
    for t in tags:
        if t.value.startswith("alert-state:"):
            return t.value.partition(":")[2]
    return None


def is_terminal(kind: str, status: str | None) -> bool:
    """True when ``status`` ends work on ``kind`` (closed/won't-do)."""
    if status is None:
        return False
    if kind == "alert":
        return status == "resolved"
    return status in _TERMINAL_STATUS.get(kind, frozenset())


def status_stamp(store: Store, ref_id: int) -> datetime | None:
    """Set-time of the ref's newest live ``STATUS`` tag row.

    The ``closed=`` date on a terminal record: ``STATUS`` rows are
    replace-prefix (at most one live row per ref), so the newest live
    row's ``created_at`` is when the current state was stamped. One
    small query per framed record — a bulk read would complicate the
    wrap sites for no measurable gain.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT max(rt.created_at) FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now())",
            (ref_id,),
        ).fetchone()
    return as_utc(row[0]) if row and row[0] is not None else None


def status_cells(store: Store, ref_ids: list[int] | set[int]) -> dict[int, str]:
    """``ref_id → state token`` for table rows (links, dependants).

    One bulk read of the live ``STATUS`` and ``alert-state:*`` tags.
    Todos and gripes without a state tag still show ``open``; other
    untagged refs have no status cell.
    """
    ids = [int(i) for i in ref_ids]
    if not ids:
        return {}
    out: dict[int, str] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.kind, t.namespace, t.value FROM refs r "
            "LEFT JOIN (ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id) "
            "ON rt.ref_id = r.ref_id "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
            "AND (t.namespace = 'STATUS' OR "
            "(t.namespace = 'OPEN' AND t.value LIKE 'alert-state:%%')) "
            "WHERE r.ref_id = ANY(%s) "
            "AND (t.namespace IS NOT NULL OR r.kind IN ('todo', 'gripe'))",
            (ids,),
        ).fetchall()
    fallback: dict[int, str] = {}
    for rid, kind, ns, val in rows:
        rid = int(rid)
        if ns == "STATUS":
            out[rid] = str(val)
        elif ns == "OPEN":
            fallback[rid] = str(val).partition(":")[2]
        elif kind in {"todo", "gripe"}:
            fallback[rid] = "open"
    for rid, val in fallback.items():
        out.setdefault(rid, val)
    return out


def open_tag(
    handle: str,
    kind: str,
    status: str,
    *,
    closed_date: str | None = None,
    nonce: str | None = None,
) -> str:
    """The ``<record …>`` open line (also the frame for ``<run>`` via
    :func:`frame_run`)."""
    tag = f"<record {handle} kind={kind} status={status}"
    if closed_date:
        tag += f" closed={closed_date}"
    return f"{tag} b={nonce or response_nonce()}>"


def close_tag(handle: str, status: str, *, nonce: str | None = None) -> str:
    """The ``</record …>`` close line, restating handle + status."""
    return f"</record {handle} status={status} b={nonce or response_nonce()}>"


def closed_banner(
    status: str,
    *,
    closed_date: str | None = None,
    redirect_to: str | None = None,
) -> str:
    """The attention-getting first line inside a closed record.

    ``redirect_to`` turns it into the tombstone form — "where it went"
    is the one fact a merged ref's reader needs most.
    """
    date = f" {closed_date}" if closed_date else ""
    if redirect_to:
        return (
            f"CLOSED{date} ({status}) — re-pointed to {redirect_to}; "
            "historical record only, do not act on the text below."
        )
    return (
        f"CLOSED{date} ({status}) — historical record only, "
        "do not act on the text below."
    )


def closed_footer(
    status: str,
    *,
    closed_date: str | None = None,
    redirect_to: str | None = None,
) -> str:
    """The mirrored notice just above the close tag ("the item above
    is done") — a reader skimming the tail sees the state too."""
    date = f" {closed_date}" if closed_date else ""
    extra = f"; re-pointed to {redirect_to}" if redirect_to else ""
    return f"CLOSED{date} ({status}) — the item above is closed{extra}."


def _fmt_day(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt is not None else None


def frame_lines(
    handle: str,
    kind: str,
    status: str,
    inner: str,
    *,
    closed_date: str | None = None,
    redirect_to: str | None = None,
    nonce: str | None = None,
) -> str:
    """Wrap ``inner`` in the record open/close pair + closed banners.

    The low-level frame: callers with a handle but no ``Ref`` (draft
    chunks) drive this directly; :func:`frame_record` fills the
    parameters from the ref.
    """
    nonce = nonce or response_nonce()
    out = [open_tag(handle, kind, status, closed_date=closed_date, nonce=nonce)]
    terminal = is_terminal(kind, status) or status == "retired"
    if terminal or redirect_to is not None:
        out.append(
            closed_banner(status, closed_date=closed_date, redirect_to=redirect_to)
        )
    out.append(inner.rstrip("\n"))
    if terminal or redirect_to is not None:
        out.append("")
        out.append(
            closed_footer(status, closed_date=closed_date, redirect_to=redirect_to)
        )
    out.append(close_tag(handle, status, nonce=nonce))
    return "\n".join(out)


def frame_record(
    store: Store,
    ref: Ref,
    inner: str,
    *,
    nonce: str | None = None,
    redirect_to: str | None = None,
) -> str:
    """Wrap one ref's rendered read in its record frame.

    ``status`` is the live ``STATUS:``/``alert-state:`` value (``live``
    for kinds with no lifecycle axis — the ref is live by construction),
    ``retired`` on a tombstone read. ``closed=`` carries the closure
    date on a terminal record — the STATUS row's set-time, falling back
    to ``retired_at`` for tombstones.
    """
    handle = handle_registry.try_format(ref.kind, ref.id) or f"{ref.kind}:{ref.id}"
    tags = store.tags_for(ref.id)
    status = status_of_tags(tags) or ("retired" if ref.retired_at else "live")
    closed_date: str | None = None
    if status == "retired":
        closed_date = _fmt_day(as_utc(ref.retired_at))
    elif is_terminal(ref.kind, status):
        closed_date = _fmt_day(status_stamp(store, ref.id))
    return frame_lines(
        handle,
        ref.kind,
        status,
        inner,
        closed_date=closed_date,
        redirect_to=redirect_to,
        nonce=nonce,
    )


def ref_for_focus(store: Store, handle: str) -> Ref | None:
    """The live ``Ref`` behind an eye/record handle, or ``None``.

    Record handles resolve their pk straight; chunk handles
    (``pc<id>``, ``dc<id>``, ``sc<id>``, …) resolve the owning ref via
    the chunks table so a chunk-focus eye frames the *paper/draft's*
    record. ``None`` for a handle with no ref behind it (``sk:<slug>``
    corpus eyes) or a dead row — the caller renders unframed.
    """
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return None
    _kind, is_chunk, pk = parsed
    ref_id = int(pk)
    if is_chunk:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM chunks WHERE chunk_id = %s", (ref_id,)
            ).fetchone()
        if row is None:
            return None
        ref_id = int(row[0])
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None or ref.retired_at is not None:
        return None
    return ref


def frame_eye_read(store: Store, handle: str, inner: str) -> str:
    """Frame a fisheye render as its focus's record, when it has one."""
    ref = ref_for_focus(store, handle)
    if ref is None:
        return inner
    return frame_record(store, ref, inner)


def frame_run(inner: str, *, label: str | None = None, nonce: str | None = None) -> str:
    """``<run …>`` around a run of source chunks (tier (c)).

    One wrapper for the whole run — the one-line chunk-handle heads
    inside stay unchanged, and no per-chunk close tag doubles a
    one-line row. ``label`` names the run's source (a paper handle)
    when known.
    """
    nonce = nonce or response_nonce()
    name = f" {label}" if label else ""
    return f"<run{name} b={nonce}>\n{inner.rstrip()}\n</run{name} b={nonce}>"
