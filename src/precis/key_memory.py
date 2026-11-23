"""Render the key-memory pointer from the authenticated account row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.handle_words import to_words
from precis.utils.eye_render import _VERBATIM_CAP

if TYPE_CHECKING:
    from precis.users import WebUser

HELP_SKILL = "precis-key-memory-help"

#: The key-memory hub body budget. A hub body past this cut hides its
#: standing orders behind a "full body" call that a narrow grant cannot
#: make, so the one hub read the instructions promise stops showing every
#: order. This is the same ``_VERBATIM_CAP`` the fisheye body rung cuts at
#: and the write path warns at (``memory_hubs``' "Memory too large" hint);
#: the key-memory skill cites this name, and tests keep the three in step.
HUB_BODY_MAX_CHARS = _VERBATIM_CAP


def instructions(user: WebUser) -> str:
    """The line shared by initialize and the first tool response."""
    memory_id = user.key_memory_id
    assert memory_id is not None
    return (
        f"You are serving {user.abbrev}. Their key memory is "
        f"me{memory_id} ({to_words(memory_id)}). "
        f"Read it first with get(kind='memory', id='me{memory_id}'). "
        "Update it when you learn something durable they stated and your "
        f"connection allows the write. See get(kind='skill', id='{HELP_SKILL}')."
    )


def may_read(user: WebUser | None, verb: str, payload: dict[str, object]) -> bool:
    """Allow narrowly scoped credentials one exact read of their own hub."""
    if user is None or user.key_memory_id is None or verb != "get":
        return False
    return payload in (
        {"kind": "memory", "id": f"me{user.key_memory_id}"},
        {"kind": "memory", "id": user.key_memory_id},
    )
