"""Per-number provenance — one sidecar convention for every stored number.

Every number a design stores carries where it came from and how hard it was
computed (multiscale-design-system-spec.md §1.4). The sidecar hangs beside
the params it describes, keyed by param name::

    {"wall_thickness": {"source": "solver",
                        "fidelity": "full_solve",
                        "solver_id": "fea:beam-v3",
                        "assumptions": ["linear elastic", "room temperature"],
                        "margin_origin": "fatigue_knockdown"}}

That shape is what turns three features into three **queries**: the margin
audit (every number with a ``margin_origin``), the fidelity ladder (every
number below the rung you now need), and library-update notification (every
number sourced from a library version that moved).

**ONE enum pair, not three.** The spec also describes a separate
``load_provenance`` enum on interface contracts
(``user_stated|llm_assumed|derived_by_statics``); it collapses into this
one mechanism — :func:`from_load_provenance` is the only place that mapping
is written down. A second vocabulary for the same idea is how provenance
rots.

**Where fidelity is required.** ``source`` is always required; ``fidelity``
is required exactly when the number was *computed*
(``derived``/``solver``/``library``), because a computed number that won't
say which rung it came off cannot be laddered and silently reads as
trustworthy. A number somebody stated or measured has no solver rung to
declare, so fidelity stays optional there. ``library_version`` is required
for ``source='library'`` for the same reason: the notification query is
meaningless without it.

Validation is loud at **write** time. A malformed sidecar that reaches
storage is worse than no sidecar: it reads as provenance and audits as
nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Where a number came from (spec §1.4). CLOSED — extend by editing this
#: tuple and the docs, never by writing a stray value through.
SOURCES: tuple[str, ...] = (
    "user_stated",
    "llm_assumed",
    "derived",
    "solver",
    "library",
    "measured",
)

#: How hard it was computed, cheapest rung first. The fidelity ladder walks
#: this order, so the order is load-bearing, not cosmetic.
FIDELITIES: tuple[str, ...] = (
    "template",
    "analytic",
    "surrogate",
    "full_solve",
    "dft",
    "high_method",
)

#: Sources that mean "a machine produced this number", and therefore owe a
#: fidelity rung.
COMPUTED_SOURCES: frozenset[str] = frozenset({"derived", "solver", "library"})

#: Every key a sidecar entry may carry. Anything else is a typo or a
#: private convention smuggled into a shared mechanism — both are rejected.
ENTRY_KEYS: tuple[str, ...] = (
    "source",
    "fidelity",
    "solver_id",
    "assumptions",
    "library_version",
    "margin_origin",
)

#: The spec's interface-contract ``load_provenance`` enum, mapped onto the
#: shared shape: ``(source, fidelity, solver_id)``. ``derived_by_statics``
#: is a derivation at the analytic rung by the statics pass — nothing more
#: exotic than that, which is precisely why it doesn't need its own enum.
_LOAD_PROVENANCE: dict[str, tuple[str, str | None, str | None]] = {
    "user_stated": ("user_stated", None, None),
    "llm_assumed": ("llm_assumed", None, None),
    "derived_by_statics": ("derived", "analytic", "statics"),
}


class ProvenanceError(ValueError):
    """A malformed provenance entry or sidecar. Raised at write time."""


def entry(
    *,
    source: str,
    fidelity: str | None = None,
    solver_id: str | None = None,
    assumptions: Iterable[str] | None = None,
    library_version: str | None = None,
    margin_origin: str | None = None,
) -> dict[str, Any]:
    """Build one validated sidecar entry.

    The typed front door to :func:`validate_entry` — callers that know
    their fields at the call site get keyword checking from the signature
    instead of a dict literal nobody spell-checks.
    """
    raw: dict[str, Any] = {"source": source}
    if fidelity is not None:
        raw["fidelity"] = fidelity
    if solver_id is not None:
        raw["solver_id"] = solver_id
    if assumptions is not None:
        raw["assumptions"] = list(assumptions)
    if library_version is not None:
        raw["library_version"] = library_version
    if margin_origin is not None:
        raw["margin_origin"] = margin_origin
    return validate_entry(raw)


def validate_entry(raw: Any, *, param: str | None = None) -> dict[str, Any]:
    """Vet one entry into its stored shape.

    Returns a normalized copy in canonical key order, with absent optionals
    dropped and ``assumptions`` always a list (possibly empty), so two
    equivalent entries compare equal. ``param`` only enriches the error
    message — it names which number was wrong, which is the first thing a
    caller wants.
    """
    where = f" for {param!r}" if param else ""
    if not isinstance(raw, Mapping):
        raise ProvenanceError(
            f"provenance{where} must be an object with a 'source', got {raw!r}"
        )
    unknown = sorted(set(raw) - set(ENTRY_KEYS))
    if unknown:
        raise ProvenanceError(
            f"unknown provenance key(s){where}: {', '.join(unknown)} — "
            f"allowed keys: {', '.join(ENTRY_KEYS)}"
        )

    source = raw.get("source")
    if source not in SOURCES:
        raise ProvenanceError(
            f"provenance{where} needs a 'source' from {', '.join(SOURCES)}, "
            f"got {source!r}"
        )
    out: dict[str, Any] = {"source": source}

    fidelity = raw.get("fidelity")
    if fidelity is None:
        if source in COMPUTED_SOURCES:
            raise ProvenanceError(
                f"provenance{where} with source {source!r} must declare a "
                f"'fidelity' from {', '.join(FIDELITIES)} — a computed number "
                "that won't name its rung can't be laddered"
            )
    else:
        if fidelity not in FIDELITIES:
            raise ProvenanceError(
                f"unknown fidelity{where}: {fidelity!r} — "
                f"allowed: {', '.join(FIDELITIES)}"
            )
        out["fidelity"] = fidelity

    for key in ("solver_id", "library_version", "margin_origin"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ProvenanceError(
                f"provenance {key!r}{where} must be a non-empty string, got {value!r}"
            )
        out[key] = value.strip()

    if source == "library" and "library_version" not in out:
        raise ProvenanceError(
            f"provenance{where} with source 'library' must declare a "
            "'library_version' — the library-update query has nothing to "
            "compare without it"
        )

    assumptions = raw.get("assumptions")
    if assumptions is None:
        out["assumptions"] = []
    else:
        if isinstance(assumptions, str) or not isinstance(assumptions, Iterable):
            raise ProvenanceError(
                f"provenance 'assumptions'{where} must be a list of strings, "
                f"got {assumptions!r}"
            )
        items = list(assumptions)
        if any(not isinstance(a, str) or not a.strip() for a in items):
            raise ProvenanceError(
                f"provenance 'assumptions'{where} must be a list of non-empty "
                f"strings, got {assumptions!r}"
            )
        out["assumptions"] = [str(a).strip() for a in items]

    # Canonical key order, so an entry round-tripped through storage is
    # byte-comparable with a freshly built one.
    return {k: out[k] for k in ENTRY_KEYS if k in out}


def validate_sidecar(raw: Any) -> dict[str, dict[str, Any]]:
    """Vet a whole ``{param: entry}`` sidecar. Empty is valid (nothing
    solved yet); a non-object, or a non-string param name, is not."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProvenanceError(
            f"a provenance sidecar must be an object keyed by param name, got {raw!r}"
        )
    out: dict[str, dict[str, Any]] = {}
    for param, value in raw.items():
        if not isinstance(param, str) or not param.strip():
            raise ProvenanceError(
                f"provenance sidecar keys must be param names, got {param!r}"
            )
        out[param.strip()] = validate_entry(value, param=param)
    return out


