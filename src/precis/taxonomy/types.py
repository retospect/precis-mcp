"""Value objects shared by the five taxonomy-bootstrap stages.

Every type here is a frozen dataclass: stages are pure functions and a stage's
output is an input to the next, so accidental mutation would make a rerun
non-reproducible. Determinism is the point (``taxonomy-bootstrap.md`` AC1), so
every collection field is an ordered tuple or a frozenset, never a list or a
dict, and every ``to_json``/``as_row`` helper emits keys in a fixed order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

#: pint reports dimensionality as ``{'[current]': 1, '[length]': -2}``. The
#: seven SI base dimensions in the canonical order ``term-taxonomy.md`` uses
#: for its ``si_vector`` string (``"0,0,-1,0,0,0,0"`` is s⁻¹, so slot 3 is
#: time). ``substance`` is pint's name for amount of substance.
SI_BASE_ORDER: Final[tuple[str, ...]] = (
    "length",
    "mass",
    "time",
    "current",
    "temperature",
    "substance",
    "luminosity",
)

#: ``term-taxonomy.md`` §Design: nullable, and ``si_vector`` is set for ``si``
#: only. ``scale`` covers non-convertible scales, ``categorical`` non-quantity
#: value types, ``currency`` needs a code and a base year and therefore cannot
#: be an SI vector at all.
DimensionKind = Literal[
    "si", "currency", "count", "dimensionless", "scale", "categorical"
]

#: What a stage-1 hit is. ``value`` is a number (with or without a unit);
#: the other two are the phrase types that qualify a nearby value and are
#: emitted separately so a value never silently absorbs them.
MentionKind = Literal["value", "reference_state", "normalisation_basis"]

Half = Literal["A", "B"]

Status = Literal["proposed", "systematic"]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Identity of the frozen corpus the stages ran against.

    Recorded in every output so a number can always be traced to the corpus
    state that produced it, and so a rerun on a grown corpus produces a diff
    instead of an overwrite.
    """

    source: str
    row_count: int
    sha256: str
    pulled_at: str
    """UTC, ``Z``-suffixed (`docs/conventions/time.md`)."""
    text_field: str
    ref_field: str
    paper_field: str = ""
    """Row field holding the source paper ref ids, when the scanned rows are
    not themselves papers.

    The norr-her-meta snapshot rows are claim *hubs*, and the usage test
    promotes on ">=3 independent papers" — so without this the test would count
    hub ids and overstate independence (1664 hubs over 620 papers, median ~3
    hubs per paper, so three hubs of one paper would promote). Empty means the
    rows are the papers and ``ref_field`` already answers the question."""

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "row_count": self.row_count,
            "sha256": self.sha256,
            "pulled_at": self.pulled_at,
            "text_field": self.text_field,
            "ref_field": self.ref_field,
            "paper_field": self.paper_field,
        }


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a mention sits in the source.

    ``source_kind`` exists because the first run scans claim-hub text
    (``refs.text``), not body chunks, and the pilot's ``ANCHOR_SCHEME = 1``
    (`paper-extraction-pilot/bindings.py`) addresses *chunks*. Rather than
    render a chunk-shaped anchor for something that is not a chunk, the kind
    is carried explicitly and the scheme travels with it.
    """

    source_ref_id: int
    start: int
    end: int
    source_kind: Literal["ref_text", "chunk"] = "ref_text"
    scheme: int = 1

    def render(self) -> str:
        return f"{self.source_ref_id}#{self.start}-{self.end}"

    def to_json(self) -> dict[str, object]:
        return {
            "ref_id": self.source_ref_id,
            "start": self.start,
            "end": self.end,
            "source_kind": self.source_kind,
            "scheme": self.scheme,
        }


@dataclass(frozen=True, slots=True)
class Mention:
    """One stage-1 hit — a literal in its place, with no interpretation.

    ``literal`` is exactly as written, including the original exponent
    notation; ``raw_unit`` is the token pint accepted, or ``None`` when no
    unit followed the number or the token did not parse. Stage 1 never names
    a measurand.
    """

    anchor: Anchor
    kind: MentionKind
    literal: str
    raw_unit: str | None = None
    context: str = ""
    marker: str | None = None
    """For ``reference_state`` / ``normalisation_basis``: the campaign config
    id that matched (``rhe``, ``per-catalyst-mass``)."""

    def to_json(self) -> dict[str, object]:
        return {
            "anchor": self.anchor.to_json(),
            "kind": self.kind,
            "literal": self.literal,
            "raw_unit": self.raw_unit,
            "context": self.context,
            "marker": self.marker,
        }


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """A resolved dimension. Named to avoid colliding with
    `precis.utils.units.Dimension`, which is a fixed enum of CAD dimensions
    and unrelated to this open set."""

    kind: DimensionKind
    si_vector: str | None = None
    currency_code: str | None = None
    base_year: int | None = None

    def comparable_with(self, other: DimensionSpec) -> bool:
        """Whether two dimensions may be pooled.

        String equality on the SI vector, per ``term-taxonomy.md``. Currency
        additionally requires the same code and base year — a 2015 USD figure
        and a 2024 USD figure are not one measurand.
        """
        if self.kind != other.kind:
            return False
        if self.kind == "si":
            return self.si_vector == other.si_vector
        if self.kind == "currency":
            return (
                self.currency_code == other.currency_code
                and self.base_year == other.base_year
            )
        return True

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "si_vector": self.si_vector,
            "currency_code": self.currency_code,
            "base_year": self.base_year,
        }


@dataclass(frozen=True, slots=True)
class DiscoveredTerm:
    """One stage-2 row: what the model made of one mention.

    Open vocabulary — ``measurand`` is the model's own string, unnormalised.
    Stage 3 is what merges these; keeping the raw string means a merge can be
    audited or undone.
    """

    mention: Mention
    measurand: str
    half: Half
    dimension_text: str | None = None
    reference_state: str | None = None
    convention: str | None = None
    normalisation_basis: str | None = None
    subject_label: str | None = None
    required_conditions: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "mention": self.mention.to_json(),
            "measurand": self.measurand,
            "half": self.half,
            "dimension_text": self.dimension_text,
            "reference_state": self.reference_state,
            "convention": self.convention,
            "normalisation_basis": self.normalisation_basis,
            "subject_label": self.subject_label,
            "required_conditions": list(self.required_conditions),
        }


@dataclass(frozen=True, slots=True)
class TermNode:
    """A candidate taxon node with the evidence that put it there.

    ``key`` is a resolution key (lowercased, punctuation to hyphens), not an
    identity: ``term-taxonomy.md`` makes the ref_id the identity and keeps
    slug and label mutable. Two nodes may share a key across dimensions —
    that is the ``TOF`` case, and it is why :attr:`dimension` participates in
    equality of meaning rather than the key alone.
    """

    key: str
    label: str
    dimension: DimensionSpec | None = None
    reference_state: str | None = None
    convention: str | None = None
    normalisation_basis: str | None = None
    aliases: tuple[str, ...] = ()
    units_seen: tuple[tuple[str, int], ...] = ()
    """``(raw_unit, count)`` sorted by descending count then unit string.

    Kept per node because "the units seen per measurand" is a deliverable in
    its own right (``norr-her-meta.md`` AC2) and because
    :attr:`canonical_unit` has to come from measured usage rather than from
    someone's expectation of what the unit ought to be."""
    paper_ref_ids: frozenset[int] = frozenset()
    halves: frozenset[str] = frozenset()
    mention_count: int = 0
    status: Status = "proposed"
    notes: tuple[str, ...] = ()
    """Why a node did *not* promote, in the words the report prints."""
    anchors: tuple[Anchor, ...] = ()

    @property
    def canonical_unit(self) -> str | None:
        """The most-used raw unit, or ``None`` when no mention carried one."""
        return self.units_seen[0][0] if self.units_seen else None

    def identity(self) -> tuple[str, str, str, str, str]:
        """The tuple that decides whether two values may be compared.

        Measurand, reference state, convention, normalisation basis and
        dimension — the same compare identity `measures-substrate.md` puts on
        a `measures` row, so a node and a row agree on what "the same
        quantity" means. Counting distinct identities per measurand is the
        number `norr-her-meta.md` AC2 asks for and the one the
        descriptor-graph section says decides a 3-month versus 2-year build.

        Normalisation basis is part of identity, not a convertible attribute:
        a yield rate per geometric area and one per catalyst mass are two
        measurands even where pint collapses them to the same dimension.
        """
        dim = ""
        if self.dimension is not None:
            dim = str(self.dimension.si_vector or self.dimension.kind)
        return (
            self.key,
            self.reference_state or "",
            self.convention or "",
            self.normalisation_basis or "",
            dim,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "dimension": self.dimension.to_json() if self.dimension else None,
            "reference_state": self.reference_state,
            "convention": self.convention,
            "normalisation_basis": self.normalisation_basis,
            "aliases": list(self.aliases),
            "units_seen": [[u, n] for u, n in self.units_seen],
            "canonical_unit": self.canonical_unit,
            "paper_ref_ids": sorted(self.paper_ref_ids),
            "halves": sorted(self.halves),
            "mention_count": self.mention_count,
            "status": self.status,
            "notes": list(self.notes),
            "anchors": [a.to_json() for a in self.anchors],
        }


