"""The `component` SERIES registry — standards families and their valid
size tables (docs/backlog/se-off-the-shelf-fabrication.md, engine 1).

A `component` ref is one SKU. Hand-entering four hundred screws is not
viable, so a **series** carries a family (ISO 4762 socket cap screws) plus
its **valid-combination size table**: M6 exists, M6x2 does not; DN50 has
one wall thickness, not a continuum. An entity is *minted* from a series
row on first use, and what lands in the DB afterwards is ordinary
`component_spec_values` rows with `method='standard'` and the series
`source` as provenance — nothing here shadows the star schema.

**File, not table** (`precis/data/component_series.json`, loaded the way
`pcb/capabilities.py` loads its rules). These are published standards
dimensions: curated, versioned, diffable, and changed by a commit rather
than a migration. The tables here are supplier-*neutral* on purpose — a
price/stock enrichment layer keyed by designation is a later, separate
integration (the catalog survey in the owning backlog item), and nothing
in this module reaches a network.

Units are whatever `component_specs.canonical_unit` says for each spec —
in practice millimetres for the length specs migration 0152 seeds, and
`length_overall` is the metres outlier. Consumers convert; they must not
assume (the wart is recorded in 0152's header).

Two resolver entry points, both pure:

- :func:`find_series` — exact `series_id` lookup, the addressed read.
- :func:`resolve` — colloquial text ("M6x30 socket cap", "1/2 in steel
  pipe", "3mm plexiglass") to ranked ``(series, size, length)``
  candidates. Ranked, never auto-picked: the caller shows the options and
  an agent names the one it meant, the `part` kind's colloquial→C-number
  precedent one level up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

_PACKAGED_DATA = "precis.data"
_FILE = "component_series.json"

#: Size keys are compared case- and separator-insensitively so an agent's
#: "M6 x 30", "m6x30" and "M6×30" all reach the same row.
_NORM_RE = re.compile(r"[\s_·×x*]+", re.IGNORECASE)


@dataclass(frozen=True)
class SeriesSize:
    """One valid row of a series' size table."""

    key: str
    specs: dict[str, Any]
    #: Discrete lengths this size is stocked in, for a series with a length
    #: variable (`length_spec`). Empty for a series without one — a nut has
    #: no length axis, and an empty list is that fact, not a data gap.
    lengths: tuple[float, ...] = ()


@dataclass(frozen=True)
class Series:
    """One standards family plus its size table."""

    series_id: str
    name: str
    category: str
    source: str
    retrieved: str
    #: The published standard this transcribes (``'ISO 4762'``). ``None``
    #: for a stock range that has no designation — acrylic sheet is a set
    #: of thicknesses suppliers hold, not a standard, and saying so is the
    #: point.
    designation: str | None = None
    #: Which spec the length variable writes to (``'length'`` for a
    #: fastener, ``'length_overall'`` for tube). ``None`` = no length axis.
    length_spec: str | None = None
    #: Specs shared by every size (``drive_type`` for a whole screw family).
    specs: dict[str, Any] = field(default_factory=dict)
    sizes: tuple[SeriesSize, ...] = ()
    aliases: tuple[str, ...] = ()

    def size(self, key: str) -> SeriesSize | None:
        """The size row whose key matches ``key``, normalized."""
        want = normalize(key)
        for s in self.sizes:
            if normalize(s.key) == want:
                return s
        return None


def normalize(text: str) -> str:
    """Fold a size key / query token to its comparison form: lowercase,
    every separator (space, ``x``, ``×``, ``*``, ``·``, ``_``) collapsed
    away. ``'M6 x 30'``, ``'m6x30'`` and ``'M6×30'`` all become
    ``'m630'`` — which is why parsing splits a compound designation
    *before* normalizing (see :func:`split_designation`)."""
    return _NORM_RE.sub("", str(text)).strip().lower()


def _load_raw() -> dict[str, Any]:
    raw = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


@lru_cache(maxsize=1)
def load_series() -> tuple[Series, ...]:
    """Every series in the registry, in file order."""
    out: list[Series] = []
    for r in _load_raw()["series"]:
        sizes = tuple(
            SeriesSize(
                key=str(s["key"]),
                specs=dict(s.get("specs", {})),
                lengths=tuple(float(x) for x in s.get("lengths", ())),
            )
            for s in r.get("sizes", ())
        )
        out.append(
            Series(
                series_id=str(r["series_id"]),
                name=str(r["name"]),
                category=str(r["category"]),
                source=str(r["source"]),
                retrieved=str(r["retrieved"]),
                designation=r.get("designation"),
                length_spec=r.get("length_spec"),
                specs=dict(r.get("specs", {})),
                sizes=sizes,
                aliases=tuple(str(a) for a in r.get("aliases", ())),
            )
        )
    return tuple(out)


