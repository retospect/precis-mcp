"""The registry of private kinds — the one owner of "which kinds are private".

Private kinds authenticate an owner and bypass the shared audit sample, the
result/continuation machinery and the search-feedback fold; a personal MCP
token or a non-full OAuth connection may reach nothing else. Every gate —
dispatch, the audit sample, the token allowlist, the OAuth scope table,
whoami's permission line — reads this one registry, so a kind cannot be
private at dispatch yet public in the audit sample or the token allowlist.
A second private kind is one entry here plus its handler.

Stdlib-only so ``tools``, ``runtime``, ``oauth`` and ``whoami`` can all
import it without cycles; the handler class resolves lazily so importing
the registry never pulls a handler (and its store deps) into a caller that
only needs the classifier.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.protocol import Handler


@dataclass(frozen=True, slots=True)
class PrivateKind:
    """One private kind's declarations, consumed by every privacy gate."""

    kind: str
    #: The public skill document a scoped credential may read verbatim.
    help_skill: str
    #: The OAuth grant scope that admits this kind's calls (and its help
    #: read) on a connection without ``mcp:full``.
    oauth_scope: str
    #: ``"module:Class"`` of the handler, resolved lazily for its KindSpec.
    handler: str
    #: The recovery hint every private refusal carries.
    next_hint: str

    def handler_class(self) -> type[Handler]:
        """Resolve ``module:Class`` on first need, never at import."""
        module, _, name = self.handler.partition(":")
        cls: type[Handler] = getattr(importlib.import_module(module), name)
        return cls


#: Registered private kinds by name; learning is the only one today.
PRIVATE_KINDS: dict[str, PrivateKind] = {
    "learning": PrivateKind(
        kind="learning",
        help_skill="precis-learning-help",
        oauth_scope="learning",
        handler="precis.handlers.learning:LearningHandler",
        next_hint="get(kind='learning', id='/activity', args={'days': 14})",
    ),
}


def private_kind_of(payload: Mapping[str, Any]) -> str | None:
    """The private kind a payload addresses, or ``None``.

    Classifier of record for the private route: an explicit ``kind=``, one
    entry of a comma-combined kind list, or the canonical ``<kind>:`` id
    prefix, case- and whitespace-insensitive. Deliberately broader than
    accepted syntax so malformed private calls get the same payload
    redaction as valid ones; the private dispatcher refuses the malformed
    shapes itself.
    """
    kind = payload.get("kind")
    declared = (
        {part.strip().lower() for part in kind.split(",")}
        if isinstance(kind, str)
        else set()
    )
    ident = payload.get("id")
    ids = ident if isinstance(ident, (list, tuple)) else (ident,)
    spelled = [value.strip().lower() for value in ids if isinstance(value, str)]
    for name in PRIVATE_KINDS:
        if name in declared or any(value.startswith(f"{name}:") for value in spelled):
            return name
    return None
