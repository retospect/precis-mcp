"""Slug-addressed ref resolution shared by handlers and workers.

Lives in ``utils`` so ``precis.workers`` can resolve a ``(kind, slug)``
pair without importing ``precis.handlers``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound
from precis.hints import bare_numeric_hint
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store


def resolve_live_slug_ref(
    store: Store,
    *,
    kind: str,
    id: str | int,
    next_hint: str | None = None,
    options: Sequence[str] | None = None,
) -> Ref:
    """Coerce a ``(kind, slug)`` pair to a live :class:`Ref` or raise.

    Canonicalises the pattern that every slug-addressed handler
    (oracle, paper, patent, conv, markdown, plaintext)
    repeats in its ``get`` / ``tag`` / ``link`` / ``delete`` entry
    points::

        ref = store.get_ref(kind=kind, id=slug)
        if ref is None:
            raise NotFound(f"{kind} slug {slug!r} not found", next=...)

    Returns the live :class:`Ref` (``.id``, ``.slug``, ``.title``,
    ``.meta``, …) so callers destructure whatever they need. Note
    that :meth:`Store.get_ref` already filters out soft-deleted
    rows, so the returned ref is always live — callers handling
    tombstones should use the lower-level store API directly.

    Args:
        store: Store instance to probe.
        kind: Kind slug, used both for the DB lookup and to shape
              the default error message.
        id:   The slug the caller passed in. Coerced to :class:`str`
              and stripped of surrounding whitespace.
        next_hint: Optional override for the error's ``next:``
              hint. Defaults to the canonical
              ``search(kind=..., q='...') to find existing``.
              Pass a richer hint when the handler already has a
              concrete path (``_suggest_paper_slugs``-style fuzzy
              matches belong in ``options=`` instead).
        options: Optional spelling suggestions forwarded into the
              :class:`NotFound` envelope so the agent sees
              ``options: [...]`` in the error body. Falsy values
              (empty list / ``None``) are normalised to ``None``
              so the envelope stays tidy.

    Raises:
        NotFound: the ref does not exist (or was soft-deleted and
                  is therefore unreachable through ``get_ref``).
    """
    slug = str(id).strip()
    # A ``/<view>`` list-path (e.g. ``/recent``) reaching the slug resolver
    # is a list-view misfire, not a missing ref: slug-addressed kinds are
    # keyed by slug/handle, and the numeric kinds' ``/recent`` shape isn't
    # universal (gr48523). Handlers that DO support a bare list intercept it
    # before calling here, so anything ``/``-prefixed that lands here has no
    # such view — give a real recovery path, not the misleading
    # "slug '/recent' not found".
    if slug.startswith("/"):
        raise BadInput(
            f"{kind!r} has no {slug!r} list view — it is addressed by slug/handle",
            next=next_hint or f"search(kind={kind!r}, q='...') to find refs",
        )
    # accept the universal record handle (e.g. ``or123``) — the
    # form output now emits — resolving it by ref_id; else the slug path.
    parsed = handle_registry.parse(slug)
    if parsed is not None and parsed[0] == kind and not parsed[1]:
        ref = store.get_ref(kind=kind, id=parsed[2])
    else:
        ref = store.get_ref(kind=kind, id=slug)
    if ref is None and slug.isdigit():
        # A1: the agent passed a bare number (the prefix stripped off a
        # ``<code><id>`` handle). Slug-addressed kinds are never addressed by a
        # bare number, so it is almost certainly the ref_id — resolve it as
        # such, then admonish so the habit (and bare numbers in cited text)
        # doesn't take hold.
        ref = store.get_ref(kind=kind, id=int(slug))
        if ref is not None:
            handle = handle_registry.try_format(kind, ref.id)
            # A slug-addressed kind's own slug can itself be a bare
            # numeral (e.g. a plan named after its owning project's id).
            # When that's what just resolved, the caller's digits WERE
            # this ref's real, canonical slug all along — narrating "you
            # meant the handle, use it next time" is a false
            # self-correction in that case (gr311347 #13). Only admonish
            # when the digits were a genuine ref_id guess, i.e. they
            # don't match the ref's own registered slug.
            if handle is not None and ref.slug != slug:
                store.emit_hint(bare_numeric_hint(kind, slug, handle))
    if ref is None:
        cause = f"{kind} slug {slug!r} not found"
        if slug.isdigit():
            # The digits were already tried as a ref_id fallback (the A1
            # branch above) and that also missed — say so, or the bare
            # "not found" reads like a slug typo and invites another
            # numeric-id retry. Kinds reaching this helper are
            # slug-addressed; numeric ref ids are only that one
            # best-effort fallback, not a first-class address form.
            cause += (
                f" ({kind} is slug-addressed; the digits were also tried "
                "as a ref_id, which missed too)"
            )
        raise NotFound(
            cause,
            next=next_hint or f"search(kind={kind!r}, q='...') to find existing",
            options=list(options) if options else None,
        )
    return ref
