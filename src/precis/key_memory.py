"""Render the key-memory pointer from the authenticated account row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.handle_words import to_words

if TYPE_CHECKING:
    from precis.users import WebUser


def instructions(user: WebUser) -> str:
    """The line shared by initialize and the first tool response."""
    memory_id = user.key_memory_id
    assert memory_id is not None
    return (
        f"You are serving {user.abbrev}. Their key memory is "
        f"me{memory_id} ({to_words(memory_id)}). "
        f"Read it first with get(kind='memory', id='me{memory_id}'). "
        "Update it when you learn something durable they stated and your "
        "connection allows the write. For voice, use words in speech and the "
        "numeric handle in calls. See get(kind='skill', id='precis-word-handles-help')."
    )


def may_read(user: WebUser | None, verb: str, payload: dict[str, object]) -> bool:
    """Allow narrowly scoped credentials one exact read of their own hub."""
    if user is None or user.key_memory_id is None or verb != "get":
        return False
    return payload in (
        {"kind": "memory", "id": f"me{user.key_memory_id}"},
        {"kind": "memory", "id": user.key_memory_id},
    )
