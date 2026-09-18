"""The op walker that knows about the store-aware ops.

``precis_se.ops.apply_ops`` is pure by design — it never reads or writes
the store — but a handful of ops need it: ``bind_structure``/
``unbind_structure``/``generate`` (the atomic mode's 3, the source
``structure`` design has to exist / a generated one has to be minted) and
``realize`` (se-print-implementer.md — mints a ``cad`` design, the first
non-atomic op to create another kind's ref). So they are intercepted
*here*, before ``apply_ops`` ever sees them, and ``put``/``edit`` walk
their ops list through this function instead: the ``import_fragment``
precedent (``precis.handlers.structure::_apply_ops_with_imports``),
transferred from ``precis_nm.handler._apply_ops_with_bindings`` by the
nm→se merge (docs/backlog/nm-se-merge.md) and reused by ``realize``
rather than duplicated, since the store-write-deferred shape
(:mod:`precis_se.atomic.generate`'s "orphan on partial failure" reasoning)
is identical for both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis_se.atomic.bind import bind_structure, unbind_structure
from precis_se.atomic.generate import PendingGenerate, finish_generate, prepare_generate
from precis_se.atomic.vocab import check_dof_axis_ports
from precis_se.ops import OpError, SeTree, apply_ops, known_ops
from precis_se.realize import PendingRealize, finish_realize, prepare_realize
from precis_se.simp_bridge import SimpRequest, prepare_simp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: ``realize`` strategies. ``analytic`` (the default) is the envelope seed
#: (:mod:`precis_se.realize`); ``simp`` is the enqueued topology solve
#: (:mod:`precis_se.simp_bridge`).
REALIZE_STRATEGIES = ("analytic", "simp")

#: The store-aware ops intercepted in :func:`apply_ops_with_atomic` — they
#: never reach :func:`precis_se.ops.apply_ops`, so
#: :func:`precis_se.ops.known_ops` (the pure ops table) can't see them on
#: its own. Named here, once, as the single source (with ``known_ops()``)
#: both the real dispatch below and the unknown-op error's roster read from
#: (gripe 334767: the roster used to come from ``known_ops()`` alone,
#: silently omitting these from what was actually accepted).
HANDLER_LEVEL_OPS = ("bind_structure", "unbind_structure", "generate", "realize")


def all_op_names() -> frozenset[str]:
    """Every op name ``put``/``edit`` accept — the pure table
    (:func:`precis_se.ops.known_ops`) unioned with
    :data:`HANDLER_LEVEL_OPS`. One source for both the real dispatch and
    every unknown-op error's roster, so the two can never drift."""
    return known_ops() | set(HANDLER_LEVEL_OPS)


