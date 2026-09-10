"""The se side of force-density form-finding — the ``formfind`` op
(docs/backlog/structural-solution-space.md build order slice 2).

This module is the bridge between the design vocabulary and the pure
solver: it reads the tree's **axial subgraph** (the same members
:mod:`precis_se.stability` analyses), turns roles into tension-positive
force densities (tie ``+1``, strut ``−1``, rod ``+1`` by default —
``q_tie``/``q_strut``/``q_rod`` override the defaults, a ``q=`` list of
``{'a', 'b', 'q'}`` entries overrides per member), takes anchors from
``objectives.fixed`` (per axis, the stability vocabulary), and calls
:func:`precis.structsolve.form_find`. Solved poses are written back
stamped ``origin: 'proposed'`` — solver output is a proposal, never the
user's contract.

Which nodes may move is the contract question, answered explicitly:

- by default only nodes whose pose is **already** ``proposed`` move — a
  user-origin pose (the default; absent stamp) is contract and is never
  overwritten silently;
- ``move=[...names...]`` (or ``move='all'``) is the design author's
  explicit authorization to relocate named nodes regardless of stamp;
- a fully fixed node (all three axes in ``objectives.fixed``) is an
  anchor and never moves — listing one in ``move`` is rejected rather
  than ignored.

Loud-beats-silent posture throughout: an axial member whose role is
undeclared (no capacity pair) has no honest default sign, so it needs an
explicit ``q`` entry; a solve whose q ratios collapse nodes onto each
other is refused with the members named, not written back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.structsolve import FormFindError, form_find
from precis_se import stability as se_stability
from precis_se.ops import (
    ConnectSpec,
    OpError,
    SeTree,
    _connects_endpoint_pair,
    _split_endpoint,
)

#: Role → the op key holding its default force density. ``undeclared``
#: is deliberately absent: a member without a capacity pair has no
#: honest default sign — the op demands an explicit per-member ``q``.
_ROLE_DEFAULT_KEYS = {"tie": "q_tie", "strut": "q_strut", "rod": "q_rod"}
_ROLE_DEFAULTS = {"q_tie": 1.0, "q_strut": -1.0, "q_rod": 1.0}

#: Sign each role's force density must respect (tension-positive):
#: +1 = must be > 0, −1 = must be < 0, 0 = any nonzero.
_ROLE_SIGN = {"tie": 1, "strut": -1, "rod": 0, "undeclared": 0}

_ALLOWED_KEYS = {"op", "q", "q_tie", "q_strut", "q_rod", "move"}

#: A solved member shorter than this fraction of the structure's extent
#: is a collapse — the q ratios put its endpoints on top of each other.
_COLLAPSE_RTOL = 1e-9


@dataclass
class _Member:
    """One axial member as the solve sees it — the live connect plus its
    role. Kept structural (the connect's own endpoint fields), never
    re-parsed out of a rendered subject string."""

    conn: ConnectSpec
    role: str

    @property
    def subject(self) -> str:
        c = self.conn
        return f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"

    @property
    def pair(self) -> frozenset[tuple[str, str]]:
        c = self.conn
        return _connects_endpoint_pair(c.a_block, c.a_port, c.b_block, c.b_port)


def _vet_density(value: Any, *, what: str, sign: int) -> float:
    try:
        q = float(value)
    except (TypeError, ValueError) as exc:
        raise OpError(f"formfind: {what} must be a number, got {value!r}") from exc
    if not math.isfinite(q) or q == 0.0:
        raise OpError(f"formfind: {what} must be finite and nonzero, got {value!r}")
    if sign > 0 and q <= 0.0:
        raise OpError(
            f"formfind: {what} must be > 0 — a tie is tension-only "
            "(tension-positive force density)"
        )
    if sign < 0 and q >= 0.0:
        raise OpError(
            f"formfind: {what} must be < 0 — a strut is compression-only "
            "(tension-positive force density)"
        )
    return q


def _live_members(tree: SeTree) -> list[_Member]:
    """The axial members the solve runs over. Unlike stability's checker
    (where coincident endpoints kill the line of action), coincident or
    unset endpoint poses are fine here — the method ignores free nodes'
    input coordinates — so only the structurally malformed rows reject:
    a self-loop or a dangling endpoint contributes a wrong Laplacian, and
    silently excluding it would form-find a different structure than the
    one stored."""
    members: list[_Member] = []
    bad: list[str] = []
    for conn, _params, role in se_stability.axial_connects(tree):
        member = _Member(conn=conn, role=role)
        if (
            conn.a_block == conn.b_block
            or conn.a_block not in tree.blocks
            or conn.b_block not in tree.blocks
        ):
            bad.append(member.subject)
        members.append(member)
    if bad:
        raise OpError(
            "formfind: axial member(s) with no usable line of action — "
            f"{'; '.join(bad)} — disconnect or repair them first"
        )
    return members


def _densities(op: dict[str, Any], members: list[_Member]) -> list[float]:
    defaults = {
        key: _vet_density(
            op.get(key, fallback),
            what=f"'{key}'",
            sign=_ROLE_SIGN[key.removeprefix("q_")],
        )
        for key, fallback in _ROLE_DEFAULTS.items()
    }
    overrides: dict[frozenset[tuple[str, str]], float] = {}
    raw = op.get("q")
    if raw is not None:
        if not isinstance(raw, list):
            raise OpError(
                "formfind 'q' must be a list of {'a': 'block.port', "
                "'b': 'block.port', 'q': <number>} member overrides"
            )
        pairs = {m.pair: m for m in members}
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("a") or not entry.get("b"):
                raise OpError(
                    f"formfind 'q' entry must be {{'a', 'b', 'q'}}, got {entry!r}"
                )
            a_block, a_port = _split_endpoint(entry["a"], "formfind q 'a'")
            b_block, b_port = _split_endpoint(entry["b"], "formfind q 'b'")
            pair = _connects_endpoint_pair(a_block, a_port, b_block, b_port)
            member = pairs.get(pair)
            if member is None:
                roster = ", ".join(m.subject for m in members) or "(none)"
                raise OpError(
                    f"formfind 'q' names no live axial member between "
                    f"{entry['a']!r} and {entry['b']!r}. Axial members: {roster}"
                )
            if pair in overrides:
                # two entries for one member is an ambiguity, and
                # last-wins would resolve it silently (the connect/
                # add_note duplicate posture).
                raise OpError(
                    f"formfind 'q' lists {member.subject} twice — one entry per member"
                )
            overrides[pair] = _vet_density(
                entry.get("q"),
                what=f"'q' for {member.subject}",
                sign=_ROLE_SIGN[member.role],
            )
    out: list[float] = []
    unsigned: list[str] = []
    for member in members:
        if member.pair in overrides:
            out.append(overrides[member.pair])
        elif member.role in _ROLE_DEFAULT_KEYS:
            out.append(defaults[_ROLE_DEFAULT_KEYS[member.role]])
        else:
            unsigned.append(member.subject)
            out.append(0.0)
    if unsigned:
        raise OpError(
            "formfind: member(s) declare no capacity pair, so their force-"
            f"density sign has no honest default — {'; '.join(unsigned)} — "
            "declare tension_capacity/compression_capacity (set_joint "
            "params) or pass an explicit q entry for each"
        )
    return out


def _fixed_axes(tree: SeTree, name: str) -> set[str]:
    """The stability module's reading of ``objectives.fixed`` — one
    parser, so the solve's anchors can never disagree with the
    checker's supports."""
    return set(se_stability._fixed_axes(tree, name))


def _movable(tree: SeTree, op: dict[str, Any], node_names: list[str]) -> set[str]:
    fully_fixed = {n for n in node_names if _fixed_axes(tree, n) == {"x", "y", "z"}}
    raw = op.get("move")
    if raw is None:
        movable = {
            n
            for n in node_names
            if n not in fully_fixed and tree.blocks[n].origins.get("pose") == "proposed"
        }
        if not movable:
            raise OpError(
                "formfind: no movable nodes — every axial node's pose is "
                "user contract (origin unset = user) or fully fixed. "
                "Authorize explicitly with move=[...block names...] or "
                "move='all', or stamp guess poses revisable "
                "(set_pose origin='proposed')"
            )
        return movable
    if raw == "all":
        movable = set(node_names) - fully_fixed
        if not movable:
            raise OpError(
                "formfind: move='all' but every axial node is fully fixed "
                "— anchors cannot move"
            )
        return movable
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise OpError(
            f"formfind 'move' must be 'all' or a list of block names, got {raw!r}"
        )
    movable = set()
    for name in raw:
        if name not in node_names:
            roster = ", ".join(node_names)
            raise OpError(
                f"formfind 'move' names no axial node: {name!r}. Axial nodes: {roster}"
            )
        if name in fully_fixed:
            raise OpError(
                f"formfind 'move' lists {name!r}, but it is fully fixed "
                "(objectives.fixed grounds all three axes) — an anchor "
                "cannot move; unfix it first (set_load) if it should"
            )
        movable.add(name)
    if not movable:
        raise OpError("formfind 'move' is an empty list — nothing to solve")
    return movable


def op_formfind(tree: SeTree, op: dict[str, Any]) -> None:
    """Apply the ``formfind`` op (module docstring): solve, vet the
    result, write solved poses back stamped ``proposed``. Raises
    :class:`OpError` before any mutation on every refusal path — the op
    either lands whole or not at all."""
    strays = sorted(set(op) - _ALLOWED_KEYS)
    if strays:
        raise OpError(
            f"formfind: unknown key(s) {', '.join(strays)} — takes q "
            "(per-member overrides), q_tie/q_strut/q_rod (role defaults), "
            "move ('all' or block names)"
        )
    members = _live_members(tree)
    if not members:
        raise OpError(
            "formfind: no axial members — the solve runs over connects "
            "whose joint class is 'axial'"
        )
    q = _densities(op, members)
    node_names = sorted(
        {m.conn.a_block for m in members} | {m.conn.b_block for m in members}
    )
    movable = _movable(tree, op, node_names)

    index = {name: i for i, name in enumerate(node_names)}
    coords = np.array([tree.blocks[n].pose for n in node_names], dtype=float)
    axis_index = {"x": 0, "y": 1, "z": 2}
    fixed = np.zeros((len(node_names), 3), dtype=bool)
    for name in node_names:
        if name not in movable:
            fixed[index[name], :] = True
        else:
            for axis in _fixed_axes(tree, name):
                fixed[index[name], axis_index[axis]] = True
    member_idx = np.array(
        [[index[m.conn.a_block], index[m.conn.b_block]] for m in members], dtype=int
    )

    try:
        result = form_find(coords, member_idx, np.asarray(q, dtype=float), fixed)
    except FormFindError as exc:
        raise OpError(
            f"formfind: {exc} (anchors come from objectives.fixed / "
            "unmoved nodes; q ratios from q_tie/q_strut/q_rod and the "
            "q= overrides)"
        ) from exc

    extent = float(np.max(np.ptp(result.coords, axis=0)))
    collapsed = [
        members[k].subject
        for k in range(len(members))
        if result.lengths[k] <= _COLLAPSE_RTOL * extent
    ]
    if extent == 0.0 or collapsed:
        where = "; ".join(collapsed) or "every member"
        raise OpError(
            "formfind: the solve collapses member endpoint(s) onto each "
            f"other — {where} — these q ratios and anchors admit no "
            "spread geometry; adjust the strut/cable balance or the "
            "anchor positions (nothing was written back)"
        )

    move_tol = _COLLAPSE_RTOL * extent
    for name in movable:
        node = tree.blocks[name]
        new = result.coords[index[name]]
        if float(np.linalg.norm(new - np.asarray(node.pose, dtype=float))) <= move_tol:
            continue  # already at equilibrium — keep the pose AND its stamp
        node.pose = [float(v) for v in new]
        node.origins["pose"] = "proposed"
