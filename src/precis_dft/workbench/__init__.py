"""Workbench — pure-function operations on structures and drafts.

The handler shims in :mod:`precis_dft.handlers` wrap these functions
and route their results through precis-mcp's store. Keeping the
business logic here means we can unit-test the workbench end-to-end
without standing up a Postgres-backed handler.

Operations:

- :func:`register_structure` — compute the content-addressed id for
  a POSCAR; returns ``(id, ref_meta)`` that the structure handler
  inserts as a ``kind='structure'`` ref.
- :func:`fork_draft` — create a ``structure_draft`` from a frozen
  parent; assigns a uuid id, copies the parent's POSCAR, initialises
  an empty edit_log.
- :func:`apply_edit` — apply a list of typed ops to a draft's
  current POSCAR. Returns either one new POSCAR (mutation chain) or
  a list (combinatorial expansion).
- :func:`commit_draft` — promote a draft to a frozen
  ``structure:<sha>``. Returns the frozen id + meta + the
  ``derived_from`` link payload connecting child to parent.
- :func:`views_for` — run the annotator and return all views (the
  cognitive surface for the LLM).

Every function is referentially transparent: same inputs → same
outputs, no IO. The handler does the IO.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ase import Atoms

from precis_dft.annotator import VIEW_VERSION, annotate
from precis_dft.ops.apply import apply_ops
from precis_dft.structures import (
    atoms_from_poscar,
    canonical_poscar,
    sha256_of,
)

#: Prefix on the content-addressed frozen-structure id.
_FROZEN_PREFIX = "structure:"

#: Prefix on the mutable draft id.
_DRAFT_PREFIX = "draft:"


@dataclass(frozen=True)
class FrozenStructure:
    """The data the structure handler stores about a frozen structure."""

    id: str  #: ``structure:<sha>``
    poscar: str
    sha: str
    n_atoms: int
    formula: str
    composition: dict[str, int]
    dimensionality: str


@dataclass
class DraftStructure:
    """The data the structure_draft handler stores about a draft."""

    id: str  #: ``draft:<uuid>``
    poscar: str
    parent_id: str  #: Frozen structure this draft was forked from.
    edit_log: list[dict[str, Any]] = field(default_factory=list)
    view_version: int = 0
    views_pending: bool = True

    def append_edits(self, ops: list[dict[str, Any]], msg: str | None = None) -> None:
        """Record one edit-log entry."""
        self.edit_log.append({"ops": list(ops), "msg": msg})


@dataclass(frozen=True)
class CommitResult:
    """Result of promoting a draft to a frozen structure.

    The handler:
    1. Inserts ``frozen`` as a new ``kind='structure'`` ref (or
       returns the existing one if the sha already exists).
    2. Writes the ``derived_from`` link with ``ops_payload`` as the
       link meta so the derivation tree is queryable.
    3. Soft-deletes (or just orphans) the draft.
    """

    frozen: FrozenStructure
    ops_payload: dict[str, Any]


# ── Register a frozen structure ────────────────────────────────────


def register_structure(poscar: str) -> FrozenStructure:
    """Compute the canonical id + summary for a POSCAR.

    The handler calls this on ``put(kind='structure', poscar=...)``
    to dedupe by sha before inserting. Two identical POSCARs
    produce the same FrozenStructure and the handler returns the
    existing ref.
    """
    atoms = atoms_from_poscar(poscar)
    # Re-emit canonical POSCAR so the hash is stable across input
    # formatting quirks (different spacing, etc.).
    canon = canonical_poscar(atoms)
    sha = sha256_of(canon)
    composition = _composition(atoms)
    return FrozenStructure(
        id=_FROZEN_PREFIX + sha,
        poscar=canon,
        sha=sha,
        n_atoms=len(atoms),
        formula=atoms.get_chemical_formula(),
        composition=composition,
        dimensionality=_dimensionality(atoms),
    )


def register_atoms(atoms: Atoms) -> FrozenStructure:
    """Convenience overload — caller already has ASE Atoms in hand."""
    canon = canonical_poscar(atoms)
    return register_structure(canon)


# ── Fork a draft from a frozen parent ──────────────────────────────


def fork_draft(parent: FrozenStructure) -> DraftStructure:
    """Create a fresh draft starting from ``parent``.

    The draft id is a uuid hex so two forks of the same parent are
    distinguishable. The draft starts with the parent's POSCAR
    verbatim; subsequent edits mutate it.
    """
    draft_id = _DRAFT_PREFIX + uuid.uuid4().hex
    return DraftStructure(
        id=draft_id,
        poscar=parent.poscar,
        parent_id=parent.id,
        edit_log=[],
        view_version=0,
        views_pending=True,
    )


# ── Apply edits to a draft ─────────────────────────────────────────


def apply_edit(
    draft: DraftStructure,
    ops: list[dict[str, Any]],
    *,
    msg: str | None = None,
) -> DraftStructure | list[DraftStructure]:
    """Apply ``ops`` to ``draft``'s current POSCAR.

    Returns a single mutated DraftStructure for mutation chains, or
    a list of sibling drafts for a combinatorial op
    (``substitute(..., enumerate='all')``). Sibling drafts each get
    a fresh uuid id and share the parent_id of the original draft;
    the handler links them back to the original draft via the
    edit_log payload.

    Pre/post invariants:
    - The draft's edit_log is appended to with ``{ops, msg}``.
    - ``view_version`` is bumped.
    - ``views_pending`` is set to True (annotator should re-run).
    """
    atoms = atoms_from_poscar(draft.poscar)
    result = apply_ops(atoms, ops)
    if isinstance(result, list):
        siblings: list[DraftStructure] = []
        for child_atoms in result:
            siblings.append(
                DraftStructure(
                    id=_DRAFT_PREFIX + uuid.uuid4().hex,
                    poscar=canonical_poscar(child_atoms),
                    parent_id=draft.parent_id,
                    edit_log=[*draft.edit_log, {"ops": ops, "msg": msg}],
                    view_version=draft.view_version + 1,
                    views_pending=True,
                )
            )
        return siblings
    # Single mutation — mutate in place.
    draft.poscar = canonical_poscar(result)
    draft.append_edits(ops, msg)
    draft.view_version += 1
    draft.views_pending = True
    return draft


# ── Commit a draft to a frozen structure ───────────────────────────


def commit_draft(draft: DraftStructure, *, msg: str | None = None) -> CommitResult:
    """Promote ``draft`` to a content-addressed frozen structure.

    Returns:
    - ``frozen``: the FrozenStructure to insert/dedupe in the store
    - ``ops_payload``: the ``derived_from`` link meta. Schema::

        {
          "ops":          [<every op from draft.edit_log>],
          "messages":     [<each commit message in order>],
          "view_version": int,
          "msg":          str | None  (this commit's message)
        }

    The handler writes ``link(from=frozen.id, to=draft.parent_id,
    rel='derived_from', meta=ops_payload)`` so the derivation tree
    is walkable in both directions.
    """
    frozen = register_structure(draft.poscar)
    flat_ops: list[dict[str, Any]] = []
    messages: list[str] = []
    for entry in draft.edit_log:
        flat_ops.extend(entry.get("ops", []))
        if entry.get("msg"):
            messages.append(str(entry["msg"]))
    ops_payload = {
        "ops": flat_ops,
        "messages": messages,
        "view_version": draft.view_version,
        "msg": msg,
    }
    return CommitResult(frozen=frozen, ops_payload=ops_payload)


# ── Compute views (called by the view_worker) ──────────────────────


def views_for(poscar: str) -> dict[str, Any]:
    """Compute the full annotator view dict for a POSCAR.

    Returns the same shape :func:`precis_dft.annotator.annotate`
    produces, with ``view_version`` set to the annotator's current
    algorithm version.
    """
    atoms = atoms_from_poscar(poscar)
    out = annotate(atoms)
    out["view_version"] = VIEW_VERSION
    return out


def view_toc(poscar: str) -> dict[str, Any]:
    """Compute a compact TOC for a POSCAR.

    Same shape as ``views_for`` minus the heavy adjacency graph and
    the per-atom site list. Useful for the LLM's first read where
    the full graph is overkill.
    """
    full = views_for(poscar)
    return {
        "header": full["header"],
        "special_sites": full["special_sites"],
        "symmetry": full["symmetry"],
        "warnings": full["warnings"],
        "ascii_top": full["ascii_top"],
        "ascii_side": full["ascii_side"],
        "n_sites": len(full["sites"]),
        "view_version": full["view_version"],
    }


# ── Small helpers ─────────────────────────────────────────────────


def _composition(atoms: Atoms) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(atoms.get_chemical_symbols()))


def _dimensionality(atoms: Atoms) -> str:
    from precis_dft.annotator.header import detect_dimensionality

    return detect_dimensionality(atoms)


__all__ = [
    "CommitResult",
    "DraftStructure",
    "FrozenStructure",
    "apply_edit",
    "commit_draft",
    "fork_draft",
    "register_atoms",
    "register_structure",
    "view_toc",
    "views_for",
]
