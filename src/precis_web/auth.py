"""HTTP Basic auth for every precis-web route.

Before this, the tab UI — ``/secrets``, ``/env``, ``/console`` and every
mutation route — was served to anything that could reach the port, with
tailnet membership as the only boundary. This module is the lock on that
door. It is deliberately coarse: **every row in ``web_users`` is fully
authorized**. Per-route ACLs, roles and per-user ask-routing are a
separate, deferred design (``docs/backlog/user-identity-and-ask-routing.md``).

Three responses:

- **401** + ``WWW-Authenticate: Basic`` — no/bad credentials. Browsers
  turn this into the native login prompt, and ``curl -u`` works.
- **503** — the ``web_users`` table is empty. Fail *closed* on a fresh
  or wiped DB, with the exact ``precis users add`` line in the body,
  rather than serving the corpus open while someone notices.
- **503** — a row needs a pepper the vault can't produce
  (:class:`precis.users.PepperUnavailable`). An operator problem must
  not masquerade as a wrong password.

**Why a raw ASGI middleware and not a FastAPI dependency.** A dependency
has to be attached to every router (39 of them) and would miss
``/static``, the mounted ``StaticFiles`` app, and anything added later —
the failure mode being a route that silently isn't covered. Wrapping the
whole app means new routes are covered by construction; the exemption
list is short, explicit, and lives here.

**The credential cache exists for a reason.** scrypt costs ~60 ms by
design. A single page pulls a dozen static assets, so deriving per
request would add most of a second to every page load and hand anyone a
trivial CPU-exhaustion lever. Verified ``(login, sha256(password))``
pairs are cached for :data:`_CACHE_TTL` in a bounded dict; the cache
keys on the password digest, so a changed password can only ever *fail*
against a stale entry, never succeed.

**The session cookie exists for Safari.** Shipping Safari won't reliably
replay cached Basic credentials into iframe subnavigations, which
blanked the workbench panes and the PDF reader. Every
Basic-authenticated response therefore sets a signed ``SameSite=Lax``
session cookie (:class:`SessionTokens`) the gate accepts as an
alternative credential; cookies always ride same-origin iframe
requests. The roster row still decides on every cookie request
(:func:`authorize_session_login`), so disable/rm bite immediately, and
``POST /account/logout`` clears the cookie alongside its Basic
realm-rotation challenge.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from precis.users import (
    PepperUnavailable,
    WebUser,
    burn_verify,
    normalize_login,
    resolve_pepper,
    verify_password,
)

log = logging.getLogger(__name__)

#: Paths served without credentials. ``/healthz`` only: the launchd /
#: systemd probe and the ansible post-deploy check hit it, and a probe
#: that needs a password is a probe that reports the service down when
#: the password rotates.
_OPEN_PATHS = frozenset({"/healthz"})

#: Path prefixes that authenticate themselves. ``/podcast`` accepts a
#: per-user ``?t=`` feed token because phone podcast clients handle Basic
#: auth on enclosure URLs inconsistently — the route (not this
#: middleware) does that check, and falls back to Basic when no token is
#: presented. See :mod:`precis_web.routes.podcast`.
_SELF_AUTH_PREFIXES = ("/podcast",)


def _is_self_auth(path: str) -> bool:
    """Is *path* inside a self-authenticating subtree?

    Matches by **path segment**, not string prefix. A bare
    ``str.startswith("/podcast")`` also exempts ``/podcastfoo`` — and
    would silently exempt any future ``/podcasts`` or
    ``/podcast-admin``: a route born unauthenticated, reachable from the
    public internet, with nothing in review to catch it. The exemption
    has to name a subtree, so only the prefix itself and things under
    its ``/`` qualify.
    """
    return any(path == p or path.startswith(f"{p}/") for p in _SELF_AUTH_PREFIXES)


_CACHE_TTL = 300.0
_CACHE_MAX = 512

#: Session cookie riding alongside Basic auth. Shipping Safari refuses to
#: replay cached Basic credentials into iframe subnavigations (the
#: workbench panes and the PDF reader load blank while the top-level page
#: works fine); cookies have no such carve-out — a same-origin iframe
#: request always carries them. So the gate issues a signed, HttpOnly,
#: ``SameSite=Lax`` cookie on every Basic-authenticated response and
#: accepts it as an alternative credential. ``Lax`` means it never rides
#: a cross-site POST, preserving the CSRF posture
#: :func:`check_same_origin` documents. The signing secret is
#: process-local and unpersisted: after a web restart the cookie goes
#: stale, the next top-level navigation re-sends Basic (browsers always
#: do) and mints a fresh one — no config, no schema, nothing to rotate.
SESSION_COOKIE = "precis_session"
_SESSION_TTL = 12 * 3600.0


class SessionTokens:
    """Mint and verify the session cookie's signed tokens.

    Format ``expires.hexsig.login`` — expiry and login are authenticated
    by the HMAC, and :func:`verify` recomputes over the *parsed* fields
    so a token only verifies if it round-trips exactly.
    """

    def __init__(self, *, ttl: float = _SESSION_TTL) -> None:
        self._secret = secrets.token_bytes(32)
        self._ttl = ttl

    def _pack(self, expires: int, login: str) -> str:
        sig = hmac.new(
            self._secret, f"{expires}.{login}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{expires}.{sig}.{login}"

    def issue(self, login: str) -> str:
        return self._pack(int(time.time() + self._ttl), login)

    def verify(self, token: str | None) -> str | None:
        """The token's login when valid and unexpired, else ``None``."""
        if not token:
            return None
        expires_s, _, rest = token.partition(".")
        _sig, _, login = rest.partition(".")
        if not (expires_s.isdigit() and login):
            return None
        expires = int(expires_s)
        if expires <= time.time():
            return None
        if not hmac.compare_digest(self._pack(expires, login), token):
            return None
        return login


