"""Pad and net handle spellings shared by the renderers and :mod:`.argue`.

Lives apart from :mod:`.argue` so the wheel-shipped renderers
(:mod:`.gerber_view`, :mod:`.schematic`) need not import the store/LLM-side
argue module, which the ``[pcb-kernel]`` wheel slice does not carry.
"""

from __future__ import annotations


def pad_handle(refdes: str, pin: str) -> str:
    return f"{refdes}.{pin}"


def net_handle(name: str) -> str:
    return f"net:{name}"
