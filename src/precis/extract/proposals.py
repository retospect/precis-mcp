"""The per-chunk extraction contract: a model reply → checked measure runs.

``survey_extract`` shows the local reader one chunk at a time, rendered
by :func:`precis.extract.anchors.anchored_text` with ``[n]`` sentence
markers, and asks for ``{"results": [...]}`` naming measurands from the
vocabulary it was shown. This module turns that reply into
:class:`~precis.store._measures_ops.MeasureSpec` runs the store writes —
or into recorded drops with a reason, never into silently-kept garbage.

Per result the deterministic checks are the pilot's, ported:

* the anchor id resolves inside this chunk — an unresolvable anchor is
  a drop (``unresolvable_anchor``);
* the literal is verbatim in the resolved span, its numeric regions
  complete — a mismatch drops the result (``literal_not_in_span``);
  categorical literals are verbatim-checked only;
* the reported unit is spelled in the span — a miss is a FLAG
  (``unit_without_text_support``), the row still lands (the pilot's
  ruling: flags, not refusals, for things a sampler can still judge);
* the measurand resolves to the vocabulary — an unknown name drops the
  result (``unknown_measurand``) so a small model cannot mint synonyms.

Attribution → tier follows Build C: ``own_work`` + anchor ⇒ ``measured``
(the default), ``cited_work`` ⇒ ``asserted`` (the store refuses
``measured`` anyway), anything not established ⇒ ``asserted``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from precis.extract import anchors as _anchors
from precis.extract import verify as _verify
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.utils.text import slugify

_ATTRIBUTIONS = {"own_work", "cited_work", "not_established"}
_TIERS = {"measured", "computed", "derived", "asserted"}
_ROLES = {"context", "preparation", "model"}
_MEASURAND_STATUSES = {"explicit", "interpreted", "ambiguous"}
_DIRECTIONS = {"input", "covariate"}

_RESULT_FIELDS = {
    "subject",
    "group",
    "measurand",
    "literal",
    "unit",
    "anchor",
    "attribution",
    "tier",
    "measurand_status",
    "reference",
    "normalization",
    "value_form",
    "inputs",
}
_INPUT_FIELDS = {
    "condition",
    "measurand",
    "literal",
    "unit",
    "anchor",
    "role",
    "direction",
    "attribution",
    "value_text",
}


@dataclass(frozen=True, slots=True)
class VocabEntry:
    """One measurand taxon as the reader sees it."""

    ref_id: int
    slug: str
    name: str
    aliases: tuple[str, ...]
    canonical_unit: str | None
    display_unit: str | None
    required_conditions: tuple[str, ...]

    def lookup_keys(self) -> set[str]:
        keys = {slugify(self.slug), slugify(self.name)}
        keys |= {slugify(a) for a in self.aliases}
        return {k for k in keys if k}


@dataclass(slots=True)
class RunProposal:
    """One result's output + input specs, ready for ``insert_measure``."""

    output: MeasureSpec
    inputs: list[MeasureSpec]
    location: str
    flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Outcome:
    """What one chunk's reply produced."""

    proposals: list[RunProposal] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)


def vocab_entry(ref_id: int, meta: dict[str, Any]) -> VocabEntry:
    """A VocabEntry from a taxon's stored meta dict."""
    return VocabEntry(
        ref_id=ref_id,
        slug=str(meta.get("slug") or ""),
        name=str(meta.get("name") or meta.get("slug") or ""),
        aliases=tuple(
            str(a) for a in (meta.get("aliases") or []) if isinstance(a, str)
        ),
        canonical_unit=(
            str(meta["canonical_unit"]) if meta.get("canonical_unit") else None
        ),
        display_unit=(str(meta["display_unit"]) if meta.get("display_unit") else None),
        required_conditions=tuple(
            str(c)
            for c in (meta.get("required_conditions") or [])
            if isinstance(c, str)
        ),
    )


def resolve_measurand(name: object, vocab: dict[int, VocabEntry]) -> VocabEntry | None:
    """The vocabulary entry for a model-emitted name — slug, name or
    alias, slugified (case/punctuation insensitive). Unknown → ``None``."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = slugify(name)
    for entry in vocab.values():
        if key in entry.lookup_keys():
            return entry
    return None


def _tier(attribution: str, raw_tier: object) -> str | None:
    """The stored tier for the model's attribution + tier hint."""
    if attribution == "cited_work":
        return "asserted"
    if attribution != "own_work":
        return "asserted"
    if raw_tier in _TIERS:
        return str(raw_tier)
    return "measured"


