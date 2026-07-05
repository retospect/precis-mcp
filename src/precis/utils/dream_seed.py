"""Dream PROCESS lens seeds (Part B of
``docs/design/tool-friction-reflection-and-dreams.md``).

The dream cycle colours each pass with a **lens** injected into the dream
prompt's *variable* layer so the same steps come out with a different
register. The single-stance **persona** lenses (Feynman, Napoleon,
Shannon, …) now live as first-class oracle traditions and are drawn via
:mod:`precis.utils.oracle_lens` (the dream's default ``sci`` lens); this
module now serves only the **process** lenses — a ``process`` is a
sequential multi-phase pass (Disney's Dreamer → Realist → Critic) that
doesn't fit the oracle's one-block-per-entry "random wisdom" shape.

Lenses are data — ``precis/data/dream_lenses.yaml`` — so a new process
lens is a yaml entry, no code change. :func:`select_lens` still offers a
deterministic rotation for callers that want even coverage across a set.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

log = logging.getLogger(__name__)

_PACKAGED_DATA = "precis.data"
_LENSES_FILE = "dream_lenses.yaml"


def load_lenses() -> list[dict[str, Any]]:
    """The lens definitions from the packaged ``dream_lenses.yaml``.

    Returns an empty list (and logs) if the resource is unreadable or
    malformed — the caller then simply runs the dream unlensed, never
    failing the pass over a missing seed file.
    """
    try:
        from importlib import resources

        raw = (
            resources.files(_PACKAGED_DATA)
            .joinpath(_LENSES_FILE)
            .read_text(encoding="utf-8")
        )
        doc = yaml.safe_load(raw)
    except (FileNotFoundError, ModuleNotFoundError, OSError, yaml.YAMLError):
        log.exception("dream_seed: dream_lenses.yaml unreadable")
        return []
    lenses = doc.get("lenses") if isinstance(doc, dict) else None
    if not isinstance(lenses, list):
        log.error("dream_seed: dream_lenses.yaml has no 'lenses' list")
        return []
    return [lens for lens in lenses if isinstance(lens, dict) and lens.get("prompt")]


def select_lens(lenses: list[dict[str, Any]], *, bucket: int) -> dict[str, Any] | None:
    """Pick one lens for this cycle by a rotating ``bucket`` index.

    Deterministic rotation (``bucket % N``) rather than random choice so
    the whole set is covered evenly over successive passes. The worker
    derives ``bucket`` from wall-clock time (one step per dream cadence).
    Returns ``None`` when there are no lenses.
    """
    if not lenses:
        return None
    return lenses[bucket % len(lenses)]


def render_lens_block(lens: dict[str, Any]) -> str:
    """The variable-layer text injected at the top of the dream prompt."""
    name = lens.get("name", lens.get("id", "?"))
    prompt = str(lens.get("prompt", "")).strip()
    return f"## This cycle's lens: {name}\n\n{prompt}\n"


__all__ = ["load_lenses", "render_lens_block", "select_lens"]
