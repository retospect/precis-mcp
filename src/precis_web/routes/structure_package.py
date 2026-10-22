"""Structure package downloads (gr504830; :mod:`precis_web.structure_package`).

* ``GET /se/{slug}/package/{block}.png`` (also ``.svg``) — the package for the
  structure an se block binds: title and intent from the block, the relax
  verdict and atoms from the bound structure.
* ``GET /structure/{slug}/package.png`` (also ``.svg``) — the same for a bare
  structure, without the se context.

Rendered per request (seconds at worst; the atom cap bounds it) and served
``inline`` so a browser shows it and "copy image" works for chat.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.utils import handle_registry
from precis_se import persist as se_persist
from precis_web import relax_provenance
from precis_web import structure_package as pkg
from precis_web.deps import get_store

router = APIRouter(tags=["structure"])

_MEDIA = {"png": "image/png", "svg": "image/svg+xml"}


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _render(
    store: Any,
    struct_ref: Any,
    *,
    title: str,
    ref_line: str,
    url: str,
    intent: str,
    record: dict[str, Any],
    fmt: str,
) -> bytes:
    """Blocking: load the stored atoms, build the caption, render."""
    scene, _handles = store.structure_load(struct_ref.id)
    if not scene.atoms:
        raise NotFound(f"structure {struct_ref.slug!r} has no atoms to draw")
    relax = relax_provenance.structure_relax(store, struct_ref)
    meta = struct_ref.meta or {}
    rendered = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    description = str(meta.get("description") or "")
    info = pkg.PackageInfo(
        title=title,
        ref_line=ref_line,
        url=url,
        relax=relax,
        description=description,
        intent=intent,
        rendered_utc=rendered,
        record={
            **record,
            "structure": struct_ref.slug,
            "handle": handle_registry.try_format(
                "structure", struct_ref.id, chunk=False
            ),
            "structure_version": meta.get("version"),
            "url": url,
            "relax": relax.record,
            "intent": intent,
            "description": description,
            "rendered_utc": rendered,
        },
    )
    svg, caption = pkg.package_svg(scene, info)
    if fmt == "svg":
        return svg.encode("utf-8")
    return pkg.package_png(svg, pkg.png_text_fields(info, caption))


def _struct_ref_line(struct_ref: Any) -> str:
    handle = handle_registry.try_format("structure", struct_ref.id, chunk=False)
    version = (struct_ref.meta or {}).get("version")
    return (
        f"structure:{struct_ref.slug}"
        + (f" ({handle})" if handle else "")
        + (f" v{version}" if version is not None else "")
    )


def _se_package(store: Any, base: str, slug: str, block: str, fmt: str) -> bytes:
    ref = resolve_live_slug_ref(store, kind="se", id=slug)
    tree = se_persist.load_tree(store, ref.id)
    node = tree.blocks.get(block)
    if node is None:
        raise NotFound(f"no block {block!r} in design {slug!r}")
    if node.bound_kind != "structure" or not node.bound:
        raise NotFound(f"block {block!r} is not bound to a structure")
    struct_ref = resolve_live_slug_ref(store, kind="structure", id=node.bound)
    return _render(
        store,
        struct_ref,
        title=f"{block} — {ref.title or ref.slug}",
        ref_line=f"se:{ref.slug}~{block} · {_struct_ref_line(struct_ref)}",
        url=f"{base}/structure/{quote(str(struct_ref.slug), safe='')}",
        intent=str(getattr(node, "descr", None) or ""),
        record={"design": ref.slug, "block": block},
        fmt=fmt,
    )


def _structure_package(store: Any, base: str, slug: str, fmt: str) -> bytes:
    struct_ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    return _render(
        store,
        struct_ref,
        title=str(struct_ref.title or struct_ref.slug),
        ref_line=_struct_ref_line(struct_ref),
        url=f"{base}/structure/{quote(str(struct_ref.slug), safe='')}",
        intent="",
        record={},
        fmt=fmt,
    )


async def _respond(fn: Any, filename: str, fmt: str, *args: Any) -> Response:
    try:
        body = await asyncio.to_thread(fn, *args)
    except NotFound as exc:
        return PlainTextResponse(f"{exc}\n", status_code=404)
    except pkg.TooLarge as exc:
        return PlainTextResponse(
            f"too many atoms to draw in-request: {exc}\n", status_code=422
        )
    return Response(
        content=body,
        media_type=_MEDIA[fmt],
        headers={
            "Content-Disposition": f'inline; filename="{filename}.{fmt}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/se/{slug}/package/{block_fmt}")
async def se_structure_package(request: Request, slug: str, block_fmt: str) -> Response:
    """The package of the structure ``block`` binds (``<block>.png``/``.svg``)."""
    block, _, fmt = block_fmt.rpartition(".")
    fmt = fmt.lower()
    if not block or fmt not in _MEDIA:
        return PlainTextResponse(
            f"expected /se/<slug>/package/<block>.png (or .svg), got {block_fmt!r}\n",
            status_code=404,
        )
    return await _respond(
        _se_package,
        f"{slug}-{block}-package",
        fmt,
        get_store(request),
        _base(request),
        slug,
        block,
        fmt,
    )


@router.get("/structure/{slug}/package.{fmt}")
async def structure_package(request: Request, slug: str, fmt: str) -> Response:
    """The package of one structure (``package.png``/``.svg``)."""
    fmt = fmt.lower()
    if fmt not in _MEDIA:
        return PlainTextResponse(
            f"expected /structure/<slug>/package.png (or .svg), got {fmt!r}\n",
            status_code=404,
        )
    return await _respond(
        _structure_package,
        f"{slug}-package",
        fmt,
        get_store(request),
        _base(request),
        slug,
        fmt,
    )
