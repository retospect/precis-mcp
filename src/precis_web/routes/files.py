"""Token-authenticated file downloads — the agent's way out of the
corpus (gr517423 slice 3; spec ``paper-backup-pdf-transfer.md``).

``GET /files/pdf/{sha}?t=<token>`` streams a held paper's PDF. The token
is the whole credential: ``get(kind='paper', view='pdf_link')`` mints it
over ``(op='get', sha, ref_id, exp)`` — see :mod:`precis.file_links` —
reusable until expiry (default 600 s, cap 3600 s).

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
from typing import BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from precis import file_links
from precis.corpus_layout import rebase_onto_local, resolve_local_pdf
from precis.ingest import pdf_upload
from precis_web.config import WebConfig
from precis_web.deps import get_store, get_web_config

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
    directory = pdf_upload.uploads_dir()
    if directory is None:
        raise HTTPException(
            status_code=503,
            detail=f"{pdf_upload.UPLOAD_STAGING_ENV} unset on this node",
        )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > pdf_upload.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 50 MB cap")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    stem = pdf_upload.nonce(claims.exp, (request.query_params["t"]).rsplit(".", 1)[1])
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
    try:
        fh = await asyncio.to_thread(part.open, "xb")
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > pdf_upload.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail="PDF exceeds the 50 MB cap"
                    )
                if len(head) < 4:
                    head += chunk[: 4 - len(head)]
                    if len(head) == 4 and not head.startswith(b"%PDF"):
                        raise HTTPException(status_code=415, detail="not a PDF")
                await asyncio.to_thread(_store_chunk, fh, hasher, chunk)
        finally:
            await asyncio.to_thread(fh.close)
        if len(head) < 4:
            raise HTTPException(status_code=415, detail="not a PDF")
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
    sha = hasher.hexdigest()
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


@router.get("/files/pdf/{sha}")
def pdf_by_token(
    sha: str,
    request: Request,
    cfg: WebConfig = Depends(get_web_config),
) -> FileResponse:
    store = get_store(request)
    claims = _verify(request, op="get", sha=sha)

    ref = store.get_ref(kind="paper", id=claims.ref_id)
    if ref is None or getattr(ref, "pdf_sha256", None) != sha:
        # Valid token, but the paper no longer owns this sha — it was
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
    stem = ref.slug or f"pa{ref.id}"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{stem}.pdf",
        headers={"Cache-Control": "private, no-store"},
    )
