"""Draft pin lint — warnings on ``[fi<hub>>pc<chunk>]`` / ``[fi<hub>+pc<chunk>]``
(signed-grounding recurrence, builds G3 and G4). Never blocks.

A pin picks the passage that carries a draft sentence: ``>`` replaces the
hub's default passage, ``+`` adds one. The failure it exists for: a
nanobuds-paper sentence about TEM and STS measurements, cited to fi189535
and re-pinned to pc209495, an abstract chunk that names neither — the
re-pin silently dropped the coverage the hub's own grounding had.

* **G3, pin coverage** — the sentence that holds the token (when it
  carries several hub cites, only the pin's own span, see
  :func:`find_pins`), run through G2's rule (:func:`precis.taproot.coverage.uncovered_terms`, via
  :func:`precis.nanopub.term_coverage.analyse`) against the pinned
  passage(s); a ``+`` pin is judged together with the hub's default
  (derived-grounding) passages. The acronym map comes from the hub's
  evidence papers, and each uncovered term carries G2's ranked
  suggestions. Bare years (1800-2099, no unit, no decimal) are citation
  history, not claimed values, and are not checked
  (:func:`_drop_years`; G2 at approve/sign still counts them). A pin on a paper (``pa``/``pt``, no passage) has nothing to
  compare and is skipped.
* **G4, the pin rule** — when the hub has a frozen grounding (a live
  publish row in reviewed/signed/anchored/published carrying
  ``grounding.passages``), the pinned chunk must belong to one of the
  grounding's *papers*. It need not be a grounding passage.

Where they surface: the draft write path appends them to the ``put``/``edit``
response (:func:`precis.handlers._draft_lint.pin_hint`, same channel as the
other advisory hints) and the exporters add them to ``ExportResult.warnings``
(:func:`export_warnings`). At **edit** only *changes* warn
(:func:`edit_findings`): a newly added pin (G3 + G4), or an existing pin
whose sentence changed so that a term is now uncovered that was not before.
At **export** every pin warns.

Known noise, left for the D3 sweep: a *negated* term ("rather than
atomically resolved imaging") still counts as claimed, and PDF extraction
that glues words ("ofCNT-C60CNBs") hides a term the passage does carry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.nanopub import evidence, term_coverage
from precis.nanopub.gates import integral_chunk_id
from precis.nanopub.term_coverage import PaperChunk, UncoveredTerm
from precis.taproot import seniority
from precis.taproot.coverage import KIND_NUMBER
from precis.utils import handle_registry, mentions
from precis.utils.sentences import split_sentences

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Publish states whose grounding is frozen (the rungs of
#: :func:`precis.nanopub.state.frozen_rung` that are not ``''``).
FROZEN_STATES = ("reviewed", "signed", "anchored", "published")

_TERM_CAP = 6
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


@dataclass(frozen=True, slots=True)
class Pin:
    """One pinned hub citation and the sentence that carries it."""

    token: str
    hub_ref_id: int
    hub_handle: str
    op: str
    handles: tuple[str, ...]
    #: The sentence holding the token, citation markup stripped.
    sentence: str

    @property
    def key(self) -> tuple[int, str, tuple[str, ...]]:
        return (self.hub_ref_id, self.op, self.handles)


@dataclass(frozen=True, slots=True)
class PinFinding:
    pin: Pin
    #: Terms the sentence names that the pinned passage(s) do not
    #: (G3); empty when covered or not checkable.
    uncovered: tuple[UncoveredTerm, ...] = ()
    #: The G4 message, or ``None``.
    rule: str | None = None


# ------------------------------------------------------------- parsing


def _mask(text: str) -> str:
    """``text`` with every bracketed reference replaced by same-length
    filler, so the sentence splitter never sees ``.`` / ``>`` / URLs inside
    markup and offsets stay valid."""
    out = text
    for pattern in (
        mentions.AUTHORING_PATTERN,
        mentions.DISPLAY_LINK_PATTERN,
        mentions.BARE_BRACKET_REF_PATTERN,
    ):
        out = pattern.sub(lambda m: "x" * len(m.group(0)), out)
    return out


def _strip_markup(text: str) -> str:
    """The sentence as prose: citation tokens dropped, display links
    reduced to their text."""
    text = mentions.AUTHORING_PATTERN.sub("", text)
    text = mentions.DISPLAY_LINK_PATTERN.sub(lambda m: m.group("disp"), text)
    text = mentions.BARE_BRACKET_REF_PATTERN.sub("", text)
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", _WS_RE.sub(" ", text)).strip()


def sentence_bounds(
    text: str, masked: str, spans: list[tuple[int, int]], start: int, end: int
) -> tuple[int, int]:
    """``(lo, hi)`` of the sentence of ``text`` holding the token at
    ``start:end``. A token that opens its sentence — ``…measured. [fi1>pc2]
    Next …`` — belongs to the sentence before it (also when the token
    follows the full stop with no space, which the splitter keeps in one
    sentence: then the sentence ends at the token)."""
    if not spans:
        return 0, len(text)
    idx = len(spans) - 1
    for i, (lo, hi) in enumerate(spans):
        if start < hi:
            idx = i
            break
    if idx > 0 and not masked[spans[idx][0] : start].strip():
        idx -= 1
    lo, hi = spans[idx]
    if start > lo and masked[start - 1] in ".!?" and end < hi:
        hi = end
    return lo, hi


def _has_words(text: str) -> bool:
    return re.search(r"[^\W\d_]{2,}", _strip_markup(text)) is not None


def _pin_text(text: str, lo: int, hi: int, cites: list[tuple[int, int]], k: int) -> str:
    """The prose a pin vouches for. ``cites`` are the hub-cite token spans
    inside the sentence ``text[lo:hi]``, ``k`` this pin's index among them.
    One cite: the whole sentence. Several: the span from the end of the
    previous cite (or the sentence start) up to and including this token —
    and when that span has no words (``X [fi1][fi2]``), back over the
    previous span(s) until one has; the whole sentence if none do."""
    if len(cites) <= 1:
        return _strip_markup(text[lo:hi])
    starts = [lo] + [c[1] for c in cites[:-1]]
    j = k
    while j >= 0:
        span = text[starts[j] : cites[k][1]]
        if _has_words(span):
            return _strip_markup(span).lstrip(" ,;:")
        j -= 1
    return _strip_markup(text[lo:hi])


def find_pins(text: str) -> list[Pin]:
    """Every pinned hub citation in ``text``, in order, each with the prose
    it vouches for (:func:`_pin_text`). Pins on a non-finding handle are not
    hub pins and are skipped."""
    out: list[Pin] = []
    if not text or "[fi" not in text:
        return out
    masked = _mask(text)
    spans = [
        (x.char_offset, x.char_offset + len(x.text)) for x in split_sentences(masked)
    ]
    matches: list[tuple[re.Match[str], int]] = []
    hub_cites: list[tuple[int, int]] = []
    for m in mentions.BARE_BRACKET_REF_PATTERN.finditer(text):
        parsed = handle_registry.parse(m.group("bare"))
        if parsed is None or parsed[0] != "finding" or parsed[1]:
            continue
        hub_cites.append((m.start(), m.end()))
        if m.group("pin"):
            matches.append((m, parsed[2]))
    for m, hub_ref_id in matches:
        op, handles = mentions.parse_pin_suffix(m.group("pin"))
        if op is None or not handles:
            continue
        lo, hi = sentence_bounds(text, masked, spans, m.start(), m.end())
        inside = [c for c in hub_cites if c[0] >= lo and c[1] <= hi]
        out.append(
            Pin(
                token=m.group(0),
                hub_ref_id=hub_ref_id,
                hub_handle=m.group("bare"),
                op=op,
                handles=tuple(handles),
                sentence=_pin_text(
                    text, lo, hi, inside, inside.index((m.start(), m.end()))
                ),
            )
        )
    return out


#: A year in running prose is citation history ("first reported in 1985"),
#: not a claimed value — unless it carries a unit or a decimal.
_YEAR_RE = re.compile(r"^(?:18|19|20)\d\d$")
_UNIT_AFTER_RE = re.compile(
    r"^(?:nm|[µμu]m|mm|cm|m|km|å|k|ev|mev|kev|v|mv|ma|g|mg|kg|s|ms|ns|ps|fs|hz|khz|"
    r"mhz|ghz|thz|pa|kpa|mpa|gpa|j|kj|mol|mmol|wt|vol|ppm|ppb|atm|bar|torr|w|mw|"
    r"kw|t|mt|min|h|hr|hrs|day|days|hour|hours|year|years|cycle|cycles|atoms?|"
    r"molecules?|samples?|devices?|k\)?|°c|°|%)[.,;:)]*$",
    re.IGNORECASE,
)


def _bare_year(term: str, sentence: str) -> bool:
    """True when ``term`` is a 4-digit year-like integer that occurs in
    ``sentence`` at least once with no glued unit, decimal or following
    unit word."""
    if not _YEAR_RE.match(term):
        return False
    words = sentence.split()
    for i, raw in enumerate(words):
        if raw.strip("()[]{},;:~±").rstrip(".") != term:
            continue  # also rejects a decimal ("1985.5") or a glued unit
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if nxt and _UNIT_AFTER_RE.match(nxt):
            continue
        return True
    return False


def _drop_years(sentence: str, items: list[UncoveredTerm]) -> list[UncoveredTerm]:
    return [
        i
        for i in items
        if not (i.term.kind == KIND_NUMBER and _bare_year(i.term.text, sentence))
    ]


# ------------------------------------------------------------- DB reads


@dataclass(slots=True)
class _Hub:
    is_hub: bool = False
    default_texts: list[str] | None = None
    default_refs: set[int] | None = None
    source_refs: set[int] | None = None
    chunks: list[PaperChunk] | None = None
    abstracts: dict[int, str] | None = None
    frozen_state: str = ""
    frozen_refs: set[int] | None = None


class PinContext:
    """Per-call read cache: one hub bundle / paper-chunk load per hub, one
    chunk lookup per pinned handle. The export walks every chunk of a
    draft, so it shares one context across them."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._hubs: dict[int, _Hub] = {}
        self._chunk_infos: dict[int, evidence.ChunkInfo | None] = {}
        self._extra: dict[int, tuple[list[PaperChunk], dict[int, str]]] = {}

    def hub(self, hub_ref_id: int) -> _Hub:
        cached = self._hubs.get(hub_ref_id)
        if cached is not None:
            return cached
        hub = _Hub()
        self._hubs[hub_ref_id] = hub
        if not seniority.is_claim_hub(self.store, hub_ref_id):
            return hub
        hub.is_hub = True
        bundle = evidence.load_bundle(self.store, hub_ref_id)
        hub.default_texts = [c.text for c in bundle.grounding_chunks if c.text]
        hub.default_refs = {c.ref_id for c in bundle.grounding_chunks}
        hub.source_refs = {s.ref_id for s in bundle.sources}
        hub.chunks, hub.abstracts = term_coverage.paper_chunks(
            self.store, sorted(hub.source_refs | hub.default_refs)
        )
        row = self.store.nanopub_publish_row(hub_ref_id)
        hub.frozen_refs = set()
        if row is not None and row.state in FROZEN_STATES:
            ids = [
                cid
                for p in (row.grounding or {}).get("passages") or []
                if (cid := integral_chunk_id(p.get("chunk_id"))) is not None
            ]
            refs = {c.ref_id for c in evidence.fetch_chunks(self.store, ids)}
            if refs:
                hub.frozen_state = row.state
                hub.frozen_refs = refs
        return hub

    def chunk_info(self, chunk_id: int) -> evidence.ChunkInfo | None:
        if chunk_id not in self._chunk_infos:
            found = evidence.fetch_chunks(self.store, [chunk_id])
            self._chunk_infos[chunk_id] = found[0] if found else None
        return self._chunk_infos[chunk_id]

    def extra_chunks(self, ref_id: int) -> tuple[list[PaperChunk], dict[int, str]]:
        if ref_id not in self._extra:
            self._extra[ref_id] = term_coverage.paper_chunks(self.store, [ref_id])
        return self._extra[ref_id]

    def pinned(self, pin: Pin) -> tuple[list[evidence.ChunkInfo], set[int]]:
        """``(pinned chunks, pinned paper ref ids)`` — a ``pc`` handle
        yields its chunk and that chunk's paper; a ``pa``/``pt`` handle
        only the paper."""
        chunks: list[evidence.ChunkInfo] = []
        refs: set[int] = set()
        for handle in pin.handles:
            parsed = handle_registry.parse(handle)
            if parsed is None or parsed[0] not in ("paper", "patent"):
                continue
            _kind, is_chunk, pk = parsed
            if is_chunk:
                info = self.chunk_info(pk)
                if info is not None:
                    chunks.append(info)
                    refs.add(info.ref_id)
            else:
                refs.add(pk)
        return chunks, refs


