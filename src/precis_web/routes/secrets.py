"""``/secrets`` — the secrets-vault editor (ADR 0055).

Reads **`vault.list()` only** (name + masked hint + updated_at): the page never
decrypts, never holds ciphertext, and cannot reveal a plaintext even if the
process is compromised.

Writes are **write-only**: each row's input is empty (placeholder = the current
hint); a blank submit is a no-op, so the form can never round-trip existing
values and a stray Save changes nothing — you must type into one field to
replace that one secret. There is no bulk op and no reveal affordance.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis import secrets as vault
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/secrets", tags=["secrets"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the masked inventory + the write-only editor."""
    store = get_store(request)
    rows = vault.list_secrets(store=store)
    return templates.TemplateResponse(
        request,
        "secrets/index.html.j2",
        {"active_tab": "secrets", "rows": rows},
    )


@router.post("/set")
async def set_secret(
    request: Request,
    name: str = Form(...),
    value: str = Form(""),
) -> Response:
    """Store/replace one secret. Blank value ⇒ no-op (write-only guard)."""
    name = name.strip()
    if name and value:
        vault.set_secret(name, value, store=get_store(request))
    return RedirectResponse("/secrets", status_code=303)


@router.post("/delete")
async def delete_secret(request: Request, name: str = Form(...)) -> Response:
    """Delete one secret."""
    name = name.strip()
    if name:
        vault.delete_secret(name, store=get_store(request))
    return RedirectResponse("/secrets", status_code=303)
