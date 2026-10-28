"""Closed-axis vocabulary from ``data/axes/*.yaml`` (ontology-v2 slice 0d).

The categorizer passes write ``<NS>:<value>`` tags where ``NS`` is the axis
yaml's ``id`` uppercased — ``material.yaml`` -> ``MATERIAL:mof``. Until now
``_CLOSED_VOCAB`` was a hand-maintained copy of a few of those namespaces,
so any axis not duplicated there was refused at the agent boundary
(``search(tags=['MATERIAL:mof'])`` -> "unknown closed-prefix axis",
gr504808). The registry now reads the axis files directly at import: a new
``data/axes/<id>.yaml`` registers its namespace automatically.

Two tiers:

* **value-checked** — the yaml ``values:`` list becomes the axis's closed
  vocabulary (merged with any code-side entry, e.g. ``TAPROOT``). Unknown
  values refuse with close matches.
* **declared** — :data:`DECLARED_AXES`: namespaces the corpus carries but
  whose values are dynamic (pass version markers, leases) or orphaned
  (``SEV``, ``NOTE`` — no live writer). Any value parses; the axis name is
  still protected against typos. Slice 6 moves the bookkeeping set to
  ``refs.meta.passes``; these rows exist so filtering on them isn't
  refused meanwhile.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

_AXES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "data" / "axes"


@dataclass(frozen=True, slots=True)
class AxisDef:
    """One ``data/axes/<id>.yaml``: namespace, closed values, level and the
    kinds the pass writes them on (``applies_to_kinds ∪ opt_in_kinds``,
    ``["paper"]`` when the file says neither). ``ref_kinds`` whitelists
    the namespace as a *ref-level* tag on those kinds even when the axis
    itself is chunk-level — ``junk.yaml`` (gr520915) is the one caller:
    a ``JUNK:junk`` verdict on a paper ref is the "don't surface this
    paper by default" stamp the chunk gate implies at corpus level."""

    values: frozenset[str]
    level: str
    kinds: frozenset[str]
    ref_kinds: frozenset[str]


def load_categorizer_axes() -> dict[str, AxisDef]:
    """``{NAMESPACE: AxisDef}`` for every ``data/axes/*.yaml`` that carries
    ``id`` + ``values`` (``journal_domains.yaml`` is a lookup table, not an
    axis — no ``id``). Unparseable files are skipped like
    ``discover_axis_ids`` does; the registry must never break an import."""
    out: dict[str, AxisDef] = {}
    for path in sorted(_AXES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("id") or not data.get("values"):
            continue
        kinds = set(data.get("applies_to_kinds") or ["paper"])
        kinds |= set(data.get("opt_in_kinds") or [])
        out[str(data["id"]).upper()] = AxisDef(
            values=frozenset(str(v) for v in data["values"]),
            level=str(data.get("level") or "ref"),
            kinds=frozenset(kinds),
            ref_kinds=frozenset(str(k) for k in (data.get("ref_kinds") or [])),
        )
    return out


#: Per-axis sweep markers a pass writes next to its value tag
#: (``<NS>CASCADE:<version>`` done-marker, ``<NS>CASCADEATTEMPT`` failure
#: brake from ``workers.ref_lease.attempt_ns``) — declared, values are the
#: pass's version strings.
def marker_namespaces() -> frozenset[str]:
    return frozenset(
        f"{ns}{suffix}"
        for ns in load_categorizer_axes()
        for suffix in ("CASCADE", "CASCADEATTEMPT")
    )


#: Namespaces live on prod that no yaml describes: classifier markers from
#: the hardcoded passes (``classify_topics``' TOPICCASCADE, the chase
#: pipeline's tags, bib sweeps) and the orphaned review axes ``SEV`` /
#: ``NOTE`` (no writer in the tree; spec §6 lists SEV as registered).
DECLARED_AXES: Final[frozenset[str]] = frozenset(
    {
        "TOPICCASCADE",
        "TOPICCASCADEATTEMPT",
        "CHASETRIG",
        "BIBMARK",
        "BIBPARSEATTEMPT",
        "TAPROOT_DUE",
        "TAPROOT_REFINE_ATTEMPT",
        "SEV",
        "NOTE",
    }
    | marker_namespaces()
)
