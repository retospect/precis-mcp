"""Shared host-identity helpers (no precis imports — safe from store and workers)."""

from __future__ import annotations

import re

#: A Docker container booted with no ``--hostname`` gets the short (12
#: lowercase hex char) form of its container ID as ``socket.gethostname()``
#: (gr306275). Such an identity is throwaway: it never boots again.
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def is_container_id_host(host: str) -> bool:
    """True when ``host`` looks like an unset Docker container ID."""
    return bool(CONTAINER_ID_RE.match(host))
