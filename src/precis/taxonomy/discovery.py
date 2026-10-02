"""Stage 2 — open-vocabulary model discovery over stage-1 mentions.

``taxonomy-bootstrap.md`` §Design, stage 2. Hubs are split into halves A and
B *before* a run (:func:`split_halves`); the split has to be reproducible
from the salt alone, because the A/B vocabulary overlap is the run's own
honesty check (AC2) — a split chosen after seeing results would measure
nothing.

Per hub sentence, :func:`build_prompt` asks the model for one JSON row per
marked mention: an open-vocabulary measurand string, dimension text,
reference state, convention, normalisation basis, the conditions the value
depends on, and a subject label. Nothing here enumerates candidate
measurands — a menu would close the vocabulary discovery is supposed to
open. Campaign qualifier vocabulary (``categorical_qualifiers``,
``site_classes``) is passed as hints for the *qualifier* fields only, kept
visually separate from the open measurand field so the model cannot read it
as a menu either.

The vocabulary is open; the *shape* of a measurand is not. The prompt caps
it at :data:`_MEASURAND_MAX_WORDS` words, names the field each other part of
the answer belongs in, and shows the shape with an out-of-domain example —
because the first probe, which asked only for "your own words", returned
sentences rather than names and scored 0.046 A/B overlap against a signed
0.80. :func:`build_prompt`'s docstring has the evidence.

:func:`parse_response` is defensive by construction: a malformed reply, a
missing or out-of-range mention index, or a row with no measurand each
produce a warning string and are skipped — never an exception, never a
fabricated :class:`~precis.taxonomy.types.DiscoveredTerm`. Warnings are
returned, never printed and never silently dropped, so :func:`discover` can
run over a whole snapshot and still report exactly what the model did not
address. Two of them are not failures and are worded to say so: a null
measurand carrying a ``skip_reason`` is the model declining a mention that
labels rather than measures, and an over-cap measurand is *kept* with a
warning, because dropping it would destroy the evidence about the prompt
that the warning exists to collect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.types import DiscoveredTerm, Half, Mention

log = logging.getLogger(__name__)

#: Strips a single leading/trailing markdown code fence (`````json ... ````
#: or plain ` ``` `), the shape a chat model reaches for even when told not
#: to. Matched non-greedily against the whole stripped payload so a fence
#: elsewhere in the text (there shouldn't be one) is left alone.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

#: Word cap stated in :func:`build_prompt` and checked in
#: :func:`parse_response`. Six, not three: a species-specific quantity
#: legitimately spends words on the species (``Faradaic efficiency for
#: NH3``, ``NH3 partial current density``), and the species is part of
#: *which* quantity this is rather than a condition on it. Over the cap the
#: row is kept and a warning is emitted — the model has been told the rule,
#: so a breach is evidence about the prompt, and dropping the row would
#: destroy exactly the evidence needed to fix it.
_MEASURAND_MAX_WORDS: Final[int] = 6


@dataclass(frozen=True, slots=True)
class Reply:
    """One transport reply: the raw text plus whatever the transport metered.

    Every metering field is optional because transports differ in what they
    report (``claude -p`` gives cost and the four token counts, a loopback
    local model gives neither) — ``None`` means *unreported*, never zero.
    ``duration_s`` is the transport's own reading when it has one;
    :func:`discover` measures the wall-clock around the call itself.
    """

    text: str
    model: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One metered discovery call, failed or not — a ``responses.jsonl`` row.

    ``taxonomy-bootstrap.md`` §Resume: the paid full run is ~1231 calls, and
    the choice between a thread pool and packing several hubs per call
    turns on whether the shared prompt prefix is served from cache — a
    number only the transport's own token counts can give. The row keeps
    the raw ``payload`` too, so a parse rule can change and be replayed
    over what the model actually said without paying again.
    ``prompt_sha256`` identifies the prompt without storing it (the prompt
    is rebuilt deterministically from the snapshot row, AC1);
    ``duration_s`` is :func:`discover`'s own wall-clock around the call. A
    failed call has ``payload=None`` and ``error`` set — it still cost time
    and possibly money, so it is still a row. ``terms``/``warnings`` count
    what :func:`parse_response` made of the payload.

    ``pack_size`` > 1 marks a hub that shared its call with others
    (:func:`build_packed_prompt`): the rows of one call share
    ``prompt_sha256``, the metering is that hub's share of the call, and
    ``payload`` is the hub's own slice of the reply.
    """

    ref_id: int
    half: Half
    prompt_sha256: str
    prompt_chars: int
    duration_s: float
    payload: str | None
    error: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    terms: int = 0
    warnings: int = 0
    pack_size: int = 1

    def to_json(self) -> dict[str, object]:
        return {
            "ref_id": self.ref_id,
            "half": self.half,
            "prompt_sha256": self.prompt_sha256,
            "prompt_chars": self.prompt_chars,
            "duration_s": self.duration_s,
            "payload": self.payload,
            "error": self.error,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "terms": self.terms,
            "warnings": self.warnings,
            "pack_size": self.pack_size,
        }


