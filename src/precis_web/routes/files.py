"""Token-authenticated file downloads — the agent's way out of the
corpus (gr517423 slice 3; spec ``paper-backup-pdf-transfer.md``).

``GET /files/pdf/{sha}?t=<token>`` streams a held paper's PDF. The token
is the whole credential: ``get(kind='paper', view='pdf_link')`` mints it
over ``(op='get', sha, ref_id, exp)`` — see :mod:`precis.file_links` —
reusable until expiry (default 600 s, cap 3600 s).

``GET /files/blob/{sha}`` and ``GET /files/pdf_page/{sha}/{page}`` are the
``page_image`` view's two doors (gr520326): a ``chunk_blobs`` figure crop
and one PDF page rendered to PNG. Same token shape, same ref-bound rule —
``op='get'`` signed over the sha, and the fetch refuses when the token's
ref doesn't own it.

``POST /files/upload/{ref_id}?t=<token>`` is the slice-6 ingress half —
``put(kind='paper', args={'pdf_upload_link': True})`` mints it over
``(op='put', sha='upload', ref_id, exp)``. The body is the raw PDF
(``curl --data-binary``, no multipart framing): the token is verified
before a single body byte is read, the stream is written under
``$PRECIS_UPLOAD_STAGING`` and hashed as it arrives, and the
50 MB cap aborts mid-read (``413``) — so an unauthenticated or oversize
body never reaches disk (the route is exempt from the Basic gate, see
below). On success the part file renames to ``<ref_id>-<sha>.pdf``: the
staged file IS the record — no ``pdfs`` row is written at upload time
(ingest creates it when the attach drop folds, with the real
page_count/storage_path), and the ref in the name binds the staging to
the token's stub so a later attach can't re-home it.
Single-use: the token's ``exp.sig`` nonce is tombstoned ``.done``
*before* the body streams (a replay is a 409) — the download link's
reusable-until-expiry rule does NOT hold here. A failed upload
(``413``/``415``/disconnect) drops the tombstone again, leaving the
token reusable until expiry.

``POST /files/upload_tree/{ref_id}?t=<token>`` is the same door for an
se workspace's out/ tree (gr517643): ``{'op': 'workspace_upload_link'}``
on ``edit(kind='se')`` mints it over ``(op='put', sha='upload_tree',
ref_id, exp)``, the body is an uncompressed tar (``ustar`` magic at
offset 257 is the format gate), and the part renames to
``<ref_id>-<sha>.tar`` for ``import_workspace``'s ``staged`` key to
consume.

**This route authenticates itself**, which is why
:mod:`precis_web.auth` exempts the ``/files`` prefix from its blanket
Basic gate — and why there is deliberately NO Basic fallback here
(podcast's fallback exists for phone clients that can't send Basic; a
file-link caller always can, but accepting Basic would let one logged-in
user fetch a PDF another's token was never meant to widen). Order
matters: the token is verified before anything else is touched, so a
caller without a valid token learns nothing — not even whether the sha
exists. Only a well-signed claim earns the existence-revealing 404s.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from precis import file_links
from precis.corpus_layout import rebase_onto_local, resolve_local_pdf
from precis.ingest import pdf_upload
from precis_web.config import WebConfig
from precis_web.deps import get_store, get_web_config

if TYPE_CHECKING:
    from precis.store.types import Ref

router = APIRouter(tags=["files"])


def _store_chunk(fh: BinaryIO, hasher: hashlib._Hash, chunk: bytes) -> None:
    """Write one streamed chunk and fold it into the sha — run off the
    event loop via ``asyncio.to_thread`` (NAS writes block)."""
    fh.write(chunk)
    hasher.update(chunk)


def _verify(request: Request, *, op: str, sha: str):
    """The shared verify-then-anything gate: a bad/absent token is a
    403, an honest expiry a 410 — nothing else is touched first."""
    store = get_store(request)
    try:
        return file_links.verify(
            request.query_params.get("t") or "", op=op, sha=sha, store=store
        )
    except file_links.TokenExpired as exc:
        raise HTTPException(status_code=410, detail="link expired") from exc
    except file_links.LinkTokenError as exc:
        raise HTTPException(status_code=403, detail="invalid token") from exc


async def _stage_stream(
    request: Request,
    directory: Path,
    *,
    stem: str,
    magic_offset: int,
    magic: bytes,
    oversized: str,
    not_what: str,
) -> tuple[str, int]:
    """Claim ``<stem>.done``, then stream the request body to
    ``<stem>.part`` hashing it as it arrives; return ``(sha256, size)``
    for the caller to rename the part into its staged name.

    ``magic`` at ``magic_offset`` is the format gate — a body that ends
    before the check completes, or whose bytes fail it, is a 415;
    ``MAX_UPLOAD_BYTES`` is a 413 raised mid-read so an over-cap body
    never reaches disk in full. A failed upload (413/415/disconnect)
    drops both artifacts so the token stays reusable until expiry.
    """
    part, done = directory / f"{stem}.part", directory / f"{stem}.done"
    try:
        # The nonce is claimed BEFORE the body streams: the ``.done``
        # tombstone is created exclusively up front, so a replay is
        # refused no matter where the first upload stands — writing it
        # only after the rename left a double-claim window between the
        # ``.part`` rename and the touch.
        await asyncio.to_thread(done.touch, exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="upload link already used") from exc
    hasher = hashlib.sha256()
    size = 0
    head = bytearray()
    want = magic_offset + len(magic)
    checked = False
    try:
        fh = await asyncio.to_thread(part.open, "xb")
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > pdf_upload.MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=oversized)
                if not checked:
                    head += chunk[: want - len(head)]
                    if len(head) >= want:
                        checked = True
                        if bytes(head[magic_offset:want]) != magic:
                            raise HTTPException(status_code=415, detail=not_what)
                await asyncio.to_thread(_store_chunk, fh, hasher, chunk)
        finally:
            await asyncio.to_thread(fh.close)
        if not checked:
            raise HTTPException(status_code=415, detail=not_what)
    except FileExistsError as exc:
        # A stray ``.part`` under our own fresh nonce — cannot happen
        # (the tombstone claim serializes the nonce); still a 409, never
        # a clobbered in-flight upload.
        await asyncio.to_thread(done.unlink, missing_ok=True)
        raise HTTPException(status_code=409, detail="upload link already used") from exc
    except BaseException:
        # A failed upload (413/415/disconnect) frees the nonce again —
        # the token stays reusable until expiry.
        await asyncio.to_thread(part.unlink, missing_ok=True)
        await asyncio.to_thread(done.unlink, missing_ok=True)
        raise
    return hasher.hexdigest(), size


def _staging_dir_or_503() -> Path:
    directory = pdf_upload.uploads_dir()
    if directory is None:
        raise HTTPException(
            status_code=503,
            detail=f"{pdf_upload.UPLOAD_STAGING_ENV} unset on this node",
        )
    return directory


def _check_declared_length(request: Request, detail: str) -> None:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > pdf_upload.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=detail)


@router.post("/files/upload/{ref_id}")
async def upload_pdf(ref_id: int, request: Request):
    """Stage one agent-held PDF under ``PRECIS_UPLOAD_STAGING`` for
    later attach.

    Takes the raw PDF body — no ``UploadFile``/``File(...)`` param, so
    FastAPI never spools the (uncapped) body ahead of the token check.
    Writes nothing but the staging artifacts — the paper's own attach is
    the separate ``pdf_upload`` door (same two-step as
    ``pdf_url``/``paper_pull``: bytes first, attach second).
    """
    # The verify reads the vault key (and possibly the store) — off the
    # event loop like every other blocking call in this route.
    claims = await asyncio.to_thread(
        _verify, request, op="put", sha=file_links.UPLOAD_SHA
    )
    if claims.ref_id != ref_id:
        # A well-signed token — but for another paper's upload. Same
        # 403 a sha-bound token earns off its own resource.
        raise HTTPException(status_code=403, detail="token names another ref")
    store = get_store(request)
    directory = _staging_dir_or_503()
    _check_declared_length(request, "PDF exceeds the 50 MB cap")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    stem = pdf_upload.nonce(claims.exp, (request.query_params["t"]).rsplit(".", 1)[1])
    sha, size = await _stage_stream(
        request,
        directory,
        stem=stem,
        magic_offset=0,
        magic=b"%PDF",
        oversized="PDF exceeds the 50 MB cap",
        not_what="not a PDF",
    )
    part = directory / f"{stem}.part"
    await asyncio.to_thread(
        os.replace, part, directory / pdf_upload.staged_name(ref_id, sha)
    )
    ref = await asyncio.to_thread(store.get_ref, kind="paper", id=ref_id)
    handle = str(getattr(ref, "slug", "") or "") or f"pa{ref_id}"
    return {
        "sha256": sha,
        "bytes": size,
        "next": (
            f"put(kind='paper', id='{handle}', args="
            f"{{'pdf_upload': '{sha}', 'reason': '<why>'}})"
        ),
    }


@router.post("/files/upload_tree/{ref_id}")
async def upload_workspace_tree(ref_id: int, request: Request):
    """Stage one agent-held out/ tree — an uncompressed tar — under
    ``PRECIS_UPLOAD_STAGING`` for ``import_workspace``'s ``staged`` key
    (gr517643): the se analog of :func:`upload_pdf`, so workspace
    geometry bytes ride HTTP, never model tokens. Same contract —
    token verified before a body byte is read, single-use nonce
    tombstone, streamed hash — with ``ustar`` magic at offset 257 as
    the format gate instead of ``%PDF``. Its own token sentinel
    (``upload_tree``) means a paper-upload token cannot feed it and a
    tree token cannot stage a PDF."""
    claims = await asyncio.to_thread(
        _verify, request, op="put", sha=file_links.UPLOAD_TREE_SHA
    )
    if claims.ref_id != ref_id:
        raise HTTPException(status_code=403, detail="token names another ref")
    store = get_store(request)
    directory = _staging_dir_or_503()
    _check_declared_length(request, "tar exceeds the 50 MB cap")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    stem = pdf_upload.nonce(claims.exp, (request.query_params["t"]).rsplit(".", 1)[1])
    sha, size = await _stage_stream(
        request,
        directory,
        stem=stem,
        magic_offset=257,
        magic=b"ustar",
        oversized="tar exceeds the 50 MB cap",
        not_what="not an uncompressed tar",
    )
    part = directory / f"{stem}.part"
    await asyncio.to_thread(
        os.replace, part, directory / pdf_upload.staged_tree_name(ref_id, sha)
    )
    refs = await asyncio.to_thread(store.fetch_refs_by_ids, [ref_id])
    target = refs.get(ref_id)
    handle = str(getattr(target, "slug", "") or "") or f"ref{ref_id}"
    return {
        "sha256": sha,
        "bytes": size,
        "next": (
            f"edit(kind='se', id='{handle}', ops=[{{'op': "
            f"'import_workspace', 'workspace': '<workspace slug>', "
            f"'staged': '{sha}'}}])"
        ),
    }


def _resolve_ref_pdf(store, cfg: WebConfig, ref_id: int, sha: str) -> tuple[Ref, Path]:
    """Token-verified ref → the on-disk path of the PDF it owns.

    Shared by ``/files/pdf`` and ``/files/pdf_page``. Kind-agnostic on
    purpose (``fetch_refs_by_ids``, not ``get_ref(kind='paper')``) — the
    datasheet kind that motivated ``page_image`` is not a paper, so a
    kind-scoped lookup would 404 the ref a token was minted for. A valid
    token whose ref moved/dropped/retired the sha is a 404, and a
    cite_key-convention hit is corroborated against the recorded byte
    size before it may serve — a replace can leave an older PDF under
    the same convention name.
    """
    ref = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
    if ref is None or getattr(ref, "pdf_sha256", None) != sha:
        # Valid token, but the ref no longer owns this sha — it was
        # moved to another ref, dropped, or the ref retired since the
        # link was minted.
        raise HTTPException(status_code=404, detail="no such file")
    recorded = store.pdf_storage_path(sha)
    path = resolve_local_pdf(cfg.corpus_dirs, recorded, store.ref_cite_keys(ref.id))
    if path is None:
        raise HTTPException(status_code=404, detail="file not on this node")
    # A cite_key-convention hit can be a stale earlier PDF of the same
    # ref — a replace that left the old bytes under the same name. The
    # link promises ``sha256=<sha>``, so corroborate the byte size the
    # ``pdfs`` row recorded for that sha before serving. The recorded
    # ``storage_path`` (and its cross-mount rebase) is the authoritative
    # leg and needs no check.
    on_recorded_leg = bool(recorded) and path in (
        Path(recorded),
        rebase_onto_local(recorded, cfg.corpus_dirs),
    )
    if not on_recorded_leg:
        size = store.pdf_size_bytes(sha)
        if size is None or path.stat().st_size != size:
            raise HTTPException(
                status_code=404, detail="stored bytes no longer match the record"
            )
    return ref, path


@router.get("/files/pdf/{sha}")
def pdf_by_token(
    sha: str,
    request: Request,
    cfg: WebConfig = Depends(get_web_config),
) -> FileResponse:
    store = get_store(request)
    claims = _verify(request, op="get", sha=sha)
    ref, path = _resolve_ref_pdf(store, cfg, claims.ref_id, sha)
    stem = ref.slug or f"pa{ref.id}"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
        headers={"Cache-Control": "private, no-store"},
    )


# The blob door serves only raster images — the DB ``mime`` is a claim,
# not a truth, and ``image/svg+xml`` verbatim would be a same-origin
# script context. Today only PIL-written png/jpeg land here anyway.
_BLOB_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


@router.get("/files/blob/{sha}")
def blob_by_token(sha: str, request: Request) -> Response:
    """A stored figure crop — the ``page_image`` view's blob half
    (gr520326). The token signs ``(op='get', sha, ref_id)`` and the
    fetch is ref-bound: :meth:`chunk_blob_for_ref` resolves the sha only
    under the ref that owns the chunk, so a valid token for *this* sha
    still 404s when it names another ref's blob. The mime is served only
    when it's an allowlisted raster image, always with ``nosniff``."""
    store = get_store(request)
    claims = _verify(request, op="get", sha=sha)
    hit = store.chunks.chunk_blob_for_ref(sha, claims.ref_id)
    if hit is None or hit[1] not in _BLOB_IMAGE_MIMES:
        raise HTTPException(status_code=404, detail="no such blob")
    data, mime = hit
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/files/pdf_page/{sha}/{page}")
def pdf_page_by_token(
    sha: str,
    page: int,
    request: Request,
    cfg: WebConfig = Depends(get_web_config),
) -> Response:
    """One page of a held PDF rendered to PNG — the ``page_image``
    view's page half (gr520326). Same ref-bound resolve+verify as
    ``/files/pdf`` (:func:`_resolve_ref_pdf`, kind-agnostic, with the
    stale-bytes size check on the cite_key leg). The page index rides
    the path unsigned, so ``< 0`` and out-of-range refuse here (fitz's
    ``doc[-1]`` would happily render the last page)."""
    store = get_store(request)
    claims = _verify(request, op="get", sha=sha)
    if page < 0:
        raise HTTPException(status_code=404, detail="page out of range")

    _ref, path = _resolve_ref_pdf(store, cfg, claims.ref_id, sha)
    from precis.figdigit import render_page_png

    try:
        png, _page_count = render_page_png(path, page=page)
    except IndexError:
        raise HTTPException(status_code=404, detail="page out of range") from None
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store"},
    )
