"""``/account`` — the signed-in user's own page.

The self-service half of :mod:`precis.cli.users`: what you can do to your
*own* row without SSHing to the box. Roster management (creating people,
disabling them, deleting) deliberately stays CLI-only — every account is
fully authorized, so a web affordance for "add a user" would let anyone
who reaches one credential mint more of them.

Three things live here today:

* **Change password** — the only self-service credential path there is.
* **Profile** — full name, email, and ORCID iD. The first two are
  display-only; the iD is not. A nanopub signature is attributed to an
  ORCID, never to a login (:func:`precis.nanopub.keys.load_profile`
  builds the signing profile around one, and the artifact stores it as
  ``signer``), so this field is where the human at the keyboard is
  connected to the identity the claims they attest will carry.
* **Sign out** — see below; Basic auth makes this stranger than it
  should be.
* **Podcast feed link** — the subscribe URL, shown whole so it can be
  copied into a podcast app, plus mint/revoke. The row stores only the
  token's digest, so the readable copy comes from the vault
  (:func:`precis.users.recall_feed_token`); when the vault can't produce
  one, the page falls back to "mint to see a link".

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

**Signing out is a 401, not a cookie delete.** There is no session to
end: the credential lives in the browser, which re-sends it on every
request until it decides to forget. The one lever a server has is to
answer with a fresh challenge, which is what :func:`logout` does — a
401 carrying ``WWW-Authenticate`` under a realm the cached credential
was never accepted for. Every mainstream browser responds by dropping
what it had and prompting again; cancelling that prompt is the actual
sign-out, and the page says so, because a "signed out" banner the
browser then silently un-does would be a lie. Quitting the browser
remains the only guarantee, and that is a property of HTTP Basic, not
of this route.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.users import (
    MIN_PASSWORD_LENGTH,
    PepperUnavailable,
    WebUser,
    forget_feed_token,
    hash_password,
    mint_feed_token,
    normalize_orcid,
    recall_feed_token,
    remember_feed_token,
    resolve_pepper,
    validate_password,
    verify_password,
)
from precis_web.auth import REALM, SESSION_COOKIE, current_user
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
    feed_url: str | None = None,
) -> HTMLResponse:
    """Render the page, looking the feed URL up unless one was passed.

    ``feed_url=None`` means "work it out" — only the mint path passes one
    explicitly, because that token isn't readable back until the vault
    write it just did. Defaulting to ``""`` instead would mean every
    error re-render (wrong current password, mismatched confirm, bad
    email) silently dropped the URL, and the template reads a missing URL
    on a user who *has* a token as "the vault can't read your link,
    generate a new one" — advice that would kill a working subscription
    because someone mistyped a password.
    """
    if feed_url is None:
        feed_url = _feed_url(request, user) if user else ""
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
        # The body carries a live credential on every render now, not
        # just after minting. Same rule the other sensitive dynamic
        # routes use — keep it out of the disk and back-forward caches.
        headers={"Cache-Control": "no-store"},
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
        notice = "Saved."
    if user is None:
        return _render(request, user=None, notice=notice)
    return _render(request, user=_fresh(request, user), notice=notice)


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
    orcid: str = Form(""),
) -> Response:
    """Update the profile fields. Empty clears.

    The iD is validated (shape *and* ISO 7064 checksum) before anything
    is written, and stored canonically dashed — paste
    ``https://orcid.org/0000-…`` and the URL wrapper is stripped, because
    the prefix is rendering and storing it would make "same iD?" a
    string-shape question. A mistyped digit is refused here rather than
    surfacing later as a nanopub attributed to a stranger.
    """
    user = _require_self(request)
    address = email.strip()
    if address and "@" not in address:
        return _render(
            request, user=user, error=f"{address!r} doesn't look like an address."
        )
    try:
        researcher_id = normalize_orcid(orcid)
        # The store raises for a *taken* iD as well as a malformed one —
        # the unique index is the only thing that can decide the first,
        # and both are the same kind of correctable typo from here.
        get_store(request).update_web_user(
            user.login,
            full_name=full_name.strip(),
            email=address,
            orcid=researcher_id,
        )
    except ValueError as exc:
        return _render(request, user=user, error=str(exc))
    return RedirectResponse("/account?saved=1", status_code=303)


@router.post("/logout", response_class=HTMLResponse)
async def logout(request: Request) -> Response:
    """Answer with a challenge the browser's cached credential can't satisfy.

    POST, not a link: a GET would be followed by every prefetcher and
    link-checker that sees the page, and being logged out by a browser's
    speculative fetch is an unpleasant way to learn that.

    The realm carries a random suffix. Browsers key a cached Basic
    credential on origin *and* realm, so an unfamiliar realm is what makes
    them prompt instead of silently re-sending — a fixed "signed out"
    realm would itself be cached after the first use, and the second
    sign-out of the session would do nothing.

    The 401 body is the page you are meant to read after cancelling the
    prompt. ``no-store`` because this is the one page whose whole purpose
    is that the *next* request re-authenticates.
    """
    user = current_user(request)
    if user is not None:
        log.info("account: sign-out challenge issued for %s", user.login)
    return templates.TemplateResponse(
        request,
        "account/logout.html.j2",
        {"active_tab": "account", "user": user},
        status_code=401,
        headers={
            # ASCII only, and no em dash: a header value is latin-1 by
            # spec, and starlette raises rather than mangling one.
            "WWW-Authenticate": (
                f'Basic realm="{REALM} signed-out {secrets.token_hex(4)}", '
                "charset=UTF-8"
            ),
            "Cache-Control": "no-store",
            # The session cookie signs in without Basic, so it must die
            # with the sign-out — the gate never mints on a 401, so this
            # deletion is not raced by a re-issue on the same response.
            "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0",
        },
    )


@router.post("/feed-token", response_class=HTMLResponse)
async def feed_token(request: Request, action: str = Form("rotate")) -> Response:
    """Mint or revoke the podcast ``?t=`` credential.

    The minted URL is rendered inline rather than passed through a
    redirect: a token in a redirect target lands in browser history, the
    address bar, and any referrer that follows. Revoking clears both the
    row digest (which is what authenticates) and the vault copy (which is
    only what makes the link readable) — leaving either behind is a
    credential nobody knows is still there.
    """
    user = _require_self(request)
    store = get_store(request)
    if action == "revoke":
        store.set_web_user_feed_token(user.login, None)
        if not forget_feed_token(user.login, store=store):
            # The link is dead either way — the digest is what
            # authenticates — but the plaintext outliving it is the exact
            # leftover this feature is supposed not to create.
            return _render(
                request,
                user=_fresh(request, user),
                error=(
                    "Link revoked, but the stored copy of the token could not "
                    "be deleted — the vault didn't answer. It no longer works; "
                    "check the precis-web log."
                ),
            )
        return RedirectResponse("/account?saved=1", status_code=303)

    token, digest = mint_feed_token()
    store.set_web_user_feed_token(user.login, digest)
    vaulted = remember_feed_token(user.login, token, store=store)
    notice = "New feed link — any previous one stopped working."
    if not vaulted:
        notice += (
            " Copy it now: this deployment has no vault, so it can't be shown again."
        )
    return _render(
        request,
        # Re-read: the row now has a digest, which is what the template
        # keys "revoke" and "generate *new*" off.
        user=_fresh(request, user),
        feed_url=f"{_base_url(request)}/podcast/feed.xml?t={token}",
        notice=notice,
    )


def _feed_url(request: Request, user: WebUser) -> str:
    """The subscribe URL, or "" when there is no readable token.

    Guarded on ``feed_token_sha256``: the row is what decides whether a
    link is live, and a vault entry can outlive it (``precis users
    feed-token --clear`` on an older build wrote only the row). Showing a
    URL that 401s would be worse than showing none.
    """
    if not user.has_feed_token:
        return ""
    token = recall_feed_token(user.login, store=get_store(request))
    if not token:
        return ""
    return f"{_base_url(request)}/podcast/feed.xml?t={token}"


def _base_url(request: Request) -> str:
    """Origin for the feed URL — same rule the feed itself uses."""
    cfg: Any = get_web_config(request)
    return cfg.podcast_base_url or str(request.base_url).rstrip("/")