def _session_cookie_value(headers: dict[bytes, bytes]) -> str | None:
    raw = headers.get(b"cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw.decode("latin-1", "replace"))
    except CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


#: Methods that don't change state, and so don't need the cross-site
#: check. HEAD/OPTIONS ride along with GET.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Realm string in the challenge. Shows up in the browser's prompt.
REALM = "precis"


class CredentialCache:
    """Bounded TTL cache of successful verifications.

    **It caches the scrypt, not the authorization decision.** Every
    request still reads the row and still decides on what the row says
    right now; a cache hit only means "this exact password already
    derived to this exact stored hash, so don't spend 60 ms proving it
    again". That distinction is what makes ``precis users passwd`` /
    ``disable`` / ``rm`` take effect on the *next* request rather than
    whenever the TTL happens to lapse — those run in a different process
    and can't reach into this dict, so an entry that outlived its row
    must be unable to authorize anything. See :func:`authenticate`.

    Keyed on ``(login, sha256(password))`` — never the plaintext, so a
    heap dump of a long-running web process doesn't hand over passwords.
    """

    def __init__(self, *, ttl: float = _CACHE_TTL, maxsize: int = _CACHE_MAX) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        # value: (expires_at, stored password_hash this was verified against)
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}

    @staticmethod
    def _key(login: str, password: str) -> tuple[str, str]:
        return login, hashlib.sha256(password.encode("utf-8")).hexdigest()

    def matches(self, login: str, password: str, password_hash: str) -> bool:
        """True when this pair was already verified against *this* hash.

        A rotated password writes a new ``password_hash``, so every entry
        from before the rotation stops matching — inert until it expires,
        rather than a live credential.
        """
        key = self._key(login, password)
        entry = self._entries.get(key)
        if entry is None:
            return False
        expires, verified_against = entry
        if expires <= time.monotonic():
            self._entries.pop(key, None)
            return False
        return hmac.compare_digest(verified_against, password_hash)

    def remember(self, login: str, password: str, password_hash: str) -> None:
        if len(self._entries) >= self._maxsize:
            # Cheap eviction: drop everything expired, then (if still
            # full) the whole cache. A web process has a handful of
            # users; reaching the cap at all means something abnormal,
            # and re-deriving is correct-but-slow, never wrong.
            now = time.monotonic()
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            if len(self._entries) >= self._maxsize:
                self._entries.clear()
        self._entries[self._key(login, password)] = (
            time.monotonic() + self._ttl,
            password_hash,
        )

    def clear(self) -> None:
        self._entries.clear()


