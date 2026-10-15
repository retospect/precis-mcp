"""Datasheet-extracted part facts on the part ref (datasheet-facts-mesh).

A pulled ``datasheet`` (``precis.workers.job_types.datasheet_pull``) is read
once per part by the ``datasheet_facts`` worker job
(``precis.workers.job_types.datasheet_facts``); what lands here is the
*fact layer* the pcb review consumes — not a finding per pin (knowledge-mesh-4
rejected that: it floods the claim graph) but one structured block per
package on the part ref's ``meta.datasheet_facts``:

- **pins** — the datasheet's pin table: ``pin`` (the table's number),
  ``name``, ``function``, and the electrical columns ERC and the review
  read with the SAME spelling the design layer already uses
  (``_pcb_ops._pcb_insert_pins``: ``etype`` in :data:`PIN_ETYPES`,
  ``rail``, ``max_voltage_v``, ``vih``, ``vil``, ``max_current_a``,
  ``nc``). A column the datasheet doesn't give is absent/``null`` —
  ``None`` at read time means *unknown*, and a consumer asks the user
  rather than guessing (pcb-datasheet-review ruling 2026-10-13).
- **required_externals** — the "needs a 10 µF X5R on VIN" rows:
  ``pins``, ``part_class``, ``value``, ``min_dielectric``,
  ``min_voltage_v``, ``max_distance_mm``, ``note``.
- **conditional_tables** — strap/address/mode tables stored as printed:
  ``inputs`` (pin names) + ``rows`` (levels → output). The generic
  lookup derives the instance's row from its strap nets.

Every fact carries ``confidence`` (extraction's self-reported 0–1) and a
page-cited provenance (``chunk_id`` on the source datasheet ref +
``page``). ``chunk_id`` is validated at write to belong to the declared
datasheet, so the ``part:<C#>~<pin>`` selector can resolve a pin to its
datasheet row (``precis.utils.link_target``) and the cite opens the right
page (``chunks.page_first``).

The block keeps a ``rev`` bumped on every write — part-fact revisions are
the staleness key a verdict records (pcb-datasheet-review slice 6). Facts
are trusted by default; an unclear or contradictory read is a low
``confidence``, not a refusal.

Reads: :func:`part_facts` (typed :class:`PartFacts` for one C-number),
:func:`pin_fact` (one pin row), :func:`facts_revision` (the staleness
key, 0 = never extracted). ``get(kind='part', id='C…', view='facts')``
renders the same data for an agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.pcb import PIN_ETYPES
from precis.utils.timeutil import now_iso

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The ``refs.meta`` key the block lives under on a ``part`` ref.
FACTS_META_KEY = "datasheet_facts"

#: The per-pin fields a reviewer checks; ``None`` = the datasheet fact is
#: unknown — ask the user, never guess (pcb-datasheet-review ruling).
ELECTRICAL_FIELDS: tuple[str, ...] = (
    "etype",
    "rail",
    "max_voltage_v",
    "vih",
    "vil",
    "max_current_a",
)

_PIN_KEYS: frozenset[str] = frozenset(
    {
        "pin",
        "name",
        "function",
        "etype",
        "rail",
        "max_voltage_v",
        "vih",
        "vil",
        "max_current_a",
        "nc",
        "unused",
        "must_terminate",
        "confidence",
        "page",
        "chunk_id",
    }
)

_EXTERNAL_KEYS: frozenset[str] = frozenset(
    {
        "pins",
        "part_class",
        "value",
        "min_dielectric",
        "min_voltage_v",
        "max_distance_mm",
        "note",
        "confidence",
        "page",
        "chunk_id",
    }
)

_TABLE_KEYS: frozenset[str] = frozenset(
    {"name", "inputs", "rows", "output_key", "confidence", "page", "chunk_id"}
)


@dataclass(frozen=True, slots=True)
class FactSource:
    """Where a fact is printed: a chunk of the datasheet ref + page."""

    datasheet_ref_id: int
    chunk_id: int | None
    page: int | None

    def describe(self) -> str:
        bits: list[str] = []
        if self.page is not None:
            bits.append(f"p.{self.page}")
        if self.chunk_id is not None:
            bits.append(f"chunk {self.chunk_id}")
        bits.append(f"datasheet ref {self.datasheet_ref_id}")
        return ", ".join(bits)


@dataclass(frozen=True, slots=True)
class PinFact:
    """One pin-table row. An ``ELECTRICAL_FIELDS`` member that is ``None``
    is *unknown* — the caller asks the user; it is never guessed."""

    pin: str
    name: str | None = None
    function: str | None = None
    etype: str | None = None
    rail: str | None = None
    max_voltage_v: float | None = None
    vih: float | None = None
    vil: float | None = None
    max_current_a: float | None = None
    nc: bool = False
    unused: bool = False
    must_terminate: bool = False
    confidence: float | None = None
    source: FactSource | None = None

    def missing(self, fields: tuple[str, ...] = ELECTRICAL_FIELDS) -> tuple[str, ...]:
        """The checked fields this row does not declare (the "unknown,
        ask the user" set for a review pass)."""
        return tuple(f for f in fields if getattr(self, f) is None)


@dataclass(frozen=True, slots=True)
class RequiredExternal:
    """ "needs a 100 nF X5R within 5 mm of VDD" — one datasheet demand."""

    pins: tuple[str, ...]
    part_class: str
    value: str | None = None
    min_dielectric: str | None = None
    min_voltage_v: float | None = None
    max_distance_mm: float | None = None
    note: str | None = None
    confidence: float | None = None
    source: FactSource | None = None


@dataclass(frozen=True, slots=True)
class ConditionalTable:
    """A strap/address/mode table stored as printed: ``rows`` maps
    ``{input-pin-name: level}`` → the output (``output_key`` names the
    column, e.g. ``address``)."""

    name: str
    inputs: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    output_key: str | None = None
    confidence: float | None = None
    source: FactSource | None = None


@dataclass(frozen=True, slots=True)
class PackageFacts:
    """One package variant's block (the spec's "structured block per
    package")."""

    package: str
    pins: tuple[PinFact, ...] = ()
    required_externals: tuple[RequiredExternal, ...] = ()
    conditional_tables: tuple[ConditionalTable, ...] = ()

    def pin(self, number: str | int) -> PinFact | None:
        """The row for pin ``number`` (the table's pin number), or None."""
        want = str(number).strip()
        return next((p for p in self.pins if p.pin == want), None)


@dataclass(frozen=True, slots=True)
class PartFacts:
    """The whole fact block for one part: ``packages`` keyed by package
    name, ``rev`` the write counter verdicts record, ``source`` naming
    the datasheet + extraction job. ``packages`` empty means extraction
    ran and found nothing citable — distinct from "never extracted"
    (:func:`part_facts` returns ``None`` for that)."""

    part_ref_id: int
    rev: int
    extracted_at: str | None
    datasheet_ref_id: int | None
    job_ref_id: int | None
    packages: dict[str, PackageFacts] = field(default_factory=dict)

    def package(self, name: str | None = None) -> PackageFacts | None:
        """One package block: the named one, or the only one when the
        part has exactly one (the common case — the caller shouldn't have
        to know the package string to ask for the pin table)."""
        if name is not None:
            return self.packages.get(name)
        if len(self.packages) == 1:
            return next(iter(self.packages.values()))
        return None

    def pin(self, number: str | int, *, package: str | None = None) -> PinFact | None:
        pkg = self.package(package)
        return pkg.pin(number) if pkg is not None else None


# ── write ────────────────────────────────────────────────────────────


def _confidence(v: Any, *, where: str) -> float | None:
    if v is None:
        return None
    try:
        c = float(v)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{where}: confidence {v!r} is not a number") from exc
    if not 0.0 <= c <= 1.0:
        raise BadInput(f"{where}: confidence {c} outside [0, 1]")
    return c


def _num(v: Any, *, where: str) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"{where}: {v!r} is not a number") from exc