# ------------------------------------------------------------- the checks


def coverage_core(
    pin: Pin,
    *,
    pinned_texts: list[str],
    pinned_refs: set[int],
    default_texts: list[str],
    default_refs: set[int],
    frozen_refs: set[int],
    chunks: list[PaperChunk],
    abstracts: dict[int, str],
) -> list[UncoveredTerm]:
    """The DB-free core of :func:`pin_coverage` (G3): the passages the pin
    puts forward — its own, plus the hub's default ones for a ``+`` pin —
    against the pin's sentence. ``chunks``/``abstracts`` are the hub's
    evidence papers' (and the pinned papers') live body chunks."""
    texts = list(pinned_texts)
    grounding_refs = set(pinned_refs) | frozen_refs
    if pin.op == "+":
        texts += default_texts
        grounding_refs |= default_refs
    return _drop_years(
        pin.sentence,
        term_coverage.analyse(pin.sentence, texts, chunks, abstracts, grounding_refs),
    )


def outside_grounding(pinned_refs: set[int], frozen_refs: set[int]) -> list[int]:
    """G4's test: the pinned papers that are not papers of the frozen
    grounding (nothing to test when there is no frozen grounding)."""
    if not frozen_refs:
        return []
    return sorted(pinned_refs - frozen_refs)