class DiscoveryClient(Protocol):
    """The one-shot completion seam stage 2 depends on.

    ``complete_json`` takes a fully built prompt and returns the model's raw
    text reply — bare (an unmetered reply, what the test fakes return) or
    as a :class:`Reply` carrying the transport's metering. Tests inject a
    fake; production wires :func:`router_client`. A transport failure is
    raised, never returned as empty text: :func:`discover` turns it into a
    warning and a failed :class:`CallRecord`.
    """

    def complete_json(self, prompt: str) -> Reply | str: ...


def split_halves(ref_ids: Sequence[int], *, salt: str) -> dict[int, Half]:
    """Assign each ``ref_id`` to half ``"A"`` or ``"B"``, deterministically.

    Hashes ``salt:ref_id`` with ``sha256`` (never :func:`hash`, which is
    randomised per interpreter process) and takes the parity of the first
    digest byte. The split must be reproducible from the salt alone and
    independent of the order ``ref_ids`` is supplied in — it is computed
    *before* a discovery run, and the run's own A/B vocabulary-overlap check
    (AC2) is only honest if the split could not have been chosen after
    seeing which side a term would land on. Only "roughly balanced" is
    promised; nothing tighter.
    """
    halves: dict[int, Half] = {}
    for ref_id in ref_ids:
        digest = hashlib.sha256(f"{salt}:{ref_id}".encode()).digest()
        halves[ref_id] = "A" if digest[0] % 2 == 0 else "B"
    return halves


def build_prompt(text: str, mentions: Sequence[Mention], config: CampaignConfig) -> str:
    """Build the stage-2 prompt for one hub sentence.

    Deliberately open: the measurand field is asked for in the model's own
    words, with no candidate list anywhere in the text — a menu would make
    discovery circular. Campaign vocabulary
    (``config.categorical_qualifiers``, ``config.site_classes``) is offered
    only as qualifier hints, under a heading that says so, never mixed into
    the measurand instruction.

    **Open vocabulary is not the same as free-form prose.** The first probe
    asked only for "what is being measured, in your own words" and got 166
    distinct strings over 204 rows — A/B vocabulary overlap 0.046 against a
    signed threshold of 0.80. The strings were not wrong, they were
    *sentences*: five spellings of Faradaic efficiency, a measurand reading
    ``applied electrode potential at which the yield rate and Faradaic
    efficiency were measured`` (a condition folded into the name), another
    reading ``rate of NH3 production normalized to electrode area (yield)``
    (the normalisation basis folded in), and several of the form ``... of the
    Cu surface identified as the most active support for the isolated Pd
    atom`` (a disambiguating clause folded in). Every one of those had an
    empty field of its own to go in.

    So this prompt constrains the *shape* of the answer — short noun phrase,
    a length cap, one worked example, and an explicit list of what belongs in
    the other fields instead — while still never naming a candidate
    measurand. The worked example is drawn from thermal transport, a field
    this campaign does not cover, so that demonstrating the shape cannot seed
    the vocabulary.

    The last instruction is the one that lets the model say "no": a
    crystallographic facet index and a composition subscript are labels, not
    quantities, and the first probe turned them into measurands like
    ``compositional index X`` because the prompt gave it no other move.
    """
    lines = [
        "You are reading one sentence from a scientific claim. For each "
        "numbered mention below, name the quantity the number measures.",
        "",
        f"Sentence: {text}",
        "",
        "Mentions:",
        *_mention_lines(mentions),
        "",
        *_hint_lines(config),
        *_SHAPE_LINES,
        "Reply with a JSON array and nothing else — no prose, no "
        "markdown fence. One object per mention, with exactly these "
        "keys:",
        *_ROW_KEY_LINES,
        "Every mention index above must appear exactly once.",
    ]
    return "\n".join(lines)