def apply_ops_with_atomic(
    store: Store,
    tree: SeTree,
    ops: list[dict[str, Any]],
    *,
    design_slug: str,
    pending_jobs: list[SimpRequest] | None = None,
) -> str | None:
    """Walk ``ops`` in order, applying the store-aware ops here (the
    atomic mode's 3 plus ``realize``) and everything else through the
    ordinary :func:`precis_se.ops.apply_ops`, one op at a time — so a
    ``bind_structure``/``generate``/``realize`` sharing a call with an
    earlier ``add_block``/``add_port`` sees exactly what that op already
    placed. ``design_slug`` (the se design's own slug) names every
    ``generate``-minted structure design and every ``realize``-minted cad
    design — threaded through rather than read off the handler because
    ``put`` knows it before the ref exists (a fresh design has no row to
    read it back from).

    **``generate``/``realize`` are store-write-deferred, everything else
    isn't.** ``bind_structure``/``unbind_structure`` only ever mutate the
    in-memory ``tree`` (their store use is read-only), so a later op's
    failure never strands a partial write from either — the caller's own
    ``persist.save_tree`` is the only commit either is part of.
    ``generate``/``realize`` are different: each mints a brand-new design
    of another kind (``structure`` / ``cad``) and its own
    ``structure_save``/``cad_save`` commits on its own, so both run in two
    halves (:mod:`precis_se.atomic.generate`'s module docstring,
    :mod:`precis_se.realize`'s) and every deferred write only happens in
    the loop below, after the entire ops list has validated with no
    exception.

    **Unknown-op roster** (gripe 334767): every op name is checked up
    front against :func:`all_op_names`, the same union the loop below
    dispatches through, so an op name matching neither group is rejected
    here, by name, with the FULL roster — rather than falling through to
    ``apply_ops``'s own ``OpError``, whose roster only ever sees the pure
    table.

    **``add_block``'s ``dof`` axis_ports check is deferred** (gripe
    334765): :func:`precis_se.ops._op_add_block` vets a ``dof``'s SHAPE
    eagerly and rolls the block back out on failure, but a just-minted
    block never has any ports of its own yet — any ``add_port`` for it
    necessarily comes *later* in the same list — so the PORT-existence
    half (:func:`precis_se.atomic.vocab.check_dof_axis_ports`) can only run
    once the whole list has been walked. The check after the main loop
    skips a block a *later* op already removed or ``clear_dof``'d (the same
    "never re-validate something a later op already undid" rule
    ``finish_generate`` follows for its own deferred half).

    **``realize(strategy='simp')`` is validated here and enqueued by the
    caller** (docs/backlog/structural-solution-space.md slice 4 bridge):
    :func:`precis_se.simp_bridge.prepare_simp` runs the op's whole
    refusal set against the in-memory tree, and the resulting
    :class:`~precis_se.simp_bridge.SimpRequest` is appended to
    ``pending_jobs`` — the handler enqueues one ``se_simp`` job per entry
    *after* its own ``save_tree`` commits, so the job's ``load_tree`` sees
    exactly the tree this call wrote (a job enqueued mid-walk could run
    against the previous save). The block itself is untouched until the
    job binds it. A caller that passes no ``pending_jobs`` list refuses
    the strategy rather than dropping the request on the floor.

    Returns a compact echo of every atomic store-aware op (for the caller's
    response), or ``None`` when there were none."""
    roster = all_op_names()
    echoes: list[str] = []
    pending_generates: list[PendingGenerate] = []
    pending_realizes: list[PendingRealize] = []
    pending_dof_checks: list[str] = []
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            raise BadInput(f"op missing 'op' key: {op!r}")
        name = op["op"]
        if name not in roster:
            raise BadInput(f"unknown op: {name!r}; known: {', '.join(sorted(roster))}")
        if name == "bind_structure":
            echoes.append(bind_structure(store, tree, op))
            continue
        if name == "unbind_structure":
            echoes.append(unbind_structure(tree, op))
            continue
        if name == "generate":
            echo, pending = prepare_generate(store, tree, op, design_slug)
            echoes.append(echo)
            if pending is not None:  # dry-run blocks mint nothing
                pending_generates.append(pending)
            continue
        if name == "realize":
            strategy = str(op.get("strategy") or "analytic").strip().lower()
            if strategy not in REALIZE_STRATEGIES:
                raise BadInput(
                    f"realize: unknown strategy {strategy!r}; known: "
                    f"{', '.join(REALIZE_STRATEGIES)}"
                )
            if strategy == "simp":
                if pending_jobs is None:
                    raise BadInput(
                        "realize(strategy='simp') needs a caller that can enqueue "
                        "the se_simp job (put/edit on kind='se')"
                    )
                try:
                    echo, request = prepare_simp(tree, op)
                except OpError as exc:
                    raise BadInput(str(exc)) from exc
                echoes.append(echo)
                pending_jobs.append(request)
                continue
            try:
                echo, realize_pending = prepare_realize(store, tree, op, design_slug)
            except OpError as exc:
                raise BadInput(str(exc)) from exc
            echoes.append(echo)
            pending_realizes.append(realize_pending)
            continue
        try:
            apply_ops(tree, [op])
        except OpError as exc:
            raise BadInput(str(exc)) from exc
        if name == "add_block" and op.get("dof") is not None:
            block_name = str(op.get("name") or "").strip()
            if block_name:
                pending_dof_checks.append(block_name)
    for pending in pending_generates:
        finish_generate(store, tree, pending)
    for realize_pending in pending_realizes:
        finish_realize(store, tree, realize_pending)
    for block_name in pending_dof_checks:
        node = tree.blocks.get(block_name)
        if node is None or node.dof is None:
            continue  # a later op removed the block or cleared its dof
        try:
            check_dof_axis_ports(node, node.dof, block_name, what="add_block")
        except OpError as exc:
            raise BadInput(str(exc)) from exc
    return "\n".join(echoes) if echoes else None
