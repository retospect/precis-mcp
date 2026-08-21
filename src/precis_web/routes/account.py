"""``/account`` — the signed-in user's own page.

The self-service half of :mod:`precis.cli.users`: what you can do to your
*own* row without SSHing to the box. Roster management (creating people,
disabling them, deleting) deliberately stays CLI-only — every account is
fully authorized, so a web affordance for "add a user" would let anyone
who reaches one credential mint more of them.

Three things live here today:

* **Change password** — the only self-service credential path there is.
* **Profile** — full name and email, both display-only fields.
* **Podcast feed link** — mint or revoke the per-user ``?t=`` token.
  Web-only by necessity: the plaintext token exists for exactly one
  moment (:func:`precis.users.mint_feed_token` stores only its digest),
  and reading it off a terminal you SSH'd into is not how you get a URL
  onto a phone.

**Changing your password signs you out, and that is not a bug.** HTTP
Basic has no session to re-issue: the browser holds the old credential
and re-sends it on the next request, which now fails. So the POST
redirects to a GET the browser will be challenged on. It re-prompts, the
new password works, and the page it lands on says what happened. Handing
back a 200 that looks fine and then breaking on the next click would be
the worse outcome.

Nothing here has to tell the gate about the change: its cache stores the
``password_hash`` each entry was verified against and re-reads the row
every request, so writing a new hash retires every entry that referenced
the old one. That has to hold for ``precis users passwd`` too, which
runs in another process entirely — see
:class:`precis_web.auth.CredentialCache`.

**Current password is required to change it.** Browsers cache Basic
credentials for the life of the tab, so "you're already authenticated"
is a weak claim about who is at the keyboard. Re-typing it is the only
thing standing between an unlocked laptop and a stolen account.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.users import (
    MIN_PASSWORD_LENGTH,
    PepperUnavailable,
    WebUser,
    hash_password,
    mint_feed_token,
    resolve_pepper,
    validate_password,
    verify_password,
)
from precis_web.auth import current_user
from precis_web.deps import get_store, get_web_config, templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])


def _require_self(request: Request) -> WebUser:
    """The caller's own account row, or a 503 explaining why there isn't one.

    With ``PRECIS_WEB_AUTH`` off nobody is authenticated, so there is no
    "your account" to act on — every mutation here would have to guess a
    login, and guessing wrong means writing to someone else's row.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "HTTP auth is disabled (PRECIS_WEB_AUTH), so this server has "
                "no signed-in user to act on. Manage accounts with "
                "`precis users` on the host."
            ),
        )
    return user


def _fresh(request: Request, user: WebUser) -> WebUser:
    """Re-read the row so the page reflects a just-written change.

    The ``user`` on the request came from the gate, which caches — after
    a profile edit the cached copy is a render behind.
    """
    store = get_store(request)
    try:
        found = store.get_web_user(user.login)
    except Exception:  # pragma: no cover - degraded DB, still render
        log.debug("account: re-read failed", exc_info=True)
        return user
    return found or user


def _render(
    request: Request,
    *,
    user: WebUser | None,
    error: str = "",
    notice: str = "",
    feed_url: str = "",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "account/index.html.j2",
        {
            "active_tab": "account",
            "user": user,
            "error": error,
            "notice": notice,
            "feed_url": feed_url,
            "auth_on": get_web_config(request).auth_required,
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, changed: str = "", saved: str = "") -> HTMLResponse:
    """The account page.

    ``changed=1`` is set by the password POST's redirect. Reaching this
    render at all means the browser was re-challenged and answered with
    the *new* password, so the banner is a statement of fact, not a hope.
    """
    user = current_user(request)
    notice = ""
    if changed:
        notice = "Password changed — this page loaded with the new one."
    elif saved:
        notice = "Profile saved."
    return _render(
        request,
        user=_fresh(request, user) if user else None,
        notice=notice,
    )


@router.post("/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    """Verify the current password, then replace it.

    Verification goes through :func:`precis.users.verify_password`
    directly rather than :func:`precis_web.auth.authenticate` — the
    latter would re-populate the credential cache with the password we
    are about to retire.
    """
    user = _require_self(request)
    store = get_store(request)

    found = store.get_web_user_credentials(user.login)
    if found is None:  # pragma: no cover - row deleted mid-session
        raise HTTPException(status_code=503, detail="account row vanished")
    _, record = found
    pepper = resolve_pepper(store=store)
    try:
        ok = verify_password(current_password, record, pepper=pepper)
    except PepperUnavailable as exc:
        log.error("account: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        return _render(request, user=user, error="Current password is not correct.")
    if new_password != confirm_password:
        return _render(request, user=user, error="The new passwords don't match.")
    try:
        validate_password(new_password)
    except ValueError as exc:
        return _render(request, user=user, error=str(exc))

    # Hash under whatever pepper this deployment has now: an account
    # created before the pepper existed upgrades to scrypt-pepper-v1 by
    # the simple act of changing its password.
    store.set_web_user_password(user.login, hash_password(new_password, pepper=pepper))
    log.info("account: password changed for %s", user.login)

    # See the module docstring: the browser still holds the old
    # credential, so this redirect is what triggers the re-challenge.
    return RedirectResponse("/account?changed=1", status_code=303)


@router.post("/profile")
async def save_profile(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
) -> Response:
    """Update the two display fields. Empty clears."""
    user = _require_self(request)
    address = email.strip()
    if address and "@" not in address:
        return _render(
            request, user=user, error=f"{address!r} doesn't look like an address."
        )
    get_store(request).update_web_user(
        user.login, full_name=full_name.strip(), email=address
    )
    return RedirectResponse("/account?saved=1", status_code=303)


@router.post("/feed-token", response_class=HTMLResponse)
async def feed_token(request: Request, action: str = Form("rotate")) -> Response:
    """Mint or revoke the podcast ``?t=`` credential.

    The minted URL is rendered inline rather than passed through a
    redirect: a token in a redirect target lands in browser history, the
    address bar, and any referrer that follows.
    """
    user = _require_self(request)
    store = get_store(request)
    if action == "revoke":
        store.set_web_user_feed_token(user.login, None)
        return RedirectResponse("/account?saved=1", status_code=303)

    token, digest = mint_feed_token()
    store.set_web_user_feed_token(user.login, digest)
    return _render(
        request,
        user=user,
        feed_url=f"{_base_url(request)}/podcast/feed.xml?t={token}",
        notice=(
            "New feed link — copy it now, it isn't stored and can't be "
            "shown again. Any previous link stopped working."
        ),
    )


def _base_url(request: Request) -> str:
    """Origin for the feed URL — same rule the feed itself uses."""
    cfg: Any = get_web_config(request)
    return cfg.podcast_base_url or str(request.base_url).rstrip("/")
