"""Token-authenticated file downloads — the agent's way out of the
corpus (gr517423 slice 3; spec ``paper-backup-pdf-transfer.md``).

``GET /files/pdf/{sha}?t=<token>`` streams a held paper's PDF. The token
is the whole credential: ``get(kind='paper', view='pdf_link')`` mints it
over ``(op='get', sha, ref_id, exp)`` — see :mod:`precis.file_links` —
reusable until expiry (default 600 s, cap 3600 s).

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

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from precis import file_links
from precis.corpus_layout import rebase_onto_local, resolve_local_pdf
from precis_web.config import WebConfig
from precis_web.deps import get_store, get_web_config

router = APIRouter(tags=["files"])


@router.get("/files/pdf/{sha}")
def pdf_by_token(
    sha: str,
    request: Request,
    cfg: WebConfig = Depends(get_web_config),
) -> FileResponse:
    store = get_store(request)
    try:
        claims = file_links.verify(
            request.query_params.get("t") or "", op="get", sha=sha, store=store
        )
    except file_links.TokenExpired as exc:
        raise HTTPException(status_code=410, detail="link expired") from exc
    except file_links.LinkTokenError as exc:
        raise HTTPException(status_code=403, detail="invalid token") from exc

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