def build_packed_prompt(
    hubs: Sequence[tuple[int, str, Sequence[Mention]]], config: CampaignConfig
) -> str:
    """Build one stage-2 prompt for several hub sentences at once.

    ``hubs`` is ``(ref_id, sentence, mentions)`` per hub, in call order.
    ``taxonomy-bootstrap.md`` §Third probe: each ``claude -p`` call carried
    ~21 000 tokens of per-process harness prefix against a ~1 000-token
    prompt, so one hub per call pays that overhead once per hub. Packing K
    hubs pays it once per K.

    The instructions are :func:`build_prompt`'s, word for word — the field
    rules, the worked example and the decline are what three probes tuned,
    and a packed run that reworded them would measure the rewording, not
    the packing. What changes is the framing: each sentence is labelled with
    its claim id and read on its own, and the reply is a JSON object keyed
    by claim id whose values are exactly the per-hub arrays
    :func:`build_prompt` asks for, so :func:`parse_packed_response` hands
    each value to the same row parser.
    """
    lines = [
        f"You are reading {len(hubs)} sentences, each from a different "
        "scientific claim and each labelled with its claim id. For each "
        "numbered mention under a sentence, name the quantity the number "
        "measures. Read every claim on its own: a mention's quantity comes "
        "from its own sentence, never from another claim's.",
        "",
    ]
    for ref_id, text, mentions in hubs:
        lines.extend(
            [
                f"Claim {ref_id}:",
                f"Sentence: {text}",
                "Mentions:",
                *_mention_lines(mentions),
                "",
            ]
        )
    lines.extend(
        [
            *_hint_lines(config),
            *_SHAPE_LINES,
            "Reply with a JSON object and nothing else — no prose, no "
            "markdown fence. Its keys are the claim ids above, as strings; "
            "each value is an array with one object per mention of that "
            "claim, with exactly these keys:",
            *_ROW_KEY_LINES,
            "Every claim id above must appear exactly once as a key, and "
            "every mention index under a claim must appear exactly once in "
            "that claim's array.",
        ]
    )
    return "\n".join(lines)


def _mention_lines(mentions: Sequence[Mention]) -> list[str]:
    lines = []
    for index, mention in enumerate(mentions):
        unit = mention.raw_unit if mention.raw_unit is not None else "(no unit)"
        lines.append(
            f"  [{index}] literal={mention.literal!r} kind={mention.kind} "
            f"unit={unit} context={mention.context!r}"
        )
    return lines


def _hint_lines(config: CampaignConfig) -> list[str]:
    if not (config.categorical_qualifiers or config.site_classes):
        return []
    lines = [
        "Vocabulary hints for QUALIFIER fields only — never use these "
        "to name the measurand itself:"
    ]
    for key, values in config.categorical_qualifiers.items():
        lines.append(f"  {key}: {', '.join(values)}")
    if config.site_classes:
        lines.append(f"  site_class: {', '.join(config.site_classes)}")
    lines.append("")
    return lines


