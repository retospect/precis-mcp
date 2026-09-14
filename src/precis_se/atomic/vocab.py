"""The atomic mode's own block-tree vocabulary — dof, threading, bonds.

The three pieces of L2 statement that only an atomic-mode block makes, and
that :mod:`precis_se.ops` therefore keeps out of its own body: the
**declared degree of freedom** (a rotaxane's macrocycle spins about the
axle), the **threading** invariant (it is *on* that axle, and that fact is
stored, never re-derived from coordinates), and the **bond capability
gate** (a covalent connect needs both ports to afford the role). All three
came from ``precis_nm.ops`` with their semantics untouched by the merge
(docs/backlog/nm-se-merge.md) — what changed is only where they live and
which tree class they read.

Pure vetting/gating functions over values and already-resolved nodes: no
store access (the ``ops.py`` discipline), and no mutation — every function
here either returns the canonical value to store or raises
:class:`~precis.blocktree.types.OpError`. The ops that *apply* these
verdicts stay in :mod:`precis_se.ops`, next to the other 20-odd ops, so
there is exactly one op table and one dispatch path for an agent to learn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.blocktree.types import OpError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis_se.ops import PortSpec, SeBlock

#: What an ``axis_ports``-bearing DOF's ``kind`` may be — the L2 vocabulary
#: (originally the retired ``nm`` kind's "L2", folded into ``se`` atomic
#: mode by the nm→se merge, docs/backlog/nm-se-merge.md).
DOF_KINDS = ("rotational", "translational")

#: The only keys a dof payload may carry — ``declare_dof``'s own top-level
#: ``kind=``/``axis_ports=`` op fields, or ``add_block``'s nested
#: ``dof={...}`` dict; either way, exactly these two. Anything else (gripe
#: 334765's reported ``states``/``driver``) is a loud reject via
#: :func:`vet_dof_shape`, never a silent drop.
_DOF_ALLOWED_KEYS = frozenset({"kind", "axis_ports"})

#: What a ``connect``'s ``kind`` may be when the edge *is* an atomic one. A
#: connect with no ``kind`` at all is se's ordinary structural edge (its L2
#: statement is the ``joint``), which is why this tuple has no third
#: "structural" member and why the gate below fires on ``'bond'`` exactly.
CONNECT_KINDS = ("bond", "interaction")


@dataclass
class ThreadingSpec:
    """One L2 threading invariant: ``a`` is threaded through ``b`` (e.g. a
    macrocycle ``a`` on an axle ``b``) — directional, name-keyed like
    every other cross-block reference in the tree. Stored explicitly
    (``se_topology``), never re-derived from geometry: mechanical
    interlocking is a topological fact that a pose can't lose."""

    a: str
    b: str


def connect_role(kind: str | None, objectives: dict[str, Any]) -> str | None:
    """The role a ``kind='bond'`` connect's endpoints must both afford —
    ``objectives={'role': ...}`` overrides the default ``'covalent'``.
    ``None`` for anything else: ``kind='interaction'`` (non-bonded) isn't
    capability-gated, and a connect with no ``kind`` is se's ordinary
    structural edge, which has no chemistry to gate."""
    if kind != "bond":
        return None
    role = objectives.get("role")
    return str(role).strip() if role else "covalent"


def check_bond_capability(
    a_block: str,
    a_port: str,
    a_spec: PortSpec,
    b_block: str,
    b_port: str,
    b_spec: PortSpec,
    role: str,
) -> None:
    """Both endpoints of a bond must afford ``role`` (the capability set is
    a *set*, never an equivalence relation — legal attachments are derived
    here at connect time, never stored as a second relation)."""
    role_label = "bond" if role == "covalent" else repr(role)
    for blk, prt, spec in ((a_block, a_port, a_spec), (b_block, b_port, b_spec)):
        if role not in spec.roles:
            raise OpError(
                f"connect: {blk}.{prt} does not afford {role!r} "
                f"(its roles: {spec.roles or ['(none)']}) — a {role_label} "
                "connect needs both ports to afford the role; add it via "
                "add_port, or pass objectives={'role': '<a role both ports "
                "have>'} to gate on a different one"
            )


def vet_dof_shape(dof: Any, *, what: str) -> dict[str, Any]:
    """Vet a dof payload's SHAPE — dict-ness, allowlisted keys
    (:data:`_DOF_ALLOWED_KEYS`), a valid ``kind`` (:data:`DOF_KINDS`), and
    ``axis_ports`` as a list of exactly 2 non-empty strings — the half of
    dof vetting that needs nothing but the payload itself, shared by
    ``declare_dof`` (``kind=``/``axis_ports=`` as direct op fields,
    collected into a dict before this call) and ``add_block``
    (``dof={...}``, already a dict). Returns the canonical
    ``{"kind": ..., "axis_ports": [...]}`` to store — never the raw input,
    so a stray extra key can never ride along even if a caller forgets to
    use the return value. Does NOT check that ``axis_ports`` resolve to
    real ports on any particular block — see :func:`check_dof_axis_ports`,
    the other half."""
    if not isinstance(dof, dict):
        raise OpError(f"{what} must be a JSON object, got {dof!r}")
    unknown = sorted(set(dof) - _DOF_ALLOWED_KEYS)
    if unknown:
        raise OpError(
            f"{what} has unknown key(s) {unknown}; allowed keys: "
            f"{sorted(_DOF_ALLOWED_KEYS)}"
        )
    kind = dof.get("kind")
    if kind not in DOF_KINDS:
        raise OpError(f"{what} 'kind' must be one of {DOF_KINDS}, got {kind!r}")
    axis_raw = dof.get("axis_ports")
    if (
        not isinstance(axis_raw, list)
        or len(axis_raw) != 2
        or not all(isinstance(p, str) and p.strip() for p in axis_raw)
    ):
        raise OpError(
            f"{what} needs 'axis_ports' as a list of exactly 2 port names, "
            f"got {axis_raw!r}"
        )
    return {"kind": kind, "axis_ports": [p.strip() for p in axis_raw]}


def check_dof_axis_ports(
    node: SeBlock, dof: dict[str, Any], block_name: str, *, what: str
) -> None:
    """The other half of dof vetting — every ``axis_ports`` name must
    resolve on ``node``'s *own* ports (gripe 334765's exact repro: a
    portless block accepted ``axis_ports`` naming ports that existed
    nowhere). ``declare_dof`` runs this immediately (the block's ports
    already exist by then — ordinary usage); ``add_block`` defers this same
    call to the end of the whole ops list (the handler's atomic
    interception), since a block minted by ``add_block`` never has any
    ports of its own yet at that exact moment — any ``add_port`` for it
    necessarily comes later in the same call."""
    for p in dof["axis_ports"]:
        if p not in node.ports:
            roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
            raise OpError(
                f"{what}: no such port on block {block_name!r}: {p!r}. "
                f"Available ports: {roster}"
            )
