"""Allow-list of ``refs.meta`` worker-retry stamps an operator may clear
via ``edit(kind='paper', args={'clear_stamp': <key>})`` (gr514347).

A pass that stops re-claiming a paper once a ``refs.meta`` stamp exists
(a converged / skip stamp — e.g. ``context_sentence``'s
``META_FAILED_KEY``) registers its clearable keys beside the constants
that write them, then adds its module to :data:`_DECLARING_MODULES`.
:func:`clearable_stamps` lazily imports exactly that list, so a stamp is
clearable only after its owning worker opts in — the registry IS the
allow-list; there is no door to clear an arbitrary ``refs.meta`` key.
"""

from __future__ import annotations

import importlib

__all__ = ["clearable_stamps", "register_retry_stamps"]

#: Worker modules whose import registers their clearable retry stamps.
#: Keeping the list explicit is what makes the door an allow-list: a new
#: worker's stamp becomes clearable only once its module is named here.
_DECLARING_MODULES: tuple[str, ...] = (
    "precis.workers.context_sentence",
    "precis.workers.openalex_enrich",
    "precis.workers.stub_rank",
)

_STAMPS: dict[str, str] = {}
_loaded = False


def register_retry_stamps(owner: str, *keys: str) -> None:
    """Declare top-level ``refs.meta`` keys this pass stamps to gate
    re-claim as operator-clearable. ``owner`` is the pass name — it
    surfaces in refusal hints and ack lines."""
    for key in keys:
        _STAMPS[key] = owner


def clearable_stamps() -> dict[str, str]:
    """``{meta_key: pass_name}`` — the registered allow-list, populated
    by lazily importing :data:`_DECLARING_MODULES` on first use."""
    global _loaded
    if not _loaded:
        for module in _DECLARING_MODULES:
            importlib.import_module(module)
        _loaded = True
    return dict(_STAMPS)