def from_load_provenance(value: str) -> dict[str, Any]:
    """The spec's interface-contract ``load_provenance`` enum → one sidecar
    entry. The single place that collapse is written down."""
    mapped = _LOAD_PROVENANCE.get(value)
    if mapped is None:
        raise ProvenanceError(
            f"unknown load_provenance {value!r} — "
            f"allowed: {', '.join(sorted(_LOAD_PROVENANCE))}"
        )
    source, fidelity, solver_id = mapped
    return entry(source=source, fidelity=fidelity, solver_id=solver_id)


def merged(
    base: Mapping[str, Any] | None, update: Mapping[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Overlay ``update`` on ``base``, per param (a re-solve replaces that
    number's whole entry, never half of it — a new fidelity with the old
    assumptions still attached would be a lie). Both sides are validated."""
    out = validate_sidecar(base)
    out.update(validate_sidecar(update))
    return out


def missing_provenance(
    params: Iterable[str], sidecar: Mapping[str, Any] | None
) -> list[str]:
    """Which of ``params`` have no provenance entry, sorted.

    The "every solved param carries provenance" check, kept here so every
    caller asks it the same way instead of each growing its own loop.
    """
    have = set(sidecar or {})
    return sorted(p for p in params if p not in have)


def margin_audit(sidecar: Mapping[str, Any] | None) -> dict[str, str]:
    """``{param: margin_origin}`` for every number a margin rule touched —
    the margin audit, as a plain dict comprehension over the sidecar."""
    return {
        param: str(entry_raw["margin_origin"])
        for param, entry_raw in (sidecar or {}).items()
        if isinstance(entry_raw, Mapping) and entry_raw.get("margin_origin")
    }


def below_fidelity(sidecar: Mapping[str, Any] | None, rung: str) -> list[str]:
    """Params computed at a rung cheaper than ``rung`` — the fidelity-ladder
    query. Numbers with no fidelity (stated, assumed, measured) are NOT
    listed: they aren't on the ladder at all, and reporting them as "too
    cheap" would send a caller re-solving a measurement."""
    if rung not in FIDELITIES:
        raise ProvenanceError(
            f"unknown fidelity {rung!r} — allowed: {', '.join(FIDELITIES)}"
        )
    want = FIDELITIES.index(rung)
    out: list[str] = []
    for param, entry_raw in (sidecar or {}).items():
        if not isinstance(entry_raw, Mapping):
            continue
        fidelity = entry_raw.get("fidelity")
        if isinstance(fidelity, str) and fidelity in FIDELITIES:
            if FIDELITIES.index(fidelity) < want:
                out.append(str(param))
    return sorted(out)
