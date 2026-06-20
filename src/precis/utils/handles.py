"""Opaque chunk handles for drafts (ADR 0033 §1).

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
"""

from __future__ import annotations

import secrets

# base-58 (Bitcoin): no 0 O l I, so a hand-typed / read-aloud handle is
# not misread. Order is irrelevant (handles are opaque, not compared).
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HANDLE_LEN = 6


def new_handle() -> str:
    """A fresh random 6-char base-58 handle (unguessable). Callers insert
    under the global ``UNIQUE(handle)`` index and retry on violation.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(HANDLE_LEN))


def is_handle(s: str) -> bool:
    """True if ``s`` is shaped like a minted handle (len + alphabet)."""
    return isinstance(s, str) and len(s) == HANDLE_LEN and all(c in ALPHABET for c in s)