class AuthError(Exception):
    """Internal signal carrying the status + body the gate should emit."""

    def __init__(self, status: int, detail: str, *, challenge: bool = False) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.challenge = challenge


def parse_basic_header(value: str | None) -> tuple[str, str] | None:
    """``Authorization: Basic <b64>`` → ``(login, password)``.

    ``None`` for anything malformed — a garbled header is treated as no
    header (challenge), not as an error, because that is what a browser
    does with a stale credential.
    """
    if not value:
        return None
    scheme, _, payload = value.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    login, sep, password = decoded.partition(":")
    if not sep:
        return None
    return normalize_login(login), password


def check_same_origin(scope: Scope, headers: dict[bytes, bytes]) -> None:
    """Refuse a state-changing request that came from another origin.

    HTTP Basic makes every mutating route in this app a CSRF target, and
    the gate is what creates the exposure: the browser attaches the
    cached ``Authorization`` header to *any* request to this origin,
    including a form on an attacker's page auto-submitting to
    ``/console`` or ``/secrets``. The session cookie is ``SameSite=Lax``
    so it never accompanies a cross-site POST, but the Basic header has
    no such attribute — so the check stays the stateless one: compare
    the browser's stated origin against the one it addressed.

    A request with neither ``Origin`` nor ``Referer`` is allowed
    through. That is not a hole: browsers always send ``Origin`` on a
    cross-origin POST (that is the header's entire purpose), so the
    no-header case is ``curl``/scripts/the MCP client, which have no
    ambient credential to be tricked into replaying.

    ``Origin: null`` (an *opaque* origin) is refused, deliberately: a
    sandboxed ``<iframe>`` on an attacker's page POSTs with exactly that
    header, so letting it through reopens the hole. The flip side is
    that our own pages must never carry ``Referrer-Policy:
    no-referrer`` — the Fetch spec serializes ``Origin`` as ``null``
    under that policy even same-origin, which locks every browser form
    out (see ``security_headers.py``).
    """
    if scope.get("method", "GET").upper() in _SAFE_METHODS:
        return
    stated = headers.get(b"origin") or headers.get(b"referer")
    if stated is None:
        return
    parts = urlsplit(stated.decode("latin-1", "replace"))
    presented = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
    host = headers.get(b"host")
    expected = (
        f"{scope.get('scheme') or 'http'}://{host.decode('latin-1', 'replace')}"
        if host
        else ""
    )
    if not expected or presented != expected:
        log.warning(
            "precis-web: refused cross-site %s %s from origin %r",
            scope.get("method"),
            scope.get("path"),
            presented,
        )
        raise AuthError(
            403,
            "Cross-site request refused. This page was submitted from "
            f"{presented or 'an unstated origin'}, not from precis itself.",
        )


def require_roster(store: Any) -> None:
    """Raise the "nobody can log in" 503.

    Checked *before* the missing-credentials challenge, not inside
    :func:`authenticate`: on a fresh deploy the operator opens a browser
    and has no credentials to offer, so a 401 would hand them a login
    prompt they cannot satisfy and no clue why. They get the fix instead.

    "Nobody can log in" counts *enabled* accounts, so disabling the last
    one lands here too rather than on a 401 — from the operator's side
    that is the same situation with the same fix, and reading it as a
    mistyped password would send them hunting in the wrong place. With
    any other account still enabled, a disabled user gets the ordinary
    401 from :func:`authenticate`.
    """
    if store.count_web_users() == 0:
        raise AuthError(
            503,
            "No precis-web users are configured. On the host running "
            "precis-web, create one:\n\n"
            "    precis users add <login> --abbrev <ab>\n",
        )


