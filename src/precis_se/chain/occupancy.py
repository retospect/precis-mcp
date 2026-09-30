"""Per-state foothold occupancy — the walker's ``{"<strand>.<ord>":
"<helix>@<offset>" | None}`` map (``se-walker-light-protocol``), and the
one place it is parsed, vetted and applied.

A walker state is *which foothold each leg occupies*. Rather than teach
every chain consumer a ``state=`` argument, the map is applied to the
domain rows themselves — a transient rewrite of the rows the state
overrides, on a tree that is discarded at the end of the read
(:func:`precis_se.handler._apply_state_arg`) or of the settle
(:func:`precis_se.chain.relax.op_relax_chain`). Downstream — pairing, the
``chain_*`` findings, the loop springs, ``view='chain'`` — then reads the
per-state route through the same :func:`~precis_se.chain.vocab.group_domains`
it always did, and cannot disagree with itself about what the state means.

Semantics of one entry, ``"<strand>.<ord>"`` → target:

* ``"<helix>@<offset>"`` — the domain sits on that helix with its
  **start** at that offset; its length and direction come from the row,
  so the addressing never depends on a 1-bp override (2026-09-28 ruling).
* ``None`` — a **free** leg: the domain is lifted off every helix. It is
  exempt from ``chain_dangling_domain`` and from pairing in that state,
  contributes no loop spring, and shows as ``free`` in the chain view.
  :func:`apply_occupancy` marks the copy :attr:`DomainSpec.free` and
  :func:`group_domains` drops it from every table, which is what "exempt"
  means operationally.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from precis_se.chain.vocab import (
    HELIX_ROLE,
    STRAND_ROLE,
    ChainError,
    DomainSpec,
    chain_role,
)

__all__ = [
    "GUARD_BOUND",
    "GUARD_FREE",
    "apply_occupancy",
    "effective_target",
    "guard_violations",
    "parse_target",
    "vet_guard",
    "vet_occupancy",
]

#: The two predicate words a transition's ``params.guard`` accepts
#: besides an explicit ``<helix>@<offset>`` target.
GUARD_BOUND = "bound"
GUARD_FREE = "free"


def parse_target(raw: Any, *, what: str) -> tuple[str, int]:
    """``"<helix>@<offset>"`` → ``(helix, offset)``, or :class:`ChainError`
    naming the malformed token."""
    if not isinstance(raw, str) or raw.count("@") != 1:
        raise ChainError(
            f"{what}: an occupancy target is '<helix>@<offset>' (or null for "
            f"a free leg), got {raw!r}"
        )
    helix, _, offset_raw = raw.partition("@")
    helix = helix.strip()
    try:
        offset = int(offset_raw.strip())
    except ValueError:
        raise ChainError(
            f"{what}: occupancy offset must be an integer, got {offset_raw!r} "
            f"in {raw!r}"
        ) from None
    if not helix or offset < 0:
        raise ChainError(f"{what}: bad occupancy target {raw!r}")
    return helix, offset


def _domain_by_key(domains: list[DomainSpec]) -> dict[str, DomainSpec]:
    return {f"{d.strand}.{d.ord}": d for d in domains}


def vet_occupancy(
    tree: Any, raw: Any, *, what: str, n_units: dict[str, int] | None = None
) -> dict[str, str | None]:
    """Vet one state's occupancy payload against ``tree``: every key names
    an existing domain row of an existing strand block, every non-null
    target names a helix block with room for the domain from that offset.
    Returns the normalised map (targets re-serialised as
    ``"<helix>@<offset>"``). ``n_units`` may supply helix lengths the
    caller already computed; otherwise they are read off each helix's
    ``chain`` record."""
    if not isinstance(raw, dict):
        raise ChainError(
            f"{what}: 'occupancy' must be a JSON object of "
            "{'<strand>.<ord>': '<helix>@<offset>' | null}"
        )
    domains = _domain_by_key(list(getattr(tree, "domains", []) or []))
    out: dict[str, str | None] = {}
    for key, target in raw.items():
        key_s = str(key).strip()
        domain = domains.get(key_s)
        if domain is None:
            strand = key_s.rsplit(".", 1)[0] if "." in key_s else key_s
            node = tree.blocks.get(strand)
            if node is None or chain_role(node) != STRAND_ROLE:
                raise ChainError(
                    f"{what}: occupancy key {key_s!r} names no strand block — "
                    "keys are '<strand>.<ord>' over declared strands"
                )
            have = sorted(k for k in domains if k.startswith(strand + "."))
            raise ChainError(
                f"{what}: occupancy key {key_s!r} names no domain of strand "
                f"{strand!r} — its rows are {', '.join(have) or 'none'} (add_domain)"
            )
        if target is None:
            out[key_s] = None
            continue
        helix, offset = parse_target(target, what=what)
        node = tree.blocks.get(helix)
        if node is None or chain_role(node) != HELIX_ROLE:
            raise ChainError(
                f"{what}: occupancy for {key_s!r} names helix {helix!r}, which "
                f"{'does not exist' if node is None else 'is not a helix'}"
            )
        units = (
            n_units.get(helix)
            if n_units is not None
            else _int_or_none((node.chain or {}).get("n_units"))
        )
        if units is not None and offset + domain.n_units > units:
            raise ChainError(
                f"{what}: occupancy for {key_s!r} puts a {domain.n_units}-unit "
                f"domain at {helix}@{offset}, past the helix's {units} units"
            )
        out[key_s] = f"{helix}@{offset}"
    return out


def vet_guard(tree: Any, raw: Any, *, what: str) -> dict[str, str]:
    """Vet a transition's ratchet guard — ``{'<strand>.<ord>': 'bound' |
    'free' | '<helix>@<offset>'}``, a predicate over the FROM state's
    occupancy (se-walker-light-protocol: a leg may only lift while the
    other is bound). Same key vocabulary as :func:`vet_occupancy`; a
    target names an existing helix. Returns the normalised map."""
    if not isinstance(raw, dict) or not raw:
        raise ChainError(
            f"{what}: 'guard' must be a non-empty JSON object of "
            "{'<strand>.<ord>': 'bound' | 'free' | '<helix>@<offset>'}"
        )
    domains = _domain_by_key(list(getattr(tree, "domains", []) or []))
    out: dict[str, str] = {}
    for key, want in raw.items():
        key_s = str(key).strip()
        if key_s not in domains:
            raise ChainError(
                f"{what}: guard key {key_s!r} names no domain row — keys are "
                f"'<strand>.<ord>' over declared strands "
                f"({', '.join(sorted(domains)) or 'none declared'})"
            )
        if isinstance(want, str) and want.strip().lower() in (GUARD_BOUND, GUARD_FREE):
            out[key_s] = want.strip().lower()
            continue
        helix, offset = parse_target(want, what=what)
        node = tree.blocks.get(helix)
        if node is None or chain_role(node) != HELIX_ROLE:
            raise ChainError(
                f"{what}: guard for {key_s!r} names helix {helix!r}, which "
                f"{'does not exist' if node is None else 'is not a helix'}"
            )
        out[key_s] = f"{helix}@{offset}"
    return out


def effective_target(
    domain: DomainSpec, occupancy: dict[str, str | None] | None
) -> str | None:
    """Where a domain row sits in a state: the state's own entry when it
    names the row (``None`` = free), else the authored row's
    ``<helix>@<start>``."""
    key = f"{domain.strand}.{domain.ord}"
    if occupancy and key in occupancy:
        return occupancy[key]
    return f"{domain.helix}@{domain.start}"


def guard_violations(
    guard: dict[str, str],
    occupancy: dict[str, str | None] | None,
    domains: list[DomainSpec],
) -> list[str]:
    """Every guard entry the state's occupancy fails, as readable
    clauses (``'lb.0 is f1@4, guard wants free'``); empty = the guard
    holds."""
    by_key = _domain_by_key(domains)
    out: list[str] = []
    for key, want in guard.items():
        domain = by_key.get(key)
        if domain is None:
            out.append(f"{key}: no such domain row")
            continue
        actual = effective_target(domain, occupancy)
        if want == GUARD_BOUND:
            ok = actual is not None
        elif want == GUARD_FREE:
            ok = actual is None
        else:
            ok = actual == want
        if not ok:
            out.append(f"{key} is {actual or 'free'}, guard wants {want}")
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_occupancy(
    domains: list[DomainSpec], occupancy: dict[str, str | None] | None
) -> list[DomainSpec]:
    """A copy of ``domains`` with ``occupancy`` applied: an overridden
    row moves to its target helix/offset (same length, same direction,
    its stored loop curve dropped — a moved route no longer has the curve
    that was settled for it), a ``None`` row is marked :attr:`DomainSpec.free`.
    Rows the map does not name are copied unchanged; a key naming no row
    is ignored here (vetting is :func:`vet_occupancy`'s, at write time).
    ``None``/empty → an unchanged copy, so callers need no branch."""
    if not occupancy:
        return [dataclasses.replace(d) for d in domains]
    out: list[DomainSpec] = []
    for d in domains:
        key = f"{d.strand}.{d.ord}"
        if key not in occupancy:
            out.append(dataclasses.replace(d))
            continue
        target = occupancy[key]
        if target is None:
            out.append(dataclasses.replace(d, free=True, loop_curve=None))
            continue
        helix, offset = parse_target(target, what="occupancy")
        out.append(
            dataclasses.replace(
                d,
                helix=helix,
                start=offset,
                end=offset + d.n_units,
                loop_curve=None,
            )
        )
    return out