def pin_coverage(ctx: PinContext, pin: Pin) -> list[UncoveredTerm] | None:
    """G3 for one pin, or ``None`` when it is not checkable (not a hub,
    no pinned passage, an empty sentence)."""
    if not pin.sentence:
        return None
    hub = ctx.hub(pin.hub_ref_id)
    if not hub.is_hub:
        return None
    pinned, pinned_refs = ctx.pinned(pin)
    if not pinned:
        return None
    chunks = list(hub.chunks or [])
    abstracts = dict(hub.abstracts or {})
    for ref_id in sorted(
        pinned_refs - (hub.source_refs or set()) - (hub.default_refs or set())
    ):
        more, more_abs = ctx.extra_chunks(ref_id)
        chunks += more
        abstracts.update(more_abs)
    return coverage_core(
        pin,
        pinned_texts=[c.text for c in pinned],
        pinned_refs=pinned_refs,
        default_texts=hub.default_texts or [],
        default_refs=hub.default_refs or set(),
        frozen_refs=hub.frozen_refs or set(),
        chunks=chunks,
        abstracts=abstracts,
    )


def pin_rule(ctx: PinContext, pin: Pin) -> str | None:
    """G4 for one pin: the warning text when the hub has a frozen grounding
    and the pin names a paper outside its papers, else ``None``."""
    hub = ctx.hub(pin.hub_ref_id)
    if not hub.is_hub:
        return None
    _chunks, pinned_refs = ctx.pinned(pin)
    outside = outside_grounding(pinned_refs, hub.frozen_refs or set())
    if not outside:
        return None
    return (
        f"{pin.token} pins {_papers(ctx.store, outside)}, which is not one of the "
        f"papers of the hub's {hub.frozen_state} grounding "
        f"({_papers(ctx.store, sorted(hub.frozen_refs or set()))}). Warning only — "
        "pin a chunk of a grounding paper (it need not be a grounding passage), "
        "or re-ground the hub."
    )


