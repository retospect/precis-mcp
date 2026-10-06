"""Tabulated reaction ledger and explicit keep through the rxn handler.

The rxn handler owns computations, ledger tables and source/licence notes;
the shared TOON renderer presents them unchanged, without recalculation or
text-to-number parsing. Explicit keep validates inputs and stores them with
an archival ledger in an rxn ref. The `/refs/rxn/<id>` reader recomputes
through that same handler instead of using the snapshot.

Equation sets use rxn refs so catalyst pathway runs and future station
records remain separate. This view supplies tabulated energetics; it creates
no barriers, electrode references or measured property rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis_web.deps import await_dispatch, get_store, templates

router = APIRouter(prefix="/rxn", tags=["rxn"])


def _page(
    request: Request, *, q: str, T: str, n_electrons: str, body: str, is_error: bool
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "rxn/index.html.j2",
        {
            "active_tab": "rxn",
            "q": q,
            "T": T,
            "n_electrons": n_electrons,
            "body": body,
            "is_error": is_error,
        },
        status_code=400 if is_error else 200,
    )


@router.get("", response_class=HTMLResponse)
async def index(
    request: Request, q: str = "", T: str = "298.15", n_electrons: str = ""
) -> HTMLResponse:
    body, is_error = "", False
    if q.strip():
        body, is_error = await await_dispatch(
            request,
            "get",
            {
                "kind": "rxn",
                "view": "energetics",
                "q": q,
                "T": T,
                "n_electrons": n_electrons.strip() or None,
            },
        )
    return _page(
        request, q=q, T=T, n_electrons=n_electrons, body=body, is_error=is_error
    )


@router.post("/keep", response_model=None)
async def keep(
    request: Request,
    q: str = Form(""),
    T: str = Form("298.15"),
    n_electrons: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    # Hash the submitted inputs without parsing: validation and conversion
    # belong to the handler, including malformed/nonfinite temperatures.
    inputs = {"q": q.strip(), "T": T, "n_electrons": n_electrons.strip() or None}
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    slug = f"ledger-{digest}"
    body, is_error = await await_dispatch(
        request,
        "put",
        {
            "kind": "rxn",
            "id": slug,
            "title": f"Reaction ledger at {T} K",
            "meta": {"energetics": inputs},
        },
    )
    if is_error:
        return _page(
            request, q=q, T=T, n_electrons=n_electrons, body=body, is_error=True
        )
    ref = await asyncio.to_thread(get_store(request).get_ref, kind="rxn", id=slug)
    if ref is None:
        raise RuntimeError("rxn keep succeeded without a stored ref")
    return RedirectResponse(url=f"/refs/rxn/{ref.id}", status_code=303)
