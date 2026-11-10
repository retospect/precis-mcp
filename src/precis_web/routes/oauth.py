"""OAuth protocol routes and authenticated per-user connection management.

The pinned MCP SDK owns authorization and token exchange validation. Precis
supplies persistent grants and its existing web login for consent. Public
protocol paths are exact exemptions from Basic auth, installed only when
OAuth is configured; consent and connection management retain that gate.
The token adapter checks the resource parameter the SDK currently parses
without enforcing. The revocation adapter supports registered public clients
without the SDK 1.30 handler's mandatory ``client_secret`` form field.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import anyio
from fastapi import HTTPException
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from pydantic import AnyHttpUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from precis.errors import BadInput, Denied
from precis.oauth.provider import OAuthProvider
from precis_web.deps import get_store, templates
from precis_web.routes.account import _require_token_owner

if TYPE_CHECKING:
    from fastapi import FastAPI

    from precis.oauth.config import OAuthConfig

PROTOCOL_PATHS = frozenset(
    {"/.well-known/oauth-authorization-server", "/authorize", "/token", "/revoke"}
)
_MAX_BODY_SIZE = 64 * 1024
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
_SCOPE_LABELS = {
    "learning": "Read and update your private learning activity and settings",
    "mcp:full": "Full MCP access: use all tools, read and change the shared corpus, and use configured services",
}


def _provider(request: Request) -> OAuthProvider:
    """Resolve the lifespan-owned store, never retain a request's identity."""
    return OAuthProvider(get_store(request), request.app.state.oauth_config)


def _error(error: str, *, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers=_PRIVATE_HEADERS)


def _form(body: bytes) -> dict[str, str]:
    """Bound and disambiguate the OAuth form before SDK model validation."""
    try:
        pairs = parse_qsl(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=20,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            400, "Invalid OAuth form", headers=_PRIVATE_HEADERS
        ) from exc
    values = dict(pairs)
    if len(values) != len(pairs):
        raise HTTPException(400, "Duplicate OAuth field", headers=_PRIVATE_HEADERS)
    return values


class _ProtocolEndpoint:
    """Use the SDK with a runtime store that exists only after lifespan starts."""

    def __init__(self, config: OAuthConfig, path: str) -> None:
        self.config = config
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if len(scope.get("query_string", b"")) > _MAX_BODY_SIZE:
            await _error("invalid_request")(scope, receive, send)
            return
        request = Request(scope, receive)
        provider = _provider(request)
        options = ClientRegistrationOptions(
            enabled=False,
            valid_scopes=list(self.config.scopes),
            default_scopes=list(self.config.scopes),
        )
        if self.path == "/.well-known/oauth-authorization-server":
            metadata = build_metadata(
                AnyHttpUrl(self.config.issuer),
                None,
                options,
                RevocationOptions(enabled=True),
            )
            metadata.token_endpoint_auth_methods_supported = ["none"]
            metadata.revocation_endpoint_auth_methods_supported = ["none"]
            payload = metadata.model_dump(mode="json", exclude_none=True)
            # Issuer identifiers must match resource discovery byte-for-byte;
            # AnyHttpUrl otherwise appends a slash to a bare HTTPS origin.
            payload["issuer"] = self.config.issuer
            response = JSONResponse(payload)
            await response(scope, receive, send)
            return
        body = b""
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";", 1)[
                0
            ].strip().lower() != ("application/x-www-form-urlencoded"):
                await _error("invalid_request")(scope, receive, send)
                return
            body = await request.body()
            values = _form(body)
            if (
                self.path == "/token"
                and "resource" in values
                and values["resource"] != self.config.resource
            ):
                await _error("invalid_target")(scope, receive, send)
                return
            if self.path == "/revoke":
                client_id, token = values.get("client_id", ""), values.get("token", "")
                if not client_id or not token:
                    await _error("invalid_request")(scope, receive, send)
                    return
                if await provider.get_client(client_id) is None:
                    await _error("invalid_client", status=401)(scope, receive, send)
                    return
                await anyio.to_thread.run_sync(
                    provider.revoke_by_token, client_id, token
                )
                await Response(status_code=200, headers=_PRIVATE_HEADERS)(
                    scope, receive, send
                )
                return

        async def replay_body() -> dict[str, Any]:
            nonlocal body
            result, body = body, b""
            return {"type": "http.request", "body": result, "more_body": False}

        for route in create_auth_routes(
            provider,
            AnyHttpUrl(self.config.issuer),
            client_registration_options=options,
        ):
            if route.path == self.path:
                await route.app(
                    scope, replay_body if request.method == "POST" else receive, send
                )
                return
        await _error("invalid_request", status=404)(scope, receive, send)


