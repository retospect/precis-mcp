"""Opt-in, pure post-topology termination preparation; no public writer.

Complete carbon graphs and independently supplied source identities are required.
Unsaved candidates use ordinal artifact identities, never fictitious database rows.
Record validation recomputes evidence; a producer string or trusted flag is not a
cap-removal warrant. Public generate/join/rebuild/finish paths do not call this
module. Persistence and supported exposure acquisition remain a separate slice.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

import numpy as np

from hexfold.build import Net
from hexfold.report import Profile
from precis.structure.completion_codec import canonical_number, dumps_wire, loads_wire
from precis.structure.completion_identity import (
    exact_token,
    geometry_snapshot,
    stored_graph_token,
)
from precis.structure.completion_types import ArtifactIdentity, GeometrySnapshot
from precis_se.atomic.generators.hexfold_spec import _terminate_open_edges

_Mode = Literal["H", "ports-open", "none"]
_MODES = ("H", "ports-open", "none")
_POLICY = {
    "id": "post-join-exposed-H",
    "revision": 1,
    "source": "precis_se.atomic.termination:_resolve_termination_scope",
}


class _TerminationError(ValueError):
    """Unsupported or contradictory preparation; inputs are never modified."""


@dataclass(frozen=True)
class _Request:
    # None means legacy request shape UNKNOWN; False means positively omitted.
    present: bool | None
    mode: _Mode | None


@dataclass(frozen=True)
class _Exposure:
    topology_token: str
    eligible_hosts: frozenset[int]
    reserved_hosts: frozenset[int]
    reserved_ports: tuple[str, ...]
    # Complete consumed-rim ordinals from the Net, not invented released ports.
    consumed_rims: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class _Scope:
    requested: _Request
    effective: _Mode
    parent_modes: tuple[_Mode, _Mode]
    exposure: _Exposure


@dataclass(frozen=True)
class _Cap:
    h: int
    host: int
    bond: int  # ordinal in the candidate bond sequence; never a DB row ID


@dataclass(frozen=True)
class _StoredCap:
    h: int
    host: int
    h_row_id: int
    host_row_id: int
    bond_row_id: int


@dataclass(frozen=True)
class _Diagnostics:
    h_c_A: float
    h_h_A: float
    method: str = "cap-pairs-euclidean-strict-less-than"
    revision: int = 1
    exclusions: str = "covalent-host-only"


@dataclass(frozen=True)
class _CarbonSource:
    coordinates: tuple[tuple[float, float, float], ...]
    bonds: tuple[tuple[int, int, float, str], ...]


@dataclass(frozen=True)
class _ParentMap:
    source_token: str
    atoms: tuple[int, ...]  # source carbon ordinal -> candidate carbon ordinal
    translation_A: tuple[float, float, float]


@dataclass(frozen=True)
class _Prepared:
    # Tuples prevent a caller from mutating the evidence through numpy aliases.
    coordinates: tuple[tuple[float, float, float], ...]
    elements: tuple[str, ...]
    hybridizations: tuple[str, ...]
    bonds: tuple[tuple[int, int, float, str], ...]
    caps: tuple[_Cap, ...]
    contacts: tuple[tuple[int, int, float], ...]
    scope: _Scope
    precap: ArtifactIdentity
    final: ArtifactIdentity
    diagnostics: _Diagnostics


@dataclass(frozen=True)
class _Parent:
    """Private verifier result, not a deserialized metadata/trust object.

    Stored bindings are minted only from a validated complete snapshot compared to
    an independently captured expected identity. Artifact parents are minted only
    by validating their whole preparation against verified parents recursively.
    """

    effective: _Mode
    requested: _Request
    binding: dict[str, Any]
    caps: tuple[_Cap | _StoredCap, ...]
    carbon: _CarbonSource
    history: str | None = None  # validated lossless record, not a mutable mapping


def _mode(value: Any) -> _Mode:
    if type(value) is not str or value not in _MODES:
        raise _TerminationError("unknown termination mode")
    return cast(_Mode, value)


def _request(value: _Request, *, legacy: bool = False) -> None:
    if value.present is None and value.mode is None and legacy:
        return
    if type(value.present) is not bool:
        raise _TerminationError("request shape unavailable")
    if value.present:
        _mode(value.mode)
    elif value.mode is not None:
        raise _TerminationError("omitted request carries a mode")


def _topology_token(net: Net) -> str:
    """No coords, DB identifiers, policy, or token fields in this preimage."""
    return exact_token(
        "hexfold.termination.topology",
        {
            "atoms": [(a.ord, str(a.path), a.element, a.hyb) for a in net.atoms],
            "bonds": net.bonds,
            "rings": net.rings,
            "ports": [(name, asdict(port)) for name, port in net.ports],
            "consumed_rims": net.consumed_rims,
            "sigma_CH_A": canonical_number(net.lattice.sigma_CH_A),
        },
    )


def _carbon_graph(net: Net) -> tuple[int, ...]:
    n = len(net.atoms)
    if not n or any(
        a.ord != i or a.element != "C" or a.hyb != "sp2"
        for i, a in enumerate(net.atoms)
    ):
        raise _TerminationError("complete supported carbon sp2 graph required")
    degree = [0] * n
    seen: set[tuple[int, int]] = set()
    for i, j, order in net.bonds:
        if (
            type(i) is not int
            or type(j) is not int
            or not 0 <= i < n
            or not 0 <= j < n
            or i == j
            or type(order) is not int
            or order != 1
        ):
            raise _TerminationError("unsupported carbon bond")
        pair = (min(i, j), max(i, j))
        if pair in seen:
            raise _TerminationError("duplicate carbon bond")
        seen.add(pair)
        degree[i] += 1
        degree[j] += 1
    if any(d < 1 or d > 3 for d in degree):
        raise _TerminationError("unsupported carbon coordination/deficit")
    for ring in net.rings:
        if len(ring) < 3 or len(set(ring)) != len(ring):
            raise _TerminationError("malformed ring")
        if any(
            tuple(sorted((a, b))) not in seen for a, b in zip(ring, ring[1:] + ring[:1])
        ):
            raise _TerminationError("ring not supported by graph")
    return tuple(degree)


def _resolve_termination_scope(
    net: Net,
    *,
    requested: _Request,
    parent_modes: tuple[_Mode | None, _Mode | None],
    exposure: _Exposure | None,
) -> _Scope:
    """Revision-one deterministic policy; no public exposure inference/release."""
    _request(requested)
    degree = _carbon_graph(net)
    if exposure is None or exposure.topology_token != _topology_token(net):
        raise _TerminationError("missing or stale exposure intent")
    if len(parent_modes) != 2:
        raise _TerminationError("two verified parent modes required")
    a, b = (_mode(m) for m in parent_modes)
    if a != b:
        raise _TerminationError("conflicting parent modes")
    mode = _mode(requested.mode) if requested.present else a
    if (a == "none" and mode != "none") or (a == "ports-open" and mode == "H"):
        raise _TerminationError("request overrides parent constraint")
    eligible, reserved = exposure.eligible_hosts, exposure.reserved_hosts
    if any(type(i) is not int or not 0 <= i < len(degree) for i in eligible | reserved):
        raise _TerminationError("invalid exposure host")
    if eligible & reserved or eligible | reserved != {
        i for i, d in enumerate(degree) if d < 3
    }:
        raise _TerminationError("exposure must partition ALL deficient hosts")
    ports = dict(net.ports)
    if len(ports) != len(net.ports) or set(exposure.reserved_ports) != set(ports):
        raise _TerminationError("all retained ports must remain explicitly reserved")
    if len(set(exposure.reserved_ports)) != len(exposure.reserved_ports):
        raise _TerminationError("duplicate reserved port")
    for port in ports.values():
        rim = port.dangling or port.atoms
        if any(type(i) is not int or not 0 <= i < len(degree) for i in rim):
            raise _TerminationError("invalid port host")
        if not {i for i in rim if degree[i] < 3} <= reserved:
            raise _TerminationError("retained port exposed without authority")
    if exposure.consumed_rims != net.consumed_rims:
        raise _TerminationError("consumed boundary identity mismatch")
    if any(
        type(i) is not int or not 0 <= i < len(degree)
        for rim in net.consumed_rims
        for i in rim
    ):
        raise _TerminationError("invalid consumed host")
    return _Scope(requested, mode, (a, b), exposure)


def _artifact(
    net: Net,
    xyz: tuple[tuple[float, float, float], ...],
    elements: tuple[str, ...],
    hybridizations: tuple[str, ...],
    bonds: tuple[tuple[int, int, float, str], ...],
) -> ArtifactIdentity:
    # No physical cell is invented. This is a finite Cartesian construction
    # artifact, not a saved GeometrySnapshot or a periodic system.
    frame = {
        "coordinates": "structure-local-cartesian",
        "units": "angstrom",
        "cell": None,
        "pbc": [False] * 3,
    }
    atoms = [
        {
            "ordinal": i,
            "element": el,
            "hybridization": hyb,
            "position": [canonical_number(v) for v in p],
        }
        for i, (el, hyb, p) in enumerate(
            zip(elements, hybridizations, xyz, strict=True)
        )
    ]
    edges = [
        {"ordinal": k, "i": i, "j": j, "order": canonical_number(order), "kind": kind}
        for k, (i, j, order, kind) in enumerate(bonds)
    ]
    preimage = {
        "frame": frame,
        "atoms": atoms,
        "bonds": edges,
        "topology": _topology_token(net),
    }
    digest = exact_token("hexfold.termination.artifact", preimage)
    return {
        "artifact_id": digest,
        "snapshot_codec": "hexfold.termination.candidate:v1",
        "snapshot_digest": digest,
        "atom_identity_digest": exact_token("hexfold.termination.atoms", atoms),
        "graph_digest": exact_token(
            "hexfold.termination.graph", {"atoms": atoms, "edges": edges}
        ),
        "cell_digest": exact_token("hexfold.termination.frame", frame),
        "natoms": len(atoms),
    }


def _finalize_termination(
    net: Net,
    coordinates: np.ndarray,
    *,
    scope: _Scope,
    profile: Profile = Profile.DEFAULT,
) -> _Prepared:
    """Prepare caps after topology/preview, retaining every diagnostic contact."""
    diagnostics = _Diagnostics(profile.clash_bar("H", "C"), profile.clash_bar("H", "H"))
    if any(
        isinstance(v, bool) or not np.isfinite(v) or v <= 0
        for v in (diagnostics.h_c_A, diagnostics.h_h_A)
    ):
        raise _TerminationError("invalid diagnostic thresholds")
    checked = _resolve_termination_scope(
        net,
        requested=scope.requested,
        parent_modes=scope.parent_modes,
        exposure=scope.exposure,
    )
    if checked != scope:
        raise _TerminationError("forged effective scope")
    xyz = np.array(coordinates, dtype=np.float64, copy=True)
    if xyz.shape != (len(net.atoms), 3) or not np.isfinite(xyz).all():
        raise _TerminationError("invalid complete coordinates")
    if not np.isfinite(net.lattice.sigma_CH_A) or net.lattice.sigma_CH_A <= 0:
        raise _TerminationError("invalid C-H length")
    for i, j, _ in net.bonds:
        if float(np.linalg.norm(xyz[i] - xyz[j])) <= 1e-12:
            raise _TerminationError("zero-length bonded neighbours")
    elements = tuple(a.element for a in net.atoms)
    hybs = tuple(a.hyb for a in net.atoms)
    bonds = tuple((i, j, 4.0 / 3.0, "aromatic") for i, j, _ in net.bonds)
    before_xyz = tuple(tuple(float(v) for v in row) for row in xyz)
    before_xyz = cast(tuple[tuple[float, float, float], ...], before_xyz)
    precap = _artifact(net, before_xyz, elements, hybs, bonds)
    hosts, points = _terminate_open_edges(
        net, xyz, scope.effective, eligible_hosts=scope.exposure.eligible_hosts
    )
    degree = _carbon_graph(net)
    expected = (
        Counter({i: 3 - degree[i] for i in scope.exposure.eligible_hosts})
        if scope.effective != "none"
        else Counter()
    )
    if Counter(hosts) != expected:
        raise _TerminationError("placement did not satisfy eligible valences")
    n, nb = len(elements), len(bonds)
    caps = tuple(_Cap(n + k, host, nb + k) for k, host in enumerate(hosts))
    if points:
        xyz = np.vstack((xyz, points))
    if not np.isfinite(xyz).all():
        raise _TerminationError("nonfinite cap placement")
    for cap in caps:
        if not np.isclose(
            np.linalg.norm(xyz[cap.h] - xyz[cap.host]),
            net.lattice.sigma_CH_A,
            # Relative numerical precision, independent of coordinate units.
            rtol=1e-9,
            atol=0.0,
        ):
            raise _TerminationError("degenerate cap direction")
    elements += ("H",) * len(caps)
    hybs += ("s",) * len(caps)
    bonds += tuple((cap.host, cap.h, 1.0, "pairwise") for cap in caps)
    contacts: list[tuple[int, int, float]] = []
    # Only the actual covalent host is excluded; two-hop pairs remain visible.
    for cap in caps:
        for other in range(cap.h):
            if other == cap.host:
                continue
            distance = float(np.linalg.norm(xyz[cap.h] - xyz[other]))
            if distance < profile.clash_bar("H", elements[other]):
                contacts.append((cap.h, other, distance))
    final_xyz = cast(
        tuple[tuple[float, float, float], ...],
        tuple(tuple(float(v) for v in row) for row in xyz),
    )
    return _Prepared(
        final_xyz,
        elements,
        hybs,
        bonds,
        caps,
        tuple(contacts),
        scope,
        precap,
        _artifact(net, final_xyz, elements, hybs, bonds),
        diagnostics,
    )


def _captured_snapshot(snapshot: GeometrySnapshot) -> GeometrySnapshot:
    """Re-validate content, not a caller's supplied identity token."""
    identity = snapshot["identity"]
    ref_id = identity["ref_id"]
    raw = {
        "ref_id": ref_id,
        "version": identity["version"],
        "lattice": snapshot["lattice"],
        "pbc": snapshot["pbc"],
        "atoms": [
            {
                "id": a["row_id"],
                "ref_id": ref_id,
                "label": a["label"],
                "element": a["element"],
                "fa": a["fractional"][0],
                "fb": a["fractional"][1],
                "fc": a["fractional"][2],
                "fixed": a["fixed_mask"],
                "magmom": a["magmom"],
                "oxidation": a["oxidation"],
                "charge": a["charge"],
                "hybridization": a["hybridization"],
            }
            for a in snapshot["atoms"]
        ],
        "bonds": [
            {
                "id": b["row_id"],
                "ref_id": ref_id,
                "i": b["i"],
                "j": b["j"],
                "image": b["image"],
                "bond_order": b["order"],
                "kind": b["kind"],
                "provenance": b["provenance"],
            }
            for b in snapshot["bonds"]
        ],
    }
    captured = geometry_snapshot(raw)
    if captured["identity"] != identity:
        raise _TerminationError("snapshot token does not bind its rows")
    return captured


