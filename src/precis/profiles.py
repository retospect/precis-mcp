"""``kind='profile'`` storage — owner-scoped branding profiles.

Slice 6 of ``docs/backlog/draft-poster-genre-and-themes.md`` (Reto rulings
2026-10-15): a profile is a numeric ``refs`` row of ``kind='profile'``
whose payload lives in ``refs.meta['profile']`` — a dict shaped exactly
like :class:`precis.export.poster_profile.PosterProfile`'s field list
(``name``, ``palette`` role→hex, ``fonts``, ``logos`` figure ids,
``sponsor_line``, ``owner``) plus a ``default`` flag marking the owner's
fallback profile. ``refs.title`` mirrors ``name`` and
``refs.owner_login`` carries the owning ``web_users.login`` — the same
per-record ownership column ``anki`` uses (migration 0164). No chunk, no
migration: the profile is structured config, not prose, and the boot
upsert registers the ``kinds`` row.

**ACL (ruling 3 — owner-only read, owner-checked write)** keys on the
acting identity: a write/read issued under ``actor='user:<abbrev>'``
(:func:`precis.acting_user.actor_for` — web ``human_write_context`` or a
stdio ``PRECIS_ACTING_USER`` serve) maps to that account's ``login`` and
must match the row's ``owner_login``. An unidentified caller — the fleet
``agent`` actor, plain CLI — is treated as the deployment itself and
passes; the guard exists to keep *users'* private palettes from each
other, not to fence off agents that already hold the shared credential.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.export.poster_profile import (
    PosterProfile,
    validate_poster_profile,
)
from precis.store.revision_context import (
    current_revision_context,
    process_actor,
)

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref

log = logging.getLogger(__name__)

#: ``refs.meta`` key holding the profile payload.
META_KEY = "profile"

KIND = "profile"


def acting_login(store: Store) -> str | None:
    """The calling identity's ``web_users.login``, or ``None``.

    Reads the revision actor (``user:<abbrev>``) of the enclosing write
    context, else the process actor a stdio ``precis serve`` installed
    from ``PRECIS_ACTING_USER``. ``agent``/absent → ``None``.
    """
    ctx = current_revision_context()
    actor = (ctx.actor if ctx else None) or process_actor() or ""
    if not actor.startswith("user:"):
        return None
    try:
        user = store.get_web_user_by_abbrev(actor[len("user:") :])
    except Exception:  # pragma: no cover - store hiccup; treat as unidentified
        log.debug("profiles: acting-user lookup failed", exc_info=True)
        return None
    return user.login if user is not None else None


def payload_of(ref: Ref) -> dict[str, Any]:
    """The stored profile payload (``meta['profile']``), ``{}`` if absent."""
    meta = ref.meta or {}
    raw = meta.get(META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def profile_of_ref(ref: Ref) -> PosterProfile:
    """The ref's payload as a :class:`PosterProfile` (unvalidated)."""
    return PosterProfile.from_dict(payload_of(ref))


def profiles_for_owner(store: Store, login: str) -> list[Ref]:
    """Live profile refs owned by ``login``, id order."""
    refs = store.list_refs(kind=KIND, owner_login=login, limit=200)
    return sorted(refs, key=lambda r: r.id)


def all_profiles(store: Store) -> list[Ref]:
    """Every live profile ref, id order (the agent-scope listing)."""
    return sorted(store.list_refs(kind=KIND, limit=200), key=lambda r: r.id)


def owner_default(store: Store, login: str) -> Ref | None:
    """``login``'s default profile: the ``default``-flagged one (lowest id
    wins when several carry the flag), else their only profile, else
    ``None``."""
    mine = profiles_for_owner(store, login)
    if not mine:
        return None
    flagged = [r for r in mine if payload_of(r).get("default")]
    if flagged:
        return flagged[0]
    return mine[0] if len(mine) == 1 else None


def resolve_profile_ref(store: Store, ident: Any, *, owner: str | None) -> Ref | None:
    """Resolve a profile selector — numeric id, ``pf<N>`` handle, or a
    profile *name* (scoped to ``owner``'s profiles when known) — to a
    live ref. ``None`` when nothing matches."""
    s = str(ident).strip()
    if not s:
        return None
    if s.lower().startswith("pf") and s[2:].isdigit():
        s = s[2:]
    if s.isdigit():
        return store.get_ref(kind=KIND, id=int(s))
    if owner:
        for ref in profiles_for_owner(store, owner):
            if payload_of(ref).get("name") == s or ref.title == s:
                return ref
    for ref in all_profiles(store):
        if payload_of(ref).get("name") == s or ref.title == s:
            return ref
    return None


def draft_owner_login(store: Store, ref: Ref) -> str | None:
    """The draft's owning account login — ``refs.owner_login`` on the
    draft, else on its owning project todo (the ``draft-of`` 1:1)."""
    if ref.owner_login:
        return ref.owner_login
    try:
        pid = store.drafts.draft_owner(ref.id)
    except Exception:  # pragma: no cover - defensive; absence is not fatal
        return None
    if pid is None:
        return None
    owner = store.get_ref(kind="todo", id=int(pid))
    return owner.owner_login if owner is not None else None