@dataclass(frozen=True, slots=True)
class AxisEdge:
    """A proposed ``specialises`` edge, labelled with the axis it widens.

    The relation is always ``specialises`` — ``term-taxonomy.md`` reuses the
    existing ``generalises``/``specialises`` pair rather than minting
    ``is-a``. The axis goes on the edge, not the node, so a node can have
    several parents and a widening query can move along exactly one axis at a
    time (``Pd(111)`` widens to ``Pd`` along composition without also
    widening to every ``(111)`` surface).
    """

    child: str
    parent: str
    axis: str

    def to_json(self) -> dict[str, object]:
        return {"child": self.child, "parent": self.parent, "axis": self.axis}


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The knobs a human signs off instead of signing off a list.

    Defaults adopted 2026-09-28 (``taxonomy-bootstrap.md`` §Thresholds). A
    campaign may tighten them; any override is recorded in the frozen list so
    a number is never readable without the rule that admitted it.
    """

    min_papers: int = 3
    """Independent papers a term needs before it can be ``systematic``."""
    min_hubs: int = 30
    """Hubs in the campaign's target cells before an entry joins the list."""
    min_join_side: int = 15
    """Hubs on *each* side of an experimental-vs-computational comparison."""
    min_stability: float = 0.80
    """Mention-weighted A/B vocabulary overlap below which the run fails."""
    min_probe_ratio: float = 0.60
    """The n≈100 probe criterion, stated 2026-09-30: stability as a fraction
    of :func:`~precis.taxonomy.select.unit_key_ceiling`. At probe size the
    0.80 overlap is unreadable (most quantities occur in one hub, capping
    the overlap near 0.5 whatever the prompt), so a probe is judged against
    what the deterministic unit key reaches on the same rows; the full run
    is still judged by ``min_stability``, where the ceiling approaches one.
    Reported by the CLI, never a freeze gate."""
    max_escape_rate: float = 0.10
    """Share of a paper's values fitting no entry that forces regeneration."""
    require_both_halves: bool = True
    require_single_dimension: bool = True

    def to_json(self) -> dict[str, object]:
        return {
            "min_papers": self.min_papers,
            "min_hubs": self.min_hubs,
            "min_join_side": self.min_join_side,
            "min_stability": self.min_stability,
            "min_probe_ratio": self.min_probe_ratio,
            "max_escape_rate": self.max_escape_rate,
            "require_both_halves": self.require_both_halves,
            "require_single_dimension": self.require_single_dimension,
        }