def find_series(series_id: str) -> Series | None:
    """The series with this id, matched on the normalized designation too
    (``'ISO 4762'`` reaches ``iso-4762``). ``None`` when unknown — the
    caller owns the error message, which is where the known-ids list
    belongs."""
    want = normalize(series_id)
    for s in load_series():
        if normalize(s.series_id) == want or (
            s.designation is not None and normalize(s.designation) == want
        ):
            return s
    return None


def split_designation(text: str) -> tuple[str, float | None]:
    """Split a compound size designation into ``(size_key, length)``:
    ``'M6x30'`` → ``('M6', 30.0)``, ``'M6'`` → ``('M6', None)``,
    ``'DN25x1000'`` → ``('DN25', 1000.0)``, ``'3mm'`` → ``('3mm', None)``.

    Only a *trailing bare number* after a separator is read as a length,
    so ``'3mm'`` (a thickness key) and ``'DN15'`` survive intact. A caller
    that needs the length elsewhere passes it explicitly instead."""
    m = re.fullmatch(
        r"\s*(.*?[A-Za-z0-9])\s*[x×*]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:mm)?\s*",
        str(text),
        re.IGNORECASE,
    )
    if m is None:
        return str(text).strip(), None
    return m.group(1), float(m.group(2))


@dataclass(frozen=True)
class SeriesMatch:
    """One ranked resolver hit — everything :func:`mint_specs` needs."""

    series: Series
    size: SeriesSize
    length: float | None
    score: float
    #: Why this scored: the query tokens that matched, joined for display.
    why: str


def resolve(query: str, *, limit: int = 8) -> list[SeriesMatch]:
    """Rank ``(series, size, length)`` candidates for a colloquial query.

    Scoring is deliberately crude and explainable rather than clever: a
    size-key hit is worth more than a name/alias hit, an exact stocked
    length more than a plausible one, and every contributing token is
    reported in ``why`` so an agent can see *why* it was offered a part
    instead of trusting a number. Returns ``[]`` rather than guessing when
    nothing matches — an unresolved designation is a question for the
    caller, never a silently-wrong part."""
    raw = str(query).strip()
    if not raw:
        return []
    # Parse token by token, not over the whole string: "M6x30 socket cap"
    # must still yield the length 30, and a whole-string parse only works
    # for a bare designation.
    tokens = [t for t in re.split(r"[\s,]+", raw.lower()) if t]
    parsed = [split_designation(t) for t in tokens]
    norm_tokens = {normalize(head) for head, _ in parsed} | {
        normalize(t) for t in tokens
    }
    norm_tokens.discard("")
    lengths = [ln for _, ln in parsed if ln is not None]
    length = lengths[0] if lengths else None

    out: list[SeriesMatch] = []
    for series in load_series():
        text_hits = [
            a
            for a in (series.name, series.designation or "", *series.aliases)
            if a and _phrase_hit(a, raw.lower())
        ]
        for size in series.sizes:
            score = 0.0
            why: list[str] = []
            nk = normalize(size.key)
            if nk and nk in norm_tokens:
                score += 3.0
                why.append(size.key)
            # A size key mentioned anywhere in the query still counts, but
            # for less — "M6 washers for the M8 frame" should rank M6 first
            # without hiding M8.
            elif nk and nk in normalize(raw):
                score += 1.0
                why.append(size.key)
            if text_hits:
                score += 2.0
                why.append(text_hits[0])
            if score == 0.0:
                continue
            # A length is only meaningful where the series has a length
            # axis; a nut does not acquire one because the query mentioned
            # a number.
            pick = length if series.length_spec is not None else None
            if pick is not None:
                # Stocked beats merely plausible. An off-list length still
                # ranks (a supplier will cut one) — it is demoted and
                # flagged, never dropped.
                stocked = not size.lengths or pick in size.lengths
                score += 1.0 if stocked else -0.5
                why.append(f"{pick:g}mm" if stocked else f"{pick:g}mm?")
            out.append(
                SeriesMatch(
                    series=series,
                    size=size,
                    length=pick,
                    score=score,
                    why=" + ".join(why),
                )
            )
    out.sort(key=lambda c: (-c.score, c.series.series_id, c.size.key))
    return out[:limit]