def _chunk_anchor_id(chunk_id: int, raw: object) -> object:
    """Normalize the model's chunk-local anchor ('4', '4-6', '#2') into
    the index's '<chunk_id>.<n>' / '<chunk_id>#<k>' / raw-offsets forms."""
    if isinstance(raw, (list, tuple)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"unrecognized anchor {raw!r}")
    text = raw.strip()
    if text.startswith("#"):
        return f"{chunk_id}{text}"
    if "." in text:
        return text
    if re.fullmatch(r"\d+-\d+", text):
        first, last = text.split("-", 1)
        return f"{chunk_id}.{first}-{last}"
    if text.isdigit():
        return f"{chunk_id}.{text}"
    return text


def _resolve(
    index: dict[str, tuple[int, int, int]],
    chunk_id: int,
    raw: object,
) -> tuple[int, int, int]:
    """An anchor the model gave to a ``(chunk_id, start, end)`` span on
    THIS chunk — anything else raises."""
    anchor = _chunk_anchor_id(chunk_id, raw)
    resolved = _anchors.resolve_anchor(index, anchor)
    if resolved[0] != chunk_id:
        raise ValueError(f"anchor {raw!r} resolves off this chunk")
    return resolved


def build_runs(
    reply: dict[str, Any],
    *,
    chunk_id: int,
    chunk_text: str,
    paper_ref_id: int,
    vocab: dict[int, VocabEntry],
    extraction_run: str,
    model: str | None,
) -> Outcome:
    """Validate one parsed reply and build its runs.

    ``reply`` is the model's ``{"results": [...]}`` object. Each result
    yields a :class:`RunProposal`, a drop record, or a flag; the shape
    errors of the reply itself come back as a single drop.
    """
    out = Outcome()
    index = _anchors.anchor_index([(chunk_id, chunk_text)])
    results = reply.get("results")
    if not isinstance(results, list):
        out.dropped.append(
            {
                "location": "reply",
                "reason": "bad_shape",
                "detail": "reply['results'] is not a list",
            }
        )
        return out

    for i, raw in enumerate(results):
        location = f"results[{i}]"
        if not isinstance(raw, dict):
            out.dropped.append(
                {"location": location, "reason": "bad_shape", "detail": "not an object"}
            )
            continue
        unknown = sorted(set(raw) - _RESULT_FIELDS)
        if unknown:
            out.dropped.append(
                {
                    "location": location,
                    "reason": "bad_shape",
                    "detail": f"unknown field(s) {unknown}",
                }
            )
            continue
        literal = raw.get("literal")
        if not isinstance(literal, str) or not literal.strip():
            out.dropped.append(
                {
                    "location": location,
                    "reason": "bad_shape",
                    "detail": "missing literal",
                }
            )
            continue
        entry = resolve_measurand(raw.get("measurand"), vocab)
        if entry is None:
            out.dropped.append(
                {
                    "location": location,
                    "reason": "unknown_measurand",
                    "detail": repr(raw.get("measurand")),
                }
            )
            continue
        try:
            _cid, start, end = _resolve(index, chunk_id, raw.get("anchor"))
        except ValueError as exc:
            out.dropped.append(
                {
                    "location": location,
                    "reason": "unresolvable_anchor",
                    "detail": str(exc),
                }
            )
            continue
        span_text = chunk_text[start:end]
        categorical = raw.get("value_form") == "categorical"
        if not _verify.literal_supported(
            literal,
            span_text,
            chunk_text,
            (start, end),
            numeric_required=not categorical,
        ):
            out.dropped.append(
                {
                    "location": location,
                    "reason": "literal_not_in_span",
                    "detail": f"{literal!r} vs span {span_text[:120]!r}",
                }
            )
            continue
        flags: list[str] = []
        unit = raw.get("unit")
        if not _verify.unit_has_text_support(unit, chunk_text):
            flags.append("unit_without_text_support")

        attribution = (
            raw["attribution"]
            if raw.get("attribution") in _ATTRIBUTIONS
            else "not_established"
        )
        status = (
            raw["measurand_status"]
            if raw.get("measurand_status") in _MEASURAND_STATUSES
            else None
        )
        value_form = raw.get("value_form")
        if value_form is not None and value_form not in {
            "point",
            "interval",
            "upper",
            "lower",
            "boolean",
            "categorical",
            "not_established",
        }:
            value_form = None
        inputs: list[MeasureSpec] = []
        raw_inputs = raw.get("inputs")
        if raw_inputs is not None and not isinstance(raw_inputs, list):
            out.flags.append({"location": f"{location}.inputs", "flag": "bad_shape"})
            raw_inputs = None
        for j, raw_in in enumerate(raw_inputs or []):
            in_loc = f"{location}.inputs[{j}]"
            if not isinstance(raw_in, dict):
                out.flags.append({"location": in_loc, "flag": "bad_shape"})
                continue
            unknown_in = sorted(set(raw_in) - _INPUT_FIELDS)
            if unknown_in:
                out.flags.append(
                    {
                        "location": in_loc,
                        "flag": "bad_shape",
                        "detail": f"unknown field(s) {unknown_in}",
                    }
                )
                continue
            in_entry = resolve_measurand(raw_in.get("measurand"), vocab)
            in_literal = raw_in.get("literal")
            if (
                in_entry is None
                or not isinstance(in_literal, str)
                or not in_literal.strip()
            ):
                out.flags.append(
                    {
                        "location": in_loc,
                        "flag": "bad_shape",
                        "detail": "unknown measurand or missing literal",
                    }
                )
                continue
            in_anchor = raw_in.get("anchor")
            if in_anchor is not None:
                try:
                    _c, s2, e2 = _resolve(index, chunk_id, in_anchor)
                    in_span = MeasureAnchor(
                        paper_ref_id, chunk_id, "offsets", [chunk_id, s2, e2]
                    )
                except ValueError:
                    out.flags.append(
                        {"location": in_loc, "flag": "unresolvable_anchor"}
                    )
                    continue
            else:
                in_span = None
            role = raw_in.get("role") if raw_in.get("role") in _ROLES else None
            direction = (
                raw_in["direction"] if raw_in.get("direction") in _DIRECTIONS else None
            )
            in_meta: dict[str, Any] = {}
            if isinstance(raw_in.get("condition"), str) and raw_in["condition"].strip():
                in_meta["condition"] = raw_in["condition"].strip()
            in_attr = (
                raw_in["attribution"]
                if raw_in.get("attribution") in _ATTRIBUTIONS
                else "own_work"
            )
            inputs.append(
                MeasureSpec(
                    measurand_ref_id=in_entry.ref_id,
                    literal=in_literal,
                    subject_ref_id=paper_ref_id,
                    reported_unit=(
                        raw_in["unit"] if isinstance(raw_in.get("unit"), str) else None
                    ),
                    value_text=(
                        raw_in["value_text"]
                        if isinstance(raw_in.get("value_text"), str)
                        else None
                    ),
                    role=role,
                    direction=direction,
                    source_attribution=in_attr,
                    anchor=in_span,
                    meta=in_meta,
                )
            )

        meta: dict[str, Any] = {
            "extraction_run": extraction_run,
            "anchor_id": _chunk_anchor_id(chunk_id, raw.get("anchor")),
            "anchor_scheme": _anchors.ANCHOR_SCHEME,
            "extraction": {"chunk": chunk_id, "model": model},
        }
        output = MeasureSpec(
            measurand_ref_id=entry.ref_id,
            literal=literal,
            subject_ref_id=paper_ref_id,
            reported_unit=unit if isinstance(unit, str) else None,
            subject=raw["subject"] if isinstance(raw.get("subject"), str) else None,
            subject_group=(raw["group"] if isinstance(raw.get("group"), str) else None),
            value_form=value_form,
            reference=(
                raw["reference"] if isinstance(raw.get("reference"), str) else None
            ),
            tier=_tier(attribution, raw.get("tier")),
            source_attribution=attribution,
            measurand_status=status,
            normalization=(
                raw["normalization"]
                if isinstance(raw.get("normalization"), str)
                else None
            ),
            normalization_status=(
                "explicit" if isinstance(raw.get("normalization"), str) else None
            ),
            anchor=MeasureAnchor(
                paper_ref_id, chunk_id, "offsets", [chunk_id, start, end]
            ),
            meta=meta,
        )
        out.proposals.append(
            RunProposal(output=output, inputs=inputs, location=location, flags=flags)
        )
    return out