#: The field rules every discovery prompt carries — single-hub and packed
#: alike. :func:`build_prompt`'s docstring has the probe evidence for each
#: sentence; change them there, in one place, or a packed run and a
#: single-hub run stop being comparable.
_SHAPE_LINES: Final[tuple[str, ...]] = (
    "The measurand is the NAME OF THE QUANTITY AND NOTHING ELSE. "
    f"Write it as a short noun phrase of at most {_MEASURAND_MAX_WORDS} "
    "words — the way a table column header or a figure axis label "
    "reads, not the way a sentence reads. Use your own words; there "
    "is no list to pick from. Two mentions of the same quantity, in "
    "different papers and different sentences, must come back as the "
    "same string.",
    "",
    "Everything that is not the quantity's name has its own field:",
    "  - the conditions the value was measured at, and any clause "
    "that picks this value out from other values of the same quantity "
    "-> required_conditions",
    "  - what the value is per / divided by / normalised to -> normalisation_basis",
    "  - what a potential or energy is measured against -> reference_state",
    "  - the sign or direction convention -> convention",
    "  - the material, electrode, site or structure the value belongs "
    "to -> subject_label",
    "",
    'So the measurand contains no parentheses, no "at which ...", '
    'no "of the ... that ...", no "normalized to ...", and no '
    "clause saying which of several compared cases this one is. Do "
    "include the chemical species when the quantity is "
    "species-specific: that is part of which quantity it is, not a "
    "condition.",
    "",
    "If the number is a difference or a change between two cases "
    "rather than a value, name the quantity with one leading word "
    '("change in ...", "difference in ...") and put the two cases '
    "being compared in required_conditions.",
    "",
    "Example of the shape (a different field, so do not reuse these "
    'words). For the sentence "the thermal conductivity of the '
    "annealed film reached 42 W/m/K at 300 K, referenced to the "
    'as-grown film", the mention 42 gives measurand "thermal '
    'conductivity", required_conditions ["temperature 300 K"], '
    'reference_state "as-grown film", subject_label "annealed film" '
    '— and NOT "thermal conductivity of the annealed film at 300 K".',
    "",
    "Not every number measures a quantity. If a mention is an "
    "identifier or a label rather than a measurement — a "
    "crystallographic facet index, a composition subscript standing "
    "for a series member, a sample or figure number, a count of "
    "samples — do not invent a quantity for it. Return that mention "
    'as {"index": N, "measurand": null, "skip_reason": "<why>"}.',
    "",
)

#: The per-mention row keys, shared by both prompt shapes.
_ROW_KEY_LINES: Final[tuple[str, ...]] = (
    "  index (integer, matching a mention above)",
    "  measurand (string — required, or null with a skip_reason)",
    "  dimension_text (string or null — what kind of quantity this "
    'is, in words, e.g. "potential" or "mass per time per area"; '
    "recorded for audit only, the unit above is what is parsed)",
    "  reference_state (string or null)",
    "  convention (string or null)",
    "  normalisation_basis (string or null)",
    "  subject_label (string or null)",
    "  required_conditions (array of strings, may be empty)",
    "  skip_reason (string, only on a null measurand)",
)


def _strip_fences(payload: str) -> str:
    text = payload.strip()
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def parse_response(
    payload: str, mentions: Sequence[Mention], half: Half
) -> tuple[tuple[DiscoveredTerm, ...], tuple[str, ...]]:
    """Turn one raw model reply into terms + warnings, never an exception.

    Every failure mode — non-JSON payload, a reply that isn't a JSON array,
    a row missing or with an invalid ``index``, an index outside
    ``mentions``, a row with no (or blank) ``measurand`` — produces a
    warning string naming the problem and is skipped, not raised and not
    turned into a fabricated :class:`DiscoveredTerm`. A mention the model
    never addressed also produces a warning, so a caller can tell "the model
    said nothing" apart from "the model said something malformed".
    """
    text = _strip_fences(payload)
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        return (), (f"malformed JSON reply: {exc}",)
    if not isinstance(rows, list):
        return (), (f"reply is not a JSON array (got {type(rows).__name__})",)
    return _parse_rows(rows, mentions, half)