def _papers(store: Store, ref_ids: list[int]) -> str:
    refs = store.fetch_refs_by_ids(ref_ids)
    names = []
    for rid in ref_ids:
        ref = refs.get(rid)
        kind = ref.kind if ref is not None else "paper"
        handle = handle_registry.try_format(kind, rid) or f"ref {rid}"
        slug = getattr(ref, "slug", None)
        names.append(f"{handle} ({slug})" if slug else handle)
    return ", ".join(names)


def _key(term: UncoveredTerm) -> tuple[str, str]:
    return (term.term.kind, term.term.text)


def newly_uncovered(
    now: list[UncoveredTerm], before: list[UncoveredTerm] | None
) -> list[UncoveredTerm]:
    """The terms uncovered ``now`` that were not uncovered ``before``
    (all of them when there was no ``before`` — a new pin)."""
    if before is None:
        return now
    seen = {_key(t) for t in before}
    return [t for t in now if _key(t) not in seen]


def check_pin(ctx: PinContext, pin: Pin, *, rule: bool = True) -> PinFinding:
    return PinFinding(
        pin=pin,
        uncovered=tuple(pin_coverage(ctx, pin) or ()),
        rule=pin_rule(ctx, pin) if rule else None,
    )


def export_findings(
    store: Store, text: str, ctx: PinContext | None = None
) -> list[PinFinding]:
    """Every pin in ``text``, checked (G3 + G4)."""
    return _check_all(store, find_pins(text), ctx)