def _verify_generated_caps(
    snapshot: GeometrySnapshot,
    *,
    expected: Mapping[str, Any],
    bare: Net,
    generated: Mapping[str, Any],
) -> _Parent:
    """Verify a legacy hexfold-origin graph; request shape stays unknown.

    `expected` and `bare` must be independently captured/reconstructed inputs, not
    fields read back from the untrusted generated record. The helper performs no
    builder, store access or removal. Authored H in `bare` is unsupported.
    """
    degree = _carbon_graph(bare)
    captured = _captured_snapshot(snapshot)
    binding = {
        "kind": "stored",
        "geometry": captured["identity"],
        "graph_token": stored_graph_token(captured),
    }
    if exact_token("hexfold.termination.source", binding) != exact_token(
        "hexfold.termination.source", expected
    ) or any(captured["pbc"]):
        raise _TerminationError("source identity/PBC mismatch")
    # Supported provenance is checked against the independent bare spec and all
    # actual incidence. A matching generator string alone authorizes nothing.
    from hexfold.text import parse

    spec = generated.get("spec")
    if (
        generated.get("generator") != "hexfold"
        or not isinstance(spec, str)
        or parse(spec) != bare.spec
    ):
        raise _TerminationError("unsupported or unmatched source producer/spec")
    term = generated.get("terminated")
    if not isinstance(term, dict):
        raise _TerminationError("unknown H provenance")
    mode = _mode(term.get("mode"))
    count = term.get("count")
    if type(count) is not int or count < 0 or term.get("element") != "H":
        raise _TerminationError("invalid cap count/element")
    declared_hosts = term.get("hosts")
    if not isinstance(declared_hosts, list) or any(
        type(host) is not int for host in declared_hosts
    ):
        raise _TerminationError("invalid cap host ordinals")
    atoms, bonds = captured["atoms"], captured["bonds"]
    n = len(bare.atoms)
    if (
        len(atoms) != n + count
        or [a["element"] for a in atoms] != ["C"] * n + ["H"] * count
    ):
        raise _TerminationError("cap tail/element sequence mismatch")
    if [a["hybridization"] for a in atoms] != ["sp2"] * n + ["s"] * count:
        raise _TerminationError("unsupported species hybridization")
    row_to_ord = {a["row_id"]: i for i, a in enumerate(atoms)}
    carbon: set[tuple[int, int]] = set()
    incidence: dict[int, list[tuple[int, int]]] = {i: [] for i in range(n, len(atoms))}
    seen: set[tuple[int, int]] = set()
    for bond in bonds:
        i, j = row_to_ord[bond["i"]], row_to_ord[bond["j"]]
        pair = (min(i, j), max(i, j))
        if i == j or pair in seen or any(bond["image"]):
            raise _TerminationError("duplicate/self/image bond")
        seen.add(pair)
        if i < n and j < n:
            if bond["kind"] != "aromatic" or canonical_number(
                bond["order"]
            ) != canonical_number(4.0 / 3.0):
                raise _TerminationError("noncap bond metadata mismatch")
            carbon.add(pair)
        else:
            if (i >= n and j >= n) or bond["kind"] != "pairwise" or bond["order"] != 1:
                raise _TerminationError("invalid C-H incidence")
            host, h = (i, j) if i < n else (j, i)
            incidence[h].append((host, bond["row_id"]))
    if carbon != {tuple(sorted((i, j))) for i, j, _ in bare.bonds}:
        raise _TerminationError("noncap graph mismatch")
    if any(len(links) != 1 for links in incidence.values()):
        raise _TerminationError("H must have exactly one host")
    caps = tuple(
        _StoredCap(
            h,
            links[0][0],
            atoms[h]["row_id"],
            atoms[links[0][0]]["row_id"],
            links[0][1],
        )
        for h, links in incidence.items()
    )
    port_hosts = {i for _, p in bare.ports for i in (p.dangling or p.atoms)}
    expected_hosts = (
        Counter(
            {
                i: 3 - deg
                for i, deg in enumerate(degree)
                if deg < 3 and not (mode == "ports-open" and i in port_hosts)
            }
        )
        if mode != "none"
        else Counter()
    )
    hosts = Counter(cap.host for cap in caps)
    ordered_hosts = tuple(
        i for i, count in sorted(expected_hosts.items()) for _ in range(count)
    )
    if (
        hosts != expected_hosts
        or term.get("hosts") != sorted(hosts)
        or tuple(cap.host for cap in caps) != ordered_hosts
    ):
        raise _TerminationError("cap multiplicity/unique-host projection mismatch")
    if canonical_number(term.get("bond_A")) != canonical_number(
        bare.lattice.sigma_CH_A
    ):
        raise _TerminationError("cap lattice provenance mismatch")
    # Captured cells are row lattices. Bind the actual source coordinates, not
    # a regenerated seed, so mappings cannot attach a genuine hash to another net.
    xyz = np.asarray([a["fractional"] for a in atoms[:n]], dtype=float) @ np.asarray(
        captured["lattice"], dtype=float
    )
    source = _CarbonSource(
        cast(
            tuple[tuple[float, float, float], ...],
            tuple(tuple(float(v) for v in row) for row in xyz),
        ),
        tuple((i, j, 4.0 / 3.0, "aromatic") for i, j, _ in bare.bonds),
    )
    return _Parent(mode, _Request(None, None), binding, caps, source)


