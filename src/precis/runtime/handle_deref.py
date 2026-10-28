"""Exact-handle value substitution for MCP ``put``/``edit`` text arguments.

A same-session exact read is the receipt that permits substitution. Expansion
uses current stored bytes, never the earlier rendered response, and never
leaves expanded content in the tool-call audit arguments.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput, Denied, NotFound
from precis.store.types import ResolvedHandle
from precis.tool_ledger import current_session_key
from precis.utils import handle_registry

TEXT_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024
_RECEIPT_TTL_S = 3600.0
_RECEIPT_MAX = 4096
_Identity = tuple[str, int, int | None]


@dataclass(frozen=True, slots=True)
class DereferencedText:
    requested_handle: str
    resolved_handle: str
    text: str

    @property
    def ack(self) -> str:
        redirect = (
            f" -> {self.resolved_handle}"
            if self.resolved_handle != self.requested_handle
            else ""
        )
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        return (
            f"dereferenced text <- {self.requested_handle}{redirect} "
            f"({len(self.text)} chars, sha256:{digest})"
        )


class _ReadReceipts:
    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, _Identity], float] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > _RECEIPT_MAX:
            self._entries.popitem(last=False)

    def record(self, session_id: str, resolved: ResolvedHandle) -> None:
        identity = (resolved.kind, resolved.ref_id, resolved.chunk_id)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            key = (session_id, identity)
            self._entries[key] = now + _RECEIPT_TTL_S
            self._entries.move_to_end(key)

    def contains(self, session_id: str, resolved: ResolvedHandle) -> bool:
        identity = (resolved.kind, resolved.ref_id, resolved.chunk_id)
        key = (session_id, identity)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            present = key in self._entries
            if present:
                self._entries.move_to_end(key)
            return present

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_RECEIPTS = _ReadReceipts()


def reset_read_receipts() -> None:
    _RECEIPTS.clear()


def record_exact_read(handle: Any, *, store: Any, explicit_kind: Any = None) -> bool:
    """Record a successful exact-handle MCP get; return whether it qualified."""
    if not isinstance(handle, str):
        return False
    raw = handle.strip()
    if not raw or "~" in raw or "/" in raw:
        return False
    parsed = handle_registry.parse(raw)
    if parsed is None:
        return False
    resolved = store.resolve_handle(raw)
    if resolved is None:
        return False
    if explicit_kind is not None and explicit_kind != resolved.kind:
        return False
    session_id, _ = current_session_key()
    if session_id is None:
        return False
    _RECEIPTS.record(session_id, resolved)
    return True


def _envelope_handle(value: Any) -> str | None:
    if not isinstance(value, dict) or "$ref" not in value:
        return None
    if set(value) != {"$ref"}:
        raise BadInput(
            "text reference envelope must contain only $ref",
            next="text={'$ref': 'dc123'}",
        )
    raw = value["$ref"]
    if not isinstance(raw, str) or not raw.strip():
        raise BadInput(
            "text $ref must be one universal handle string",
            next="text={'$ref': 'dc123'}",
        )
    handle = raw.strip()
    if "~" in handle or "/" in handle or handle_registry.parse(handle) is None:
        raise BadInput(
            f"text $ref is not an exact universal handle: {handle!r}",
            next="text={'$ref': 'dc123'} with no selector or cursor",
        )
    return handle_registry.normalize(handle)


def expand_text_reference(
    value: Any,
    *,
    store: Any,
    chunk_readable: Callable[[ResolvedHandle], bool],
) -> DereferencedText | None:
    requested = _envelope_handle(value)
    if requested is None:
        return None
    resolved = store.resolve_handle(requested)
    if resolved is None:
        raise NotFound(
            f"text reference {requested!r} resolves to no live ref or chunk",
            next=f"check it exists: get(id={requested!r})",
        )
    if resolved.chunk_id is not None and not chunk_readable(resolved):
        record_handle = handle_registry.format_handle(resolved.kind, resolved.ref_id)
        raise BadInput(
            f"chunk handle {requested!r} has no exact-handle get read",
            next=f"read and reference its owning record instead: {record_handle}",
        )
    session_id, _ = current_session_key()
    if session_id is None:
        raise BadInput(
            "text $ref requires a live MCP session and an earlier exact read",
            next="pass literal text= instead, or use the MCP get/put path",
        )
    if not _RECEIPTS.contains(session_id, resolved):
        raise Denied(
            f"text reference {requested!r} was not read exactly in this MCP session",
            next=f"get(id={requested!r})",
        )
    if resolved.chunk_id is not None:
        text = store.chunks.chunk_text_by_id(resolved.chunk_id)
        if text is None:
            raise NotFound(
                f"text reference {requested!r} has no live body chunk",
                next=f"read its current content: get(id={requested!r})",
            )
        resolved_handle = handle_registry.format_handle(
            resolved.kind, resolved.chunk_id, chunk=True
        )
    else:
        chunks = store.chunks.live_body_text_for_ref(resolved.ref_id)
        if not chunks:
            raise BadInput(
                f"record handle {requested!r} has no live body chunks",
                next="pass literal text= or reference a concrete body chunk",
            )
        text = "\n\n".join(chunks)
        resolved_handle = handle_registry.format_handle(resolved.kind, resolved.ref_id)
    size = len(text.encode("utf-8"))
    if size > TEXT_PAYLOAD_MAX_BYTES:
        raise BadInput(
            f"dereferenced text is {size} bytes, exceeds the "
            f"{TEXT_PAYLOAD_MAX_BYTES}-byte cap",
            next="reference one smaller chunk or split the write",
        )
    return DereferencedText(
        requested_handle=requested,
        resolved_handle=resolved_handle,
        text=text,
    )


__all__ = [
    "TEXT_PAYLOAD_MAX_BYTES",
    "DereferencedText",
    "expand_text_reference",
    "record_exact_read",
    "reset_read_receipts",
]