def _str(v: Any, *, where: str) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _pint(v: Any, *, where: str) -> int | None:
    """``page`` / ``chunk_id`` coercer: integral number required."""
    if v is None:
        return None
    n = _num(v, where=where)
    assert n is not None  # v is not None here
    if n != int(n):
        raise BadInput(f"{where}: {v!r} is not an integer")
    return int(n)


def _validate_pin(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadInput(f"{where}: pin row must be an object, got {raw!r}")
    pin = _str(raw.get("pin"), where=where)
    if pin is None:
        raise BadInput(f"{where}: pin row has no 'pin' (the table's pin number)")
    etype = _str(raw.get("etype"), where=where)
    if etype is not None and etype not in PIN_ETYPES:
        raise BadInput(
            f"{where} pin {pin}: etype {etype!r} not in {sorted(PIN_ETYPES)}"
        )
    row: dict[str, Any] = {"pin": pin}
    for key in _PIN_KEYS - {"pin", "confidence", "page", "chunk_id"}:
        if raw.get(key) is not None:
            row[key] = raw[key]
    for key in ("nc", "unused", "must_terminate"):
        if key in row:
            row[key] = bool(row[key])
    for key in ("max_voltage_v", "vih", "vil", "max_current_a"):
        if key in row:
            row[key] = _num(row[key], where=f"{where} pin {pin} {key}")
    conf = _confidence(raw.get("confidence"), where=f"{where} pin {pin}")
    if conf is not None:
        row["confidence"] = conf
    for key in ("page", "chunk_id"):
        n = _pint(raw.get(key), where=f"{where} pin {pin} {key}")
        if n is not None:
            row[key] = n
    return row


def _validate_external(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadInput(f"{where}: required_external must be an object")
    row: dict[str, Any] = {}
    pins = raw.get("pins")
    if not isinstance(pins, list) or not pins:
        raise BadInput(f"{where}: required_external needs a non-empty 'pins' list")
    row["pins"] = [str(p).strip() for p in pins]
    cls = _str(raw.get("part_class"), where=where)
    if cls is None:
        raise BadInput(f"{where}: required_external needs 'part_class'")
    row["part_class"] = cls
    for key in ("value", "min_dielectric", "note"):
        s = _str(raw.get(key), where=where)
        if s is not None:
            row[key] = s
    for key in ("min_voltage_v", "max_distance_mm"):
        f = _num(raw.get(key), where=f"{where} {key}")
        if f is not None:
            row[key] = f
    conf = _confidence(raw.get("confidence"), where=where)
    if conf is not None:
        row["confidence"] = conf
    for key in ("page", "chunk_id"):
        n = _pint(raw.get(key), where=f"{where} {key}")
        if n is not None:
            row[key] = n
    return row


def _validate_table(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadInput(f"{where}: conditional_table must be an object")
    name = _str(raw.get("name"), where=where)
    inputs = raw.get("inputs")
    rows = raw.get("rows")
    if name is None or not isinstance(inputs, list) or not isinstance(rows, list):
        raise BadInput(f"{where}: conditional_table needs 'name', 'inputs' and 'rows'")
    row: dict[str, Any] = {
        "name": name,
        "inputs": [str(i).strip() for i in inputs],
        "rows": rows,
    }
    out_key = _str(raw.get("output_key"), where=where)
    if out_key is not None:
        row["output_key"] = out_key
    conf = _confidence(raw.get("confidence"), where=where)
    if conf is not None:
        row["confidence"] = conf
    for key in ("page", "chunk_id"):
        n = _pint(raw.get(key), where=f"{where} {key}")
        if n is not None:
            row[key] = n
    return row


def _chunk_ids_of(store: Store, ref_id: int) -> set[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _check_provenance(
    store: Store, datasheet_ref_id: int | None, payload: dict[str, Any]
) -> None:
    """Every ``chunk_id`` a fact cites must be a live chunk of the
    declared datasheet — a dangling citation is a write error, not a
    stored lie."""
    live: set[int] = set()
    if datasheet_ref_id is not None:
        live = _chunk_ids_of(store, datasheet_ref_id)
    for pkg_name, pkg in (payload.get("packages") or {}).items():
        rows = (
            list(pkg.get("pins") or [])
            + list(pkg.get("required_externals") or [])
            + list(pkg.get("conditional_tables") or [])
        )
        for row in rows:
            cid = row.get("chunk_id")
            if cid is None:
                continue
            if datasheet_ref_id is None:
                raise BadInput(
                    f"package {pkg_name}: a chunk_id citation "
                    f"({cid}) needs datasheet_ref_id — which datasheet is it on?"
                )
            if int(cid) not in live:
                raise BadInput(
                    f"package {pkg_name}: chunk_id {cid} is not a live chunk "
                    f"of datasheet ref {datasheet_ref_id}"
                )


def write_facts(
    store: Store,
    part_ref_id: int,
    payload: dict[str, Any],
    *,
    datasheet_ref_id: int | None = None,
    job_ref_id: int | None = None,
    allow_empty: bool = False,
    conn: Any = None,
) -> int:
    """Validate ``payload`` and store it as the part ref's fact block;
    bumps ``rev``. Returns the new revision.

    ``payload`` shape::

        {"packages": {"<package>": {
            "pins": [{pin, name?, function?, etype?, rail?,
                      max_voltage_v?, vih?, vil?, max_current_a?,
                      nc?, confidence?, page?, chunk_id?}, ...],
            "required_externals": [{pins, part_class, value?, ...}],
            "conditional_tables": [{name, inputs, rows, ...}]}}}

    Replaces the whole block — extraction is one read of one datasheet, so
    a re-write IS a new extraction (the rev is what a stale verdict reads,
    not a field diff). ``datasheet_ref_id`` is the citable source; every
    ``chunk_id`` in the payload must be a live chunk of it.
    ``allow_empty=True`` (the extraction job's "read it, found nothing"
    path) stores a ``packages: {}`` block — a recorded extraction, not a
    missing one.
    """
    ref = store.get_ref(kind="part", id=part_ref_id)
    if ref is None:
        raise NotFound(f"no live part ref {part_ref_id}")
    packages_in = payload.get("packages")
    if not isinstance(packages_in, dict) or (not packages_in and not allow_empty):
        raise BadInput(
            "facts payload needs 'packages': {<package>: {pins, ...}}",
            next="write_facts(store, part_id, {'packages': {'QFN-32': {'pins': [...]}}})",
        )
    packages: dict[str, Any] = {}
    for name, pkg in packages_in.items():
        pkg_name = str(name).strip() or "default"
        if not isinstance(pkg, dict):
            raise BadInput(f"package {pkg_name}: block must be an object")
        where = f"package {pkg_name}"
        pins = [_validate_pin(r, where=where) for r in pkg.get("pins") or []]
        seen = [p["pin"] for p in pins]
        dupes = sorted({p for p in seen if seen.count(p) > 1})
        if dupes:
            raise BadInput(f"{where}: duplicate pin row(s) {dupes}")
        packages[pkg_name] = {
            "pins": pins,
            "required_externals": [
                _validate_external(r, where=where)
                for r in pkg.get("required_externals") or []
            ],
            "conditional_tables": [
                _validate_table(r, where=where)
                for r in pkg.get("conditional_tables") or []
            ],
        }
    block: dict[str, Any] = {
        "rev": int(((ref.meta or {}).get(FACTS_META_KEY) or {}).get("rev") or 0) + 1,
        "extracted_at": now_iso(),
        "source": {
            "datasheet_ref_id": datasheet_ref_id,
            "job_ref_id": job_ref_id,
        },
        "packages": packages,
    }
    normalized = {FACTS_META_KEY: block}
    _check_provenance(store, datasheet_ref_id, normalized[FACTS_META_KEY])
    store.update_ref(part_ref_id, meta_patch=normalized, conn=conn)
    return int(block["rev"])


def record_extraction(
    store: Store, part_ref_id: int, record: dict[str, Any], *, conn: Any = None
) -> None:
    """Stamp the extraction outcome (``{status: ok|failed, reason?, at,
    job_ref_id, model?}``) onto the block's ``extraction`` slot without
    touching the facts — like ``meta.datasheet_pull`` for the pull, this
    is how "extracted" reads differently from "never tried"."""
    ref = store.get_ref(kind="part", id=part_ref_id)
    if ref is None:
        raise NotFound(f"no live part ref {part_ref_id}")
    block = dict((ref.meta or {}).get(FACTS_META_KEY) or {})
    block.setdefault("rev", 0)
    block["extraction"] = record
    store.update_ref(part_ref_id, meta_patch={FACTS_META_KEY: block}, conn=conn)


# ── read ─────────────────────────────────────────────────────────────


def _source(datasheet_ref_id: int, row: dict[str, Any]) -> FactSource | None:
    cid = row.get("chunk_id")
    page = row.get("page")
    if cid is None and page is None:
        return None
    return FactSource(
        datasheet_ref_id=datasheet_ref_id,
        chunk_id=int(cid) if cid is not None else None,
        page=int(page) if page is not None else None,
    )


def _to_part_facts(part_ref_id: int, block: dict[str, Any]) -> PartFacts:
    src = block.get("source") or {}
    ds_id = src.get("datasheet_ref_id")
    ds_id = int(ds_id) if ds_id is not None else 0
    packages: dict[str, PackageFacts] = {}
    for name, pkg in (block.get("packages") or {}).items():
        pins = tuple(
            PinFact(
                pin=str(p["pin"]),
                name=p.get("name"),
                function=p.get("function"),
                etype=p.get("etype"),
                rail=p.get("rail"),
                max_voltage_v=p.get("max_voltage_v"),
                vih=p.get("vih"),
                vil=p.get("vil"),
                max_current_a=p.get("max_current_a"),
                nc=bool(p.get("nc")),
                unused=bool(p.get("unused")),
                must_terminate=bool(p.get("must_terminate")),
                confidence=p.get("confidence"),
                source=_source(ds_id, p),
            )
            for p in pkg.get("pins") or []
        )
        externals = tuple(
            RequiredExternal(
                pins=tuple(str(x) for x in r.get("pins") or ()),
                part_class=str(r.get("part_class") or ""),
                value=r.get("value"),
                min_dielectric=r.get("min_dielectric"),
                min_voltage_v=r.get("min_voltage_v"),
                max_distance_mm=r.get("max_distance_mm"),
                note=r.get("note"),
                confidence=r.get("confidence"),
                source=_source(ds_id, r),
            )
            for r in pkg.get("required_externals") or []
        )
        tables = tuple(
            ConditionalTable(
                name=str(t.get("name") or ""),
                inputs=tuple(str(i) for i in t.get("inputs") or ()),
                rows=tuple(t.get("rows") or ()),
                output_key=t.get("output_key"),
                confidence=t.get("confidence"),
                source=_source(ds_id, t),
            )
            for t in pkg.get("conditional_tables") or []
        )
        packages[str(name)] = PackageFacts(
            package=str(name),
            pins=pins,
            required_externals=externals,
            conditional_tables=tables,
        )
    return PartFacts(
        part_ref_id=part_ref_id,
        rev=int(block.get("rev") or 0),
        extracted_at=block.get("extracted_at"),
        datasheet_ref_id=int(ds_id) if ds_id else None,
        job_ref_id=src.get("job_ref_id"),
        packages=packages,
    )


def part_facts(store: Store, lcsc: str) -> PartFacts | None:
    """The typed fact block for one C-number, or ``None`` when nothing was
    ever extracted (a consumer then reports "no datasheet facts for <part>"
    — never a pass)."""
    part_ref_id = store.part_ref_id(lcsc.strip().upper())
    if part_ref_id is None:
        return None
    return part_facts_for_ref(store, part_ref_id)


def part_facts_for_ref(store: Store, part_ref_id: int) -> PartFacts | None:
    ref = store.get_ref(kind="part", id=part_ref_id)
    if ref is None:
        return None
    block = (ref.meta or {}).get(FACTS_META_KEY)
    # ``extracted_at`` marks a real write (possibly ``packages: {}`` —
    # "read it, found nothing"). A block holding only an ``extraction``
    # failure record is NOT facts: the review's "no datasheet facts"
    # path must see it as never-extracted.
    if not isinstance(block, dict) or not block.get("extracted_at"):
        return None
    return _to_part_facts(part_ref_id, block)


def pin_fact(
    store: Store, lcsc: str, pin: str | int, *, package: str | None = None
) -> PinFact | None:
    """One pin row for ``part:<lcsc>~<pin>`` — the selector's read."""
    facts = part_facts(store, lcsc)
    return facts.pin(pin, package=package) if facts is not None else None


def facts_revision(store: Store, part_ref_id: int) -> int:
    """The block's write counter — what a verdict records as the consumed
    part-fact revision. 0 = nothing written yet."""
    ref = store.get_ref(kind="part", id=part_ref_id)
    if ref is None:
        return 0
    return int(((ref.meta or {}).get(FACTS_META_KEY) or {}).get("rev") or 0)


# ── render ───────────────────────────────────────────────────────────


def render_facts(facts: PartFacts | None, *, lcsc: str) -> str:
    """The ``view='facts'`` body for ``get(kind='part', id=<lcsc>)``:
    one block per package, pins as a compact table, externals and
    conditional tables listed, every row carrying its page anchor.
    Unknown electrical fields render ``?`` — the review's cue to ask."""
    if facts is None:
        return (
            f"no datasheet facts extracted for {lcsc} "
            "(a datasheet_facts job runs after the pull; a pcb review "
            "reports 'not checkable (no datasheet facts)')"
        )
    out = [
        f"# {lcsc} — datasheet facts (rev {facts.rev}, "
        f"extracted {facts.extracted_at or 'never'})"
    ]
    if facts.datasheet_ref_id is not None:
        out.append(f"source: datasheet ref {facts.datasheet_ref_id}")
    if not facts.packages:
        out.append("extraction ran; no package blocks recorded")
    for pkg in facts.packages.values():
        out.append(f"\n## package {pkg.package}")
        if pkg.pins:
            out.append("pins:")
            for p in pkg.pins:
                fields = [f"pin {p.pin}"]
                if p.name:
                    fields.append(str(p.name))
                if p.function:
                    fields.append(str(p.function))
                for key in ELECTRICAL_FIELDS:
                    v = getattr(p, key)
                    fields.append(
                        f"{key}={v:g}"
                        if isinstance(v, float)
                        else f"{key}={v if v is not None else '?'}"
                    )
                if p.nc:
                    fields.append("nc")
                if p.confidence is not None:
                    fields.append(f"conf={p.confidence:g}")
                if p.source is not None:
                    fields.append(f"[{p.source.describe()}]")
                out.append("  " + "  ".join(fields))
        else:
            out.append("pins: (none extracted)")
        for e in pkg.required_externals:
            bits = [
                f"required_external: {e.part_class}",
                f"on {'+'.join(e.pins)}",
            ]
            if e.value:
                bits.append(str(e.value))
            if e.min_dielectric:
                bits.append(f"dielectric>={e.min_dielectric}")
            if e.min_voltage_v is not None:
                bits.append(f">={e.min_voltage_v:g} V")
            if e.max_distance_mm is not None:
                bits.append(f"within {e.max_distance_mm:g} mm")
            if e.note:
                bits.append(f"({e.note})")
            if e.source is not None:
                bits.append(f"[{e.source.describe()}]")
            out.append("  " + "  ".join(bits))
        for t in pkg.conditional_tables:
            head = f"conditional_table {t.name!r}: inputs {'+'.join(t.inputs)}"
            if t.output_key:
                head += f" -> {t.output_key}"
            if t.source is not None:
                head += f" [{t.source.describe()}]"
            out.append("  " + head)
            for row in t.rows:
                out.append(f"    {row}")
    return "\n".join(out)


__all__ = [
    "ELECTRICAL_FIELDS",
    "FACTS_META_KEY",
    "ConditionalTable",
    "FactSource",
    "PackageFacts",
    "PartFacts",
    "PinFact",
    "RequiredExternal",
    "facts_revision",
    "part_facts",
    "part_facts_for_ref",
    "pin_fact",
    "record_extraction",
    "render_facts",
    "write_facts",
]