def parse_packed_response(
    payload: str, hubs: Sequence[tuple[int, Sequence[Mention], Half]]
) -> tuple[
    dict[int, tuple[tuple[DiscoveredTerm, ...], tuple[str, ...], str]], tuple[str, ...]
]:
    """Split one packed reply (:func:`build_packed_prompt`) per hub.

    ``hubs`` is ``(ref_id, mentions, half)`` per hub in the call. Returns
    ``(per_ref, call_warnings)``: ``per_ref[ref_id]`` is that hub's terms,
    warnings and *payload slice* — the hub's own JSON array re-serialised,
    which is exactly what :func:`parse_response` would accept, so a
    ``responses.jsonl`` row from a packed run replays through the single-hub
    parser. Where there is no slice (the whole reply is malformed, or the
    model left this claim out) the slice is the whole raw reply, so a replay
    sees what actually came back. ``call_warnings`` are about the call, not
    any one hub: a key that names no claim in the call.

    Same contract as :func:`parse_response` — never raises, never fabricates.
    A whole-reply failure is a warning on every hub in the call, because
    every one of them got nothing.
    """
    text = _strip_fences(payload)
    try:
        reply = json.loads(text)
    except json.JSONDecodeError as exc:
        failure = f"malformed JSON reply: {exc}"
        return {ref_id: ((), (failure,), payload) for ref_id, _m, _h in hubs}, ()
    if not isinstance(reply, dict):
        failure = f"packed reply is not a JSON object (got {type(reply).__name__})"
        return {ref_id: ((), (failure,), payload) for ref_id, _m, _h in hubs}, ()

    by_key = {str(key).strip(): value for key, value in reply.items()}
    expected = {str(ref_id) for ref_id, _m, _h in hubs}
    call_warnings = tuple(
        f"packed reply names claim {key!r}, which is not in this call"
        for key in by_key
        if key not in expected
    )
    per_ref: dict[int, tuple[tuple[DiscoveredTerm, ...], tuple[str, ...], str]] = {}
    for ref_id, mentions, half in hubs:
        if str(ref_id) not in by_key:
            per_ref[ref_id] = (
                (),
                ("claim not addressed in the packed reply",),
                payload,
            )
            continue
        rows = by_key[str(ref_id)]
        slice_ = json.dumps(rows, ensure_ascii=False)
        if not isinstance(rows, list):
            failure = f"claim value is not a JSON array (got {type(rows).__name__})"
            per_ref[ref_id] = ((), (failure,), slice_)
            continue
        terms, warnings = _parse_rows(rows, mentions, half)
        per_ref[ref_id] = (terms, warnings, slice_)
    return per_ref, call_warnings


def _parse_rows(
    rows: list[Any], mentions: Sequence[Mention], half: Half
) -> tuple[tuple[DiscoveredTerm, ...], tuple[str, ...]]:
    warnings: list[str] = []
    terms: list[DiscoveredTerm] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            warnings.append(f"row is not an object: {row!r}")
            continue
        index = row.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            warnings.append(f"row has no valid integer index: {row!r}")
            continue
        if index < 0 or index >= len(mentions):
            warnings.append(
                f"row names mention index {index}, out of range for "
                f"{len(mentions)} mention(s)"
            )
            continue
        seen.add(index)
        measurand = row.get("measurand")
        if not isinstance(measurand, str) or not measurand.strip():
            # A null measurand WITH a reason is the model using the decline
            # the prompt offers it, not a failure: a facet index or a
            # composition subscript is a label, and the first probe named
            # those as measurands only because it had no other move. Both
            # cases skip the row; they are worded differently because one is
            # a prompt working and the other is a prompt to fix, and a run's
            # warning list is the only place that distinction survives.
            reason = row.get("skip_reason")
            if isinstance(reason, str) and reason.strip():
                warnings.append(
                    f"mention {index}: declined as not a measurand ({reason.strip()})"
                )
            else:
                warnings.append(f"mention {index}: empty or missing measurand")
            continue
        measurand = measurand.strip()
        word_count = len(measurand.split())
        if word_count > _MEASURAND_MAX_WORDS:
            warnings.append(
                f"mention {index}: measurand is {word_count} words, over the "
                f"{_MEASURAND_MAX_WORDS}-word cap — kept: {measurand!r}"
            )
        terms.append(
            DiscoveredTerm(
                mention=mentions[index],
                measurand=measurand,
                half=half,
                dimension_text=_opt_str(row.get("dimension_text")),
                reference_state=_opt_str(row.get("reference_state")),
                convention=_opt_str(row.get("convention")),
                normalisation_basis=_opt_str(row.get("normalisation_basis")),
                subject_label=_opt_str(row.get("subject_label")),
                required_conditions=tuple(
                    str(c) for c in row.get("required_conditions") or ()
                ),
            )
        )
    for index, mention in enumerate(mentions):
        if index not in seen:
            warnings.append(
                f"mention {index} ({mention.literal!r}) not addressed by the model"
            )
    return tuple(terms), tuple(warnings)


