"""Explicit, fail-closed OAuth deployment and pre-registered client settings."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl

KNOWN_SCOPES = frozenset({"learning", "mcp:full"})
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")


def _https_url(value: str, *, origin: bool = False) -> str:
    """Validate then normalize exactly as the SDK does before exact matching."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("OAuth URLs must be valid HTTPS URLs") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(c.isspace() or ord(c) < 32 for c in value)
        or any(c in value for c in ("\\", "*"))
        or (origin and parts.path not in ("", "/"))
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("OAuth URLs need HTTPS without credentials, query or fragment")
    canonical = str(AnyHttpUrl(value))
    return canonical.rstrip("/") if origin else canonical


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """Operator-registered public client; redirect URIs are exact matches."""

    id: str
    name: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Canonical public endpoints and the deployment's maximum capabilities."""

    issuer: str
    resource: str
    clients: dict[str, OAuthClient]
    scopes: tuple[str, ...] = ("mcp:full",)

    def __post_init__(self) -> None:
        if _https_url(self.issuer, origin=True) != self.issuer:
            raise ValueError(
                "OAuth issuer must be an HTTPS origin without a trailing slash"
            )
        if _https_url(self.resource) != self.resource:
            raise ValueError("OAuth resource must use its canonical HTTPS URL")
        if not urlsplit(self.resource).path.endswith("/mcp"):
            raise ValueError("OAuth resource must be the canonical HTTPS /mcp endpoint")
        if not self.scopes or not set(self.scopes) <= KNOWN_SCOPES:
            raise ValueError("OAuth scopes must be learning or mcp:full")
        if not self.clients or len(self.clients) > 100:
            raise ValueError("OAuth needs between one and 100 registered clients")
        for key, client in self.clients.items():
            if (
                key != client.id
                or not _CLIENT_ID.fullmatch(client.id)
                or not client.name.strip()
                or len(client.name) > 100
                or not 1 <= len(client.redirect_uris) <= 10
            ):
                raise ValueError("OAuth client id, name or redirect list is invalid")
            for uri in client.redirect_uris:
                if _https_url(uri) != uri:
                    raise ValueError("OAuth redirect must use its canonical HTTPS URL")

    @classmethod
    def from_env(cls) -> OAuthConfig | None:
        """Read opt-in configuration; a partial deployment refuses startup."""
        issuer = os.environ.get("PRECIS_OAUTH_ISSUER", "").strip()
        resource = os.environ.get("PRECIS_OAUTH_RESOURCE", "").strip()
        raw_clients = os.environ.get("PRECIS_OAUTH_CLIENTS", "").strip()
        raw_scopes = os.environ.get("PRECIS_OAUTH_SCOPES", "").strip()
        if not any((issuer, resource, raw_clients, raw_scopes)):
            return None
        if not issuer or not resource:
            raise ValueError(
                "OAuth requires both PRECIS_OAUTH_ISSUER and PRECIS_OAUTH_RESOURCE"
            )
        clients = {
            "precis-claude": OAuthClient(
                "precis-claude", "Claude", ("https://claude.ai/api/mcp/auth_callback",)
            )
        }
        if raw_clients:
            try:
                records = json.loads(raw_clients)
                if not isinstance(records, list) or len(records) > 100:
                    raise ValueError
                for record in records:
                    if not isinstance(record, dict) or set(record) != {
                        "id",
                        "name",
                        "redirect_uris",
                    }:
                        raise ValueError
                    if (
                        not isinstance(record["id"], str)
                        or not isinstance(record["name"], str)
                        or not isinstance(record["redirect_uris"], list)
                        or not all(
                            isinstance(uri, str) for uri in record["redirect_uris"]
                        )
                        or record["id"] in clients
                    ):
                        raise ValueError
                    client = OAuthClient(
                        record["id"],
                        record["name"],
                        tuple(_https_url(uri) for uri in record["redirect_uris"]),
                    )
                    clients[client.id] = client
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(
                    "PRECIS_OAUTH_CLIENTS must list unique client id/name/redirect_uris"
                ) from exc
        scopes = (
            tuple(dict.fromkeys(raw_scopes.split())) if raw_scopes else ("mcp:full",)
        )
        return cls(
            _https_url(issuer, origin=True), _https_url(resource), clients, scopes
        )
