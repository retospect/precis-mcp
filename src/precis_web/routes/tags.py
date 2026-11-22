"""Tag maintenance routes — the browse surfaces moved into ``/drive``
(gr636493): ``GET /tags`` redirects to ``/drive?k=tag`` (the tag kind's
own listing) and ``GET /tags/refs`` to ``/drive?tag=…`` (the drive's
tag facet, which lists the same refs grouped as rows instead of the
old per-kind table). Both 302s preserve the meaningful params — the
index's ``q`` substring filter and page, the refs listing's tag /
kind / page — so existing links (status page chips, eye ``[ns:value]``
chips, bookmarks) keep landing on the equivalent view.

Kept here: :func:`resolve_tag_chip` + :func:`count_tag_refs` (the eye
chip's closed-vs-OPEN resolution and hover count), the protected-
namespace roster, and ``POST /tags/delete`` — the cluster-wide tag
wipe the drive's tag rows post to.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from precis import profiles
from precis.store._tag_filter import tag_filter_string
from precis.store._tags_ops import _bust_tag_caches
from precis_web.deps import get_store
from precis_web.routes.flags import _safe_local_redirect

router = APIRouter(prefix="/tags", tags=["tags"])

#: Namespaces the delete button never offers. Structural / closed-vocab
#: tags (``STATUS``, ``DREAM``, ``PRIO``, ``SRC``, ``CACHE``, ``EMBED``,
#: …) carry semantic meaning the handlers rely on. Blanket-deleting them
#: across the cluster would break things in obscure ways (a job whose
#: ``STATUS:running`` vanished would never appear failed). ``LLM`` stays
#: listed defensively even though the axis is retired (§M facet
#: normalization moved it to ``meta.llm_tier`` — migration 0102 rewrites
#: every stored ``LLM:*`` tag row, so none should remain) in case a
#: legacy row somehow survives. Per-ref removal via the standard
#: ``tag(remove=[...])`` verb is still available for those.
_PROTECTED_NAMESPACES: frozenset[str] = frozenset(
    {
        "STATUS",
        "LLM",
        "DREAM",
        "PRIO",
        "SRC",
        "CACHE",
        "EMBED",
    }
)


def _refs_drive_url(
    *,
    tag_filter: str,
    ref_kinds: list[str],
    page: int,
) -> str:
    """Build the ``/drive`` URL a ``/tags/refs`` call maps to.

    The old listing was kind-agnostic (every ref carrying the tag) and
    folder-agnostic, so the target pins every row-backed kind as an
    explicit ``k=`` (no ``k=`` would resolve the ``items_kinds`` cookie,
    silently dropping kinds) plus ``folder=*`` ("anywhere" — the drive's
    default top-level filter would hide filed refs). ``page=`` rides
    along."""
    pairs: list[tuple[str, str]] = [("tag", tag_filter)]
    pairs.extend(("k", kk) for kk in ref_kinds)
    pairs.append(("folder", "*"))
    if page > 1:
        pairs.append(("page", str(page)))
    return "/drive?" + urlencode(pairs)


def resolve_tag_chip(store: Any, text: str) -> tuple[str, str] | None:
    """Map a rendered ``[ns:value]`` chip back to its stored
    ``(namespace, value)`` row — ``None`` when no row matches either
    shape (gr575024).

    The chip text is ambiguous by design: a closed tag renders
    ``ns:value``, but an ``OPEN``/``FLAG`` chip renders just its value —
    which may itself carry a colon (``project:precis`` renders
    ``[project:precis]`` exactly like a closed ``programme``-axis tag
    would). Probe order mirrors :meth:`Tag.parse`: an all-uppercase
    prefix tries the closed ``(ns, v)`` split first, anything else tries
    the ``OPEN``/``FLAG`` sentinels with the whole text first; the
    stored row decides.
    """
    ns, sep, v = text.partition(":")
    closed_split = (ns, v) if sep and ns and v else None
    probes: list[tuple[str, str]] = []
    if closed_split is not None and ns.isupper():
        probes.append(closed_split)
    probes += [("OPEN", text), ("FLAG", text)]
    if closed_split is not None and not ns.isupper():
        probes.append(closed_split)
    marks = ", ".join("(%s, %s)" for _ in probes)
    params = tuple(p for pair in probes for p in pair)
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"SELECT namespace, value FROM tags WHERE (namespace, value) IN ({marks})",
            params,
        ).fetchall()
    found = {(str(r[0]), str(r[1])) for r in rows}
    for pair in probes:
        if pair in found:
            return pair
    return None


def count_tag_refs(store: Any, namespace: str, value: str, login: str | None) -> int:
    """Live, viewer-visible refs carrying ``(namespace, value)`` — the
    same WHERE the ``/tags/refs`` listing counted on (gr575024's
    chip-hover number)."""
    acl_sql, acl_params = profiles.visible_sql("r", login)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t USING(tag_id) "
            "WHERE t.namespace = %s AND t.value = %s "
            f"AND r.retired_at IS NULL AND {acl_sql}",
            (namespace, value, *acl_params),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


@router.get("", response_class=RedirectResponse)
@router.get("/", response_class=RedirectResponse)
async def index(q: str = "", page: int = 1) -> RedirectResponse:
    """The tag leaderboard lives in the drive now (gr636493): ``k=tag``
    lists the same roster — most-used first, ``q`` a substring filter,
    same pager."""
    pairs: list[tuple[str, str]] = [("k", "tag")]
    if q.strip():
        pairs.append(("q", q.strip()))
    if page > 1:
        pairs.append(("page", str(page)))
    return RedirectResponse(url="/drive?" + urlencode(pairs), status_code=302)


@router.get("/refs", response_class=RedirectResponse)
async def refs_by_tag(
    request: Request,
    namespace: str | None = None,
    value: str | None = None,
    tag: str | None = None,
    page: int = 1,
) -> RedirectResponse:
    """The refs-carrying-a-tag listing is the drive's ``tag=`` facet
    (gr636493). Call shapes all map:

    * ``?namespace=NS&value=V`` → ``/drive?tag=<filter>`` (+ every
      row-backed kind pinned — the old page was kind-agnostic). The
      filter string is :func:`tag_filter_string`'s canonical spelling,
      so a lowercase axis (``tier:dream`` …) keeps its stored pair as
      ``=ns:value`` instead of flattening to an OPEN/FLAG filter that
      matches nothing.
    * ``?tag=<chip text>`` → resolves the closed-vs-OPEN split via
      :func:`resolve_tag_chip` first, then the same tag= mapping —
      eye ``[ns:value]`` chips link here verbatim (gr575024).

    The ``?kind=`` narrowing param is gone (gr664412): nothing linked
    the kind-only call shape, and the one chip that combined it with a
    tag (the status page's per-STATUS count) now links ``/drive``
    directly — a kind-scoped tag pivot is ``/drive?tag=<f>&k=<kind>``.
    """
    if namespace is None and value is None and tag is None:
        raise HTTPException(
            status_code=400,
            detail="needs ?namespace=NS&value=V or ?tag=T",
        )
    store = get_store(request)
    if tag is not None and namespace is None and value is None:
        resolved = None
        if getattr(store, "pool", None) is not None:
            resolved = await asyncio.to_thread(resolve_tag_chip, store, tag)
        if resolved is not None:
            namespace, value = resolved
        else:
            # No stored row matches any probe — mirror ``Tag.parse``'s
            # split (uppercase prefix → closed pair, else the OPEN
            # whole-text the chip most likely renders).
            ns, sep, v = tag.partition(":")
            namespace, value = (ns, v) if sep and ns and ns.isupper() else ("OPEN", tag)
    page = max(1, page)
    if namespace is not None and value is not None:
        ref_kinds = sorted(await asyncio.to_thread(store.ref_kinds_present))
        target = _refs_drive_url(
            tag_filter=tag_filter_string(str(namespace), str(value)),
            ref_kinds=ref_kinds,
            page=page,
        )
    else:
        # A half-given pair (``?namespace=`` without ``value=``, or the
        # reverse) has no stored tag to pivot on — land on the tag
        # leaderboard, the nearest Drive equivalent.
        target = "/drive?k=tag"
    return RedirectResponse(url=target, status_code=302)


@router.post("/delete")
async def delete_tag(
    request: Request,
    namespace: str = Form(...),
    value: str = Form(...),
    q: str = Form(""),
    back: str = Form(""),
) -> RedirectResponse:
    """Wipe one tag from the entire cluster.

    Single ``DELETE FROM tags`` — the FK on ``ref_tags.tag_id`` is
    ``ON DELETE CASCADE``, so every ref carrying the tag is unhooked
    in the same statement. Protected (closed-vocab) namespaces refuse
    the operation; per-ref removal via the standard ``tag`` verb is
    still available for those.

    ``back`` returns to the page the button was posted from (the
    drive's tag rows pass the current filtered URL); ``q`` is the
    legacy search-box query — folded into the ``/drive?k=tag`` landing
    when no ``back`` rides along.
    """
    if namespace in _PROTECTED_NAMESPACES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"namespace {namespace!r} is protected; use the standard "
                f"tag(remove=[...]) verb to drop it from a specific ref"
            ),
        )
    store = get_store(request)
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM tags WHERE namespace = %s AND value = %s",
                (namespace, value),
            )
    # Committed now — drop this process's cached roster / namespace set so
    # the discovery surfaces (which read through this same store) reflect
    # the delete instead of serving it stale for the TTL.
    _bust_tag_caches(conn)
    pairs: list[tuple[str, str]] = [("k", "tag")]
    if q.strip():
        pairs.append(("q", q.strip()))
    # ``back`` is caller-supplied — the gr541794 open-redirect guard
    # (``/\host`` collapses to ``//host`` in the browser) before it can
    # become the 303's Location.
    target = _safe_local_redirect(back, "/drive?" + urlencode(pairs))
    return RedirectResponse(url=target, status_code=303)
