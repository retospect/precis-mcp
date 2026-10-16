"""ProfileHandler — the owner-scoped branding-profile kind.

Slice 6 of ``docs/backlog/draft-poster-genre-and-themes.md`` (Reto rulings
2026-10-15): ``kind='profile'`` stores per-user poster/deck branding as
data — a role→hex palette, an allowed font, ordered logo figure refs and
a sponsor line — validated by the same rules the poster exporter applies
(:mod:`precis.export.poster_profile`). Storage and ACL mechanics live in
:mod:`precis.profiles`: the payload is ``refs.meta['profile']`` and
ownership is ``refs.owner_login`` (migration 0164). No body chunk, no
migration — a profile is structured config, not prose.

Surface (see ``precis-profile-help``):

- ``put(kind='profile', name=…, palette={role: '#RRGGBB'}, …)`` creates;
  ``owner=`` names the owning web login (default: the acting user's).
- ``edit(kind='profile', id=N, …)`` patches fields — ``palette``/``fonts``
  merge per key, everything else replaces — and re-validates.
- ``get(kind='profile')`` lists the acting user's profiles (all of them
  when no user identity is present); ``get(id=N|'pf<N>'|'<name>')``
  renders one. ``eye``/``find_in_ref``/``search``/``tag``/``link`` apply
  the same owner check (gr490266).
- ``delete`` soft-retires.

Owner-only read + owner-checked write (ruling 3): when the call carries
a ``user:<abbrev>`` actor, the row's ``owner_login`` must match; an
unidentified caller (fleet ``agent``, CLI) passes — see
:mod:`precis.profiles`.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from precis import profiles
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import Ref
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section


class ProfileHandler(NumericRefHandler):
    kind: ClassVar[str] = "profile"
    sense: ClassVar[str] = "profile"

    spec: ClassVar[KindSpec] = KindSpec(
        kind="profile",
        title="Branding profile",
        description=(
            "An owner-scoped branding profile for poster/deck drafts: "
            "palette (role→hex), allowed font, logo strip (figure ids), "
            "sponsor line. put(kind='profile', name=…, palette={…}, "
            "fonts={…}, logos=[…], sponsor_line=…, owner=<login>) creates; "
            "edit patches fields; get lists / renders; delete retires. "
            "Export resolves a draft's meta.workspace.poster.profile, else "
            "the draft owner's default profile, else the product palette. "
            "See precis-profile-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        is_numeric=True,
        id_required=False,
        corpus_role="none",
        placement="system",
    )

    # ── ACL (ruling 3) ────────────────────────────────────────────────

    def _check_read(self, ref: Ref) -> None:
        """Owner-only read: an identified non-owner gets the same
        ``NotFound`` a missing row would — a private profile doesn't
        leak its existence."""
        owner = ref.owner_login
        login = profiles.acting_login(self.store)
        if owner and login and login != owner:
            raise NotFound(
                f"profile id={ref.id} not found",
                next="get(kind='profile') lists your own profiles",
            )

    def _check_write(self, ref: Ref | None = None, *, owner: str | None = None) -> None:
        """Owner-checked write: an identified caller may only write rows
        they own, and may only create/assign rows to themselves."""
        login = profiles.acting_login(self.store)
        if login is None:
            return
        if ref is not None and ref.owner_login and ref.owner_login != login:
            raise NotFound(
                f"profile id={ref.id} not found",
                next="get(kind='profile') lists your own profiles",
            )
        if owner is not None and owner != login:
            raise BadInput(
                f"owner={owner!r} is not your login ({login!r})",
                next="omit owner= (defaults to your account) or pass your own login",
            )

    def _resolve_ref(self, id: Any) -> Ref:
        """``id`` → live profile ref: int / ``pf<N>`` handle / name.

        Name lookups scope to the acting user's profiles when known,
        else every profile — matching the read ACL the same call then
        applies."""
        s = str(id).strip()
        login = profiles.acting_login(self.store)
        if s.lower().startswith("pf") and s[2:].isdigit():
            s = s[2:]
        ref: Ref | None
        if s.isdigit():
            # Owner check before the live check: a retired foreign row
            # must read NotFound, not Gone (no existence oracle).
            any_ref = self.store.get_ref(
                kind=self.kind, id=int(s), include_deleted=True
            )
            if any_ref is not None:
                self._check_read(any_ref)
            ref = self._resolve_live_ref(int(s))
        else:
            pool = (
                profiles.profiles_for_owner(self.store, login)
                if login
                else profiles.all_profiles(self.store)
            )
            ref = next(
                (
                    r
                    for r in pool
                    if profiles.payload_of(r).get("name") == s or r.title == s
                ),
                None,
            )
            if ref is None:
                raise NotFound(
                    f"no profile named {s!r}",
                    next="get(kind='profile') lists profiles; pass its id or pf handle",
                )
        self._check_read(ref)
        return ref

    # ── inherited verbs, owner-guarded (gr490266) ─────────────────────
    # NumericRefHandler's eye / find_in_ref / search / tag / link resolve
    # ids themselves and never reach _check_read/_check_write; each one
    # re-enters through _resolve_ref so an identified non-owner gets the
    # same NotFound get() gives.

    def _owned_ids(self, id: Any, *, write: bool) -> list[int]:
        if id is None or id == []:
            raise BadInput(
                "this verb requires id=",
                next="pass id=N|'pf<N>'|'<name>' — get(kind='profile') lists yours",
            )
        ids = id if isinstance(id, list) else [id]
        out: list[int] = []
        for one in ids:
            ref = self._resolve_ref(one)
            if write:
                self._check_write(ref)
            out.append(int(ref.id))
        return out

    def eye(
        self,
        *,
        id: Any = None,
        extent: Any = "fisheye",
        q: str | None = None,
        ring: Any = None,
    ) -> Response:
        (rid,) = self._owned_ids(id, write=False)
        return super().eye(id=rid, extent=extent, q=q, ring=ring)

    def find_in_ref(
        self,
        *,
        id: Any = None,
        q: Any = None,
        view: Any = None,
        context: Any = None,
    ) -> Response:
        (rid,) = self._owned_ids(id, write=False)
        return super().find_in_ref(id=rid, q=q, view=view, context=context)

    def search(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        view: str | None = None,
        page_size: int = 10,
        page: int = 1,
        include_ref_ids: list[int] | None = None,
        **kw: Any,
    ) -> Response:
        """Owner-only read on every search shape: an identified caller's
        hits are restricted to their own profile ids (an empty owned set
        matches nothing). The ranked path and ``view='count'`` take the
        id filter; the q-less tag listing is answered here from the
        owner-scoped ``list_refs``; a q-less ``link=`` listing pages the
        link table unscoped, so it is refused (pass ``q=`` with it)."""
        login = profiles.acting_login(self.store)
        has_q = bool(q and q.strip())
        if login is not None:
            owned = [r.id for r in profiles.profiles_for_owner(self.store, login)]
            include_ref_ids = (
                owned
                if include_ref_ids is None
                else [i for i in include_ref_ids if i in set(owned)]
            )
            if view is None and link is not None and str(link).strip():
                raise BadInput(
                    "search(kind='profile', link=…) is not available to a "
                    "signed-in user",
                    next="search(kind='profile', q='<name>') or "
                    "get(kind='profile') to list yours",
                )
            if view is None and (has_q or tags):
                return self._owned_listing(
                    login, q=q, tags=tags, page_size=page_size, page=page
                )
        return super().search(
            q=q,
            tags=tags,
            link=link,
            view=view,
            page_size=page_size,
            page=page,
            include_ref_ids=include_ref_ids,
            **kw,
        )

    def _owned_listing(
        self,
        login: str,
        *,
        q: str | None,
        tags: list[str] | None,
        page_size: int,
        page: int,
    ) -> Response:
        """An identified caller's search, answered from their own rows
        only: ``tags=`` via the owner-scoped ``list_refs``, ``q=`` as a
        case-insensitive substring of name/title/sponsor line. The base
        ranked path's header totals count every owner's matches, so it
        is not used here (gr490266)."""
        normalized = Tag.normalize_filter(tags or [], kind=self.kind)
        refs = self.store.list_refs(
            kind=self.kind,
            tags=normalized or None,
            owner_login=login,
            limit=200,
            order_by="updated_desc",
        )
        needle = (q or "").strip().lower()
        if needle:
            refs = [
                r
                for r in refs
                if any(
                    needle in str(v or "").lower()
                    for v in (
                        r.title,
                        profiles.payload_of(r).get("name"),
                        profiles.payload_of(r).get("sponsor_line"),
                    )
                )
            ]
        offset = max(0, (int(page) - 1) * int(page_size))
        window = refs[offset : offset + int(page_size)]
        if not window:
            what = f"matching {q!r}" if needle else f"tagged {normalized}"
            return Response(body=f"no profile entries {what}")
        return self._profile_table(window)

    def tag(self, **kw: Any) -> Response:
        return super().tag(**self._owned_write_kw(kw))

    def link(self, **kw: Any) -> Response:
        return super().link(**self._owned_write_kw(kw))

    def _owned_write_kw(self, kw: dict[str, Any]) -> dict[str, Any]:
        """``kw`` with ``id`` resolved to owner-checked numeric id(s),
        keeping a list a list (``batch_ids`` callers)."""
        id = kw.get("id")
        ids = self._owned_ids(id, write=True)
        return {**kw, "id": ids if isinstance(id, list) else ids[0]}

    # ── get ───────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            return self._render_index()
        ref = self._resolve_ref(id)
        if view in (None, "raw"):
            payload = profiles.payload_of(ref)
            lines = [
                f"# profile {ref.id} — {payload.get('name') or ref.title}",
                f"handle: {handle_registry.try_format(self.kind, ref.id) or ref.id}",
                f"owner: {ref.owner_login or '(unowned)'}",
                f"default: {bool(payload.get('default'))}",
                f"sponsor_line: {payload.get('sponsor_line') or ''}",
                "palette:",
            ]
            for role, hexv in sorted((payload.get("palette") or {}).items()):
                lines.append(f"  {role}: {hexv}")
            lines.append("fonts:")
            for k, v in sorted((payload.get("fonts") or {}).items()):
                lines.append(f"  {k}: {v}")
            logos = payload.get("logos") or []
            lines.append("logos:")
            for lg in logos:
                lines.append(f"  {lg.get('figure')} ({lg.get('group', 'institution')})")
            if view == "raw":
                lines += [
                    "",
                    "meta:",
                    json.dumps(ref.meta or {}, indent=2, sort_keys=True),
                ]
            return Response(body="\n".join(lines))
        return super().get(id=ref.id, view=view, **_kw)

    def _render_index(self) -> Response:
        login = profiles.acting_login(self.store)
        refs = (
            profiles.profiles_for_owner(self.store, login)
            if login
            else profiles.all_profiles(self.store)
        )
        if not refs:
            body = "no profiles yet"
            body += render_next_section(
                [
                    (
                        "put(kind='profile', name='lab', palette={…}, owner='<login>')",
                        "create your first branding profile",
                    ),
                ]
            )
            return Response(body=body)
        return self._profile_table(refs)

    def _profile_table(self, refs: list[Ref]) -> Response:
        rows = []
        for r in refs:
            p = profiles.payload_of(r)
            rows.append(
                {
                    "id": handle_registry.try_format(self.kind, r.id) or str(r.id),
                    "name": str(p.get("name") or r.title or ""),
                    "owner": r.owner_login or "",
                    "default": "*" if p.get("default") else "",
                }
            )
        from precis.format import render_agent_table

        body = "# profiles\n\n" + render_agent_table(
            rows, schema=["id", "name", "owner", "default"]
        )
        return Response(body=body)

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return ["raw"]

    # ── put / edit ────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        name: str | None = None,
        palette: dict[str, Any] | None = None,
        fonts: dict[str, Any] | None = None,
        logos: Any = None,
        sponsor_line: str | None = None,
        owner: str | None = None,
        default: bool | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        self._reject_mutating_put(
            id=id, mode=mode, untags=None, unlink=None, rel=None, link=None
        )
        acting = profiles.acting_login(self.store)
        owner = (owner or acting or "").strip()
        if not owner:
            raise BadInput(
                "put(kind='profile') needs owner=<web login> when the call "
                "carries no user identity",
                next="put(kind='profile', name='lab', owner='you', palette={…})",
            )
        self._check_write(owner=owner)
        if self.store.get_web_user(owner) is None:
            raise BadInput(
                f"owner={owner!r} is not a web account",
                next="owner= must be an existing web_users login",
            )
        profile_name = (name or "").strip() or "default"
        payload = profiles.build_payload(
            name=profile_name,
            owner=owner,
            palette=palette,
            fonts=fonts,
            logos=logos,
            sponsor_line=sponsor_line or "",
            default=bool(default),
        )
        profiles.validated_profile(self.store, payload)
        ref = self.store.insert_ref(
            kind=self.kind,
            slug=None,
            title=profile_name,
            meta={profiles.META_KEY: payload},
            owner_login=owner,
        )
        handle = handle_registry.try_format(self.kind, ref.id) or f"id={ref.id}"
        body = f"created profile {handle} '{profile_name}' (owner {owner})."
        body += render_next_section(
            [
                (
                    f"get(kind='profile', id={ref.id})",
                    "read it back",
                ),
                (
                    f"edit(kind='profile', id={ref.id}, palette={{…}})",
                    "patch a field",
                ),
            ]
        )
        return Response(body=body)

    def edit(
        self,
        *,
        id: str | int | None = None,
        name: str | None = None,
        palette: dict[str, Any] | None = None,
        fonts: dict[str, Any] | None = None,
        logos: Any = None,
        sponsor_line: str | None = None,
        owner: str | None = None,
        default: bool | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            raise BadInput(
                "edit requires id=",
                next="edit(kind='profile', id=N|'pf<N>'|'<name>', palette={…})",
            )
        ref = self._resolve_ref(id)
        self._check_write(ref)
        if owner is not None:
            owner = owner.strip()
            self._check_write(owner=owner)
            if self.store.get_web_user(owner) is None:
                raise BadInput(
                    f"owner={owner!r} is not a web account",
                    next="owner= must be an existing web_users login",
                )
        payload = profiles.merge_payload(
            profiles.payload_of(ref),
            name=name,
            palette=palette,
            fonts=fonts,
            logos=logos,
            sponsor_line=sponsor_line,
            owner=owner,
            default=default,
        )
        payload["owner"] = (
            owner
            if owner is not None
            else (ref.owner_login or payload.get("owner") or "")
        )
        profiles.validated_profile(self.store, payload)
        new_name = str(payload.get("name") or ref.title or "")
        self.store.update_ref(
            ref.id, title=new_name, meta_patch={profiles.META_KEY: payload}
        )
        if owner is not None and owner != ref.owner_login:
            self.store.set_ref_owner(ref.id, owner)
        return Response(body=f"edited profile id={ref.id} '{new_name}'")

    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None:
            raise BadInput(
                "delete requires id=",
                next="delete(kind='profile', id=N)",
            )
        ref = self._resolve_ref(id)
        self._check_write(ref)
        self.store.retire_ref(ref.id)
        return Response(body=f"deleted profile id={ref.id}")
