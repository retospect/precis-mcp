"""Closed catalog of typed edit ops.

Each entry declares the op's schema (JSON Schema fragment), whether
it's combinatorial (i.e. can produce siblings via
``enumerate='all'``), and a hint about side effects (mutation /
batch / pure).

Ops:

- ``set_species(site, element)``
- ``substitute(equivalence_class, fraction, element, enumerate, max_siblings)``
- ``add_adsorbate(site, species, orientation)``
- ``intercalate(interstitial | species+count+mode, select)``
- ``vacancy(site)``
- ``strain(component, magnitude)``
- ``supercell(repeats)``
- ``constrain(layer | sites, kind)``
- ``displace(site, vector)``
- ``set_magmom(site, magmom)``

There is no ``script`` op in v1 (deliberately rejecting the MECo
"LLM writes Python" pattern). The op catalog covers ~95% of
catalysis edits; an escape hatch can be added later if needed.
"""

from __future__ import annotations

from typing import Any

OP_CATALOG: dict[str, dict[str, Any]] = {
    "set_species": {
        "schema": {
            "type": "object",
            "required": ["site", "element"],
            "properties": {
                "site": {"type": "integer"},
                "element": {"type": "string"},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "substitute": {
        "schema": {
            "type": "object",
            "required": ["equivalence_class", "fraction", "element"],
            "properties": {
                "equivalence_class": {"type": "string"},
                "fraction": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "element": {"type": "string"},
                "enumerate": {
                    "type": "string",
                    "enum": ["all", "first", "sqs"],
                    "default": "first",
                },
                "max_siblings": {"type": "integer", "default": 32, "maximum": 256},
            },
        },
        "combinatorial": True,
        "purity": "batch",
    },
    "add_adsorbate": {
        "schema": {
            "type": "object",
            "required": ["site", "species"],
            "properties": {
                "site": {"type": "string"},
                "species": {"type": "string"},
                "orientation": {"type": ["string", "null"]},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "intercalate": {
        "schema": {
            "type": "object",
            "properties": {
                "interstitial": {"type": "string"},
                "species": {"type": "string"},
                "count": {"type": "integer"},
                "mode": {"type": "string", "enum": ["octahedral", "tetrahedral"]},
                "select": {"type": "string", "enum": ["most_symmetric", "first"]},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "vacancy": {
        "schema": {
            "type": "object",
            "required": ["site"],
            "properties": {"site": {"type": "integer"}},
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "strain": {
        "schema": {
            "type": "object",
            "required": ["component", "magnitude"],
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["biaxial", "uniaxial-x", "uniaxial-y", "uniaxial-z"],
                },
                "magnitude": {"type": "number"},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "supercell": {
        "schema": {
            "type": "object",
            "required": ["repeats"],
            "properties": {
                "repeats": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 3,
                    "maxItems": 3,
                },
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "constrain": {
        "schema": {
            "type": "object",
            "required": ["kind"],
            "properties": {
                "layer": {"type": ["integer", "array"]},
                "sites": {"type": "array", "items": {"type": "integer"}},
                "kind": {"type": "string", "enum": ["frozen", "z_only"]},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "displace": {
        "schema": {
            "type": "object",
            "required": ["site", "vector"],
            "properties": {
                "site": {"type": "integer"},
                "vector": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
    "set_magmom": {
        "schema": {
            "type": "object",
            "required": ["site", "magmom"],
            "properties": {
                "site": {"type": "integer"},
                "magmom": {"type": "number"},
            },
        },
        "combinatorial": False,
        "purity": "mutation",
    },
}


def known_ops() -> list[str]:
    return sorted(OP_CATALOG)
