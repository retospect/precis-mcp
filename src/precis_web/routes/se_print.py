"""Checked print-file downloads for an ``se`` design.

* ``GET /se/{slug}/print/{block}.3mf`` (also ``.stl``) — the file
  ``get(kind='se', id=slug, view='print', args={'block': block, 'fmt': '3mf'})``
  writes, as a browser download. Both go through
  :func:`precis_se.printing.export_block_mesh` on a ``report_for(...,
  mesh_checks=True)`` report, so the floating-region check runs and the
  web file and the agent file are the same bytes. Response headers
  ``X-Precis-Print-Errors`` / ``X-Precis-Print-Findings`` carry the
  report's error-severity and total finding counts. Unknown design or
  block -> 404; a block that cannot be printed (not fdm, an instance, a
  print-group root, unrealized, net-empty, no ``manifold3d``) -> 409 with
  the reason as plain text.

:func:`print_files` is the page-render half: the reader page lists the
printable blocks from the CHEAP report (``mesh_checks=False``, no mesh
slice), so the heavy check runs only when a link is clicked.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from precis.cad.export import ExportError
from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_se import modes as se_modes
from precis_se import persist as se_persist
from precis_se import printgroup as se_printgroup
from precis_se import printing as se_printing
from precis_web.deps import get_store

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis_se.ops import SeTree

router = APIRouter(tags=["se"])

log = logging.getLogger(__name__)

_MEDIA = {"3mf": "model/3mf", "stl": "model/stl"}


class _Refused(Exception):
    """A block that cannot be printed; ``str(exc)`` is the reason."""


@dataclass(frozen=True)
class PrintFile:
    """One printable block on the reader page (cheap report only)."""

    block: str
    url: str
    errors: int
    warns: int


def _refusal(tree: SeTree, block: str) -> str | None:
    """Why ``block`` is not a single-block print target, from the tree alone
    (mirrors the handler's checks in ``_render_print``)."""
    node = tree.blocks[block]
    if node.template is not None:
        return (
            f"block {block!r} is an instance (of {node.template!r}) — print "
            f"status lives on the template, {node.template!r}"
        )
    family = se_modes.family_of(node.mode)
    if family is None or family.key != "fdm":
        return (
            f"block {block!r}'s resolved mode family isn't fdm — only fdm blocks "
            "have a checked print file"
        )
    if se_printgroup.is_group_root(tree, block):
        return (
            f"block {block!r} is a print group root — export it with "
            f"get(kind='se', view='print', args={{'block': {block!r}, 'fmt': '3mf'}})"
        )
    return None


def print_files(store: Store, tree: Any, slug: str) -> list[PrintFile]:
    """The printable blocks of ``tree``, from the cheap per-block report.

    Printable = ``report_for(..., mesh_checks=False)`` found a realized,
    non-empty solid with a build frame. Never builds the print mesh. A
    block whose cheap report raises is skipped (logged) — the page must not
    fail over a download list."""
    out: list[PrintFile] = []
    for block in sorted(tree.blocks):
        if _refusal(tree, block) is not None:
            continue
        try:
            report = se_printing.report_for(
                tree, block, cad_store_reader=store, mesh_checks=False
            )
        except Exception:  # a list item is never worth a 500
            log.warning("se print list: %s/%s failed", slug, block, exc_info=True)
            continue
        if report is None or report.printed is None or report.chosen_down is None:
            continue
        out.append(
            PrintFile(
                block=block,
                url=f"/se/{quote(slug, safe='')}/print/{quote(block, safe='')}.3mf",
                errors=sum(1 for f in report.findings if f.severity == "error"),
                warns=sum(1 for f in report.findings if f.severity == "warn"),
            )
        )
    return out


def _build(
    store: Store, slug: str, block: str, fmt: str
) -> tuple[str, bytes, int, int]:
    """Blocking: the checked export -> ``(ref slug, bytes, errors, findings)``.
    Raises :class:`NotFound` (design/block) or :class:`_Refused`."""
    ref = resolve_live_slug_ref(store, kind="se", id=slug)
    tree = se_persist.load_tree(store, ref.id)
    if block not in tree.blocks:
        raise NotFound(f"no block {block!r} in design {slug!r}")
    why = _refusal(tree, block)
    if why is not None:
        raise _Refused(why)
    try:
        report = se_printing.report_for(tree, block, cad_store_reader=store)
        if report is None:  # pragma: no cover — _refusal covers the family
            raise _Refused(f"block {block!r} is not an fdm block")
        with tempfile.TemporaryDirectory() as td:
            exported = se_printing.export_block_mesh(
                report,
                fmt,
                Path(td) / f"{ref.slug}-{block}.{fmt}",
                title=f"{ref.slug}-{block}",
            )
            data = exported.path.read_bytes()
    except (
        se_printing.PrintUnsupported,
        se_printing.NothingToExport,
        ExportError,
    ) as exc:
        raise _Refused(str(exc)) from exc
    errors = sum(1 for f in exported.findings if f.severity == "error")
    return str(ref.slug), data, errors, len(exported.findings)


@router.get("/se/{slug}/print/{block_fmt}")
async def se_print_download(request: Request, slug: str, block_fmt: str) -> Response:
    """Download the checked print file of one block (``<block>.3mf`` or
    ``<block>.stl``)."""
    block, _, fmt = block_fmt.rpartition(".")
    fmt = fmt.lower()
    if not block or fmt not in _MEDIA:
        return PlainTextResponse(
            f"expected /se/<slug>/print/<block>.3mf (or .stl), got {block_fmt!r}",
            status_code=404,
        )
    store = get_store(request)
    try:
        ref_slug, data, errors, findings = await asyncio.to_thread(
            _build, store, slug, block, fmt
        )
    except NotFound as exc:
        return PlainTextResponse(str(exc), status_code=404)
    except _Refused as exc:
        return PlainTextResponse(str(exc), status_code=409)
    return Response(
        content=data,
        media_type=_MEDIA[fmt],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ref_slug}-{block}-print.{fmt}"'
            ),
            "X-Precis-Print-Errors": str(errors),
            "X-Precis-Print-Findings": str(findings),
        },
    )