@dataclass(frozen=True, slots=True)
class HubCount:
    """Per-node hub evidence: how much, from where, and split by side.

    Lives here rather than in `select` because two modules must agree on it —
    `run` builds it from the snapshot and `select` reads it against the
    thresholds. It is keyed by a node's :meth:`TermNode.identity`, never by its
    ``key``: keying by key gives every reference-state or convention split
    variant the *union* count, so a `vs Ag/AgCl` potential with 20 hubs would
    clear a >=30 threshold on the `vs RHE` variant's evidence and undo the
    split stage 3 just made.

    ``ref_ids`` is carried, not just counted, because AC4 requires every frozen
    entry to list the hubs that put it there.
    """

    total: int
    by_side: Mapping[str, int] = field(default_factory=dict)
    ref_ids: tuple[int, ...] = ()
    paper_ref_ids: frozenset[int] = frozenset()

    def to_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_side": dict(sorted(self.by_side.items())),
            "ref_ids": list(self.ref_ids),
            "paper_ref_ids": sorted(self.paper_ref_ids),
        }


@dataclass(frozen=True, slots=True)
class ListEntry:
    """One frozen measurand, with the provenance that admitted it."""

    key: str
    label: str
    dimension: DimensionSpec
    canonical_unit: str | None = None
    convention: str | None = None
    allowed_reference_states: tuple[str, ...] = ()
    normalisation_bases: tuple[str, ...] = ()
    required_conditions: tuple[str, ...] = ()
    convert: bool = False
    """``False`` means never convert — two normalisation bases stay two
    entries (``norr-her-meta.md`` step 2)."""
    hub_count: int = 0
    paper_count: int = 0
    contributing_hubs: tuple[int, ...] = ()
    aliases: tuple[str, ...] = ()

    def identity(self) -> tuple[str, str, str, str]:
        """The tuple that makes this entry distinguishable from its siblings.

        ``key`` alone is **not** unique and is not meant to be: AC5 requires
        two `TOF` entries to coexist, and a reference-state or convention split
        produces two entries under one key by design. Anything that indexes
        entries by key — a diff, a lookup, a threshold read — silently drops
        one of them.
        """
        return (
            self.key,
            self.convention or "",
            "|".join(self.allowed_reference_states),
            str(self.dimension.si_vector or self.dimension.kind),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "dimension": self.dimension.to_json(),
            "canonical_unit": self.canonical_unit,
            "convention": self.convention,
            "allowed_reference_states": list(self.allowed_reference_states),
            "normalisation_bases": list(self.normalisation_bases),
            "required_conditions": list(self.required_conditions),
            "convert": self.convert,
            "hub_count": self.hub_count,
            "paper_count": self.paper_count,
            "contributing_hubs": list(self.contributing_hubs),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class MeasurandList:
    """The stage-5 artifact: ``list.vN.yaml``.

    Carries everything needed to read an entry without trusting the reader's
    memory — snapshot identity, the thresholds in force, and the procedure
    version, which is what a binding document cites alongside
    ``ANCHOR_SCHEME``.
    """

    version: int
    campaign: str
    snapshot: Snapshot
    thresholds: Thresholds
    procedure_version: int
    entries: tuple[ListEntry, ...] = ()
    stability: float | None = None
    rejected: tuple[tuple[str, str], ...] = field(default=())
    """``(key, reason)`` for terms that cleared discovery but not selection —
    kept because "why is X missing" is the first question asked of a list."""

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "campaign": self.campaign,
            "procedure_version": self.procedure_version,
            "snapshot": self.snapshot.to_json(),
            "thresholds": self.thresholds.to_json(),
            "stability": self.stability,
            "entries": [e.to_json() for e in self.entries],
            "rejected": [{"key": k, "reason": r} for k, r in self.rejected],
        }
