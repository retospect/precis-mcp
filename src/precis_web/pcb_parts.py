"""Part cards for the pcb workbench (docs/backlog/pcb-workbench-layout.md
slice 3): on hover a part shows refdes, value, package, LCSC C-number and
stock, plus a thumbnail.

The thumbnail URL cannot be derived from the C-number, so the server
resolves it ONCE from the part's LCSC product page (``og:image``) through
:func:`precis.utils.http.polite_safe_get` — the SSRF-guarded fetch under
the politeness core's ``lcsc`` policy — then
caches ``{url, mime, b64}`` on the part ref's ``meta`` (a lazily-minted
``refs(kind='part')`` row via :meth:`Store.ensure_part_ref`). The page only
ever serves the cached copy through ``/pcb/{slug}/part/{refdes}/thumb`` —
it never hotlinks. A part with no C-number, or a failed fetch, gets no
image (and the miss is cached briefly so hover storms don't refetch).
"""

from __future__ import annotations

import base64
import re
from typing import Any

from precis.errors import NotFound
from precis_web.pcb_hover import part_summary

_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_MAX_IMAGE_BYTES = 256 * 1024
#: Raster types only — an ``image/svg+xml`` thumb served same-origin would
#: execute script on a direct navigation (reviewer finding).
_ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


def cards(view: Any) -> dict[str, dict[str, Any]]:
    """``{refdes: {value, package, lcsc, stock}}`` for every live instance of
    the shown snapshot — the data behind both panes' hover cards."""
    tables = view.raw["tables"]
    components = {r["component_id"]: r for r in tables["pcb_components"]}
    parts = {r["lcsc"]: r for r in tables.get("parts", [])}
    out: dict[str, dict[str, Any]] = {}
    for inst in tables["pcb_instances"]:
        if inst.get("retired_at"):
            continue
        comp = components.get(inst["component_id"]) or {}
        row = parts.get(comp.get("part_lcsc")) or {}
        out[str(inst["refdes"])] = {
            **part_summary(parts.get(comp.get("part_lcsc")), comp),
            "stock": row.get("stock"),
        }
    return out


def _fetch_thumbnail(lcsc: str) -> tuple[bytes, str] | None:
    """Resolve the product page's ``og:image`` and fetch it — both hops
    through ``polite_safe_get`` (``safe_get`` under the politeness core's
    ``lcsc`` policy). ``None`` on any miss; never raises outward."""
    import httpx  # noqa: F401  (type surface)

    # Local imports: lets tests monkeypatch safe_fetch.safe_get (the repo's
    # standard seam, see precis.pcb.easyeda) without a real pinned client.
    from precis.utils.http import POLICIES, http_client, polite_safe_get

    try:
        with http_client(timeout=15.0) as client:
            page = polite_safe_get(
                client,
                f"https://www.lcsc.com/product-detail/{lcsc}.html",
                service="lcsc",
                policy=POLICIES["lcsc"],
            )
            if page.status_code != 200:
                return None
            m = _OG_IMAGE.search(page.text)
            if not m:
                return None
            img = polite_safe_get(
                client, m.group(1), service="lcsc", policy=POLICIES["lcsc"]
            )
            if img.status_code != 200 or len(img.content) > _MAX_IMAGE_BYTES:
                return None
            mime = img.headers.get("content-type", "").split(";")[0].strip()
            if mime not in _ALLOWED_MIME:
                return None
            return img.content, mime
    except Exception:
        return None


def thumbnail(store: Any, lcsc: str, *, fetch: Any = None) -> tuple[bytes, str] | None:
    """The cached thumbnail for a C-number, resolving+caching on first ask.
    Caches on the part ref's ``meta.thumb`` — minted via
    :meth:`Store.ensure_part_ref` when the catalog holds the part. ``fetch``
    is the test seam; production always uses :func:`_fetch_thumbnail`, whose
    two hops go through ``safe_get``."""
    try:
        ref_id = store.ensure_part_ref(lcsc)
    except (NotFound, ValueError):
        return None
    ref = store.get_ref(kind="part", id=ref_id)
    if ref is None:
        return None
    meta = dict(ref.meta or {})
    cached = meta.get("thumb")
    if isinstance(cached, dict):
        if cached.get("b64"):
            mime = cached.get("mime", "image/jpeg")
            if mime not in _ALLOWED_MIME:
                return None
            return base64.b64decode(cached["b64"]), mime
        if cached.get("miss"):
            return None
    got = (fetch or _fetch_thumbnail)(lcsc)
    if got is not None and got[1] not in _ALLOWED_MIME:
        got = None
    thumb: dict[str, Any] = (
        {"miss": True}
        if got is None
        else {
            "mime": got[1],
            "b64": base64.b64encode(got[0]).decode("ascii"),
        }
    )
    meta["thumb"] = thumb
    from psycopg.types.json import Jsonb

    with store.tx() as conn:
        conn.execute("UPDATE refs SET meta=%s WHERE ref_id=%s", (Jsonb(meta), ref_id))
    if got is None:
        return None
    return got


__all__ = ["cards", "thumbnail"]