async def consent(request: Request) -> Response:
    """Approve one app connection as the authenticated account, with a nonce."""
    user = await anyio.to_thread.run_sync(_require_token_owner, request)
    provider = _provider(request)
    try:
        if request.method == "GET":
            page = await anyio.to_thread.run_sync(
                provider.prepare_consent,
                request.query_params.get("request", ""),
                user.id,
            )
            return templates.TemplateResponse(
                request,
                "oauth/consent.html.j2",
                {"consent": page, "user": user, "scope_labels": _SCOPE_LABELS},
                headers=_PRIVATE_HEADERS,
            )
        values = _form(await request.body())
        decision = values.get("decision")
        if decision not in {"allow", "deny"}:
            raise BadInput("Choose Allow or Deny", next="open the consent page again")
        location = await anyio.to_thread.run_sync(
            partial(
                provider.complete_consent,
                values.get("request", ""),
                user.id,
                values.get("csrf_token", ""),
                allow=decision == "allow",
                label=values.get("label", ""),
            )
        )
        return RedirectResponse(location, status_code=303, headers=_PRIVATE_HEADERS)
    except (BadInput, Denied) as exc:
        raise HTTPException(
            status_code=400 if isinstance(exc, BadInput) else 403,
            detail=exc.cause,
            headers=_PRIVATE_HEADERS,
        ) from exc


async def connections(request: Request) -> Response:
    """List connection metadata belonging only to the signed-in account."""
    user = await anyio.to_thread.run_sync(_require_token_owner, request)
    grants = await anyio.to_thread.run_sync(_provider(request).list_grants, user.id)
    return templates.TemplateResponse(
        request,
        "oauth/connections.html.j2",
        {"connections": grants, "user": user, "scope_labels": _SCOPE_LABELS},
        headers=_PRIVATE_HEADERS,
    )


async def revoke_connection(request: Request) -> Response:
    """A posted grant ID can select only an active grant owned by the caller."""
    user = await anyio.to_thread.run_sync(_require_token_owner, request)
    values = _form(await request.body())
    grant_id = values.get("grant_id", "")
    if not grant_id.isascii() or not grant_id.isdecimal() or len(grant_id) > 19:
        raise HTTPException(
            400, "Choose a connection to revoke", headers=_PRIVATE_HEADERS
        )
    numeric_id = int(grant_id)
    if not 1 <= numeric_id <= 2**63 - 1:
        raise HTTPException(
            400, "Choose a connection to revoke", headers=_PRIVATE_HEADERS
        )
    revoked = await anyio.to_thread.run_sync(
        _provider(request).revoke_grant, user.id, numeric_id
    )
    if not revoked:
        raise HTTPException(404, "Connection not found", headers=_PRIVATE_HEADERS)
    return RedirectResponse(
        "/account/connections", status_code=303, headers=_PRIVATE_HEADERS
    )


def install_routes(app: FastAPI, config: OAuthConfig) -> None:
    """Install exact public protocol routes and protected account routes."""
    app.state.oauth_protocol_paths = PROTOCOL_PATHS
    for path in sorted(PROTOCOL_PATHS):
        methods = ["GET", "POST"] if path == "/authorize" else ["POST", "OPTIONS"]
        if path.startswith("/.well-known/"):
            methods = ["GET", "OPTIONS"]
        endpoint: Any = RequestBodyLimitMiddleware(
            _ProtocolEndpoint(config, path), _MAX_BODY_SIZE
        )
        if path != "/authorize":
            endpoint = CORSMiddleware(
                endpoint, allow_origins=["*"], allow_methods=methods
            )
        app.router.routes.append(Route(path, endpoint=endpoint, methods=methods))
    for path, handler, methods in (
        ("/oauth/consent", consent, ["GET", "POST"]),
        ("/account/connections", connections, ["GET"]),
        ("/account/connections/revoke", revoke_connection, ["POST"]),
    ):
        # Keep the handler as endpoint metadata: shared template context
        # processors use its qualified name to resolve page navigation.
        route = Route(path, endpoint=handler, methods=methods)
        route.app = RequestBodyLimitMiddleware(route.app, _MAX_BODY_SIZE)
        app.router.routes.append(route)
