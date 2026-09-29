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

:func:`parse_response` is defensive by construction: a malformed reply, a
missing or out-of-range mention index, or a row with no measurand each
produce a warning string and are skipped — never an exception, never a
fabricated :class:`~precis.taxonomy.types.DiscoveredTerm`. Warnings are
returned, never printed and never silently dropped, so :func:`discover` can
run over a whole snapshot and still report exactly what the model did not
address.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.types import DiscoveredTerm, Half, Mention

#: Strips a single leading/trailing markdown code fence (`````json ... ````
#: or plain ` ``` `), the shape a chat model reaches for even when told not
#: to. Matched non-greedily against the whole stripped payload so a fence
#: elsewhere in the text (there shouldn't be one) is left alone.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class DiscoveryClient(Protocol):
    """The one-shot completion seam stage 2 depends on.

    ``complete_json`` takes a fully built prompt and returns the model's raw
    text reply. Tests inject a fake; production wires :func:`router_client`.
    """

    def complete_json(self, prompt: str) -> str: ...


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
    """
    lines = [
        "You are reading one sentence from a scientific claim. For each "
        "numbered mention below, describe what is being measured — in your "
        "own words, not from a fixed list. Name it the way the paper would.",
        "",
        f"Sentence: {text}",
        "",
        "Mentions:",
    ]
    for index, mention in enumerate(mentions):
        unit = mention.raw_unit if mention.raw_unit is not None else "(no unit)"
        lines.append(
            f"  [{index}] literal={mention.literal!r} kind={mention.kind} "
            f"unit={unit} context={mention.context!r}"
        )
    lines.append("")
    if config.categorical_qualifiers or config.site_classes:
        lines.append(
            "Vocabulary hints for QUALIFIER fields only — never use these "
            "to name the measurand itself:"
        )
        for key, values in config.categorical_qualifiers.items():
            lines.append(f"  {key}: {', '.join(values)}")
        if config.site_classes:
            lines.append(f"  site_class: {', '.join(config.site_classes)}")
        lines.append("")
    lines.extend(
        [
            "Reply with a JSON array and nothing else — no prose, no "
            "markdown fence. One object per mention, with exactly these "
            "keys:",
            "  index (integer, matching a mention above)",
            "  measurand (string, your own words — required)",
            "  dimension_text (string or null)",
            "  reference_state (string or null)",
            "  convention (string or null)",
            "  normalisation_basis (string or null)",
            "  subject_label (string or null)",
            "  required_conditions (array of strings, may be empty)",
            "Every mention index above must appear exactly once.",
        ]
    )
    return "\n".join(lines)


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
            warnings.append(f"mention {index}: empty or missing measurand")
            continue
        terms.append(
            DiscoveredTerm(
                mention=mentions[index],
                measurand=measurand.strip(),
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


def discover(
    rows: Sequence[Mapping[str, object]],
    mentions_by_ref: Mapping[int, Sequence[Mention]],
    config: CampaignConfig,
    client: DiscoveryClient,
    *,
    halves: Mapping[int, Half],
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
    """
    ref_field = config.snapshot.ref_field
    text_field = config.snapshot.text_field
    terms: list[DiscoveredTerm] = []
    warnings: list[str] = []
    for row in rows:
        ref_raw = row.get(ref_field)
        if ref_raw is None:
            warnings.append(f"row missing {ref_field!r} — skipped")
            continue
        ref_id = int(ref_raw)  # type: ignore[call-overload]
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
        prompt = build_prompt(str(text_raw), mentions, config)
        try:
            payload = client.complete_json(prompt)
        except Exception as exc:
            warnings.append(f"ref {ref_id}: discovery call failed: {exc}")
            continue
        row_terms, row_warnings = parse_response(payload, mentions, half)
        terms.extend(row_terms)
        warnings.extend(f"ref {ref_id}: {warning}" for warning in row_warnings)
    return tuple(terms), tuple(warnings)


@dataclass(frozen=True, slots=True)
class _RouterDiscoveryClient:
    """Adapts a router :class:`~precis.utils.llm.router.DispatchClient` (a
    ``.complete(messages) -> LlmResult`` shape) to :class:`DiscoveryClient`.
    """

    dispatch: Any

    def complete_json(self, prompt: str) -> str:
        result = self.dispatch.complete([{"role": "user", "content": prompt}])
        return getattr(result, "text", "") or ""


def router_client(*, source: str = "taxonomy_discovery") -> DiscoveryClient:
    """Build a production :class:`DiscoveryClient` on the repo's LLM router.

    ``Tier.BIG`` — sonnet-class — not ``Tier.MEDIUM`` (haiku-class): the
    router names tiers by capability, not by "how big a model sounds", and
    ``taxonomy-bootstrap.md`` rejects Haiku explicitly (the survey's Haiku
    classification pass was ~50% wrong and was discarded). Reading
    ``MEDIUM`` as "the middle rung" would silently put discovery back on
    that model — do not "optimise" this tier down.
    """
    from precis.utils.llm.router import DispatchClient, Tier

    return _RouterDiscoveryClient(
        DispatchClient(tier=Tier.BIG, source=source, log_call=True)
    )


__all__ = [
    "DiscoveryClient",
    "build_prompt",
    "discover",
    "parse_response",
    "router_client",
    "split_halves",
]