def _source_token(parent: _Parent) -> str:
    return exact_token(
        "hexfold.termination.parent-source",
        {"binding": parent.binding, "carbon": asdict(parent.carbon)},
    )


def _candidate_carbon(prepared: _Prepared) -> _CarbonSource:
    n = prepared.precap["natoms"]
    return _CarbonSource(
        prepared.coordinates[:n],
        tuple(b for b in prepared.bonds if b[0] < n and b[1] < n),
    )


def _checked_lineage(
    prepared: _Prepared,
    parents: tuple[_Parent, _Parent],
    mapping: tuple[_ParentMap, _ParentMap] | None,
) -> dict[str, Any]:
    """Only translated disjoint union is supported; no seam/fuse/cut inference.

    Each carbon and carbon bond must have exactly one source. Cap removal has
    already been verified by the parent verifier. Added/deleted/merged carbon
    atoms or bonds, rotations, deformations and unbound ancestry are refused.
    """
    if mapping is None or len(parents) != 2 or len(mapping) != 2:
        raise _TerminationError("checked source mapping required")
    candidate = _candidate_carbon(prepared)
    covered: set[int] = set()
    edges: set[tuple[int, int, float, str]] = set()
    for parent, m in zip(parents, mapping, strict=True):
        source = parent.carbon
        if m.source_token != _source_token(parent) or len(m.atoms) != len(
            source.coordinates
        ):
            raise _TerminationError("mapping source binding mismatch")
        if len(m.translation_A) != 3 or not np.isfinite(m.translation_A).all():
            raise _TerminationError("unsupported source transform")
        for i, j in enumerate(m.atoms):
            if (
                type(j) is not int
                or j in covered
                or not 0 <= j < len(candidate.coordinates)
            ):
                raise _TerminationError("mapping must cover candidate bijectively")
            translated = tuple(
                float(a + b)
                for a, b in zip(source.coordinates[i], m.translation_A, strict=True)
            )
            if translated != candidate.coordinates[j]:
                raise _TerminationError("mapped source coordinates differ")
            covered.add(j)
        for i, j, order, kind in source.bonds:
            a, b = m.atoms[i], m.atoms[j]
            edges.add((min(a, b), max(a, b), order, kind))
    expected = {
        (min(i, j), max(i, j), order, kind) for i, j, order, kind in candidate.bonds
    }
    if covered != set(range(len(candidate.coordinates))) or edges != expected:
        raise _TerminationError("unsupported carbon topology change")
    return {
        "method": "translated-disjoint-union",
        "revision": 1,
        "coordinate_arithmetic": "float64-row-lattice-then-translation",
        "allowed_carbon_topology_change": "none",
        "parents": [asdict(m) for m in mapping],
    }