def _record(
    ref_id: int,
    half: Half,
    prompt: str,
    duration_s: float,
    reply: Reply | None,
    *,
    error: str | None = None,
    terms: int = 0,
    warnings: int = 0,
    payload: str | None = None,
    share: tuple[int, int] = (0, 1),
) -> CallRecord:
    """One :class:`CallRecord`; ``share=(i, k)`` is hub ``i`` of a ``k``-hub call.

    A packed call's metering is apportioned so a column summed over
    ``responses.jsonl`` is still the run's total: cost and duration divide
    evenly, a token count splits with the remainder on the first hubs, and
    an unreported field stays ``None`` (never ``0``). ``payload`` overrides
    the reply text — a packed hub's row stores its own slice of the reply.
    """
    metered = reply if reply is not None else Reply(text="")
    index, size = share

    def tokens(total: int | None) -> int | None:
        if total is None:
            return None
        return total // size + (1 if index < total % size else 0)

    return CallRecord(
        ref_id=ref_id,
        half=half,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_chars=len(prompt),
        duration_s=duration_s / size,
        payload=payload if payload is not None else (reply.text if reply else None),
        error=error,
        model=metered.model,
        cost_usd=None if metered.cost_usd is None else metered.cost_usd / size,
        input_tokens=tokens(metered.input_tokens),
        output_tokens=tokens(metered.output_tokens),
        cache_read_tokens=tokens(metered.cache_read_tokens),
        cache_creation_tokens=tokens(metered.cache_creation_tokens),
        terms=terms,
        warnings=warnings,
        pack_size=size,
    )


