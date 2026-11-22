"""HTTP Basic auth for every precis-web route.

Before this, the tab UI — ``/secrets``, ``/env``, ``/console`` and every
mutation route — was served to anything that could reach the port, with
tailnet membership as the only boundary. This module is the lock on that
door. It is deliberately coarse: **every row in ``web_users`` is fully
authorized**. Per-route ACLs, roles and per-user ask-routing are a
separate, deferred design (`me690624`).

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
(:func:`authorize_session_login`), so disable/rm bite immediately. The
token also authenticates a tag of the row's ``password_hash``, so a
password change (``/account/password``, or ``precis users passwd`` from
another process) kills every cookie minted under the old one on its next
request, and ``POST /account/logout`` sets a per-login not-before mark
on the gate's :class:`SessionTokens` as well as deleting the cookie —
"signed out" is a server-side fact, not only a client-side one.

**``/podcast`` is exempt from the gate only when it carries a ``?t=``
token.** The route checks the token itself (podcast apps handle Basic on
enclosure URLs inconsistently). A ``/podcast`` request *without* a token
takes the ordinary gate path — Basic or session cookie, lockout, failure
log, credential cache, cookie mint — and the route reads the signed-in
login from ``scope["state"]``. Before this the route ran its own Basic
fallback, which touched none of that: unlimited password guessing at one
scrypt per request. ``/files`` stays fully self-authenticating (its token
is the only credential). The middleware and the route both read ``t`` via
Starlette's ``QueryParams`` so they cannot disagree on "has a token".

**Lockout is keyed on login + address, with a trusted bypass and a
cookie fallback.** A lock on the login alone lets anyone who knows a login
(they are not secret) lock its owner out from anywhere. So failed Basic
attempts count against three buckets (:class:`FailureTracker`):
``pair:<login>|<address>`` (10), ``addr:<address>`` (50) and
``login:<login>`` (100, a global ceiling against a distributed run). A
locked ``pair`` or ``addr`` refuses the Basic attempt with 429 before any
scrypt. The ``login`` ceiling refuses only callers that are not *trusted*
for that login: the (login, address) pair signed in with Basic recently
(:class:`KnownAddresses`, 30 days). A cookie does not make a caller
trusted — its HMAC only proves we issued it, not that the password it was
minted under is still current.

**Burst-proof.** The lock is checked on arrival, but a failure is only
known after the scrypt, so N concurrent wrong guesses would all pass an
on-arrival check. :func:`authenticate_record` therefore takes an
:class:`AttemptGuard` and consults it *inside* each scrypt slot, recording
the failure before the slot is released: at most ``limit + slots - 1``
guesses of one bucket are verified however many arrive at once, the rest
get 429 (or 503 when the gate is saturated). The tracker is thread-safe
for that reason, and evicts fewest-hits-first so a flood of junk logins
cannot flush a victim's count.

**Cookie fallback.** A real browser sends its cached Basic header and its
session cookie together. When the lockout refuses a Basic attempt and the
request also carries a cookie for the *same* login that still authorizes
(roster row, enabled, password tag current), the request is served as that
cookie session: no password is verified, so nobody gains a guess from it,
and nothing is recorded, cleared, remembered or re-minted. A Basic attempt
that is not locked is judged as usual ("Basic wins": a wrong password
beside a valid cookie is a 401). A cookie-only request is never refused by
the Basic gate's buckets — the one carve-out is ``POST /account/password``,
below — and its 401s are logged but not counted: counting them would let
a stale cookie, replayed, lock its own owner out. The address bucket
is the resolved remote address, with IPv6 folded to its /64 (one
subscriber, 2**64 addresses) and IPv4-mapped IPv6 to the IPv4.

**``POST /account/password`` feeds the same buckets.** The change form
re-verifies the current password — a check the presented session, cookie
or already-accepted Basic, did not perform for this request — so a stolen
cookie or a hijacked browser session would otherwise be an uncounted
guessing oracle at one scrypt per POST (gr735107). The gate parks a
:class:`PasswordCheckGuard` on the request and the route runs the check
under it: misses count against the same ``pair``/``addr``/``login`` keys,
a locked bucket answers 429, and a proven password clears the pair and
marks the address known — the same bookkeeping a Basic success performs,
for the same reasons. This is the one place a cookie-only request's
failures are counted and can be refused.

**Residual.** A signed-in owner is not affected by someone else filling
their bucket. A fresh browser with no (valid) cookie in a locked bucket
waits out the window. Under a sustained distributed run above the global
ceiling, sign-in from an address not seen since the last web restart and
without a valid cookie is refused until the window slides: both the
known-address set and the cookie secret are process-local, so a restart
forgets who is trusted and which cookies are valid.

**Scrypt concurrency is bounded.** Every non-cached verification takes one
of :data:`_SCRYPT_SLOTS` slots for the derivation; a caller that cannot
get one in :data:`_SCRYPT_WAIT` seconds is answered 503 + ``Retry-After``.
That is load shedding, not a failed credential: it is not logged as one
and not counted against any bucket.

**One 401 body.** Unknown login, wrong password, disabled account and a
rejected cookie all answer ``invalid credentials``; the specific reason
(``AuthError.reason``) goes to the server log only, so the response does
not tell a prober which logins exist or which are disabled.

**OAuth authorization starts behind the browser gate.** Discovery, token exchange
and revocation self-authenticate, but ``/authorize`` requires the same login
as consent before it can reserve bounded pending-request slots. Otherwise an
anonymous caller could exhaust one app's slots and block its users.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders, QueryParams
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from precis import icons as precis_icons
from precis import profiles
from precis.users import (
    PasswordRecord,
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
#:
#: ``/.well-known/security.txt`` (gr627864) joins it deliberately: the
#: RFC 9116 file exists for external security researchers, who by
#: definition have no credentials here. It names only the public
#: reporting contact — nothing the gate protects — and it must answer
#: unauthenticated on the public funnel or scanners keep flagging it
#: missing (a 401 looks like "not served").
#: ``/favicon.ico`` + ``/favicon.svg`` (gr633600) join for the same
#: reason: a connector's icon fetch (and a browser's tab request on the
#: OAuth consent flow) happens before any credential exists, and without
#: the mark clients fall back to the infrastructure host's icon.
_OPEN_PATHS = frozenset(
    {"/healthz", "/.well-known/security.txt", *precis_icons.PUBLIC_PATHS}
)

#: Path prefixes that may authenticate themselves. ``/files`` accepts a
#: signed HMAC ``?t=`` token minted by the MCP ``pdf_link`` view — with
#: no Basic fallback, since the token *is* the credential. See
#: :mod:`precis_web.routes.files`. ``/podcast`` accepts a per-user ``?t=``
#: feed token because phone podcast clients handle Basic auth on
#: enclosure URLs inconsistently, but only *when it carries one*
#: (:data:`_TOKEN_GATED_PREFIXES`): without a token the request goes
#: through this gate like any other. See :mod:`precis_web.routes.podcast`.
_SELF_AUTH_PREFIXES = ("/podcast", "/files")

#: The subset of :data:`_SELF_AUTH_PREFIXES` exempt only with a non-empty
#: ``t`` query parameter.
_TOKEN_GATED_PREFIXES = ("/podcast",)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _is_self_auth(path: str) -> bool:
    """Is *path* inside a subtree that may authenticate itself?

    Matches by **path segment**, not string prefix. A bare
    ``str.startswith("/podcast")`` also exempts ``/podcastfoo`` — and
    would silently exempt any future ``/podcasts`` or
    ``/podcast-admin``: a route born unauthenticated, reachable from the
    public internet, with nothing in review to catch it. The exemption
    has to name a subtree, so only the prefix itself and things under
    its ``/`` qualify.
    """
    return any(_under(path, p) for p in _SELF_AUTH_PREFIXES)


def _bypasses_gate(scope: Scope, path: str) -> bool:
    """Does this request skip the gate because its route authenticates it?

    A self-auth subtree always does, except the token-gated ones
    (``/podcast``), which do only when they carry a non-empty ``t``.
    Read with :class:`QueryParams` — the same parser the route's
    ``request.query_params`` uses — so the two can't disagree about
    whether a token is present.
    """
    if not _is_self_auth(path):
        return False
    if any(_under(path, p) for p in _TOKEN_GATED_PREFIXES):
        return bool(QueryParams(scope.get("query_string", b"")).get("t"))
    return True


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


def password_tag(password_hash: str) -> str:
    """A short, non-reversible tag of the stored password hash.

    Carried (and HMAC-authenticated) inside the session token, and compared
    with the row's current tag on every cookie request: a password change
    writes a new hash, so cookies minted under the old one stop verifying.
    16 hex chars of sha256 is plenty — it only has to differ after a
    change, and it leaks nothing about the hash that the cookie holder
    could use (the hash itself never leaves the server).
    """
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


class SessionClaim(NamedTuple):
    """What a verified session token vouches for."""

    login: str
    password_tag: str


class SessionTokens:
    """Mint and verify the session cookie's signed tokens.

    Format ``expires.issued.pwtag.hexsig.login`` — expiry, issue stamp,
    password tag and login are authenticated by the HMAC, and
    :func:`parse` recomputes over the *parsed* fields so a token only
    verifies if it round-trips exactly. The login comes last because
    logins may contain dots.

    ``issued`` is a strictly increasing nanosecond stamp (never equal for
    two calls, even within one clock tick), so :meth:`sign_out`'s
    per-login not-before mark cleanly separates "before" from "after"
    even when the user signs out and straight back in within a second.
    """

    def __init__(self, *, ttl: float = _SESSION_TTL) -> None:
        self._secret = secrets.token_bytes(32)
        self._ttl = ttl
        self._lock = threading.Lock()
        self._last_stamp = 0
        self._not_before: dict[str, int] = {}

    def _stamp(self) -> int:
        with self._lock:
            self._last_stamp = max(time.time_ns(), self._last_stamp + 1)
            return self._last_stamp

    def _pack(self, expires: int, issued: int, tag: str, login: str) -> str:
        sig = hmac.new(
            self._secret, f"{expires}.{issued}.{tag}.{login}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{expires}.{issued}.{tag}.{sig}.{login}"

    def issue(self, login: str, password_tag: str = "") -> str:
        return self._pack(
            int(time.time() + self._ttl), self._stamp(), password_tag, login
        )

    def sign_out(self, login: str) -> None:
        """Void every token issued for *login* so far (this process only)."""
        self._not_before[login] = self._stamp()

    def parse(self, token: str | None) -> SessionClaim | None:
        """The token's claim when valid, unexpired and not signed out."""
        if not token:
            return None
        parts = token.split(".", 4)
        if len(parts) != 5:
            return None
        expires_s, issued_s, tag, _sig, login = parts
        if not (
            expires_s.isascii()
            and expires_s.isdigit()
            and issued_s.isascii()
            and issued_s.isdigit()
            and login
        ):
            return None
        expires, issued = int(expires_s), int(issued_s)
        if expires <= time.time():
            return None
        expected = self._pack(expires, issued, tag, login)
        if not hmac.compare_digest(expected.encode(), token.encode()):
            return None
        if issued <= self._not_before.get(login, 0):
            return None
        return SessionClaim(login, tag)

    def verify(self, token: str | None) -> str | None:
        """The token's login when valid and unexpired, else ``None``."""
        claim = self.parse(token)
        return claim.login if claim is not None else None


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


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _remote_addr(scope: Scope, headers: dict[bytes, bytes]) -> str:
    """The client address for the auth log and the per-address lockout.

    In production precis-web sits behind ``tailscale serve``/``funnel``
    on the same host, so the ASGI ``client`` tuple is the loopback proxy
    for *every* caller — keyed on that, the address bucket would be one
    global lockout. The proxy appends the real client to
    ``X-Forwarded-For``, so when the peer IS loopback the LAST entry of
    that header (the hop the trusted local proxy added — earlier entries
    are whatever the client sent) is the address. A non-loopback peer
    (local dev, a direct bind) is taken as-is and the header ignored: an
    attacker-supplied ``X-Forwarded-For`` must never pick the bucket.
    ``"-"`` when nothing is known — the caller then skips the address
    bucket rather than lumping every unknown peer together.
    """
    client = scope.get("client")
    host = str(client[0]) if client else ""
    if host in _LOOPBACK:
        raw = headers.get(b"x-forwarded-for")
        if raw:
            forwarded = raw.decode("latin-1", "replace").split(",")[-1].strip()
            if forwarded:
                return forwarded
    return host or "-"


