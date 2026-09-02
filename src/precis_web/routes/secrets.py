"""``/secrets`` — the secrets-vault editor.

Reads **`vault.list()` only** (name + masked hint + updated_at) for the
inventory: the RENDER path never decrypts, never holds ciphertext, and cannot
reveal a plaintext even if the process is compromised. The "Verify now"
probe pass (:mod:`precis_web.secret_status`) is the one path that decrypts —
server-side only, to authenticate against each provider's own API — and it
never renders or logs the plaintext it resolves; only a short status classification
(``ok``/``bad``/``unknown`` + a code/exception-derived detail string) crosses
back into this module.

Writes are **write-only**: each row's input is empty (placeholder = the current
hint); a blank submit is a no-op, so the form can never round-trip existing
values and a stray Save changes nothing — you must type into one field to
replace that one secret. There is no bulk op and no reveal affordance.

Rows are the union of the vault inventory and :data:`secret_status.KNOWN_SECRETS`
(the registry of secrets this codebase is known to consume) — known secrets
first in registry order, then any vault-only extras alphabetically.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis import secrets as vault
from precis_web import secret_status
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/secrets", tags=["secrets"])


def _short_label(detail: str) -> str:
    """A scannable one/two-word label distilled from a CheckResult's detail
    string, for the text under the status dot (the tooltip keeps the full
    ``detail``). Falls back to the full detail when nothing shorter matches."""
    low = detail.lower()
    if "timeout" in low:
        return "timeout"
    if "rate limited" in low:
        return "rate limited"
    if "partner secret missing" in low:
        return "partner missing"
    if "rejected" in low:
        return "key rejected"
    if "not an email" in low:
        return "invalid"
    return detail


def _dot_for(
    present: bool,
    known: bool,
    result: secret_status.CheckResult | None,
) -> tuple[str, str, str]:
    """(color, label, title) for a row's status dot.

    rose = not present anywhere. emerald = present and either verified,
    presence-only (no probe result to contradict it), or an unknown vault
    extra we have no registry opinion on. amber = present but the probe
    came back bad or inconclusive — ``title`` always carries the full
    ``CheckResult.detail`` (never a secret value).
    """
    if not present:
        return "rose", "missing", "not configured"
    if result is None:
        label = "present (unverified)" if known else "present"
        return "emerald", label, "present — no automated check run"
    if result.state == "ok":
        return "emerald", "verified", result.detail
    return "amber", _short_label(result.detail), result.detail


def _build_rows(
    vault_rows: list[dict[str, object]],
    results: dict[str, secret_status.CheckResult],
    *,
    store: Any,
) -> list[dict[str, object]]:
    """Merge the vault inventory with the known-secrets registry into the
    template's row shape — known secrets first (registry order), then any
    vault-only extras alphabetically."""
    vault_by_name = {r["name"]: r for r in vault_rows}
    rows: list[dict[str, object]] = []
    for spec in secret_status.KNOWN_SECRETS:
        v = vault_by_name.get(spec.name)
        present = vault.is_available(spec.name, store=store)
        result = results.get(spec.name)
        color, label, title = _dot_for(present, True, result)
        rows.append(
            {
                "name": spec.name,
                "known": True,
                "spec": spec,
                "in_vault": v is not None,
                "hint": v["hint"] if v else None,
                "updated_at": v["updated_at"] if v else None,
                "present": present,
                "env_or_file": present and v is None,
                "result": result,
                "dot_color": color,
                "dot_label": label,
                "dot_title": title,
            }
        )

    known_names = {spec.name for spec in secret_status.KNOWN_SECRETS}
    extras = sorted(
        (r for r in vault_rows if r["name"] not in known_names),
        key=lambda r: str(r["name"]),
    )
    for v in extras:
        color, label, title = _dot_for(True, False, None)
        rows.append(
            {
                "name": v["name"],
                "known": False,
                "spec": None,
                "in_vault": True,
                "hint": v["hint"],
                "updated_at": v["updated_at"],
                "present": True,
                "env_or_file": False,
                "result": None,
                "dot_color": color,
                "dot_label": label,
                "dot_title": title,
            }
        )
    return rows


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the masked inventory + registry rows + the write-only editor."""
    store = get_store(request)
    vault_rows = vault.list_secrets(store=store)
    results = await secret_status.get_results(store)
    rows = _build_rows(vault_rows, results, store=store)
    return templates.TemplateResponse(
        request,
        "secrets/index.html.j2",
        {
            "active_tab": "secrets",
            "rows": rows,
            "checked_at": secret_status.checked_at(),
        },
    )


@router.post("/check")
async def check_now(request: Request) -> Response:
    """Force a fresh probe pass across every known secret, then redirect back."""
    store = get_store(request)
    await secret_status.get_results(store, force=True)
    return RedirectResponse("/secrets", status_code=303)


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