def authenticate(
    store: Any, login: str, password: str, *, cache: CredentialCache | None = None
) -> WebUser:
    """Verify one credential pair against ``web_users``.

    Raises :class:`AuthError` on every rejection path, so callers get one
    exception type carrying the status they should emit.

    The roster check and the row read happen on **every** request,
    including cache hits — two indexed reads against a table with a
    handful of rows, next to the 60 ms the cache is actually there to
    skip. That ordering is the whole reason ``precis users disable`` (run
    over SSH, in another process, unable to touch this dict) locks
    someone out on their next request instead of up to a TTL later.
    """
    require_roster(store)

    found = store.get_web_user_credentials(login)
    if found is None:
        # Unknown login: spend the same scrypt a real one would, so the
        # roster isn't enumerable with a stopwatch.
        burn_verify()
        raise AuthError(401, "invalid credentials", challenge=True)
    user, record = found
    if not user.enabled:
        burn_verify()
        raise AuthError(401, "account disabled", challenge=True)

    if cache is not None and cache.matches(login, password, record.password_hash):
        return user

    pepper = resolve_pepper(store=store)
    try:
        ok = verify_password(password, record, pepper=pepper)
    except PepperUnavailable as exc:
        log.error("precis-web auth: %s", exc)
        raise AuthError(
            503,
            "Password pepper unavailable — the vault could not produce "
            "PRECIS_WEB_PASSWORD_PEPPER. This is a server problem, not a "
            "wrong password.",
        ) from exc
    if not ok:
        raise AuthError(401, "invalid credentials", challenge=True)

    if cache is not None:
        cache.remember(login, password, record.password_hash)
    store.touch_web_user_login(user.login)
    return user


def authorize_session_login(store: Any, login: str) -> WebUser:
    """Authorize a login vouched for by a valid session token.

    The HMAC already proved *we* issued the token, so there is no
    password to verify — but the roster row is still read and still
    decides, exactly like :func:`authenticate`'s cache-hit path: a
    ``precis users disable`` / ``rm`` locks the cookie holder out on
    their next request, not at token expiry.

    Deliberately does NOT ``touch_web_user_login``: cookie traffic is the
    high-frequency kind (every iframe pane load), and a per-request
    UPDATE is exactly the write amplification the credential cache keeps
    off the Basic path. The cookie only exists because a Basic-
    authenticated request minted it — and browsers re-send Basic on
    every top-level navigation — so ``last_login_at`` stays fresh
    through :func:`authenticate` without a second writer.
    """
    require_roster(store)
    found = store.get_web_user_credentials(login)
    if found is None:
        raise AuthError(401, "invalid credentials", challenge=True)
    user, _record = found
    if not user.enabled:
        raise AuthError(401, "account disabled", challenge=True)
    return user