def _addr_bucket(remote: str) -> str | None:
    """The lockout bucket for a resolved remote address, ``None`` for none.

    IPv4 as is. IPv6 folds to its /64 network — one subscriber is
    routinely handed a whole /64, so per-address keys would hand a
    guesser 2**64 fresh buckets — and an IPv4-mapped IPv6 address to the
    IPv4 it wraps. A string that isn't an IP at all keeps its own bucket
    (the test client's peer name, say). ``"-"`` (unresolvable) is no
    bucket: the caller skips the address keys rather than lumping every
    unknown peer together. Log lines keep the raw *remote*.
    """
    if remote == "-":
        return None
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return remote
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        # Build from the integer: a scoped address (``fe80::1%eth0``)
        # stringifies with its scope, which ``ip_network`` rejects.
        return str(ipaddress.IPv6Network(((int(ip) >> 64) << 64, 64)))
    return str(ip)


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


#: Sliding-window lockout for failed credential presentations (gr343746,
#: reshaped by gr730562). Three buckets count the 401s of one Basic
#: attempt: ``pair:<login>|<address>`` (``_LOCKOUT_FAILURES``) is what
#: stops guessing one account from one place; ``addr:<address>``
#: (``_LOCKOUT_ADDR_FAILURES``, deliberately several times higher: one
#: person mistyping must never lock out everyone who shares their
#: address) is the wide net for a run that rotates logins; and
#: ``login:<login>`` (``_LOCKOUT_LOGIN_FAILURES``) is a global ceiling
#: against a run spread over many addresses — high enough that it only
#: ever trips under a real distributed attack, and even then it refuses
#: only callers not trusted for that login (see the module docstring).
#: All 429 before any scrypt is spent. Process-local, like the credential
#: cache: a web restart forgets it, which is fine (the goal is to stop a
#: stuffing run from getting a free scrypt-bounded rate, not to persist
#: a ban).
_LOCKOUT_FAILURES = 10
_LOCKOUT_ADDR_FAILURES = 50
_LOCKOUT_LOGIN_FAILURES = 100
_LOCKOUT_WINDOW = 15 * 60.0
#: Distinct keys the tracker holds before evicting the stalest — every
#: failed attempt inserts an attacker-chosen login string, so growth must
#: be bounded (same reason ``CredentialCache`` has ``_CACHE_MAX``).
_LOCKOUT_MAX_KEYS = 4096
#: One "still locked" warning per key per this many seconds — a locked
#: key that keeps hammering shouldn't fill the log at the attacker's rate.
_LOCKED_LOG_EVERY = 60.0
#: How long a (login, address) that signed in with Basic stays trusted
#: past the global login ceiling, and how many such pairs are remembered.
_KNOWN_TTL = 30 * 86400.0
_KNOWN_MAX = 4096


