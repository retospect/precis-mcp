"""The atomic mode's own block-tree vocabulary — dof, threading, bonds.

The three pieces of L2 statement that only an atomic-mode block makes, and
that :mod:`precis_se.ops` therefore keeps out of its own body: the
**declared degree of freedom** (a rotaxane's macrocycle spins about the
axle), the **threading** invariant (it is *on* that axle, and that fact is
stored, never re-derived from coordinates), and the **bond capability
gate** (a covalent connect needs both ports to afford the role; a joining
chemistry with two *halves* — azide ↔ alkyne for CuAAC — needs one port to
afford each, see :data:`COMPLEMENTARY_ROLES`). All three came from
``precis_nm.ops`` with their semantics untouched by the merge
(docs/backlog/nm-se-merge.md) — what changed is only where they live and
which tree class they read; the complementary-halves rule is blocktree
slice 3 (docs/backlog/blocktree-library-build-plan.md), layered on top.

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


#: Complementary role halves — blocktree slice 3. Each pair is two
#: *senses* of one joining: a port affording the left half may bond only to
#: a port affording the right half, never to another left. The vocabulary
#: is the face-code alphabet of docs/backlog/nm-face-codes-and-scale.md
#: ("complementarity is elementwise: donor↔acceptor, bump↔hole, +↔−",
#: ASCII ``-`` here) plus the click-chemistry halves; ports reuse it rather
#: than minting a parallel one. Role strings match exactly, like every
#: other role. Declared intent only — a label on a port, never a claim
#: that the chemistry works. Adding a joining is one tuple here (and, when
#: it has a name of its own, one entry in :data:`JOINING_HALVES`).
COMPLEMENTARY_ROLES: tuple[tuple[str, str], ...] = (
    ("azide", "alkyne"),
    ("donor", "acceptor"),
    ("bump", "hole"),
    ("+", "-"),
)

#: A joining chemistry's name standing for its pair of halves, so a bond
#: may gate on the chemistry (``objectives={'role': 'CuAAC'}``) as well as
#: on either half (``'azide'``); all three resolve to the same pair via
#: :func:`role_halves`.
JOINING_HALVES: dict[str, tuple[str, str]] = {"CuAAC": ("azide", "alkyne")}

_COMPLEMENT_OF: dict[str, str] = {
    **{a: b for a, b in COMPLEMENTARY_ROLES},
    **{b: a for a, b in COMPLEMENTARY_ROLES},
}


def role_halves(role: str) -> tuple[str, str] | None:
    """``(half_a, half_b)`` when ``role`` is one half of a complementary
    pair or a joining name standing for one; ``None`` for a *symmetric*
    role (``'covalent'``, ``'pi_stack'``, anything unlisted), which both
    endpoints must afford as before. A half resolves to ``(itself, its
    complement)``."""
    named = JOINING_HALVES.get(role)
    if named is not None:
        return named
    other = _COMPLEMENT_OF.get(role)
    if other is not None:
        return (role, other)
    return None


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


def bond_capability_offences(
    a_block: str,
    a_port: str,
    a_spec: PortSpec,
    b_block: str,
    b_port: str,
    b_spec: PortSpec,
    role: str,
) -> list[str]:
    """Why a ``kind='bond'`` edge between these two ports would violate its
    ``role`` — one plain clause per offence (per endpoint that falls short;
    the same-half collision is one clause naming both), each citing the
    port's *actual* roles so the fix is legible; empty when the bond is
    allowed.
    The one rule both the write-time gate (:func:`check_bond_capability`)
    and the stored-data re-check (``port_capability`` in
    :mod:`precis_se.atomic.validate`) apply, so the two can never drift.

    A **symmetric** role (:func:`role_halves` → ``None``): both endpoints
    must afford it. A **complementary** role: one endpoint must afford each
    half, in either order — azide + alkyne bonds, azide + azide is refused
    naming both. Never chemistry proof: it compares labels."""
    ends = ((a_block, a_port, a_spec), (b_block, b_port, b_spec))
    halves = role_halves(role)
    if halves is None:
        return [
            f"{blk}.{prt} affords {spec.roles or ['(none)']}, missing {role!r}"
            for blk, prt, spec in ends
            if role not in spec.roles
        ]
    h1, h2 = halves
    a_has = {h for h in halves if h in a_spec.roles}
    b_has = {h for h in halves if h in b_spec.roles}
    if (h1 in a_has and h2 in b_has) or (h2 in a_has and h1 in b_has):
        return []
    if a_has and b_has:  # each carries exactly one half, and it's the same one
        (same,) = a_has
        return [
            f"{a_block}.{a_port} and {b_block}.{b_port} both afford {same!r} "
            f"({a_block}.{a_port}: {a_spec.roles}; {b_block}.{b_port}: "
            f"{b_spec.roles}) — {role!r} needs complementary halves "
            f"({h1!r} ↔ {h2!r}), never two of the same"
        ]
    offences: list[str] = []
    for (blk, prt, spec), has, other_has in (
        (ends[0], a_has, b_has),
        (ends[1], b_has, a_has),
    ):
        if has:
            continue
        needed = (
            " | ".join(repr(_COMPLEMENT_OF[h]) for h in sorted(other_has))
            if other_has
            else f"one of {h1!r} | {h2!r}"
        )
        offences.append(
            f"{blk}.{prt} affords {spec.roles or ['(none)']}, missing {needed}"
        )
    return offences


def check_bond_capability(
    a_block: str,
    a_port: str,
    a_spec: PortSpec,
    b_block: str,
    b_port: str,
    b_spec: PortSpec,
    role: str,
) -> None:
    """Refuse a bond that :func:`bond_capability_offences` finds fault with
    (the capability set is a *set*, never an equivalence relation — legal
    attachments are derived here at connect time, never stored as a second
    relation). The refusal names the offending ports' actual roles and the
    two ways to fix it."""
    offences = bond_capability_offences(
        a_block, a_port, a_spec, b_block, b_port, b_spec, role
    )
    if not offences:
        return
    role_label = "bond" if role == "covalent" else repr(role)
    halves = role_halves(role)
    if halves is None:
        fix = (
            "both ports to afford the role; add it via add_port, or pass "
            "objectives={'role': '<a role both ports have>'} to gate on a "
            "different one"
        )
    else:
        fix = (
            f"one port to afford {halves[0]!r} and the other {halves[1]!r}; "
            "give a port the missing half via add_port (a port may carry "
            "both), or pass objectives={'role': ...} to gate on a different "
            "role"
        )
    raise OpError(
        f"connect: {'; '.join(offences)} — a {role_label} connect needs {fix}"
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
