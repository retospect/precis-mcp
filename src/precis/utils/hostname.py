"""Shared host-identity helpers (no precis imports — safe from store and workers)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

#: A Docker container booted with no ``--hostname`` gets the short (12
#: lowercase hex char) form of its container ID as ``socket.gethostname()``
#: (gr306275). Such an identity is throwaway: it never boots again.
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def is_container_id_host(host: str) -> bool:
    """True when ``host`` looks like an unset Docker container ID."""
    return bool(CONTAINER_ID_RE.match(host))


def role_label_for_process(process: str) -> str:
    """Role word for a serving ``PRECIS_PROCESS`` name (gr681314).

    The agent-facing runtime surface names the *role* that serves a kind,
    never the hostname. The process name is the role signal the
    ``kind_provider`` roster already carries: ``precis-worker-compute``
    is the compute lane, bare ``precis-worker`` the collapsed worker,
    ``precis-web``/``web:<user>`` the web node. Everything non-alphanumeric
    collapses to ``-`` and user suffixes after ``:`` drop, so no personal
    name can leak through a custom process name.
    """
    p = re.sub(r"[^a-z0-9:]+", "-", (process or "").lower()).strip("-")
    if p.startswith("precis-worker-"):
        role = p[len("precis-worker-") :]
    elif p == "precis-worker":
        role = "worker"
    elif p == "precis-web" or p.startswith("web:"):
        role = "web"
    elif p.startswith("precis-"):
        role = p[len("precis-") :]
    else:
        role = p
    role = role.split(":", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", role).strip("-") or "fleet"


def _stable_host_order(host: str) -> str:
    """Hash order: stable across calls, leaks nothing about the name."""
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


def node_role_labels(routes: Iterable[tuple[str, str]]) -> list[str]:
    """Display labels for ``(host, process)`` provider routes.

    Hostnames never appear in the returned labels (gr681314, Reto
    2026-11-16): each label names the serving process's role. A role
    served by exactly one host is just the role (``compute node``); a
    role with several hosts carries a stable numeric suffix ordered by
    host hash (``compute-1 node``, ``compute-2 node``) so two providers
    stay distinguishable without leaking names. The suffix → hostname
    mapping lives only in gated operator surfaces (the fleet view and
    job logs), never in agent-facing MCP text.
    """
    by_role: dict[str, set[str]] = {}
    for host, process in routes:
        by_role.setdefault(role_label_for_process(process), set()).add(host)
    labels: list[str] = []
    for role in sorted(by_role):
        hosts = sorted(by_role[role], key=_stable_host_order)
        if len(hosts) == 1:
            labels.append(f"{role} node")
        else:
            labels.extend(f"{role}-{n} node" for n in range(1, len(hosts) + 1))
    return labels
