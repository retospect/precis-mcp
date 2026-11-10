"""OAuth resource discovery and explicit public-host transport configuration.

The web process owns authorization and consent; the MCP process is the resource
server. Both use the same configured canonical URLs and database. Discovery is
public, but every content request still crosses server._install_token_auth.
Request Host/forwarding headers never manufacture discovery URLs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from precis.oauth.config import OAuthConfig


def metadata_path(config: OAuthConfig) -> str:
    """RFC 9728's path-qualified metadata location for this MCP resource."""
    return "/.well-known/oauth-protected-resource" + urlsplit(config.resource).path


def challenge(config: OAuthConfig) -> str:
    """Advertise the authorization server without depending on proxy headers."""
    parts = urlsplit(config.resource)
    url = f"{parts.scheme}://{parts.netloc}{metadata_path(config)}"
    return f'Bearer resource_metadata="{url}", scope="{" ".join(config.scopes)}"'


def metadata(config: OAuthConfig) -> dict[str, Any]:
    """Public resource metadata; no credentials or user state."""
    return {
        "resource": config.resource,
        "authorization_servers": [config.issuer],
        "scopes_supported": list(config.scopes),
        "bearer_methods_supported": ["header"],
    }


def configure_transport(fastmcp: Any, config: OAuthConfig) -> None:
    """Extend the existing localhost policy with exact configured public names."""
    from mcp.server.transport_security import TransportSecuritySettings

    resource = urlsplit(config.resource)
    origin = f"{resource.scheme}://{resource.netloc}"
    settings = fastmcp.settings.transport_security
    if settings is None:
        settings = TransportSecuritySettings()
    settings = settings.model_copy(deep=True)
    settings.enable_dns_rebinding_protection = True
    settings.allowed_hosts = list(
        dict.fromkeys([*settings.allowed_hosts, resource.netloc])
    )
    settings.allowed_origins = list(
        dict.fromkeys([*settings.allowed_origins, origin, config.issuer])
    )
    fastmcp.settings.transport_security = settings
