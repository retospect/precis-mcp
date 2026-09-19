"""The generic block-tree dataclasses — see the package docstring
(:mod:`precis.blocktree`) for what this spine is and why it's shared.

``Tree`` is generic over its own block/connect element types
(``TBlock``/``TConnect``) so a domain subclass (e.g. ``precis_se``'s
``SeTree``) can declare ``Tree[SeBlock, ConnectSpec]`` and have
``tree.blocks``/``tree.connects`` type-check as the domain's own
subclasses everywhere, while :mod:`precis.blocktree.ops`'s helpers still
operate over the generic base. Construction of a fresh block instance goes
through :meth:`Tree.make_block` (overridden by the domain subclass) rather
than a hardcoded ``BlockNode(...)`` call, so the 9 shared ops in
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

#: The other prefix a block token may open with, and so the other one block
#: NAMES are reserved against (:func:`~precis.blocktree.ops.
#: _reject_reserved_name`): a domain with a stable per-block uid reads
#: ``'uid:41'`` — and ``'#41'``, covered by :data:`TEMPLATE_SEP` already —
#: as that uid before it looks for any label (``precis_se.identity``), so a
#: block allowed to wear one as its name could never be addressed again.
#: Declared here beside the separator it mirrors rather than in the domain,
#: because the reservation has to hold at the two places this package mints
#: a name.
UID_PREFIX = "uid:"


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


#: Where a port's :attr:`Port.pose` — or, independently,
#: :attr:`Port.rot` (:attr:`Port.rot_source`) — came from. A CLOSED enum
#: (``None`` = the port carries no pose/rot at all). ``'declared'`` is
#: design intent: an agent said where on the block this attachment point
#: sits, or how it's oriented. ``'bound'`` is measurement: the
#: displacement (or frame) read back off a realized structure the block
#: binds. The distinction is load-bearing for the consumers — a declared
#: value is a target to check a realization against, a bound one is what
#: the realization actually did — so it is stored, not inferred. A domain
#: that persists ports mirrors this enum as a DB CHECK (``se``'s
#: ``se_ports_pose_source_check``). The core ops write only ``'declared'``
#: — a measurement comes from a domain that knows what a realization *is*
#: (``se``'s ``bind_structure`` reads it off the bound structure's atom),
#: and by its rule a measurement fills an empty slot but never overwrites
#: a declared target. ``pose_source`` and ``rot_source`` are two
#: INDEPENDENT stamps off this same enum (R1, docs/backlog/
#: port-rotation-and-lever-composition.md) — a port's pose and its frame
#: can each be declared or measured on their own schedule; there is no
#: single "the port's provenance".
PORT_POSE_SOURCES: tuple[str, ...] = ("declared", "bound")


@dataclass
class Port:
    """A named attachment point on a block (the pcb pin→roles pattern:
    ``roles`` is a capability *set*; legal attachments are derived at
    connect/joint time from these roles, never stored as a second
    relation). ``annotations`` is the open dict a domain hangs its own
    descriptive (or, later, contract-classed) extras on — every key is
    *descriptive* until a domain gives it a checked consumer.

    ``pose``/``rot`` are the port's OWN placement in its block's local
    frame — the same shape (origin + Euler triple) and the same units as
    the owning :class:`BlockNode`'s, which this class likewise does not
    know the name of. Both are **nullable**, and that is the normal case:
    at box level the exact displacement from the block's origin to its
    attachment point is genuinely unknown, and a made-up number would be
    indistinguishable from a measured one. Every geometry consumer
    therefore keeps a fallback — ``se``'s bond-length check projects the
    block's envelope extent instead — and says in its output *which* of
    the two it used, so "≈" is never mistaken for a port-to-port distance.

    Invariants, enforced by the ops (and mirrored as DB CHECKs by a domain
    that persists ports): ``rot`` requires ``pose`` (a rotation with no
    origin is meaningless), ``pose_source`` is set exactly when ``pose``
    is, and — independently — ``rot_source`` is set exactly when ``rot``
    is (R1, docs/backlog/port-rotation-and-lever-composition.md: the two
    provenance stamps track their own field, never each other — see
    :data:`PORT_POSE_SOURCES`)."""

    name: str
    roles: list[str] = field(default_factory=list)
    direction: list[float] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    pose: list[float] | None = None
    rot: list[float] | None = None
    pose_source: str | None = None
    rot_source: str | None = None


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
    #: ``True`` iff this tree was reconstructed from stored rows (set by
    #: the persist loader, nowhere else). Identity assignment keys off the
    #: tree's ORIGIN, never its shape: a persisted-origin tree mints for
    #: every uid-less node (it can only be newly added — gr339743), while
    #: a caller-built tree (full ``put``) may adopt identity by label.
    #: Deriving this from "does any node carry a uid" is wrong the moment
    #: an edit removes every uid-carrying block before re-adding
    #: same-named ones. Not design data: excluded from repr/compare.
    from_persistence: bool = field(default=False, repr=False, compare=False)

    def resolve_key(self, token: Any) -> str | None:
        """A caller-supplied block token → the key it addresses in
        :attr:`blocks`, or ``None`` when nothing answers to it.

        The identity hook every op goes through to turn what the agent
        *wrote* into the block it *means* (:func:`~precis.blocktree.ops.
        _require_block`). The base implementation is the only rule this
        package knows: the token IS the key. A domain that mints a stable
        per-block identity overrides it to accept that too —
        ``precis_se``'s ``SeTree`` reads ``'#41'``/``'uid:41'``/``41`` as a
        uid (:mod:`precis_se.identity`) and falls back to the label.

        May raise :class:`OpError` — a label two blocks answer to is
        neither a hit nor a miss, and an override says so with a
        structured subclass rather than silently picking one.
        """
        key = str(token).strip()
        return key if key in self.blocks else None

    def make_block(self, **kwargs: Any) -> TBlock:
        """Construct a new block row for this tree. The base
        implementation mints a bare :class:`BlockNode`; a domain ``Tree``
        subclass overrides this to mint its own ``BlockNode`` subclass
        instead — the hook the 9 shared ops in :mod:`precis.blocktree.ops`
        use so they never hardcode a concrete block class."""
        return BlockNode(**kwargs)  # type: ignore[return-value]

    def make_connect(self, **kwargs: Any) -> TConnect:
        """Construct a new connect row for this tree — the ``Connect``
        analogue of :meth:`make_block`, for a domain whose ``connect`` op
        stays on the shared implementation (one that adds no extra
        per-connect field, unlike ``se``'s ``joint``)."""
        return Connect(**kwargs)  # type: ignore[return-value]
