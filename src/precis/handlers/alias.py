"""AliasHandler — a bidirectional, typed string-to-string map.

``kind='alias'`` stores one pair ``a ⇄ b`` per refs row (payload in
``refs.meta['alias']``, no body chunk, no migration). A lookup by
either side returns the other side, so one row answers both "what is
``X`` short for" and "what short form does ``Y`` have". Uses:

- short names for long strings an agent keeps re-sending (a URL, a long
  query, a file path) — ``rel='short-for'``;
- crosswalks between identifier systems precis does not map natively
  (vendor part number ↔ component, external id ↔ handle) —
  ``rel='same-as'`` / ``'maps-to'``;
- provisional mappings that paper over a defect until a gripe fixes it —
  ``gripe='gr<N>'`` links the pair to the gripe, and ``get(id='/stale')``
  lists every pair whose gripe has closed or whose ``expires`` has passed.

Surface (see ``precis-alias-help``):

- ``put(kind='alias', args={'a': …, 'b': …, 'rel': …, 'scope': …,
  'expires': '7d'|ISO, 'gripe': 'gr<N>', 'note': …})`` creates; an
  identical live pair returns the existing handle.
- ``get(kind='alias', id='<either side>')`` — every live, unexpired pair
  touching that string, other side shown, ``rel`` read from the side
  matched. ``id='as<N>'`` reads one pair; no id lists the newest;
  ``id='/stale'`` lists cleanup candidates.
- ``search(kind='alias', q=…)`` — case-insensitive substring over both
  sides and the note.
- ``edit`` patches ``rel``/``scope``/``expires``/``gripe``/``note``;
  ``delete`` retires.

Rows are durable on purpose (not session-scoped): a short name that
vanished at compaction would leave the summary pointing at nothing.
``expires=`` is the "temporary" knob.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import Ref
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

META_KEY = "alias"

#: Forward relation → the name it reads as from the ``b`` side. A
#: symmetric relation maps to itself. Put accepts either name; an
#: inverse name is stored as its forward form with ``a``/``b`` swapped,
#: so one pair never exists in two spellings.
RELATIONS: dict[str, str] = {
    "same-as": "same-as",
    "see-also": "see-also",
    "short-for": "expands-to",
    "renamed-to": "renamed-from",
    "maps-to": "mapped-from",
}
_INVERSE_TO_FORWARD = {inv: fwd for fwd, inv in RELATIONS.items() if inv != fwd}

MAX_SIDE_CHARS = 2000
MAX_SCOPE_CHARS = 64
MAX_NOTE_CHARS = 500
LIST_LIMIT = 50

_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_ALIAS_HANDLE_RE = re.compile(r"^as(\d+)$", re.IGNORECASE)
_GRIPE_RE = re.compile(r"^(?:gr)?(\d+)$", re.IGNORECASE)
_GRIPE_CLOSED = frozenset({"done", "wontfix"})

_LIVE_SQL = (
    "kind = 'alias' AND retired_at IS NULL AND ("
    "meta->'alias'->>'expires_at' IS NULL "
    "OR (meta->'alias'->>'expires_at')::timestamptz > now())"
)


def _side(value: Any, name: str) -> str:
    s = str(value if value is not None else "").strip()
    if not s:
        raise BadInput(
            f"{name}= is required and must be non-empty",
            next="put(kind='alias', args={'a': '<short>', 'b': '<long>', "
            "'rel': 'short-for'})",
        )
    if len(s) > MAX_SIDE_CHARS:
        raise BadInput(
            f"{name}= is {len(s)} chars; the cap is {MAX_SIDE_CHARS}",
            next="store long text as a memory or draft and alias its handle",
        )
    if _ALIAS_HANDLE_RE.match(s):
        raise BadInput(
            f"{name}={s!r} is an alias handle; an alias side cannot be another alias",
            next=f"get(kind='alias', id={s!r}) reads that pair",
        )
    return s


def _relation(rel: Any) -> tuple[str, bool]:
    """``(forward_rel, swapped)`` for a user-supplied relation name."""
    r = str(rel or "same-as").strip().lower()
    if r in RELATIONS:
        return r, False
    if r in _INVERSE_TO_FORWARD:
        return _INVERSE_TO_FORWARD[r], True
    names = sorted({*RELATIONS, *_INVERSE_TO_FORWARD})
    raise BadInput(f"unknown alias rel={r!r}", options=names, next="rel='same-as'")


def _expires_at(expires: Any) -> str | None:
    """``'7d'``/``'12h'``/``'30m'``/``'2w'`` or an ISO datetime → ISO UTC.
    ``''``/``'never'`` clears (returns ``None``)."""
    if expires is None:
        return None
    s = str(expires).strip()
    if s.lower() in ("", "never", "none"):
        return None
    m = _DURATION_RE.match(s.lower())
    if m:
        delta = timedelta(**{_DURATION_UNITS[m.group(2)]: int(m.group(1))})
        when = datetime.now(UTC) + delta
    else:
        try:
            when = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise BadInput(
                f"expires={s!r} is neither a duration nor an ISO datetime",
                next="expires='7d' (m/h/d/w) or expires='2026-12-01T00:00:00Z'",
            ) from None
        if when.tzinfo is None:
            raise BadInput(
                f"expires={s!r} has no timezone",
                next="add 'Z' — timestamps are UTC",
            )
        when = when.astimezone(UTC)
    if when <= datetime.now(UTC):
        raise BadInput(f"expires={s!r} is already in the past", next="expires='7d'")
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def payload_of(ref: Ref) -> dict[str, Any]:
    return dict((ref.meta or {}).get(META_KEY) or {})


class AliasHandler(NumericRefHandler):
    kind: ClassVar[str] = "alias"
    sense: ClassVar[str] = "alias"

    #: ``edit(id=[...])`` repeats the same patch per pair (e.g. extend
    #: several expiries at once) in one transaction.
    edit_id_list: ClassVar[str] = "repeat"

    spec: ClassVar[KindSpec] = KindSpec(
        kind="alias",
        title="Alias (bidirectional map)",
        description=(
            "A typed pair a ⇄ b looked up from either side: short names for "
            "long strings, crosswalks between identifier systems, provisional "
            "mappings tied to a gripe. put(kind='alias', args={'a':…, 'b':…, "
            "'rel':'same-as'|'short-for'|'renamed-to'|'maps-to'|'see-also', "
            "'expires':'7d', 'gripe':'gr<N>'}); get(id='<either side>') "
            "returns the other side; get(id='/stale') lists cleanup "
            "candidates. See precis-alias-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        corpus_role="none",
        placement="system",
    )

    # ── store helpers ─────────────────────────────────────────────────

    def _select(self, where: str, params: dict[str, Any], limit: int) -> list[Ref]:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT ref_id FROM refs WHERE {where} "
                "ORDER BY ref_id DESC LIMIT %(limit)s",
                {**params, "limit": int(limit)},
            ).fetchall()
        out: list[Ref] = []
        for (rid,) in rows:
            ref = self.store.get_ref(kind=self.kind, id=int(rid))
            if ref is not None:
                out.append(ref)
        return out

    def lookup(self, value: str, *, scope: str | None = None) -> list[Ref]:
        """Live pairs with ``value`` on either side — exact first, then a
        case-insensitive fallback when nothing matches exactly."""
        params: dict[str, Any] = {"v": value}
        scope_sql = ""
        if scope is not None:
            scope_sql = " AND coalesce(meta->'alias'->>'scope', '') = %(scope)s"
            params["scope"] = scope
        exact = self._select(
            f"{_LIVE_SQL}{scope_sql} AND (meta->'alias'->>'a' = %(v)s "
            "OR meta->'alias'->>'b' = %(v)s)",
            params,
            LIST_LIMIT,
        )
        if exact:
            return exact
        return self._select(
            f"{_LIVE_SQL}{scope_sql} AND (lower(meta->'alias'->>'a') = lower(%(v)s) "
            "OR lower(meta->'alias'->>'b') = lower(%(v)s))",
            params,
            LIST_LIMIT,
        )

    def _existing(self, a: str, b: str, rel: str, scope: str) -> Ref | None:
        hits = self._select(
            f"{_LIVE_SQL} AND meta->'alias'->>'a' = %(a)s "
            "AND meta->'alias'->>'b' = %(b)s AND meta->'alias'->>'rel' = %(rel)s "
            "AND coalesce(meta->'alias'->>'scope', '') = %(scope)s",
            {"a": a, "b": b, "rel": rel, "scope": scope},
            1,
        )
        return hits[0] if hits else None

    def _gripe_ref(self, gripe: Any) -> Ref:
        m = _GRIPE_RE.match(str(gripe).strip())
        ref = self.store.get_ref(kind="gripe", id=int(m.group(1))) if m else None
        if ref is None:
            raise NotFound(
                f"gripe={gripe!r} not found",
                next="gripe='gr<N>' — search(kind='gripe', q='…') finds one",
            )
        return ref

    def _gripe_closed(self, gripe_id: int) -> bool:
        return any(
            t.prefix == "STATUS" and t.value in _GRIPE_CLOSED
            for t in self.store.tags_for(gripe_id)
        )

    def _handle(self, ref_id: int) -> str:
        return handle_registry.try_format(self.kind, ref_id) or str(ref_id)

    def _resolve_ref(self, id: Any) -> Ref:
        s = str(id).strip()
        m = _ALIAS_HANDLE_RE.match(s)
        rid = int(m.group(1)) if m else int(s) if s.isdigit() else None
        if rid is None:
            raise BadInput(
                f"id={s!r} is not an alias handle",
                next=f"get(kind='alias', id={s!r}) looks the string up; "
                "edit/delete take the as<N> handle it prints",
            )
        return self._resolve_live_ref(rid)

    # ── rendering ─────────────────────────────────────────────────────

    def _row(self, ref: Ref, matched: str | None = None) -> dict[str, str]:
        p = payload_of(ref)
        a, b, rel = str(p.get("a", "")), str(p.get("b", "")), str(p.get("rel", ""))
        # Matched on the b side (exactly, or case-insensitively when a
        # isn't the exact hit): show b first and the relation inverted.
        if matched is not None and a != matched and b.lower() == matched.lower():
            this, other, shown = b, a, RELATIONS.get(rel, rel)
        else:
            this, other, shown = a, b, rel
        return {
            "handle": self._handle(ref.id),
            "this": _short(this),
            "rel": shown,
            "other": other,
            "scope": str(p.get("scope") or ""),
            "expires": str(p.get("expires_at") or ""),
            "gripe": f"gr{p['gripe']}" if p.get("gripe") else "",
        }

    def _table(self, title: str, refs: list[Ref], matched: str | None = None) -> str:
        from precis.format import render_agent_table

        rows = [self._row(r, matched) for r in refs]
        schema = ["handle", "this", "rel", "other", "scope", "expires", "gripe"]
        return f"# {title}\n\n" + render_agent_table(rows, schema=schema)

    def _render_pair(self, ref: Ref) -> str:
        p = payload_of(ref)
        lines = [
            f"# alias {self._handle(ref.id)}",
            f"a: {p.get('a', '')}",
            f"rel: {p.get('rel', '')} (from b: {RELATIONS.get(str(p.get('rel')), '')})",
            f"b: {p.get('b', '')}",
        ]
        for key in ("scope", "expires_at", "note"):
            if p.get(key):
                lines.append(f"{key}: {p[key]}")
        if p.get("gripe"):
            closed = self._gripe_closed(int(p["gripe"]))
            lines.append(f"gripe: gr{p['gripe']}{' (closed)' if closed else ''}")
        return "\n".join(lines)

    # ── get / search ──────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        scope: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or str(id).strip() in ("", "/", "/recent"):
            return self._recent()
        if isinstance(id, list):
            return Response(
                body="\n\n".join(self.get(id=one, scope=scope).body for one in id)
            )
        s = str(id).strip()
        if s == "/stale":
            return self._stale()
        if _ALIAS_HANDLE_RE.match(s):
            ref = self._resolve_ref(s)
            if view not in (None, "raw"):
                return super().get(id=ref.id, view=view, **_kw)
            return Response(body=self._render_pair(ref))
        hits = self.lookup(s, scope=scope)
        if not hits and s.isdigit():
            # The universal ``as<N>`` route reaches here with the bare pk.
            pk_ref = self.store.get_ref(kind=self.kind, id=int(s))
            if pk_ref is not None:
                return Response(
                    body=self._render_pair(self._resolve_live_ref(pk_ref.id))
                )
        if not hits:
            body = f"no live alias for {_short(s)!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='alias', q={_short(s, 40)!r})",
                        "substring match on either side",
                    ),
                    (
                        f"put(kind='alias', args={{'a': {_short(s, 40)!r}, "
                        "'b': '<other side>'}})",
                        "create the mapping",
                    ),
                ]
            )
            return Response(body=body)
        n = len(hits)
        return Response(
            body=self._table(f"alias {_short(s, 60)!r} — {n} match(es)", hits, s)
        )

    def _recent(self) -> Response:
        refs = self._select(_LIVE_SQL, {}, LIST_LIMIT)
        if not refs:
            body = "no aliases yet"
            body += render_next_section(
                [
                    (
                        "put(kind='alias', args={'a': 'short', 'b': 'long', "
                        "'rel': 'short-for'})",
                        "create the first one",
                    )
                ]
            )
            return Response(body=body)
        return Response(body=self._table(f"aliases — newest {len(refs)}", refs))

    def _stale(self) -> Response:
        """Cleanup candidates: pairs whose ``expires`` has passed (not yet
        retired) and live pairs whose linked gripe is done/wontfix."""
        expired = self._select(
            "kind = 'alias' AND retired_at IS NULL "
            "AND (meta->'alias'->>'expires_at')::timestamptz <= now()",
            {},
            LIST_LIMIT,
        )
        tied = self._select(
            f"{_LIVE_SQL} AND meta->'alias' ? 'gripe'", {}, LIST_LIMIT * 4
        )
        closed = [r for r in tied if self._gripe_closed(int(payload_of(r)["gripe"]))]
        if not expired and not closed:
            return Response(body="no stale aliases")
        parts = []
        if closed:
            parts.append(self._table("aliases whose gripe closed", closed))
        if expired:
            parts.append(self._table("expired aliases (hidden from lookup)", expired))
        body = "\n\n".join(parts)
        body += render_next_section(
            [
                ("delete(kind='alias', id='as<N>')", "retire a pair the fix made moot"),
                (
                    "edit(kind='alias', id='as<N>', args={'gripe': '', 'expires': 'never'})",
                    "keep it as a permanent mapping",
                ),
            ]
        )
        return Response(body=body)

    def search(
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        page: int = 1,
        **_kw: Any,
    ) -> Response:
        needle = (q or "").strip()
        if not needle:
            return self._recent()
        offset = max(0, (int(page) - 1) * int(page_size))
        refs = self._select(
            f"{_LIVE_SQL} AND (meta->'alias'->>'a' ILIKE %(p)s "
            "OR meta->'alias'->>'b' ILIKE %(p)s OR meta->'alias'->>'note' ILIKE %(p)s)",
            {"p": "%" + needle.replace("%", r"\%").replace("_", r"\_") + "%"},
            offset + int(page_size),
        )[offset:]
        if not refs:
            return Response(body=f"no alias matching {_short(needle, 60)!r}")
        return Response(
            body=self._table(f"aliases matching {_short(needle, 60)!r}", refs)
        )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return ["raw"]

    # ── put / edit / delete ───────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        a: str | None = None,
        b: str | None = None,
        rel: str | None = None,
        scope: str | None = None,
        expires: str | None = None,
        gripe: str | int | None = None,
        note: str | None = None,
        tags: list[str] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is not None:
            raise BadInput(
                "put(kind='alias') takes the sides in args=, not id=",
                next="put(kind='alias', args={'a': '<one side>', 'b': '<other>'})",
            )
        self._reject_mutating_put(
            id=None, mode=mode, untags=None, unlink=None, rel=None, link=None
        )
        side_a, side_b = _side(a, "a"), _side(b, "b")
        forward, swapped = _relation(rel)
        if swapped:
            side_a, side_b = side_b, side_a
        if side_a == side_b:
            raise BadInput(
                "a= and b= are the same string", next="map two different strings"
            )
        scope_s = str(scope or "").strip()
        if len(scope_s) > MAX_SCOPE_CHARS:
            raise BadInput(
                f"scope= is over {MAX_SCOPE_CHARS} chars", next="scope='url'"
            )
        dup = self._existing(side_a, side_b, forward, scope_s)
        if dup is not None:
            return Response(
                body=f"alias {self._handle(dup.id)} already maps "
                f"{_short(side_a, 60)!r} {forward} {_short(side_b, 60)!r} (unchanged)"
            )
        payload: dict[str, Any] = {"a": side_a, "b": side_b, "rel": forward}
        if scope_s:
            payload["scope"] = scope_s
        when = _expires_at(expires)
        if when:
            payload["expires_at"] = when
        if note:
            payload["note"] = str(note).strip()[:MAX_NOTE_CHARS]
        gripe_ref = self._gripe_ref(gripe) if gripe not in (None, "") else None
        if gripe_ref is not None:
            payload["gripe"] = int(gripe_ref.id)
        ref = self.store.insert_ref(
            kind=self.kind,
            slug=None,
            title=_short(f"{side_a} ⇄ {side_b}", 200),
            meta={META_KEY: payload},
        )
        if gripe_ref is not None:
            self.store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=gripe_ref.id,
                relation="related-to",
                meta={"role": "provisional-alias"},
            )
        if tags:
            self.tag(id=ref.id, add=list(tags))
        h = self._handle(ref.id)
        body = (
            f"created alias {h}: {_short(side_a, 60)!r} {forward} {_short(side_b, 60)!r}"
            + (f" (expires {when})" if when else "")
        )
        body += render_next_section(
            [
                (f"get(kind='alias', id={_short(side_a, 40)!r})", "look it up by a"),
                (f"get(kind='alias', id={_short(side_b, 40)!r})", "look it up by b"),
            ]
        )
        return Response(body=body)

    def edit(
        self,
        *,
        id: str | int | None = None,
        rel: str | None = None,
        scope: str | None = None,
        expires: str | None = None,
        gripe: str | int | None = None,
        note: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput(
                "edit requires id=",
                next="edit(kind='alias', id='as<N>', args={'expires': '30d'})",
            )
        ref = self._resolve_ref(id)
        p = payload_of(ref)
        changed: list[str] = []
        if rel is not None:
            forward, swapped = _relation(rel)
            if swapped:
                p["a"], p["b"] = p.get("b"), p.get("a")
            p["rel"] = forward
            changed.append("rel")
        if scope is not None:
            p.pop("scope", None)
            if str(scope).strip():
                p["scope"] = str(scope).strip()[:MAX_SCOPE_CHARS]
            changed.append("scope")
        if expires is not None:
            p.pop("expires_at", None)
            when = _expires_at(expires)
            if when:
                p["expires_at"] = when
            changed.append("expires")
        if note is not None:
            p.pop("note", None)
            if str(note).strip():
                p["note"] = str(note).strip()[:MAX_NOTE_CHARS]
            changed.append("note")
        if gripe is not None:
            p.pop("gripe", None)
            if str(gripe).strip():
                g = self._gripe_ref(gripe)
                p["gripe"] = int(g.id)
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=g.id,
                    relation="related-to",
                    meta={"role": "provisional-alias"},
                )
            changed.append("gripe")
        if not changed:
            raise BadInput(
                "edit(kind='alias') needs one of rel/scope/expires/gripe/note",
                next="to change a side, delete the pair and put a new one",
            )
        self.store.update_ref(ref.id, meta_patch={META_KEY: p})
        return Response(
            body=f"edited alias {self._handle(ref.id)} ({', '.join(changed)})"
        )

    def delete(
        self, *, id: str | int | None = None, reason: str | None = None, **_kw: Any
    ) -> Response:
        if id is None:
            raise BadInput(
                "delete requires id=", next="delete(kind='alias', id='as<N>')"
            )
        ref = self._resolve_ref(id)
        return self._delete(ref.id, reason=reason)