def _check_all(
    store: Store, pins: list[Pin], ctx: PinContext | None
) -> list[PinFinding]:
    if not pins:
        return []
    ctx = ctx or PinContext(store)
    checked = (check_pin(ctx, p) for p in pins)
    return [f for f in checked if f.uncovered or f.rule]


def edit_findings(
    store: Store, new_text: str, old_text: str = "", ctx: PinContext | None = None
) -> list[PinFinding]:
    """What this edit changed, pin-wise. A pin absent from ``old_text``
    (same hub, operator and handles) is *new*: G3 reports every uncovered
    term and G4 applies. A pin that was already there is re-checked only if
    its sentence changed, and reports only terms uncovered now that were
    not before (G4 is not repeated). Untouched pins say nothing."""
    new_pins = find_pins(new_text)
    if not new_pins:
        return []
    ctx = ctx or PinContext(store)
    old_by_key: dict[tuple[int, str, tuple[str, ...]], list[Pin]] = {}
    for p in find_pins(old_text):
        old_by_key.setdefault(p.key, []).append(p)
    out: list[PinFinding] = []
    seen: set[tuple[tuple[int, str, tuple[str, ...]], str]] = set()
    for pin in new_pins:
        if (pin.key, pin.sentence) in seen:
            continue
        seen.add((pin.key, pin.sentence))
        olds = old_by_key.get(pin.key)
        if olds is None:
            finding = check_pin(ctx, pin)
        else:
            if any(o.sentence == pin.sentence for o in olds):
                continue
            now = pin_coverage(ctx, pin)
            if not now:
                continue
            before = pin_coverage(ctx, olds[0]) or []
            finding = PinFinding(pin=pin, uncovered=tuple(newly_uncovered(now, before)))
        if finding.uncovered or finding.rule:
            out.append(finding)
    return out


# ------------------------------------------------------------- rendering


def render_finding(finding: PinFinding) -> list[str]:
    """One line per check that fired."""
    lines: list[str] = []
    if finding.uncovered:
        items = list(finding.uncovered)
        shown = "; ".join(term_coverage.describe(i) for i in items[:_TERM_CAP])
        more = len(items) - _TERM_CAP
        lines.append(
            f"{finding.pin.token} {len(items)} term(s) its sentence names appear "
            f"in no pinned passage: {shown}{f'; +{more} more' if more > 0 else ''}. "
            "Warning only — pin a passage that carries them, or reword the sentence."
        )
    if finding.rule:
        lines.append(finding.rule)
    return lines


def export_warnings(
    store: Store, text: str, ctx: PinContext | None = None
) -> list[str]:
    """Export-time lines for every pin in ``text`` — never raises."""
    try:
        return [
            f"pin: {line}"
            for f in export_findings(store, text, ctx)
            for line in render_finding(f)
        ]
    except Exception:
        log.warning("pin lint failed at export", exc_info=True)
        return []


def warn_export(ctx: Any, text: str) -> None:
    """Add the pin warnings for one chunk's ``text`` to an exporter's
    ``ctx.warnings`` (deduped). ``ctx`` is either exporter's ``_Ctx``:
    it carries ``store``, ``warnings`` and a ``pin_ctx`` slot that holds
    the read cache across the draft's chunks. Costs one regex scan when the
    text has no pin."""
    if ctx.store is None or "[fi" not in text:
        return
    try:
        pins = find_pins(text)
        if not pins:
            return
        if ctx.pin_ctx is None:
            ctx.pin_ctx = PinContext(ctx.store)
        for finding in _check_all(ctx.store, pins, ctx.pin_ctx):
            for line in render_finding(finding):
                warning = f"pin: {line}"
                if warning not in ctx.warnings:
                    ctx.warnings.append(warning)
    except Exception:
        log.warning("pin lint failed at export", exc_info=True)
