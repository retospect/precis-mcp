"""Exact current geometry identity; independent of rounded compute-cache keys.

No scene hydration/default lattice or chemistry inference. A malformed graph
cannot support a completion token; independent atom rendering is a different
consumer and need not discard otherwise drawable coordinates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from .completion_codec import CodecError, canonical_number, to_wire
from .completion_types import GeometryIdentity, GeometrySnapshot


class InvalidGeometry(ValueError):
    """Missing, contradictory or unsupported exact geometry data."""


def integer(value: Any, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise InvalidGeometry("invalid integer identity")
    return value


def string(value: Any) -> str:
    if type(value) is not str or not value:
        raise InvalidGeometry("missing exact string")
    return value


def vector(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidGeometry("vector requires three finite numbers")
    try:
        return [canonical_number(x) for x in value]
    except CodecError as exc:
        raise InvalidGeometry(str(exc)) from exc


def exact_token(domain: str, value: Any) -> str:
    """Canonical token encoding is independent of the smaller wire budget.

    Wrap mappings to distinguish user tag-shaped metadata from scalar tags.
    Geometry reads may contain thousands of atoms while the completion wire
    envelope contains their identity only. Keep a separate finite node budget.
    """
    count = 0

    def canonical(item: Any, depth: int = 0) -> Any:
        nonlocal count
        count += 1
        if count > 2_000_000 or depth > 64:
            raise CodecError("identity canonicalization exceeds bounded read budget")
        if isinstance(item, Mapping):
            if any(type(key) is not str for key in item):
                raise CodecError("identity keys must be strings")
            return {
                "$object": {
                    key: canonical(item[key], depth + 1) for key in sorted(item)
                }
            }
        if isinstance(item, (list, tuple)):
            return [canonical(v, depth + 1) for v in item]
        return to_wire(item)  # shared exact scalar rules and digit limits

    text = json.dumps(
        canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return (
        f"precis.{domain}:v1:sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def identity(value: Any) -> GeometryIdentity:
    if not isinstance(value, dict) or set(value) != {
        "format",
        "ref_id",
        "version",
        "token",
    }:
        raise InvalidGeometry("invalid geometry identity shape")
    if integer(value["format"]) != 1:
        raise InvalidGeometry("unsupported geometry identity format")
    integer(value["ref_id"], minimum=1)
    if value["version"] is not None:
        integer(value["version"], minimum=0)
    string(value["token"])
    return cast(GeometryIdentity, dict(value))


def geometry_snapshot(raw: Mapping[str, Any]) -> GeometrySnapshot:
    """Validate all exact fields before issuing the domain/versioned token."""
    ref_id = integer(raw.get("ref_id"), minimum=1)
    version = raw.get("version")
    if version is not None:
        integer(version, minimum=0)
    lattice = raw.get("lattice")
    if not isinstance(lattice, (list, tuple)) or len(lattice) != 3:
        raise InvalidGeometry("missing or malformed cell")
    lattice = [vector(row) for row in lattice]
    a, b, c = lattice
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    if determinant == 0:
        raise InvalidGeometry("singular cell")
    pbc = raw.get("pbc")
    if (
        not isinstance(pbc, (list, tuple))
        or len(pbc) != 3
        or any(type(x) is not bool for x in pbc)
    ):
        raise InvalidGeometry("missing or malformed PBC")
    atoms: list[dict[str, Any]] = []
    bonds: list[dict[str, Any]] = []
    ids: set[int] = set()
    labels: set[str] = set()
    if not isinstance(raw.get("atoms"), list) or not isinstance(raw.get("bonds"), list):
        raise InvalidGeometry("missing atom or bond inventory")
    for atom in raw["atoms"]:
        if not isinstance(atom, dict):
            raise InvalidGeometry("malformed atom")
        aid = integer(atom.get("id"), minimum=1)
        label = string(atom.get("label"))
        if aid in ids or label in labels or atom.get("ref_id") != ref_id:
            raise InvalidGeometry("duplicate or foreign atom")
        ids.add(aid)
        labels.add(label)
        fixed = integer(atom.get("fixed"), minimum=0)
        if fixed > 7:
            raise InvalidGeometry("invalid fixed-axis mask")
        charge = atom.get("charge")
        if charge is not None:
            integer(charge)
        intent = atom.get("hybridization")
        if intent is not None:
            string(intent)
        atoms.append(
            {
                "row_id": aid,
                "label": label,
                "element": string(atom.get("element")),
                "fractional": vector([atom.get(k) for k in ("fa", "fb", "fc")]),
                "fixed_mask": fixed,
                "magmom": None
                if atom.get("magmom") is None
                else canonical_number(atom["magmom"]),
                "oxidation": None
                if atom.get("oxidation") is None
                else canonical_number(atom["oxidation"]),
                "charge": charge,
                "hybridization": intent,
            }
        )
    bond_ids: set[int] = set()
    for bond in raw["bonds"]:
        if not isinstance(bond, dict):
            raise InvalidGeometry("malformed bond")
        bid = integer(bond.get("id"), minimum=1)
        i, j = integer(bond.get("i"), minimum=1), integer(bond.get("j"), minimum=1)
        if (
            bid in bond_ids
            or i not in ids
            or j not in ids
            or bond.get("ref_id") != ref_id
        ):
            raise InvalidGeometry("duplicate, foreign or dangling bond")
        image = bond.get("image")
        if not isinstance(image, (tuple, list)) or len(image) != 3:
            raise InvalidGeometry("malformed periodic image")
        image = [integer(x) for x in image]
        if any(image[k] and not pbc[k] for k in range(3)):
            raise InvalidGeometry("image on nonperiodic axis")
        bond_ids.add(bid)
        order = canonical_number(bond.get("bond_order"))
        if order < 0:
            raise InvalidGeometry("negative bond order")
        bonds.append(
            {
                "row_id": bid,
                "i": i,
                "j": j,
                "image": image,
                "order": order,
                "kind": string(bond.get("kind")),
                "provenance": string(bond.get("provenance")),
            }
        )
    atoms.sort(key=lambda atom: atom["row_id"])
    bonds.sort(key=lambda bond: bond["row_id"])
    content = {
        "format": 1,
        "ref_id": ref_id,
        "version": version,
        "lattice": lattice,
        "pbc": list(pbc),
        "atoms": atoms,
        "bonds": bonds,
    }
    token = exact_token("structure.geometry", content)
    return cast(
        GeometrySnapshot,
        {
            "identity": {
                "format": 1,
                "ref_id": ref_id,
                "version": version,
                "token": token,
            },
            "source": "live-statement",
            "lattice": lattice,
            "pbc": list(pbc),
            "atoms": atoms,
            "bonds": bonds,
        },
    )


def stored_graph_token(snapshot: GeometrySnapshot) -> str:
    """Exact stored graph basis; no inferred edges or circular token fields."""
    return exact_token(
        "structure.graph",
        {
            "geometry": snapshot["identity"],
            "source": "stored",
            "complete": True,
            "edges": snapshot["bonds"],
            "comparison_source": None,
            "comparison_source_graph_token": None,
            "atom_mapping_digest": None,
            "mapped_atom_count": None,
            "mapping_proof": None,
            "periodic_mapping": None,
            "algorithm": None,
            "algorithm_revision": None,
            "dependency_digest": None,
            "parameters": {},
        },
    )
