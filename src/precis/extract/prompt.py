"""Prompt text for the survey extraction pass.

Two prompts, two models:

* :func:`reader_prompt` — the per-chunk reader prompt for the local
  model: the measurand vocabulary it may name, then the chunk rendered
  by :func:`precis.extract.anchors.anchored_text` with ``[n]`` markers,
  then the exact reply contract. Kept deliberately small — the reader
  proposes bindings, the deterministic checks and the frontier sampler
  carry the burden of proof.
* :func:`review_prompt` — the per-measure sampler prompt for the
  frontier model: one written row plus the text of its anchored span;
  the verdict the acceptance step counts.
"""

from __future__ import annotations

from typing import Any

from precis.extract.anchors import anchored_text
from precis.extract.proposals import VocabEntry
from precis.utils.llm.json_reply import extract_json_object

_READER_CONTRACT = """\
Reply with ONE JSON object and nothing else:
{"results": [ ... ]}   or   {"results": []} when the chunk reports none.

Each result:
{
 "subject": "<paper-local label for the catalyst/sample/system, or null>",
 "group": "<finding or table name these results share, or null>",
 "measurand": "<a measurand slug from the list below>",
 "literal": "<the exact printed string: '95', '<1', '550-575', 'NH3'>",
 "unit": "<the printed unit, or null>",
 "anchor": "<the [n] sentence the literal is printed in: '4' or '4-6'>",
 "attribution": "own_work | cited_work | not_established",
 "tier": "measured | computed | asserted | null",
 "measurand_status": "explicit | interpreted | ambiguous | null",
 "reference": "<electrode reference etc., or null>",
 "normalization": "<what the number is normalized by, or null>",
 "value_form": "categorical | null",
 "inputs": [ ... ]
}

attribution: own_work = these authors produced the number; cited_work =
restated from another paper; not_established = unattributed.
tier: measured = their experiment, computed = their model/DFT,
asserted = stated without established origin. null when unsure.
value_form "categorical" only for a non-numeric literal (a product
name, a substrate, a phase).

Each input is a condition of THIS result:
{"condition": "<condition name, e.g. product, applied-potential,
electrolyte-concentration>", "measurand": "<slug>", "literal": "<exact
printed string>", "unit": "<or null>", "anchor": "<[n] if elsewhere in
the chunk>", "role": "context|preparation|model", "value_text": "<for a
categorical condition, e.g. 'NH3'>"}

Rules:
- the literal must be copied EXACTLY as printed, sign and dashes included;
- anchor to the sentence that prints the literal, nothing wider;
- do not invent numbers, units or measurands — emit [] rather than guess;
- a measurand not in the list cannot be named.\
"""


def reader_prompt(
    *,
    vocab: list[VocabEntry],
    chunk_text: str,
    section: str | None = None,
    paper_title: str | None = None,
) -> str:
    """One chunk's reader prompt for the local extraction model."""
    lines = [
        "You are reading one chunk of a survey paper for a numeric-extraction",
        "pass. Extract every numeric or categorical result the chunk states",
        "about a catalyst, material, electrode or reaction system — each with",
        "the sentence it is printed in and the conditions it was taken under.",
    ]
    if paper_title:
        lines.append(f"Paper: {paper_title}")
    if section:
        lines.append(f"Section: {section}")
    lines.append("")
    lines.append("Measurands you may name (slug — canonical unit):")
    for entry in vocab:
        unit = entry.display_unit or entry.canonical_unit or "-"
        req = (
            f" (needs: {', '.join(entry.required_conditions)})"
            if entry.required_conditions
            else ""
        )
        lines.append(f"- {entry.slug} — {unit}{req}")
    lines.append("")
    lines.append(_READER_CONTRACT)
    lines.append("")
    lines.append("Chunk (sentences marked [n]):")
    lines.append(anchored_text(chunk_text))
    return "\n".join(lines)


def parse_reply(text: str) -> dict[str, Any] | None:
    """The model's reply as a dict, or None on unparseable output."""
    return extract_json_object(text)


def review_prompt(
    *,
    literal: str,
    reported_unit: str | None,
    measurand: str,
    subject_label: str | None,
    span_text: str,
    conditions: list[str] | None = None,
) -> str:
    """The per-row sampler prompt: is this row what the span says?"""
    bits = [f"measurand: {measurand}", f"literal: {literal}"]
    if reported_unit:
        bits.append(f"reported unit: {reported_unit}")
    if subject_label:
        bits.append(f"subject: {subject_label}")
    for c in conditions or []:
        bits.append(f"condition: {c}")
    return (
        "A numeric-extraction pass wrote this measure row from a survey "
        "paper. The anchored source span follows the row. Reply with ONE "
        "JSON object and nothing else:\n"
        '{"verdict": "correct|wrong|undecidable", "reason": "<short>"}\n\n'
        "correct = the literal, unit and measurand say what the span says, "
        "about the stated subject. wrong = the value is misread, the unit "
        "wrong, the measurand misassigned, or the span does not contain the "
        "literal. undecidable = the span is there but the claim cannot be "
        "judged from it.\n\n" + "\n".join(bits) + "\n\nspan:\n" + span_text
    )


def parse_verdict(text: str) -> tuple[str, str | None]:
    """The sampler's verdict: ``('correct'|'wrong'|'undecidable', reason)``.

    An unparseable or unlabeled reply reads as ``('undecidable', ...)`` —
    never silently a pass.
    """
    obj = extract_json_object(text)
    if not isinstance(obj, dict):
        return "undecidable", "unparseable reply"
    verdict = obj.get("verdict")
    reason = obj.get("reason") if isinstance(obj.get("reason"), str) else None
    if verdict in ("correct", "wrong", "undecidable"):
        return verdict, reason
    return "undecidable", reason
