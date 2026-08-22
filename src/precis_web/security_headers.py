"""Response hardening headers for every precis-web response.

Pure-ASGI, installed as the **outermost** layer by
:func:`precis_web.app.create_app`, so the headers ride on 401 challenges
and static assets too — not just on handler responses.

**Why this exists.** Until 2026-08-22 precis-web was reachable only over
the tailnet, where framing and MIME-sniffing attacks need an attacker
already inside. Behind ``tailscale funnel`` the whole UI — ``/console``,
``/secrets``, ``/env`` — answers the public internet, and the browser
attaches its cached HTTP Basic credentials to *any* request for this
origin, including one made from inside an attacker's ``<iframe>``.

**Framing is the gap the CSRF check cannot close.** ``check_same_origin``
rejects a non-GET whose ``Origin`` doesn't match, which stops a form on
evil.example POSTing here. It does *not* stop evil.example framing this
origin and tricking a click: the request that click produces comes from
the framed page, so its ``Origin`` is ours and the check passes. The only
defence is refusing to be framed, which is what ``X-Frame-Options`` and
CSP ``frame-ancestors`` do.

**The CSP is deliberately frame-ancestors ONLY.** A real
``default-src``/``script-src`` policy is worth having, but the templates
carry inline scripts and styles, so a broad policy would silently break
pages. Narrow now and correct beats broad now and reverted.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Sent on every response. Values are bytes: ASGI headers are raw pairs.
_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    # Refuse framing outright. X-Frame-Options is the legacy spelling and
    # frame-ancestors the modern one; send both, since they are cheap and
    # their support sets are not identical.
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    # Don't let a browser second-guess a Content-Type into something
    # executable.
    (b"x-content-type-options", b"nosniff"),
    # The podcast feed token travels as a ``?t=`` query parameter, so a
    # referrer on any outbound navigation is a credential leak.
    (b"referrer-policy", b"no-referrer"),
    # Funnel is HTTPS-only and there is no HTTP listener to downgrade to,
    # so this is belt-and-braces. Scoped to this host: sibling tailnet
    # names (caspar.<tailnet>.ts.net) are NOT subdomains of this one, so
    # includeSubDomains cannot reach them.
    (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains",
    ),
)


class SecurityHeadersMiddleware:
    """Attach :data:`_HEADERS` to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                # Never clobber a header a handler set deliberately — the
                # only overlap today would be a route with its own CSP.
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value) for name, value in _HEADERS if name not in present
                )
            await send(message)

        await self.app(scope, receive, _send)
