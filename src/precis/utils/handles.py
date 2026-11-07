"""Opaque chunk handles for drafts.

A draft chunk's only exposed anchor is a minted, fixed-length, opaque
**handle**: a random 6-character base-58 string (Bitcoin alphabet,
dropping the ambiguous ``0 O l I``) stored in ``chunks.handle`` with a
global ``UNIQUE`` index. Minting is "generate random + insert; on the
(vanishingly rare) unique violation, regenerate and retry" — the
constraint is the collision guard, so there is no birthday math here.

The bare-inline prose sigil is ``¶`` (e.g. ``[¶5BL5xQ]``); the handle
string itself is sigil-free (URLs ``/c/<handle>``). 58⁶ ≈ 38 B — against
~10⁵–10⁶ draft chunks, the per-insert collision probability is
negligible.

One collision the UNIQUE index cannot see is with the decimal-handle
grammar (``handle_registry``): a mint like ``Dc1647`` matches
``<code><digits>`` (``parse`` is case-insensitive on the code), so every
decimal-handle resolution path — ``get_draft_chunk`` and the ``at=``
anchor resolver behind it — would look up ``chunk_id`` 1647 instead of
this row's ``handle`` column: ``NotFound`` on a miss, the wrong chunk on
a hit (gr582947). ``new_handle`` therefore re-mints any candidate
:func:`handle_registry.parse` accepts.
"""

from __future__ import annotations

import secrets

from precis.utils import handle_registry

# base-58 (Bitcoin): no 0 O l I, so a hand-typed / read-aloud handle is
# not misread. Order is irrelevant (handles are opaque, not compared).
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HANDLE_LEN = 6


def new_handle() -> str:
    """A fresh random 6-char base-58 handle (unguessable). Callers insert
    under the global ``UNIQUE(handle)`` index and retry on violation.

    Rejects (and re-mints) any candidate that
    :func:`precis.utils.handle_registry.parse` decodes as a decimal
    handle — a ``<code><digits>`` mint is ambiguous between the
    ``chunks.handle`` column and a universal ``<code><pk>`` address
    (module docstring, gr582947).
    """
    while True:
        h = "".join(secrets.choice(ALPHABET) for _ in range(HANDLE_LEN))
        if handle_registry.parse(h) is None:
            return h


def is_handle(s: str) -> bool:
    """True if ``s`` is shaped like a minted handle (len + alphabet)."""
    return isinstance(s, str) and len(s) == HANDLE_LEN and all(c in ALPHABET for c in s)
