"""``/secrets`` — the secrets-vault editor.

Reads **`vault.list()` only** (name + masked hint + updated_at) for the
inventory: the RENDER path never decrypts, never holds ciphertext, cannot
reveal a plaintext even if the process is compromised, and makes **no
outbound call** — validity is read from the stored snapshot
(:func:`precis.secret_status.stored_status`), written by the background
``secret_status`` checker job (:mod:`precis.workers.secret_check`). "Check
now" mints that job — the page shows the stored "checked <age> ago" until
the worker's pass lands. Presence (env/vault/file) is re-derived live
without decrypting (:func:`precis.secrets.present`), so a just-added row
renders correctly before the next check runs.

Writes are **write-only**: each row's input is empty (placeholder = the current
hint); a blank submit is a no-op, so the form can never round-trip existing
values and a stray Save changes nothing — you must type into one field to
replace that one secret. There is no bulk op and no reveal affordance.

Rows are the union of the vault inventory and :data:`secret_status.KNOWN_SECRETS`
(the registry of secrets this codebase is known to consume) — known secrets
first in registry order, then any vault-only extras alphabetically, excluding
per-user credentials (a vault name containing ``":"`` — the
``NAME:<login>`` convention, e.g. ``REMARKABLE_RMAPI_CONFIG:<login>``): those
are managed on ``/account``, not this fleet-wide page, and are counted for a
short note instead of listed row-by-row.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis import secret_status
from precis import secrets as vault
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
    entry: secret_status.StatusEntry | None,
) -> tuple[str, str, str]:
    """(color, label, title) for a row's status dot.

    rose = not present anywhere (live presence, not the stale record).
    emerald = present and the stored check says ok / presence-only / no
    opinion yet. amber = the stored probe said invalid or inconclusive —
    ``title`` always carries the full ``StatusEntry.detail`` (never a
    secret value). A stored ``missing`` beside live presence reads as
    unverified: the record predates the set.
    """
    if not present:
        return "rose", "missing", "not configured"
    if entry is None or entry.state == "missing":
        label = "present (unverified)" if known else "present"
        return "emerald", label, "present — no automated check run"
    if entry.state == "ok":
        return "emerald", "verified", entry.detail
    if entry.state in ("invalid", "unknown"):
        return "amber", _short_label(entry.detail), entry.detail
    return "emerald", "present", entry.detail


def _build_rows(
    vault_rows: list[dict[str, object]],
    snap: secret_status.StatusSnapshot,
) -> tuple[list[dict[str, object]], int]:
    """Merge the vault inventory with the known-secrets registry into the
    template's row shape — known secrets first (registry order), then any
    vault-only extras alphabetically, excluding per-user credentials (a
    ``NAME:<login>`` vault name). Returns ``(rows, per_user_count)``."""
    vault_by_name = {r["name"]: r for r in vault_rows}
    vault_names = {str(n) for n in vault_by_name}
    rows: list[dict[str, object]] = []
    for spec in secret_status.KNOWN_SECRETS:
        v = vault_by_name.get(spec.name)
        # Live presence, no decrypt: the stored check may predate a set.
        is_present = vault.present(spec.name, vault_names=vault_names)
        entry = snap.entries.get(spec.name)
        color, label, title = _dot_for(is_present, True, entry)
        rows.append(
            {
                "name": spec.name,
                "known": True,
                "spec": spec,
                "in_vault": v is not None,
                "hint": v["hint"] if v else None,
                "updated_at": v["updated_at"] if v else None,
                "present": is_present,
                "env_or_file": is_present and v is None,
                "entry": entry,
                "dot_color": color,
                "dot_label": label,
                "dot_title": title,
            }
        )

    known_names = {spec.name for spec in secret_status.KNOWN_SECRETS}
    per_user_count = sum(
        1 for r in vault_rows if r["name"] not in known_names and ":" in str(r["name"])
    )
    extras = sorted(
        (
            r
            for r in vault_rows
            if r["name"] not in known_names and ":" not in str(r["name"])
        ),
        key=lambda r: str(r["name"]),
    )
    for v in extras:
        entry = snap.entries.get(str(v["name"]))
        color, label, title = _dot_for(True, False, entry)
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
                "entry": entry,
                "dot_color": color,
                "dot_label": label,
                "dot_title": title,
            }
        )
    return rows, per_user_count


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the masked inventory + stored check status + the editor."""
    from precis.workers.secret_check import pending_status_check

    store = get_store(request)
    vault_rows = vault.list_secrets(store=store)
    snap = secret_status.stored_status(store)
    rows, per_user_count = _build_rows(vault_rows, snap)
    return templates.TemplateResponse(
        request,
        "secrets/index.html.j2",
        {
            "active_tab": "secrets",
            "rows": rows,
            "per_user_count": per_user_count,
            "checked_at": snap.checked_at,
            "check_queued": pending_status_check(store),
        },
    )


@router.post("/check")
async def check_now(request: Request) -> Response:
    """Queue a background status check, then redirect back. The page itself
    only ever reads stored status — the minted ``secret_status`` job runs
    the probes on a worker."""
    from precis.workers.secret_check import mint_status_job

    mint_status_job(
        get_store(request),
        origin="human",
        title="secret_status (/secrets check now)",
    )
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