class FailureTracker:
    """Per-pair, per-address and per-login sliding windows of failures.

    ``record`` after every 401 on a presented Basic credential;
    ``locked_for`` before verifying one, so a locked key is refused ahead
    of the scrypt. A success ``clear``s the pair's window (not the
    address's — a shared NAT address may carry both a legitimate user and
    the run targeting them — nor the login ceiling's). The limit is
    picked by key prefix: ``addr:``, ``login:``, else the pair limit.

    Thread-safe: the gate checks on the event loop but verifies, and
    records the failure, on worker threads (see :class:`_AttemptGuard`),
    so every public method holds one lock.
    """

    def __init__(
        self,
        *,
        failures: int = _LOCKOUT_FAILURES,
        addr_failures: int = _LOCKOUT_ADDR_FAILURES,
        login_failures: int = _LOCKOUT_LOGIN_FAILURES,
        window: float = _LOCKOUT_WINDOW,
        maxsize: int = _LOCKOUT_MAX_KEYS,
    ) -> None:
        self._failures = failures
        self._addr_failures = addr_failures
        self._login_failures = login_failures
        self._window = window
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}
        self._last_locked_log: dict[str, float] = {}

    def _limit(self, key: str) -> int:
        if key.startswith("addr:"):
            return self._addr_failures
        if key.startswith("login:"):
            return self._login_failures
        return self._failures

    def _live(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, ()) if t > now - self._window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def record(self, *keys: str) -> None:
        with self._lock:
            now = time.monotonic()
            for key in keys:
                hits = self._live(key, now)
                hits.append(now)
                self._hits[key] = hits
            if len(self._hits) > self._maxsize:
                self._evict(now)

    def _evict(self, now: float) -> None:
        """Drop expired windows, then the least-hit keys, below ``maxsize``.

        Bounded memory against a run that presents a fresh bogus login per
        request. Eviction order is (live hit count, last hit) ascending:
        fewest hits first, stalest first among equals — so a flood of
        single-hit keys evicts itself rather than a victim's key that is
        near or at its limit (evicting by staleness alone would let ~2,000
        junk logins reset the count on the account being attacked).
        Roster-unknown logins are deliberately not special-cased: that
        would turn the lockout into an enumeration oracle. Evicts down to
        7/8 of ``maxsize`` so a sustained flood pays the sort once per
        ``maxsize / 8`` inserts, not on every one.
        """
        for key in list(self._hits):
            self._live(key, now)
        if len(self._hits) <= self._maxsize:
            return
        keep = self._maxsize - self._maxsize // 8
        victims = sorted(
            self._hits, key=lambda k: (len(self._hits[k]), self._hits[k][-1])
        )
        for key in victims[: len(self._hits) - keep]:
            self._hits.pop(key, None)
            self._last_locked_log.pop(key, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
            self._last_locked_log.pop(key, None)

    def locked_for(self, *keys: str) -> float:
        """Seconds until the earliest-unlocking key among *keys* frees up,
        or ``0.0`` when none is locked."""
        with self._lock:
            now = time.monotonic()
            retry = 0.0
            for key in keys:
                hits = self._live(key, now)
                limit = self._limit(key)
                if len(hits) >= limit:
                    # The window slides: the lock lifts when the oldest hit
                    # that still counts ages out.
                    retry = max(retry, hits[-limit] + self._window - now)
            return retry

    def should_log_locked(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            last = self._last_locked_log.get(key)
            if last is not None and now - last < _LOCKED_LOG_EVERY:
                return False
            self._last_locked_log[key] = now
            return True


class KnownAddresses:
    """Bounded TTL set of ``(login, address bucket)`` pairs that signed in.

    What makes a caller *trusted* past the global login ceiling (and the
    only thing that does): the same login from the same bucket proved the
    password recently. Insertion
    order is staleness order (a re-add moves the entry to the end), so
    eviction drops from the front. Process-local — see the residual in
    the module docstring.
    """

    def __init__(self, *, ttl: float = _KNOWN_TTL, maxsize: int = _KNOWN_MAX) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._seen: dict[tuple[str, str], float] = {}

    def add(self, login: str, bucket: str | None) -> None:
        if bucket is None:
            return
        key = (login, bucket)
        self._seen.pop(key, None)
        self._seen[key] = time.monotonic() + self._ttl
        while len(self._seen) > self._maxsize:
            self._seen.pop(next(iter(self._seen)))

    def __contains__(self, item: tuple[str, str | None]) -> bool:
        login, bucket = item
        if bucket is None:
            return False
        expires = self._seen.get((login, bucket))
        if expires is None:
            return False
        if expires <= time.monotonic():
            self._seen.pop((login, bucket), None)
            return False
        return True


#: The one client-facing 401 body (see the module docstring).
_INVALID = "invalid credentials"


class AuthError(Exception):
    """Internal signal carrying the status + body the gate should emit."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        challenge: bool = False,
        retry_after: float | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.challenge = challenge
        self.retry_after = retry_after
        #: Server-log-only specifics ("account disabled"); ``detail`` is
        #: the client-facing body and must not tell rejections apart.
        self.reason = reason


def _lockout_refusal(
    failures: FailureTracker, keys: tuple[str, ...], *, login: str, remote: str
) -> AuthError | None:
    """The 429 for an attempt whose *keys* are locked now, else ``None``.

    The one place that decides "locked", logs it (once a minute per key —
    a locked key that keeps hammering mustn't fill the log at the
    attacker's rate) and builds the response; shared by the event-loop
    pre-check and the in-slot :class:`_AttemptGuard`.
    """
    retry = failures.locked_for(*keys)
    if retry <= 0:
        return None
    for key in keys:
        if failures.locked_for(key) > 0 and failures.should_log_locked(key):
            log.warning(
                "precis-web auth: locked out login=%r from %s "
                "(too many failures in %.0f s) — retry in %.0f s",
                login,
                remote,
                _LOCKOUT_WINDOW,
                retry,
            )
            break
    return AuthError(
        429,
        "too many failed sign-in attempts — try again later",
        retry_after=retry,
    )


class AttemptGuard(Protocol):
    """What :func:`authenticate_record` needs to make the lockout burst-proof.

    The pre-check runs on arrival, but a failure is only known once the
    scrypt returns — so N concurrent wrong guesses would all pass an
    on-arrival check and all be verified. The guard is consulted *inside*
    the scrypt slot instead, and records the failure before the slot is
    released, so at most ``limit + _SCRYPT_SLOTS - 1`` guesses of one
    bucket are ever verified, however many arrive at once.
    """

    def check(self) -> None:
        """Raise the 429 when any of this attempt's keys is locked now."""

    def failed(self) -> None:
        """Record this attempt's keys: its credential was rejected."""


class _AttemptKeys:
    """The :class:`AttemptGuard` over one Basic attempt's tracker keys."""

    def __init__(
        self,
        failures: FailureTracker,
        *,
        check_keys: tuple[str, ...],
        record_keys: tuple[str, ...],
        login: str,
        remote: str,
    ) -> None:
        self._failures = failures
        self._check_keys = check_keys
        self._record_keys = record_keys
        self._login = login
        self._remote = remote

    def refusal(self) -> AuthError | None:
        return _lockout_refusal(
            self._failures, self._check_keys, login=self._login, remote=self._remote
        )

    def check(self) -> None:
        err = self.refusal()
        if err is not None:
            raise err

    def failed(self) -> None:
        self._failures.record(*self._record_keys)


class PasswordCheckGuard:
    """The sign-in lockout applied to ``POST /account/password``.

    The change form re-verifies the current password — a check the
    presented session never performed — so without this a stolen cookie
    or cached Basic header is an uncounted guessing oracle at one scrypt
    per POST. The gate parks one of these on
    ``scope["state"]["password_guard"]`` and the route drives it:

    - ``refusal()`` before the row read — a locked bucket is the 429,
      ahead of any scrypt;
    - ``failed()`` after a rejected guess — counted against the same
      ``pair``/``addr``/``login`` keys a failed Basic attempt is;
    - ``succeeded()`` after a proven password — the ``basic_verified``
      bookkeeping (clear the pair, mark the address known).

    Trust is the gate's rule: the global ``login:`` ceiling does not
    refuse a (login, address) that proved the password recently, so a
    distributed run holding ``login:`` at its cap cannot lock the real
    owner out of rotating the credential under attack. A cookie alone is
    not trust — same as sign-in.

    The route's refusal → verify → record sequence contains no ``await``,
    so on the single event loop it is atomic: a burst of concurrent
    wrong guesses cannot all slip past ``refusal()`` before the first
    miss is counted — the invariant the gate's in-slot ``check()``
    provides for Basic attempts. If the handler ever grows an await or
    moves to the threadpool, this needs the slot treatment too.
    """

    def __init__(
        self,
        failures: FailureTracker,
        known: KnownAddresses,
        *,
        login: str,
        remote: str,
        bucket: str | None,
    ) -> None:
        self._failures = failures
        self._known = known
        self._login = login
        self._bucket = bucket
        pair: tuple[str, ...] = ()
        if bucket is not None:
            pair = (f"pair:{login}|{bucket}", f"addr:{bucket}")
        check = pair if (login, bucket) in known else (*pair, f"login:{login}")
        self._keys = _AttemptKeys(
            failures,
            check_keys=check,
            record_keys=(*pair, f"login:{login}"),
            login=login,
            remote=remote,
        )

    def refusal(self) -> AuthError | None:
        """The 429 when the check's buckets are locked now, else ``None``."""
        return self._keys.refusal()

    def failed(self) -> None:
        """Record a rejected current-password guess."""
        self._keys.failed()

    def succeeded(self) -> None:
        """The password proved current: the Basic-success bookkeeping.

        Clear the pair window (the owner's own typos must not lock them)
        and mark the (login, address) trusted — it just proved the
        password, which is all :class:`KnownAddresses` ever records. The
        ``addr`` and ``login`` windows keep their history, as at sign-in.
        """
        if self._bucket is not None:
            self._failures.clear(f"pair:{self._login}|{self._bucket}")
        self._known.add(self._login, self._bucket)


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


#: Concurrent scrypt derivations allowed, and how long a caller waits for
#: a slot. scrypt is ~60 ms of CPU *and* ~16 MiB; unbounded, a burst of
#: wrong passwords is a memory/CPU lever on the whole web process. A
#: caller that can't get a slot is shed with 503, not failed.
_SCRYPT_SLOTS = 4
_SCRYPT_WAIT = 2.0
_SCRYPT_GATE = threading.BoundedSemaphore(_SCRYPT_SLOTS)


class _Busy(AuthError):
    """No scrypt slot came free in time: the attempt was shed, not judged."""


@contextmanager
def _scrypt_slot() -> Iterator[None]:
    # Bind the gate once: release must hit the semaphore that was
    # acquired, never whatever the module global names by then (a swapped
    # gate would otherwise free a slot another thread still holds).
    gate = _SCRYPT_GATE
    if not gate.acquire(timeout=_SCRYPT_WAIT):
        raise _Busy(
            503,
            "precis-web is busy verifying passwords — try again shortly.",
            retry_after=max(1.0, _SCRYPT_WAIT),
        )
    try:
        yield
    finally:
        gate.release()


def authenticate_record(
    store: Any,
    login: str,
    password: str,
    *,
    cache: CredentialCache | None = None,
    guard: AttemptGuard | None = None,
) -> tuple[WebUser, PasswordRecord]:
    """Verify one credential pair against ``web_users``; also hand back
    the row's password record (the gate tags the session cookie with it).

    Raises :class:`AuthError` on every rejection path, so callers get one
    exception type carrying the status they should emit. The three 401
    paths share one client-facing body; ``reason`` carries the specific
    cause for the server log.

    *guard* (see :class:`AttemptGuard`) is consulted inside each scrypt
    slot before the derivation and told of a rejected credential before
    the slot is released. A cache hit does no scrypt and skips it.

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
        with _scrypt_slot():
            if guard is not None:
                guard.check()
            burn_verify()
            if guard is not None:
                guard.failed()
        raise AuthError(401, _INVALID, challenge=True, reason="invalid credentials")
    user, record = found
    if not user.enabled:
        with _scrypt_slot():
            if guard is not None:
                guard.check()
            burn_verify()
            if guard is not None:
                guard.failed()
        raise AuthError(401, _INVALID, challenge=True, reason="account disabled")

    if cache is not None and cache.matches(login, password, record.password_hash):
        return user, record

    pepper = resolve_pepper(store=store)
    try:
        with _scrypt_slot():
            if guard is not None:
                guard.check()
            ok = verify_password(password, record, pepper=pepper)
            if not ok and guard is not None:
                guard.failed()
    except PepperUnavailable as exc:
        log.error("precis-web auth: %s", exc)
        raise AuthError(
            503,
            "Password pepper unavailable — the vault could not produce "
            "PRECIS_WEB_PASSWORD_PEPPER. This is a server problem, not a "
            "wrong password.",
        ) from exc
    if not ok:
        raise AuthError(401, _INVALID, challenge=True, reason="invalid credentials")

    if cache is not None:
        cache.remember(login, password, record.password_hash)
    store.touch_web_user_login(user.login)
    return user, record


def authenticate(
    store: Any, login: str, password: str, *, cache: CredentialCache | None = None
) -> WebUser:
    """:func:`authenticate_record` for callers that only want the user."""
    return authenticate_record(store, login, password, cache=cache)[0]


def authorize_session_login(store: Any, login: str, tag: str) -> WebUser:
    """Authorize a login vouched for by a valid session token.

    The HMAC already proved *we* issued the token, so there is no
    password to verify — but the roster row is still read and still
    decides, exactly like :func:`authenticate`'s cache-hit path: a
    ``precis users disable`` / ``rm`` locks the cookie holder out on
    their next request, not at token expiry. *tag* is the token's
    :func:`password_tag`; it must match the row's current one, so a
    password change (from this process or another) retires the cookie.

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
        raise AuthError(401, _INVALID, challenge=True, reason="invalid credentials")
    user, record = found
    if not user.enabled:
        raise AuthError(401, _INVALID, challenge=True, reason="account disabled")
    if not hmac.compare_digest(
        tag.encode(), password_tag(record.password_hash).encode()
    ):
        raise AuthError(
            401, _INVALID, challenge=True, reason="session predates password change"
        )
    return user


class BasicAuthMiddleware:
    """Pure-ASGI gate wrapping the entire app (routes + static mounts)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.cache = CredentialCache()
        #: Fallback only: ``create_app`` parks the shared instance on
        #: ``app.state.session_tokens`` so ``/account/logout`` can reach
        #: it; see :meth:`_sessions`.
        self.sessions = SessionTokens()
        self.failures = FailureTracker()
        self.known = KnownAddresses()

    def _sessions(self, scope: Scope) -> SessionTokens:
        state = getattr(scope.get("app"), "state", None)
        shared = getattr(state, "session_tokens", None)
        return shared if isinstance(shared, SessionTokens) else self.sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # OAuth machine endpoints validate their own credentials and protocol.
        # Authorization is a browser navigation: authenticate before reserving
        # pending-request slots, just as consent and account pages require.
        oauth_paths = getattr(
            getattr(scope.get("app"), "state", None), "oauth_protocol_paths", ()
        )
        if (
            path in _OPEN_PATHS
            or _bypasses_gate(scope, path)
            or (path in oauth_paths and path != "/authorize")
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization")
        creds = parse_basic_header(raw.decode("latin-1") if raw else None)
        store = self._store(scope)
        sessions = self._sessions(scope)
        fresh_cookie: str | None = None
        remote = _remote_addr(scope, headers)
        # An unresolvable peer is not one bucket — it is no bucket.
        bucket = _addr_bucket(remote)
        claim = sessions.parse(_session_cookie_value(headers))
        # The login this request is vouching for — what a 401 is logged
        # and counted against. None until a credential is parsed.
        presented: str | None = None
        pair_keys: tuple[str, ...] = ()
        #: The lockout's refusal of this Basic attempt, if any, and whether
        #: the password was actually verified (vs. served on the cookie).
        refusal: AuthError | None = None
        basic_verified = False
        guard: _AttemptKeys | None = None

        try:
            import anyio

            # Before authenticating, not after: a cross-site POST is
            # refused on its shape alone, and pays no scrypt for it.
            check_same_origin(scope, headers)
            if store is None:
                raise AuthError(503, "precis-web has no database connection")
            presented = creds[0] if creds is not None else (claim and claim.login)
            if presented is not None:
                # The lockout key is the CANONICAL login (the roster's
                # lower+strip form), so case variants of one login share
                # one bucket and a success clears the bucket it was
                # counted under. %r in the log lines below escapes control
                # characters, so a crafted login can't forge a second log
                # record; the cap keeps one request from writing a
                # kilobyte per line.
                presented = normalize_login(presented)[:128]
            if creds is not None and presented is not None:
                if bucket is not None:
                    pair_keys = (f"pair:{presented}|{bucket}", f"addr:{bucket}")
                # Trusted for this login: it signed in from this address
                # before. Trust lifts only the global login ceiling. (A
                # cookie does not: its HMAC says nothing about whether the
                # password it was minted under is still the password.)
                trusted = (presented, bucket) in self.known
                guard = _AttemptKeys(
                    self.failures,
                    check_keys=(
                        pair_keys if trusted else (*pair_keys, f"login:{presented}")
                    ),
                    record_keys=(*pair_keys, f"login:{presented}"),
                    login=presented,
                    remote=remote,
                )
                # Locked keys are refused before the row read and the
                # scrypt — the whole point of the counter (gr343746).
                # This is the cheap pre-admission check; the guard repeats
                # it inside the scrypt slot, which is what bounds a burst.
                refusal = guard.refusal()
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
                if refusal is None:
                    try:
                        user, record = await anyio.to_thread.run_sync(
                            lambda: authenticate_record(
                                store,
                                login,
                                password,
                                cache=self.cache,
                                guard=guard,
                            )
                        )
                        basic_verified = True
                    except AuthError as exc:
                        if exc.status != 429 and not isinstance(exc, _Busy):
                            raise
                        refusal = exc
                if refusal is not None:
                    # Refused by lockout, or shed because every scrypt slot
                    # was busy (a flood of guesses must not 503 the people
                    # already signed in). A browser sends its cached Basic
                    # header and its session cookie together, so a signed-in
                    # owner whose bucket someone else filled (shared NAT, a
                    # VPN exit, one /64) would otherwise be locked out of
                    # their own session. A cookie for the SAME login that
                    # still authorizes (roster row + password tag, so a
                    # stale or disabled one does not) serves the request as
                    # that session: no password is verified, so nobody gains
                    # a guess from it, and nothing is recorded, cleared,
                    # remembered or re-minted. No such cookie: the 429 (or
                    # the 503).
                    if claim is None or normalize_login(claim.login) != presented:
                        raise refusal
                    session = claim
                    try:
                        user = await anyio.to_thread.run_sync(
                            lambda: authorize_session_login(
                                store, session.login, session.password_tag
                            )
                        )
                    except AuthError:
                        raise refusal from None
                elif scope["type"] == "http":
                    tag = password_tag(record.password_hash)
                    # A cookie minted under an older password still
                    # round-trips its HMAC; only the tag says it's stale.
                    if (
                        claim is None
                        or claim.login != user.login
                        or not hmac.compare_digest(
                            claim.password_tag.encode(), tag.encode()
                        )
                    ):
                        fresh_cookie = self._cookie_header(scope, user.login, tag)
            elif claim is not None:
                session = claim
                user = await anyio.to_thread.run_sync(
                    lambda: authorize_session_login(
                        store, session.login, session.password_tag
                    )
                )
            else:
                # Roster first, so a fresh deploy explains itself instead
                # of prompting for credentials that don't exist yet.
                await anyio.to_thread.run_sync(lambda: require_roster(store))
                raise AuthError(401, "authentication required", challenge=True)
        except AuthError as exc:
            if exc.status == 401 and presented is not None:
                # A rejected *presented* credential (unknown login, disabled
                # account, wrong password, stale cookie) — the one event a
                # stuffing run leaves behind. The bare challenge (no
                # credential at all) is every first page load and is not
                # logged. Bounded per key by the lockout: at most the
                # bucket's limit in lines per window, then one per minute.
                # The log keeps the specific reason; the client got one
                # generic body.
                log.warning(
                    "precis-web auth: 401 %s for login=%r from %s (%s)",
                    exc.reason or exc.detail,
                    presented,
                    remote,
                    "basic" if creds is not None else "cookie",
                )
                # Not counted here: a Basic guess is recorded by its guard
                # inside the scrypt slot (so a burst can't outrun the
                # count), and a cookie carries no password to guess —
                # counting its 401s (disabled, removed, password changed)
                # would let a stale cookie replayed lock its own owner out.
            await _send_error(send, exc, websocket=scope["type"] == "websocket")
            return
        if basic_verified:
            if bucket is not None:
                self.failures.clear(f"pair:{presented}|{bucket}")
            self.known.add(user.login, bucket)

        scope.setdefault("state", {})
        scope["state"]["web_user"] = user
        # /account/password re-verifies a password the session did not
        # have to present; run that check under the same lockout.
        scope["state"]["password_guard"] = PasswordCheckGuard(
            self.failures,
            self.known,
            login=user.login,
            remote=remote,
            bucket=bucket,
        )
        # The signed-in login is the profile viewer for every store read
        # this request makes (gr490266): web reads carry no revision
        # context, so without it listing filters see an unidentified caller.
        if fresh_cookie is None:
            with profiles.viewing_as(user.login):
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

        with profiles.viewing_as(user.login):
            await self.app(scope, receive, send_with_cookie)

    def _cookie_header(self, scope: Scope, login: str, tag: str) -> str:
        attrs = (
            f"{SESSION_COOKIE}={self._sessions(scope).issue(login, tag)}; Path=/; "
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


def password_check_guard(request: Any) -> PasswordCheckGuard | None:
    """The :class:`PasswordCheckGuard` the gate parked, or ``None``.

    ``None`` means the gate never ran (``PRECIS_WEB_AUTH`` off) — the
    same contract as :func:`current_user`. ``POST /account/password``
    is the only route that consults it.
    """
    state = request.scope.get("state") or {}
    guard = state.get("password_guard")
    return guard if isinstance(guard, PasswordCheckGuard) else None


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
    if exc.retry_after is not None:
        headers["retry-after"] = str(max(1, int(exc.retry_after + 0.999)))
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
    "FailureTracker",
    "KnownAddresses",
    "PasswordCheckGuard",
    "SessionClaim",
    "SessionTokens",
    "authenticate",
    "authenticate_record",
    "authorize_session_login",
    "check_same_origin",
    "current_user",
    "parse_basic_header",
    "password_check_guard",
    "password_tag",
    "require_roster",
]