class BasicAuthMiddleware:
    """Pure-ASGI gate wrapping the entire app (routes + static mounts)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.cache = CredentialCache()
        self.sessions = SessionTokens()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _OPEN_PATHS or _is_self_auth(path):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization")
        creds = parse_basic_header(raw.decode("latin-1") if raw else None)
        store = self._store(scope)
        fresh_cookie: str | None = None

        try:
            import anyio

            # Before authenticating, not after: a cross-site POST is
            # refused on its shape alone, and pays no scrypt for it.
            check_same_origin(scope, headers)
            if store is None:
                raise AuthError(503, "precis-web has no database connection")
            session_login = self.sessions.verify(_session_cookie_value(headers))
            if creds is not None:
                # Basic wins when both are presented: it re-verifies the
                # password, and (re)mints the cookie when the one sent is
                # absent, stale, or for someone else. That means a BAD
                # Basic credential denies even beside a still-valid
                # cookie — deliberately: after a password rotation the
                # browser keeps replaying the old Basic header, and
                # falling back to the cookie would mask that until the
                # cookie expired hours later, turning a crisp re-prompt
                # into a mystery 401 at 3am. An explicitly presented
                # credential is always the one judged.
                login, password = creds
                user = await anyio.to_thread.run_sync(
                    lambda: authenticate(store, login, password, cache=self.cache)
                )
                if scope["type"] == "http" and session_login != user.login:
                    fresh_cookie = self._cookie_header(scope, user.login)
            elif session_login is not None:
                user = await anyio.to_thread.run_sync(
                    lambda: authorize_session_login(store, session_login)
                )
            else:
                # Roster first, so a fresh deploy explains itself instead
                # of prompting for credentials that don't exist yet.
                await anyio.to_thread.run_sync(lambda: require_roster(store))
                raise AuthError(401, "authentication required", challenge=True)
        except AuthError as exc:
            await _send_error(send, exc, websocket=scope["type"] == "websocket")
            return

        scope.setdefault("state", {})
        scope["state"]["web_user"] = user
        if fresh_cookie is None:
            await self.app(scope, receive, send)
            return

        cookie = fresh_cookie

        async def send_with_cookie(message: Message) -> None:
            # Never on a 401: that's the sign-out path (see
            # routes/account.py::logout), and re-minting a session on the
            # very response that revokes one would undo the sign-out.
            if (
                message["type"] == "http.response.start"
                and message.get("status") != 401
            ):
                MutableHeaders(raw=message.setdefault("headers", [])).append(
                    "set-cookie", cookie
                )
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _cookie_header(self, scope: Scope, login: str) -> str:
        attrs = (
            f"{SESSION_COOKIE}={self.sessions.issue(login)}; Path=/; "
            f"Max-Age={int(_SESSION_TTL)}; HttpOnly; SameSite=Lax"
        )
        if scope.get("scheme") == "https":
            attrs += "; Secure"
        return attrs

    @staticmethod
    def _store(scope: Scope) -> Any:
        """The live ``Store``, or ``None`` when the app booted stateless.

        Read off ``app.state`` per request rather than captured at
        construction: ``create_app``'s lifespan builds the runtime
        *after* the middleware is installed.
        """
        app = scope.get("app")
        runtime = getattr(getattr(app, "state", None), "runtime", None)
        return getattr(runtime, "store", None)


def current_user(request: Any) -> WebUser | None:
    """The authenticated caller, or ``None`` when auth is off.

    ``None`` is not "anonymous, treat as guest" — it means the gate never
    ran (``PRECIS_WEB_AUTH`` off), so *nobody knows who this is*. A route
    that acts on the caller's own account must refuse rather than guess.
    """
    state = request.scope.get("state") or {}
    user = state.get("web_user")
    return user if isinstance(user, WebUser) else None


async def _send_error(send: Send, exc: AuthError, *, websocket: bool = False) -> None:
    if websocket:
        # An HTTP response frame on a websocket scope is a protocol
        # error — the server would raise or hang, depending on which one
        # it is. Nothing here speaks websocket yet; this is so the first
        # route that does inherits a correct denial rather than a trap.
        await send({"type": "websocket.close", "code": 1008})
        return
    body = exc.detail.encode("utf-8")
    headers = MutableHeaders()
    headers["content-type"] = "text/plain; charset=utf-8"
    headers["content-length"] = str(len(body))
    if exc.challenge:
        headers["www-authenticate"] = f'Basic realm="{REALM}", charset="UTF-8"'
    start: Message = {
        "type": "http.response.start",
        "status": exc.status,
        "headers": headers.raw,
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "REALM",
    "SESSION_COOKIE",
    "AuthError",
    "BasicAuthMiddleware",
    "CredentialCache",
    "SessionTokens",
    "authenticate",
    "authorize_session_login",
    "check_same_origin",
    "current_user",
    "parse_basic_header",
    "require_roster",
]