def discover(
    rows: Sequence[Mapping[str, object]],
    mentions_by_ref: Mapping[int, Sequence[Mention]],
    config: CampaignConfig,
    client: DiscoveryClient,
    *,
    halves: Mapping[int, Half],
    on_call: Callable[[CallRecord], None] | None = None,
    pack: int = 1,
) -> tuple[tuple[DiscoveredTerm, ...], tuple[str, ...]]:
    """Run stage 2 over a snapshot's rows.

    ``rows`` are the raw snapshot records (``config.snapshot.ref_field`` /
    ``.text_field`` name the columns), ``mentions_by_ref`` is stage 1's
    output keyed by ref id, ``halves`` is :func:`split_halves`'s output. A
    row with no mentions is skipped without a client call — there is
    nothing to discover. A client failure (network, quota, a bad response)
    on one row is caught and turned into a warning so one bad row cannot
    abort a whole snapshot run (AC2's stability check needs the run to
    finish even when it is about to fail its own threshold).

    ``on_call`` receives one :class:`CallRecord` per client call, failed
    calls included, *as each call completes* — a sink that appends to disk
    keeps the metering of a run that is killed at hour nine of eleven.

    ``pack`` > 1 sends ``pack`` hubs per call (:func:`build_packed_prompt`,
    the last call takes the remainder) and still emits one record per hub,
    with the call's metering apportioned (see :func:`_record`). ``pack=1``
    is the single-hub prompt, byte for byte, so earlier probes reproduce.
    A pack never mixes halves: the A/B stability check compares two
    *independent* namings, and one context naming hubs of both halves
    would make them agree by construction (the 2026-10-02 packed probe
    mixed 41 of 49 packs before this rule).
    """
    if pack < 1:
        raise ValueError(f"pack must be at least 1, got {pack}")
    ref_field = config.snapshot.ref_field
    text_field = config.snapshot.text_field
    terms: list[DiscoveredTerm] = []
    warnings: list[str] = []
    pending: dict[Half, list[tuple[int, Half, str, Sequence[Mention]]]] = {}

    def emit(record: CallRecord) -> None:
        if on_call is not None:
            on_call(record)

    def flush(group: list[tuple[int, Half, str, Sequence[Mention]]]) -> None:
        if not group:
            return
        prompt = build_packed_prompt(
            [(ref_id, text, mentions) for ref_id, _h, text, mentions in group], config
        )
        size = len(group)
        started = time.monotonic()
        try:
            raw = client.complete_json(prompt)
        except Exception as exc:
            elapsed = time.monotonic() - started
            for i, (ref_id, half, _t, _m) in enumerate(group):
                warnings.append(f"ref {ref_id}: discovery call failed: {exc}")
                emit(
                    _record(
                        ref_id,
                        half,
                        prompt,
                        elapsed,
                        None,
                        error=str(exc),
                        share=(i, size),
                    )
                )
            group.clear()
            return
        elapsed = time.monotonic() - started
        reply = raw if isinstance(raw, Reply) else Reply(text=raw)
        per_ref, call_warnings = parse_packed_response(
            reply.text,
            [(ref_id, mentions, half) for ref_id, half, _t, mentions in group],
        )
        refs = ", ".join(str(ref_id) for ref_id, _h, _t, _m in group)
        warnings.extend(f"packed call [{refs}]: {w}" for w in call_warnings)
        for i, (ref_id, half, _t, _m) in enumerate(group):
            hub_terms, hub_warnings, slice_ = per_ref[ref_id]
            terms.extend(hub_terms)
            warnings.extend(f"ref {ref_id}: {warning}" for warning in hub_warnings)
            emit(
                _record(
                    ref_id,
                    half,
                    prompt,
                    elapsed,
                    reply,
                    terms=len(hub_terms),
                    warnings=len(hub_warnings),
                    payload=slice_,
                    share=(i, size),
                )
            )
        group.clear()

    for row in rows:
        ref_raw = row.get(ref_field)
        if ref_raw is None:
            warnings.append(f"row missing {ref_field!r} — skipped")
            continue
        if not isinstance(ref_raw, (int, str)):
            warnings.append(f"row {ref_field!r} is not an id — skipped")
            continue
        ref_id = int(ref_raw)
        mentions = mentions_by_ref.get(ref_id, ())
        if not mentions:
            continue
        half = halves.get(ref_id)
        if half is None:
            warnings.append(f"ref {ref_id}: no half assignment — skipped")
            continue
        text_raw = row.get(text_field)
        if text_raw is None:
            warnings.append(f"ref {ref_id}: row missing {text_field!r} — skipped")
            continue
        if pack > 1:
            group = pending.setdefault(half, [])
            group.append((ref_id, half, str(text_raw), mentions))
            if len(group) == pack:
                flush(group)
            continue
        prompt = build_prompt(str(text_raw), mentions, config)
        started = time.monotonic()
        try:
            raw = client.complete_json(prompt)
        except Exception as exc:
            elapsed = time.monotonic() - started
            warnings.append(f"ref {ref_id}: discovery call failed: {exc}")
            if on_call is not None:
                on_call(_record(ref_id, half, prompt, elapsed, None, error=str(exc)))
            continue
        elapsed = time.monotonic() - started
        reply = raw if isinstance(raw, Reply) else Reply(text=raw)
        row_terms, row_warnings = parse_response(reply.text, mentions, half)
        terms.extend(row_terms)
        warnings.extend(f"ref {ref_id}: {warning}" for warning in row_warnings)
        if on_call is not None:
            on_call(
                _record(
                    ref_id,
                    half,
                    prompt,
                    elapsed,
                    reply,
                    terms=len(row_terms),
                    warnings=len(row_warnings),
                )
            )
    for group in pending.values():
        flush(group)
    return tuple(terms), tuple(warnings)