def _phrase_hit(phrase: str, haystack: str) -> bool:
    """Whether every word of ``phrase`` appears in ``haystack`` — so
    "socket cap" matches "M6x30 socket cap screw" but "socket" alone does
    not match "cap"."""
    words = [w for w in re.split(r"[\s-]+", phrase.lower()) if w]
    return bool(words) and all(w in haystack for w in words)


def check_length(series: Series, size: SeriesSize, length: float | None) -> str | None:
    """A one-line complaint about ``length`` against the stocked list, or
    ``None`` when there is nothing to say. Advisory by design: the size
    table is what suppliers hold, not what physics allows, so an off-list
    length is a warning ("you will be paying to have it cut") and never an
    error."""
    if series.length_spec is None:
        if length is not None:
            return f"{series.series_id} has no length axis — length {length:g} ignored"
        return None
    if length is None:
        return f"{series.series_id} needs a length (e.g. {size.key}x20)"
    if size.lengths and length not in size.lengths:
        near = min(size.lengths, key=lambda x: abs(x - length))
        return (
            f"{length:g} is not a stocked length for {size.key} "
            f"(nearest {near:g}; stocked: {_fmt_lengths(size.lengths)})"
        )
    return None


def _fmt_lengths(lengths: tuple[float, ...]) -> str:
    return ", ".join(f"{x:g}" for x in lengths)


def mint_specs(
    series: Series, size: SeriesSize, length: float | None
) -> dict[str, Any]:
    """The full spec payload one minted entity gets: the series-level
    specs, then the size row's (which win on a clash — a size is more
    specific than its family), then the length under the series'
    ``length_spec``. Pure; the caller writes the rows."""
    specs: dict[str, Any] = dict(series.specs)
    specs.update(size.specs)
    if series.length_spec is not None and length is not None:
        specs[series.length_spec] = length
    return specs


#: The unit every numeric value in the series file is written in. One unit
#: for the whole file, stated once, because a per-value unit column on
#: curated standards data is a place for a typo to hide.
FILE_LENGTH_UNIT = "mm"

#: mm → the length units the component spec registry actually uses. Not a
#: general unit system: the registry's reserved conversion layer
#: (`component_spec_values.input_unit`) is that, and this is the two-entry
#: bridge the mint needs until it exists.
_LENGTH_FACTOR_FROM_MM: dict[str, float] = {"mm": 1.0, "cm": 0.1, "m": 0.001}


def to_canonical(
    value: float, *, canonical_unit: str | None, dimension: str | None
) -> tuple[float | None, str | None]:
    """Convert one file value into a spec's canonical unit.

    Returns ``(converted, complaint)``; exactly one is ``None``. The mm
    convention above only claims to cover **lengths**, so a length spec in
    a unit this bridge doesn't know returns a complaint and *no number* —
    writing an unconverted figure into a metres column is the exact defect
    this function exists to prevent (`length_overall` is metres while
    every other length spec is mm). A non-length quantity passes through
    unchanged, because the file states no unit for it to be wrong about."""
    if dimension != "length":
        return float(value), None
    if canonical_unit is None:
        return float(value), None
    factor = _LENGTH_FACTOR_FROM_MM.get(canonical_unit)
    if factor is None:
        return None, (
            f"cannot convert {FILE_LENGTH_UNIT} to {canonical_unit!r} "
            f"(known: {', '.join(sorted(_LENGTH_FACTOR_FROM_MM))})"
        )
    return float(value) * factor, None


def suggest_slug(series: Series, size: SeriesSize, length: float | None) -> str:
    """A deterministic default slug for a minted entity —
    ``iso-4762-m6x30``. Deterministic so two agents minting the same part
    converge on one ref instead of two."""
    tail = normalize(size.key)
    if length is not None:
        tail = f"{tail}x{length:g}"
    return f"{series.series_id}-{tail}"


def title_for(series: Series, size: SeriesSize, length: float | None) -> str:
    """The human title for a minted entity — ``'M6x30 hexagon socket head
    cap screw (ISO 4762)'``."""
    head = size.key if length is None else f"{size.key}x{length:g}"
    name = series.name[0].lower() + series.name[1:] if series.name else series.name
    suffix = f" ({series.designation})" if series.designation else ""
    return f"{head} {name}{suffix}"
