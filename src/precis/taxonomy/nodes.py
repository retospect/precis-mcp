"""Pure `taxon` node helpers — meta shape, card text, slug, key validation.

Used by :class:`~precis.handlers.taxon.TaxonHandler` and, later, the seed
writer, so hand-put and seeded nodes are byte-identical. No DB, no I/O. The
fixed key set is the one in `docs/backlog/term-taxonomy.md` §Design.
"""

from __future__ import annotations

import re
from typing import Any

from precis.errors import BadInput
from precis.reading.concepts import normalize_name

#: Keys a caller may put in ``meta=`` (the spec's fixed set).
CALLER_KEYS: frozenset[str] = frozenset(
    {
        "definition",
        "aliases",
        "status",
        "start",
        "contract",
        "dimension_kind",
        "si_vector",
        "canonical_unit",
        "value_type",
        "allowed_values",
        "standard_ref",
        "higher_is_better",
        "legacy_source",
        "applies_to_ref",
    }
)
#: Keys the handler writes itself; legal on a stored node, not a caller input.
HANDLER_KEYS: frozenset[str] = frozenset({"name", "norm_name", "slug"})
ALLOWED_KEYS: frozenset[str] = CALLER_KEYS | HANDLER_KEYS

DIMENSION_KINDS: tuple[str, ...] = (
    "si",
    "currency",
    "count",
    "dimensionless",
    "scale",
    "categorical",
)
STATUSES: tuple[str, ...] = ("proposed", "systematic")
STATUS_PROPOSED = "proposed"

_SI_VECTOR_RE = re.compile(r"^-?\d+(,-?\d+){6}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, runs of non-alphanumerics to one hyphen. For resolution
    only — a slug is not identity and need not be unique."""
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")


def taxon_card_text(
    name: str, definition: str, aliases: list[str] | None = None
) -> str:
    """The embeddable ``card_combined`` text: name + definition (+ aliases)."""
    out = name.strip()
    if definition.strip():
        out += f" — {definition.strip()}"
    cleaned = [a.strip() for a in (aliases or []) if a.strip()]
    if cleaned:
        out += f" (aka {', '.join(cleaned)})"
    return out.strip()


def initial_taxon_meta(
    name: str, definition: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``meta`` stamped on a new taxon: handler-written identity keys,
    the definition, and ``status='proposed'`` (earned, never put). ``extra``
    (already validated) overlays the caller's node keys."""
    meta: dict[str, Any] = {
        "name": name.strip(),
        "norm_name": normalize_name(name),
        "slug": slugify(name),
        "definition": definition.strip(),
        "aliases": [],
        "status": STATUS_PROPOSED,
    }
    if extra:
        meta.update(extra)
    return meta


def canonical_si_vector(value: Any) -> str:
    """Seven comma-separated integers, whitespace stripped; else BadInput."""
    s = "".join(str(value).split())
    if not _SI_VECTOR_RE.match(s):
        raise BadInput(
            f"si_vector must be seven comma-separated integers, got {value!r}",
            next="e.g. si_vector='0,0,-1,0,0,0,0' (the SI base exponents, in order)",
        )
    return s


def validate_taxon_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Check a node ``meta`` against the fixed key set and per-key rules.
    Returns a normalised copy (``si_vector`` canonicalised); raises
    :class:`BadInput` naming the offending key."""
    unknown = sorted(set(meta) - ALLOWED_KEYS)
    if unknown:
        raise BadInput(
            f"unknown taxon meta key {unknown[0]!r}",
            next="allowed keys: " + ", ".join(sorted(ALLOWED_KEYS)),
        )
    out = dict(meta)

    kind = out.get("dimension_kind")
    if kind is not None and kind not in DIMENSION_KINDS:
        raise BadInput(
            f"dimension_kind {kind!r} is not one of {', '.join(DIMENSION_KINDS)}",
            next="omit dimension_kind if the term has no dimension",
        )
    if kind == "si":
        if out.get("si_vector") is None:
            raise BadInput(
                "dimension_kind='si' requires si_vector",
                next="si_vector='0,0,-1,0,0,0,0' (seven SI base exponents)",
            )
    elif out.get("si_vector") is not None:
        raise BadInput(
            "si_vector is only allowed with dimension_kind='si'",
            next="drop si_vector, or set dimension_kind='si'",
        )
    if out.get("si_vector") is not None:
        out["si_vector"] = canonical_si_vector(out["si_vector"])

    status = out.get("status")
    if status is not None and status not in STATUSES:
        raise BadInput(
            f"status {status!r} is not one of {', '.join(STATUSES)}",
            next="status is earned; a new node is 'proposed'",
        )

    aliases = out.get("aliases")
    if aliases is not None and (
        not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases)
    ):
        raise BadInput(
            "aliases must be a list of strings", next="aliases=['alt name', ...]"
        )

    if "start" in out and not isinstance(out["start"], bool):
        raise BadInput("start must be a boolean", next="start=true on a root node")

    contract = out.get("contract")
    if contract is not None:
        if not out.get("start"):
            raise BadInput(
                "contract is only allowed on a start node (start=true)",
                next="set start=true, or drop contract; descendants inherit it",
            )
        req = contract.get("required_keys") if isinstance(contract, dict) else None
        if (
            not isinstance(contract, dict)
            or set(contract) != {"required_keys"}
            or not isinstance(req, list)
            or not all(isinstance(k, str) for k in req)
        ):
            raise BadInput(
                "contract must be {'required_keys': [str, ...]}",
                next="contract={'required_keys': ['dimension_kind']}",
            )
    return out