@dataclass(frozen=True, slots=True)
class _RouterDiscoveryClient:
    """Adapts a router :class:`~precis.utils.llm.router.DispatchClient` (a
    ``.complete(messages) -> LlmResult`` shape) to :class:`DiscoveryClient`.
    """

    dispatch: Any
    #: Transport retries per call. The third probe (2026-09-30) lost 6 of 61
    #: calls to ``claude -p timed out after 120s`` with a 74 s median — a
    #: tail, not an outage — and every lost call is a hub whose mentions
    #: never reach stage 3, which reads as lower stability. One retry turns
    #: an independent 10 % tail into ~1 %; a second failure still raises.
    retries: int = 1

    def complete_json(self, prompt: str) -> Reply:
        # ``DispatchClient.complete`` raises ``DispatchError`` on a transport
        # failure, so a reply that reaches here is a real one; the metering
        # fields are the router's ``LlmResult`` fields, ``None`` where the
        # transport did not report them.
        messages = [{"role": "user", "content": prompt}]
        attempt = 0
        while True:
            try:
                result = self.dispatch.complete(messages)
                break
            except Exception as exc:
                attempt += 1
                if attempt > self.retries:
                    raise
                log.warning(
                    "taxonomy discovery call failed (%s); retry %d/%d",
                    exc,
                    attempt,
                    self.retries,
                )
        return Reply(
            text=getattr(result, "text", "") or "",
            model=getattr(result, "model", None),
            cost_usd=getattr(result, "cost_usd", None),
            input_tokens=getattr(result, "input_tokens", None),
            output_tokens=getattr(result, "output_tokens", None),
            cache_read_tokens=getattr(result, "cache_read_tokens", None),
            cache_creation_tokens=getattr(result, "cache_creation_tokens", None),
            duration_s=getattr(result, "duration_s", None),
        )


#: Wall-clock allowance per hub in a packed call. The third probe's 120 s
#: hard tail is ``claude -p``'s own default timeout
#: (``claude_p._DEFAULT_TIMEOUT_S``) against a 74 s single-hub median, so a
#: K-hub call left on that default would time out on output length alone.
PACKED_TIMEOUT_PER_HUB_S: Final[float] = 120.0


def call_timeout_s(pack: int) -> float | None:
    """The per-call timeout for ``pack`` hubs per call.

    ``None`` at ``pack=1`` leaves the transport default in force, so a
    single-hub run is the run the three probes measured.
    """
    return None if pack <= 1 else PACKED_TIMEOUT_PER_HUB_S * pack


def router_client(
    *, source: str = "taxonomy_discovery", timeout_s: float | None = None
) -> DiscoveryClient:
    """Build a production :class:`DiscoveryClient` on the repo's LLM router.

    ``Tier.BIG`` — sonnet-class — not ``Tier.MEDIUM`` (haiku-class): the
    router names tiers by capability, not by "how big a model sounds", and
    ``taxonomy-bootstrap.md`` rejects Haiku explicitly (the survey's Haiku
    classification pass was ~50% wrong and was discarded). Reading
    ``MEDIUM`` as "the middle rung" would silently put discovery back on
    that model — do not "optimise" this tier down.

    ``timeout_s`` is the per-call wall clock (:func:`call_timeout_s`);
    ``None`` keeps the transport's default.
    """
    from precis.utils.llm.router import DispatchClient, Tier

    return _RouterDiscoveryClient(
        DispatchClient(tier=Tier.BIG, source=source, log_call=True, timeout_s=timeout_s)
    )


__all__ = [
    "PACKED_TIMEOUT_PER_HUB_S",
    "CallRecord",
    "DiscoveryClient",
    "Reply",
    "build_packed_prompt",
    "build_prompt",
    "call_timeout_s",
    "discover",
    "parse_packed_response",
    "parse_response",
    "router_client",
    "split_halves",
]
