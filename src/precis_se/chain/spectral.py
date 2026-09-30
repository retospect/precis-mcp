"""The spectral channel budget — how many independently addressed light
(and reaction) channels a design's state machines spend, and whether two
of them can be told apart.

``docs/backlog/photoswitch-states-and-spectral-dof.md`` ruled that the
number of orthogonal optical channels is a scarce budget the designer
spends, not a free dimension: organic absorption bands are tens of nm wide,
so a lab addresses a handful of switches independently, never dozens. This
module is the DRC that item asked for, built here because the walker
(``se-walker-light-protocol``) is the first design that spends several
channels at once.

Two rules, both handler-side (they need the design's stored transitions and
its ``material`` rows; :func:`precis_se.chain.findings.findings` calls them):

- ``chain_channel_budget`` (**error**): more distinct channels used across
  every block's transitions than the design's authored
  ``set_optics(channels_available=N)``, the numbers quoted. A design that has
  not authored a budget gets no row — the budget is a declared constraint,
  never an assumed one.
- ``chain_spectral_crosstalk`` (**warn**): pumping one light channel at its
  wavelength excites another channel's band above :data:`CROSSTALK_WARN`
  of that band's own peak, so the two are not independently addressable.

**What a channel is**: one ``(driver_kind, driver_ref)`` pair among a
design's ``light`` and ``reaction`` transitions — ``405nm`` is one channel
however many blocks it drives, and ``rxn:<slug>`` steps count as channels
because each is a separately dispensed reagent. Thermal, redox, pH and
mechanical drivers address nothing spectrally and are not counted.

**What a band is**: a Gaussian on wavelength with a centre and a FWHM. The
centre and width come from the driving block's ``material`` rows
(:data:`precis_se.compose.LAMBDA_MAX_KEY` / :data:`precis_se.compose.FWHM_KEY`,
resolved through the same star-schema path every other block fact uses);
with no row the band is **assumed** centred at the pump wavelength with the
coded :data:`DEFAULT_FWHM_NM`, and every finding that leans on the
assumption says so. A Gaussian is a coarse stand-in for a real absorption
spectrum (:class:`precis_se.fret.Spectrum` carries sampled ones for FRET);
it is enough to rank channel pairs and to name the ones a datasheet should
settle, which is the rule's job. **One band per block, not per state**: a
switch whose forward and reverse pumps hit different bands (azobenzene's
ππ* and nπ*) reads the same ``lambda_max`` row for both today; per-state
bands want ``conditions``-keyed rows and are a later slice.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from precis.design.states import Transition
from precis.errors import BadInput
from precis.utils.units import parse_quantity
from precis_se import compose as se_compose
from precis_se import library as se_library
from precis_se.validate import ValidationIssue

__all__ = [
    "CROSSTALK_WARN",
    "DEFAULT_FWHM_NM",
    "Band",
    "Channel",
    "band_resolver",
    "channels",
    "crosstalk",
    "findings",
    "parse_wavelength_nm",
]

#: Assumed band width when the driving block has no ``fwhm`` row — the
#: "tens of nm" the photoswitch item quotes for organic chromophores
#: (azobenzene's ππ* band is ~50 nm FWHM; 40 keeps the assumption on the
#: optimistic side so a real datasheet can only make a warning louder).
DEFAULT_FWHM_NM = 40.0

#: Direct excitation of the other channel's band, as a fraction of that
#: band's own peak, above which two channels stop being separately
#: addressable. 10 %: the same order as the FRET view's crosstalk floor.
CROSSTALK_WARN = 0.10

#: The channel kinds that spend the spectral budget.
_SPENDING_KINDS = frozenset({"light", "reaction"})

_SIGMA_PER_FWHM = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))


@dataclass(frozen=True)
class Channel:
    """One independently addressed channel: a light wavelength or a
    reagent. ``pump_nm`` is the parsed wavelength for a light channel
    (``None`` when the ``driver_ref`` is not a wavelength, or for a
    reaction); ``blocks`` are the blocks whose transitions use it."""

    label: str
    driver_kind: str
    pump_nm: float | None
    blocks: tuple[str, ...]


@dataclass(frozen=True)
class Band:
    """A Gaussian absorption band and where its numbers came from."""

    centre_nm: float
    fwhm_nm: float
    source: str


def parse_wavelength_nm(raw: Any) -> float | None:
    """A ``driver_ref`` read as a wavelength in nm — ``"405nm"``,
    ``"405 nm"``, ``"0.405 um"`` or a bare number (taken as nm) — else
    ``None`` (a named source such as ``"blue LED"`` is a channel with no
    wavelength, not an error)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        metres = parse_quantity(text, "length")
    except BadInput:
        try:
            metres = float(text) * 1e-9
        except ValueError:
            return None
    # Round away the m→nm float noise (405.00000000000006) so the number
    # a make step carries is the one the author wrote.
    nm = round(metres * 1e9, 6)
    return nm if math.isfinite(nm) and nm > 0.0 else None


def crosstalk(pump_nm: float, band: Band) -> float:
    """``band``'s normalised absorption at ``pump_nm`` — 1.0 at its centre,
    0.5 half a FWHM away, 0 far outside — i.e. how strongly a pump meant
    for another channel excites this one."""
    sigma = band.fwhm_nm * _SIGMA_PER_FWHM
    if sigma <= 0.0:
        return 1.0 if pump_nm == band.centre_nm else 0.0
    delta = pump_nm - band.centre_nm
    return math.exp(-(delta * delta) / (2.0 * sigma * sigma))