def _encode_preparation(
    prepared: _Prepared,
    *,
    parents: tuple[_Parent, _Parent],
    mapping: tuple[_ParentMap, _ParentMap] | None = None,
) -> dict[str, Any]:
    lineage = _checked_lineage(prepared, parents, mapping)
    if tuple(p.effective for p in parents) != prepared.scope.parent_modes:
        raise _TerminationError("record parent policy mismatch")
    for parent in parents:
        _request(parent.requested, legacy=True)
    return {
        "format": 1,
        "policy": dict(_POLICY),
        "requested": asdict(prepared.scope.requested),
        "effective_mode": prepared.scope.effective,
        "lineage": lineage,
        "diagnostics": asdict(prepared.diagnostics),
        "parents": [
            {
                "binding": copy.deepcopy(p.binding),
                "effective_mode": p.effective,
                "requested": asdict(p.requested),
                "verified_caps": [asdict(cap) for cap in p.caps],
                "history": None if p.history is None else loads_wire(p.history),
            }
            for p in parents
        ],
        "boundary": {
            "topology_token": prepared.scope.exposure.topology_token,
            "eligible_hosts": sorted(prepared.scope.exposure.eligible_hosts),
            "reserved_hosts": sorted(prepared.scope.exposure.reserved_hosts),
            "reserved_ports": list(prepared.scope.exposure.reserved_ports),
            "consumed_rims": [list(r) for r in prepared.scope.exposure.consumed_rims],
        },
        "precap_artifact": dict(prepared.precap),
        "final_artifact": dict(prepared.final),
        "ordered_caps": [asdict(cap) for cap in prepared.caps],
        "host_multiplicity": [
            [host, count]
            for host, count in sorted(Counter(c.host for c in prepared.caps).items())
        ],
        "count": len(prepared.caps),
        "contacts_A": [list(c) for c in prepared.contacts],
        "qualification": None,
    }