def draft_user_default(store: Store, ref: Any) -> dict[str, Any] | None:
    """The ``user_default=`` dict for ``resolve_poster_profile`` on this
    draft (slice-6 seam, spec resolution order):

    1. an explicit ``meta.workspace.poster.profile`` selector — a
       profile id, ``pf<N>`` handle, or name — wins over the owner
       default (a poster may override its owner's profile);
    2. else the draft owner's ``default`` profile;
    3. else ``None`` → the product palette.

    A selector that names nothing raises :class:`NotFound` — the export
    must fail loudly on a dangling pointer rather than silently render
    the wrong branding.
    """
    meta = ref.meta or {}
    ws = meta.get("workspace")
    poster = ws.get("poster") if isinstance(ws, dict) else None
    selector = poster.get("profile") if isinstance(poster, dict) else None
    owner = draft_owner_login(store, ref)
    if selector:
        prof = resolve_profile_ref(store, selector, owner=owner)
        if prof is None:
            raise NotFound(
                f"draft {ref.public_id}: poster profile {selector!r} not found",
                next=(
                    "get(kind='profile') to list profiles, or fix "
                    "meta.workspace.poster.profile"
                ),
            )
        return payload_of(prof)
    if owner:
        prof = owner_default(store, owner)
        if prof is not None:
            return payload_of(prof)
    return None


def logo_asset(store: Store, figure_id: str) -> tuple[bytes, str] | None:
    """``(bytes, ext)`` for a strip logo — the figure-blob resolver the
    export path already uses. Lazy import: ``precis.export.latex`` is a
    heavy module this data layer must not pay for at import time."""
    from precis.export.latex import _figure_logo_asset

    return _figure_logo_asset(store, figure_id)


def normalize_logos(raw: Any) -> list[dict[str, Any]]:
    """Coerce a caller's ``logos=`` into the payload's list-of-dicts.

    Accepts ``[{'figure': '…', 'alt': '…', …}]`` or the shorthand list of
    figure ids ``['ul-logo', 42, 'fg7']`` — a bare value becomes
    ``{'figure': <value>}``. Raises :class:`BadInput` on any other shape.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise BadInput(
            f"logos= must be a list, got {type(raw).__name__}",
            next="logos=[{'figure': '<slug-or-fg-id>', 'alt': '…'}] or ['<figure>', …]",
        )
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(dict(entry))
        elif isinstance(entry, (str, int)):
            out.append({"figure": str(entry)})
        else:
            raise BadInput(
                f"logos= entries must be dicts or figure ids, got {entry!r}",
                next="logos=[{'figure': '<slug-or-fg-id>', 'alt': '…'}]",
            )
    return out


def build_payload(
    *,
    name: str,
    owner: str,
    palette: dict[str, Any] | None = None,
    fonts: dict[str, Any] | None = None,
    logos: Any = None,
    sponsor_line: str = "",
    default: bool = False,
) -> dict[str, Any]:
    """Assemble the ``meta['profile']`` payload from verb fields."""
    return {
        "name": name,
        "owner": owner,
        "palette": dict(palette or {}),
        "fonts": dict(fonts or {}),
        "logos": normalize_logos(logos),
        "sponsor_line": sponsor_line or "",
        "default": bool(default),
    }


def validated_profile(store: Store, payload: dict[str, Any]) -> PosterProfile:
    """Parse + validate a payload; raise :class:`BadInput` carrying the
    validator's issue codes on failure (the spec's ``put``/``edit``
    surface returns the poster validators' codes verbatim)."""
    profile = PosterProfile.from_dict(payload)
    issues = validate_poster_profile(
        profile, logo_resolver=lambda fid: logo_asset(store, fid)
    )
    if issues:
        codes = "; ".join(
            f"{i.code} ({i.detail})" if i.detail else i.code for i in issues
        )
        raise BadInput(
            f"invalid profile: {codes}",
            next="fix the named field(s) — see get(kind='skill', id='precis-profile-help')",
        )
    return profile


def merge_payload(
    stored: dict[str, Any],
    *,
    name: str | None = None,
    palette: dict[str, Any] | None = None,
    fonts: dict[str, Any] | None = None,
    logos: Any = None,
    sponsor_line: str | None = None,
    owner: str | None = None,
    default: bool | None = None,
) -> dict[str, Any]:
    """``edit`` semantics: ``palette``/``fonts`` merge per key (same rule
    as :func:`~precis.export.poster_profile.resolve_poster_profile`'s
    layer merge); ``name``/``sponsor_line``/``owner``/``default``/
    ``logos`` replace. Omitted fields keep their stored value."""
    out = dict(stored)
    if name is not None:
        out["name"] = name
    if owner is not None:
        out["owner"] = owner
    if sponsor_line is not None:
        out["sponsor_line"] = sponsor_line
    if default is not None:
        out["default"] = bool(default)
    if palette is not None:
        merged = dict(out.get("palette") or {})
        merged.update({str(k): str(v) for k, v in palette.items()})
        out["palette"] = merged
    if fonts is not None:
        merged = dict(out.get("fonts") or {})
        merged.update({str(k): str(v) for k, v in fonts.items()})
        out["fonts"] = merged
    if logos is not None:
        out["logos"] = normalize_logos(logos)
    return out


__all__ = [
    "KIND",
    "META_KEY",
    "acting_login",
    "all_profiles",
    "build_payload",
    "draft_owner_login",
    "draft_user_default",
    "logo_asset",
    "merge_payload",
    "normalize_logos",
    "owner_default",
    "payload_of",
    "profile_of_ref",
    "profiles_for_owner",
    "resolve_profile_ref",
    "validated_profile",
]
