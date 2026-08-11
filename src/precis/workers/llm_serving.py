"""Per-host local-LLM advertisement — discover this host's llama-swap models and
advertise them as ``served_by`` cards + ``resource_slots``, so the router can
route to them **directly** (bypassing the litellm proxy).

Runs in the per-host heartbeat (:func:`precis.workers.heartbeat._report_resource_slots`),
alongside the capability probe: each worker polls its OWN loopback llama-swap
``/v1/models``, reads ``--parallel`` from the local llama-swap config for each
model's slot capacity, and reconciles its ``served_by`` entries + slots. There is
**no feature flag** — a host with no local llama-swap answering is a no-op (the
probe fails and nothing is written). Decentralised: each host owns only its own
``served_by`` entry on a shared card, so melchior + spark both serving the same
27B advertise into one card without clobbering each other's endpoint.

Design-of-record: ``local-model-router-integration`` (git-only).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import urllib.error
import urllib.request
from typing import Any

from precis.llm_catalog import LLM_KIND, upsert_card
from precis.workers.llm_reconcile import llm_served_slots_from_cards

log = logging.getLogger(__name__)

#: The OS-default llama-swap loopback port (llama_swap_port_{macos,linux} in the
#: overlay). Overridden by ``PRECIS_LOCAL_SERVE_URL`` (config, not a flag).
_DEFAULT_PORT: dict[str, int] = {"Darwin": 11445, "Linux": 11444}


def local_serve_url() -> str | None:
    """This host's local llama-swap OpenAI base URL (``…/v1``).

    ``$PRECIS_LOCAL_SERVE_URL`` wins; else the OS-default loopback port. ``None``
    when the OS is unrecognised (no default) — the caller then no-ops.
    """
    env = os.environ.get("PRECIS_LOCAL_SERVE_URL")
    if env:
        return env.rstrip("/")
    port = _DEFAULT_PORT.get(platform.system())
    return f"http://127.0.0.1:{port}/v1" if port else None


def _get_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Loopback only (127.0.0.1) — not an agent-supplied URL, so safe_fetch's SSRF
    # guard doesn't apply; the base url is OS/env-derived, never user input.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def discover_local_models(base_url: str) -> dict[str, int] | None:
    """GET ``{base_url}/models`` → ``{model_id: max_parallel}``.

    Returns ``None`` on a probe failure (transient — the caller must NOT retract a
    real advertisement on a blip); ``{}`` when the server answers with no models
    (definitively empty → retract). ``max_parallel`` is read from the local
    llama-swap config (``--parallel``), defaulting to 1 where unknown.
    """
    try:
        data = _get_json(base_url.rstrip("/") + "/models")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    ids = [
        m["id"]
        for m in (data or {}).get("data", [])
        if isinstance(m, dict) and m.get("id")
    ]
    parallel = _parallel_by_model()
    return {mid: parallel.get(mid, 1) for mid in ids}


def _parallel_by_model() -> dict[str, int]:
    """Best-effort: per-model ``--parallel`` from the local llama-swap config.

    Path from ``$PRECIS_LOCAL_SERVE_CONFIG`` (default ``/etc/llama-swap/config.yaml``).
    Returns ``{}`` when the config is absent / unreadable / unparseable — the
    caller then defaults every model to a slot capacity of 1 (safe: a ``parallel=1``
    backend must not be over-advertised).
    """
    path = os.environ.get("PRECIS_LOCAL_SERVE_CONFIG", "/etc/llama-swap/config.yaml")
    try:
        import yaml  # llama-swap config is YAML; PyYAML is a runtime dep

        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except (OSError, ImportError, ValueError):
        return {}
    except Exception:
        log.warning(
            "llm_serving: could not parse %s for --parallel", path, exc_info=True
        )
        return {}
    out: dict[str, int] = {}
    for name, spec in (cfg.get("models") or {}).items():
        cmd = spec.get("cmd", "") if isinstance(spec, dict) else ""
        match = re.search(r"--parallel\s+(\d+)", str(cmd))
        if match:
            out[str(name)] = max(1, int(match.group(1)))
    return out


def _auto_prose(model_id: str) -> str:
    """Stable capability prose for an auto-discovered card — deliberately does NOT
    name hosts (those live in ``served_by``), so the embedding doesn't churn as
    hosts come and go."""
    return (
        f"Local model `{model_id}` — auto-discovered on the cluster's llama-swap. "
        "Directly addressable by this model_id; bind the SMALL tier "
        "(`PRECIS_SUMMARIZE_MODEL`) or an `llm.chain.big` operator override to "
        "route to it."
    )


def _served_by_signature(served: list[dict[str, Any]]) -> frozenset[tuple[Any, ...]]:
    """Order-insensitive fingerprint of a ``served_by`` list.

    The rebuild filters out this host's OLD entry then appends its NEW entry at
    the end, so a plain list ``==`` against the stored value can differ purely
    by ordering when other hosts have entries on the same card. Compare as a
    set of sorted-item tuples instead, so the dirty-check below only fires a
    write when the served_by content actually changed.
    """
    return frozenset(tuple(sorted(e.items())) for e in served)


def advertise_local_llm(
    store: Any, host: str, *, base_url: str | None = None
) -> tuple[int, int]:
    """Reconcile THIS host's local-LLM advertisement to what its llama-swap serves.

    For each served model: ensure an ``llm`` card exists, set **this host's**
    ``served_by`` entry (endpoint + server-side model + ``max_parallel``); drop
    this host's entry from cards for models it no longer serves; then re-derive the
    ``resource_slots`` from all cards. Best-effort caller (heartbeat swallows).
    Returns ``(advertised, pruned)``. No-op (``0, 0``) with no local server / a
    probe blip.

    Only touches entries this function itself owns — those with ``source="auto"``
    (the default for an entry lacking ``source``, back-compat with everything
    written before that key existed). An entry stamped ``source="static"`` (e.g.
    :func:`precis.llm_catalog.seed_slullama_card`'s remote-tunnel card) is never
    added, replaced, or pruned here, even for this same ``host`` — a static entry
    routes to a model this host's OWN llama-swap will never discover, so treating
    it as this pass's business would strip it on the very next heartbeat.
    """
    base_url = base_url or local_serve_url()
    if not base_url:
        return (0, 0)
    discovered = discover_local_models(base_url)
    if discovered is None:
        return (0, 0)  # transient probe failure — leave the existing advertisement

    cards = store.list_refs(kind=LLM_KIND, limit=1000)
    by_model = {(c.meta or {}).get("model_id"): c for c in cards}

    def _is_auto_here(e: dict[str, Any]) -> bool:
        # "auto" is the default (back-compat: every entry written before the
        # source= key existed) — only "static" opts an entry out of this pass.
        return e.get("host") == host and e.get("source", "auto") != "static"

    advertised = 0
    for model_id, max_parallel in discovered.items():
        entry = {
            "host": host,
            "endpoint": base_url,
            "model": model_id,
            "max_parallel": max_parallel,
            "source": "auto",
        }
        card = by_model.get(model_id)
        if card is None:
            upsert_card(
                store, model_id=model_id, text=_auto_prose(model_id), served_by=[entry]
            )
        else:
            current = (card.meta or {}).get("served_by") or []
            served = [e for e in current if not _is_auto_here(e)]
            served.append(entry)
            # Dirty-check: skip the refs write when the rebuilt served_by is,
            # order-insensitively, unchanged from what's already stored. Safe
            # to skip — dispatch liveness comes from the resource_slots
            # reservation + served_by.endpoint, re-read on a process-local
            # 60s timer, not from this write's cadence or updated_at.
            if _served_by_signature(served) != _served_by_signature(current):
                store.update_ref(card.id, meta_patch={"served_by": served})
            else:
                log.debug(
                    "llm_serving: served_by unchanged for %s on %s, skipping "
                    "refs write",
                    model_id,
                    host,
                )
        advertised += 1

    pruned = 0
    for card in cards:
        card_model_id = (card.meta or {}).get("model_id")
        served = (card.meta or {}).get("served_by") or []
        if card_model_id not in discovered and any(_is_auto_here(e) for e in served):
            pruned_served = [e for e in served if not _is_auto_here(e)]
            # Same dirty-check as the advertise path above.
            if _served_by_signature(pruned_served) != _served_by_signature(served):
                store.update_ref(card.id, meta_patch={"served_by": pruned_served})
            else:
                log.debug(
                    "llm_serving: served_by unchanged pruning %s on %s, "
                    "skipping refs write",
                    card_model_id,
                    host,
                )
            pruned += 1

    # Re-derive the llm: slot rows from the now-updated cards (cards are the truth;
    # the existing full-namespace reconcile keeps every host's slots consistent).
    fresh = store.list_refs(kind=LLM_KIND, limit=1000)
    store.reconcile_llm_served_slots(llm_served_slots_from_cards(fresh))

    return (advertised, pruned)


__all__ = ["advertise_local_llm", "discover_local_models", "local_serve_url"]