def _replay_preparation(
    encoded: str,
    *,
    net: Net,
    coordinates: np.ndarray,
    parents: tuple[_Parent, _Parent],
    exposure: _Exposure | None,
    mapping: tuple[_ParentMap, _ParentMap] | None = None,
    profile: Profile = Profile.DEFAULT,
) -> tuple[_Prepared, _Parent]:
    """Pure revision-one replay against independent complete inputs.

    No registry/adoption or producer flag is accepted. The expected record is
    rederived from actual inputs; extra keys, altered bindings and inherited force
    claims are rejected. Returned artifact parent can support a subsequent layer,
    but never qualifies saved geometry as complete.
    """
    record = loads_wire(encoded)
    if not isinstance(record, dict) or record.get("policy") != _POLICY:
        raise _TerminationError("unsupported record/policy revision")
    request = record.get("requested")
    if not isinstance(request, dict) or set(request) != {"present", "mode"}:
        raise _TerminationError("missing exact request")
    scope = _resolve_termination_scope(
        net,
        requested=_Request(request["present"], request["mode"]),
        parent_modes=(parents[0].effective, parents[1].effective),
        exposure=exposure,
    )
    diagnostic = record.get("diagnostics")
    if not isinstance(diagnostic, dict) or set(diagnostic) != set(
        asdict(_Diagnostics(1.0, 1.0))
    ):
        raise _TerminationError("missing exact diagnostic policy")
    prepared = _finalize_termination(net, coordinates, scope=scope, profile=profile)
    expected = _encode_preparation(prepared, parents=parents, mapping=mapping)
    canonical = dumps_wire(expected)
    if dumps_wire(record) != canonical:
        raise _TerminationError("replay evidence does not match complete inputs")
    parent = _Parent(
        scope.effective,
        scope.requested,
        {"kind": "artifact", "artifact": dict(prepared.final)},
        prepared.caps,
        _candidate_carbon(prepared),
        canonical,
    )
    return prepared, parent
