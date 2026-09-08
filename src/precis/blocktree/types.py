"""The generic block-tree dataclasses — see the package docstring
(:mod:`precis.blocktree`) for what this spine is and why it's shared.

``Tree`` is generic over its own block/connect element types
(``TBlock``/``TConnect``) so a domain subclass (e.g. ``precis_se``'s
``SeTree``) can declare ``Tree[SeBlock, ConnectSpec]`` and have
``tree.blocks``/``tree.connects`` type-check as the domain's own
subclasses everywhere, while :mod:`precis.blocktree.ops`'s helpers still
operate over the generic base. Construction of a fresh block instance goes
through :meth:`Tree.make_block` (overridden by the domain subclass) rather
than a hardcoded ``BlockNode(...)`` call, so the 8 shared ops in
:mod:`precis.blocktree.ops` mint the *domain's* block type, not the bare
core one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class OpError(ValueError):
    """A rejected block-tree op (bad reference, unknown op, malformed
    payload) — every domain reuses this class directly; there is nothing
    domain-specific about "the op was bad"."""


#: The cross-design ``template`` qualifier separator (docs/backlog/
#: blocktree-library-build-plan.md slice 1) — ``'<design-slug>#<block-
#: name>'``. Chosen deliberately, not ``'.'``: a block name may already
#: contain a dot (``ops._split_endpoint``'s docstring — only PORT names
#: give up the dot, so the ``'block.port'`` connect syntax can split on the
#: LAST one unambiguously), so reusing it here would collide with existing,
#: already-legal block names. ``'#'`` is unclaimed, and this package
#: additionally reserves it OUT of block names at both places a name is
#: minted (``op_add_block``'s ``name``, an instance's own ``name`` in
#: ``_instance_shared``) — the same trade in the other direction: block
#: names give up ``'#'`` so :func:`parse_template_ref` can split a
#: qualified reference on the FIRST one unambiguously, leaving the design
#: *slug* half free to contain whatever a slug may otherwise contain.
TEMPLATE_SEP = "#"


def parse_template_ref(raw: str) -> tuple[str | None, str]:
    """``'<design-slug>#<block-name>'`` → ``(design_slug, block_name)``; a
    bare local reference (no :data:`TEMPLATE_SEP`) → ``(None, raw stripped)``
    — the mirror image of :func:`~precis.blocktree.ops._split_endpoint`'s
    LAST-dot split for ``'block.port'`` (safe in the opposite direction,
    since here it is BLOCK names, not the design-slug half, that give up the
    character — see :data:`TEMPLATE_SEP`'s docstring)."""
    s = str(raw).strip()
    if TEMPLATE_SEP not in s:
        return None, s
    design, _, block = s.partition(TEMPLATE_SEP)
    design, block = design.strip(), block.strip()
    if not design or not block:
        raise OpError(
            f"cross-design template ref must be "
            f"'design-slug{TEMPLATE_SEP}block-name', got {raw!r}"
        )
    return design, block


#: Resolves another design's slug to ITS live tree, for a cross-design
#: ``template`` to read through. Pure data lookup — no store coupling in
#: this package (module docstring, "no store access"): the HANDLER builds
#: the real store-backed closure (a slug→ref lookup behind ``persist.
#: load_tree``) and hands it to :attr:`Tree.foreign`, memoized ONCE per
#: put/edit/get call so resolving the same foreign design from many
#: ports/blocks/cycle-check hops in one request costs one fetch, not one
#: per caller (docs/backlog/blocktree-library-build-plan.md slice 1,
#: "cache per request"). Returns ``None`` for a slug that doesn't resolve
#: (never found, or soft-retired — the ``store.get_ref`` default of
#: treating a retired ref as absent, mirrored here) — never raises; the
#: caller decides whether that is loud (write time) or silent (read time).
ForeignResolver = Callable[[str], "Tree[Any, Any] | None"]


@dataclass
class Port:
    """A named attachment point on a block (the pcb pin→roles pattern:
    ``roles`` is a capability *set*; legal attachments are derived at
    connect/joint time from these roles, never stored as a second
    relation). ``annotations`` is the open dict a domain hangs its own
    descriptive (or, later, contract-classed) extras on — every key is
    *descriptive* until a domain gives it a checked consumer."""

    name: str
    roles: list[str] = field(default_factory=list)
    direction: list[float] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass
class Connect:
    """A port↔port intent edge between two ``'block.port'`` endpoints,
    name-keyed like everything else in this tree. ``objectives`` is the
    generic loads/requirements dict a domain vets against its own
    registered vocabulary; a domain that needs a mechanism/joint slot
    alongside this (``se``'s ``joint``, ``nm``'s ``kind``) adds it as a
    subclass field — that slot is domain-specific, not shared."""

    a_block: str
    a_port: str
    b_block: str
    b_port: str
    objectives: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockNode:
    """One block, addressed by ``name`` (the stable identity — a block is
    only ever looked up by name, never a row id, so a domain's persist
    layer can rebuild ids on every save). ``parent``/``template`` are
    block *names*. ``ports`` is keyed by port name; only an ordinary
    (non-instance) block ever has entries here — an instance's ports
    resolve from its template (:func:`~precis.blocktree.ops.
    effective_ports`). Pose/rot are plain float triples in the domain's
    own unit/angle convention — this class does not know which.

    Named ``BlockNode``, not the bare ``Block``, on purpose — ``Block`` is
    a reserved/overloaded class name outside ``precis.utils.prompt.model``
    (docs/glossary.md "Overloaded — which one?", ``tests/test_vocab_lint.
    py``'s reserved-class-name gate); ``BlockNode`` keeps the domain word
    ("block", used freely in prose and in field names like ``a_block``)
    without colliding with that homonym's one Python class slot."""

    name: str
    parent: str | None = None
    template: str | None = None
    pose: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rot: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    envelope: str | None = None
    descr: str | None = None
    use: str | None = None
    ports: dict[str, Port] = field(default_factory=dict)


@dataclass
class Tree[TBlock: BlockNode, TConnect: Connect]:
    """A design's live blocks, keyed by name, plus its live ``connects``.
    Insertion order is not significant — renderers/persisters compute
    their own (tree / topological) order from ``parent``/``template``;
    ``connects`` is an unordered list (pair identity, not position)."""

    blocks: dict[str, TBlock] = field(default_factory=dict)
    connects: list[TConnect] = field(default_factory=list)
    #: This design's own slug (docs/backlog/blocktree-library-build-plan.md
    #: slice 1) — set by the handler before any op runs (``put`` knows it
    #: from ``id=`` before the ref row even exists; ``edit``/``get`` read it
    #: off the already-loaded ref), never persisted. Needed only for
    #: cross-design instancing: it is how a foreign design's template,
    #: when IT points back at ``'<this design's slug>#<block>'``, is
    #: recognised as the same node the local walk already knows rather than
    #: a distinct one (:func:`~precis.blocktree.ops._find_instance_cycle`'s
    #: docstring). ``None`` for every caller that only ever does local
    #: instancing — unaffected, a purely local graph never needs identity
    #: beyond "this tree".
    own_slug: str | None = None
    #: See :data:`ForeignResolver`'s docstring. ``repr``/``compare``
    #: excluded — it is a closure over a store, not design data; comparing
    #: or printing two trees should never depend on which handler call
    #: built them.
    foreign: ForeignResolver | None = field(default=None, repr=False, compare=False)

    def make_block(self, **kwargs: Any) -> TBlock:
        """Construct a new block row for this tree. The base
        implementation mints a bare :class:`BlockNode`; a domain ``Tree``
        subclass overrides this to mint its own ``BlockNode`` subclass
        instead — the hook the 8 shared ops in :mod:`precis.blocktree.ops`
        use so they never hardcode a concrete block class."""
        return BlockNode(**kwargs)  # type: ignore[return-value]

    def make_connect(self, **kwargs: Any) -> TConnect:
        """Construct a new connect row for this tree — the ``Connect``
        analogue of :meth:`make_block`, for a domain whose ``connect`` op
        stays on the shared implementation (one that adds no extra
        per-connect field, unlike ``se``'s ``joint``)."""
        return Connect(**kwargs)  # type: ignore[return-value]