def channels(transitions: Mapping[str, Sequence[Transition]]) -> list[Channel]:
    """The distinct channels a design's transitions spend, keyed by block
    name → its transitions. Sorted by kind then label so a render is
    stable."""
    seen: dict[tuple[str, str], list[str]] = {}
    for block, edges in transitions.items():
        for t in edges:
            if t.driver_kind not in _SPENDING_KINDS:
                continue
            ref = (t.driver_ref or "").strip()
            users = seen.setdefault((t.driver_kind, ref), [])
            if block not in users:
                users.append(block)
    out: list[Channel] = []
    for (kind, ref), blocks in sorted(seen.items()):
        if kind == "light":
            label = ref or "light (no driver_ref)"
            pump = parse_wavelength_nm(ref)
        else:
            label = f"rxn:{ref}" if ref else "reaction (no driver_ref)"
            pump = None
        out.append(
            Channel(label=label, driver_kind=kind, pump_nm=pump, blocks=tuple(blocks))
        )
    return out


def findings(
    used: Sequence[Channel],
    *,
    channels_available: int | None,
    band_of: Callable[[Channel], Band],
) -> list[ValidationIssue]:
    """The two spectral rows over ``used`` channels (module docstring).
    ``band_of`` supplies each light channel's band — :func:`band_resolver`
    for the store-backed one, anything for a test."""
    rows: list[ValidationIssue] = []
    if not used:
        return rows
    if channels_available is not None and len(used) > channels_available:
        rows.append(
            ValidationIssue(
                rule="chain_channel_budget",
                subject="design",
                severity="error",
                detail=(
                    f"{len(used)} independently addressed channel(s) used "
                    f"({', '.join(c.label for c in used)}) > {channels_available} "
                    "available (set_optics channels_available) — sequence the "
                    "switches so fewer are addressed independently, or free a "
                    "channel"
                ),
            )
        )
    lit = [c for c in used if c.pump_nm is not None]
    bands = {c.label: band_of(c) for c in lit}
    for i, a in enumerate(lit):
        assert a.pump_nm is not None
        for b in lit[i + 1 :]:
            assert b.pump_nm is not None
            a_excites_b = crosstalk(a.pump_nm, bands[b.label])
            b_excites_a = crosstalk(b.pump_nm, bands[a.label])
            worst = max(a_excites_b, b_excites_a)
            if worst < CROSSTALK_WARN:
                continue
            pump, victim = (a, b) if a_excites_b >= b_excites_a else (b, a)
            assert pump.pump_nm is not None
            band = bands[victim.label]
            rows.append(
                ValidationIssue(
                    rule="chain_spectral_crosstalk",
                    subject=f"{a.label} ↔ {b.label}",
                    severity="warn",
                    detail=(
                        f"the {pump.label} pump excites {victim.label}'s band at "
                        f"{worst:.0%} of its own peak ({abs(pump.pump_nm - band.centre_nm):.0f} nm "
                        f"from its centre; band {band.source}; warn threshold "
                        f"{CROSSTALK_WARN:.0%}) — the two channels are not "
                        "independently addressable; move one wavelength, narrow "
                        "the band with a material row, or address them in sequence"
                    ),
                )
            )
    return rows


def band_resolver(store: Any, tree: Any) -> Callable[[Channel], Band]:
    """A ``band_of`` for :func:`findings` over the design's ``material``
    rows: the first of the channel's blocks with a ``lambda_max`` row sets
    the centre, its ``fwhm`` row the width; each missing number falls back
    to the pump / the coded default and the band's ``source`` says which."""
    slug = getattr(tree, "own_slug", None)
    ref = store.get_ref(kind="se", id=slug) if slug and store is not None else None
    cache = se_library._ReadCache(store) if ref is not None else None

    def row_nm(block: str, key: str) -> float | None:
        if ref is None or cache is None:
            return None
        node = tree.blocks.get(block)
        if node is None:
            return None
        cand = se_library._Candidate(str(slug), block, node, tree, ref.id)
        hit = se_library._resolve_star_value(cand, key, cache)
        if hit is None:
            return None
        row, unit, _provenance = hit
        value = se_library.value_row_number(row)
        if value is None:
            return None
        try:
            metres = parse_quantity(
                f"{value} {unit or se_compose.unit_label(cache, key)}", "length"
            )
        except BadInput:
            return None
        return metres * 1e9 if math.isfinite(metres) and metres > 0.0 else None

    def band_of(channel: Channel) -> Band:
        assert channel.pump_nm is not None
        centre = fwhm = None
        for block in channel.blocks:
            centre = (
                centre
                if centre is not None
                else row_nm(block, se_compose.LAMBDA_MAX_KEY)
            )
            fwhm = fwhm if fwhm is not None else row_nm(block, se_compose.FWHM_KEY)
        notes: list[str] = []
        if centre is None:
            centre = channel.pump_nm
            notes.append("centre ASSUMED at the pump (no lambda_max row)")
        else:
            notes.append(f"lambda_max {centre:.0f} nm from a material row")
        if fwhm is None:
            fwhm = DEFAULT_FWHM_NM
            notes.append(f"coded FWHM {DEFAULT_FWHM_NM:.0f} nm (no fwhm row)")
        else:
            notes.append(f"FWHM {fwhm:.0f} nm from a material row")
        return Band(centre_nm=centre, fwhm_nm=fwhm, source="; ".join(notes))

    return band_of
