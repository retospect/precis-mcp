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
  categorical literals are verbatim-checked only. Inside a pipe table
  (which splits one sentence span per line) a mismatch first retries
  against the unique table row that carries the literal — a reader
  that anchored the header while quoting a data row is re-resolved
  instead of dropped (gr514627);
* the reported unit is spelled in the span — a miss is a FLAG
  (``unit_without_text_support``), the row still lands (the pilot's
  ruling: flags, not refusals, for things a sampler can still judge);
* the measurand resolves to the vocabulary — an unknown name drops the
  result (``unknown_measurand``) so a small model cannot mint synonyms;
  a ``null`` measurand is the reader's "no listed measurand fits" answer
  and drops as ``no_fitting_measurand`` (gr511568 — a forced nearest
  slug filled 'fraction attributable to climate change' with unrelated
  shares);
* ``attribution: cited_work`` on a result drops it (``cited_context``):
  only the paper's own findings become measures — a cited threshold,
  another technology's number or a scenario baseline is context, and the
  review door rejects these rows whatever their tier (gr511459). Cited
  values still land as *inputs* of a result they condition;
* an emitted ``bound`` is checked against the span's bound cue: ``at
  most 0.2`` supports ``upper``, ``exceeding 100,000`` supports
  ``lower``. A claim with no cue drops (``unsupported_bound``), a claim
  against the opposite cue drops (``bound_direction_mismatch``), and a
  printed cue the model did not claim flags the point
  (``bound_cue_ignored``) — the bound stays worded, never a silent point;
* ``sign: -1`` is the decrease channel ('cools by 0.9' stores -0.9):
  a decrease cue must precede the literal or the claim drops
  (``unsupported_sign``); ``range`` carries a verbatim bracketed spread
  into ``value_low``/``value_high``, and a point that cannot sit inside
  its own bracket has its sign corrected by it (``sign_from_range``);
  ``conditions`` is a name → printed-value map (baseline, period,
  model, region) stored on the row's ``conditions`` column — every
  value must occur in the chunk (``condition_not_in_chunk`` flags and
  drops one entry);
* a PDF-split table's lone sign cell merges onto the next number for
  the checks: ``'− | 1.93'`` signs as ``'-1.93'``, and an unsigned
  ``'1.93'`` literal then fails the region check outright (gr511461).

Every written run's ``meta.extraction.prompt`` carries
:data:`READER_PROMPT_VERSION`, bumped whenever the reader contract in
:mod:`precis.extract.prompt` changes, so rows from different prompt
generations stay distinguishable.

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
from precis.extract.tables import _is_table_separator, _table_line_cells
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.taxonomy.measure_units import parse_literal
from precis.utils.text import slugify

_ATTRIBUTIONS = {"own_work", "cited_work", "not_established"}
_TIERS = {"measured", "computed", "derived", "asserted"}
_ROLES = {"context", "preparation", "model"}
_MEASURAND_STATUSES = {"explicit", "interpreted", "ambiguous"}
_DIRECTIONS = {"input", "covariate"}
_BOUNDS = {"upper", "lower"}

#: The reader-contract generation stamped on every written run's
#: ``meta.extraction.prompt`` (r1 = the unversioned castor contract).
READER_PROMPT_VERSION = "r3"

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
    "bound",
    "sign",
    "range",
    "conditions",
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


def _literal_table_row(
    literal: str,
    chunk_text: str,
    anchored_start: int,
    table_rows: list[tuple[int, int, int]],
    *,
    categorical: bool,
) -> tuple[int, int] | None:
    """The unique data row in the anchored header's table supporting
    ``literal``.

    Consulted only when the anchored span is a table *header* line — the
    first line of a ``|``-line block, or the line right before a
    ``|---|`` separator — and fails
    :func:`~precis.extract.verify.literal_supported`: the reader
    numbered the header as sentence 0 while quoting a data row
    (gr514627). Candidates are the other lines of *that* table (the run
    of adjacent ``|`` lines containing the anchor), not every ``|`` line
    in the chunk — an anchored data row, a literal living in another
    table, and zero or several qualifying rows all return ``None``: the
    evidence span must be unambiguous or the drop stands.
    """
    idx = next(
        (i for i, (s, e, _c) in enumerate(table_rows) if s <= anchored_start < e),
        None,
    )
    if idx is None:
        return None
    lo = idx
    while lo > 0 and table_rows[lo - 1][1] + 1 == table_rows[lo][0]:
        lo -= 1
    hi = idx
    while hi + 1 < len(table_rows) and table_rows[hi][1] + 1 == table_rows[hi + 1][0]:
        hi += 1
    block = table_rows[lo : hi + 1]
    offset = idx - lo
    if not (
        offset == 0
        or (
            offset + 1 < len(block)
            and _is_table_separator(
                chunk_text[block[offset + 1][0] : block[offset + 1][1]]
            )
        )
    ):
        return None
    matches = [
        (s, e)
        for j, (s, e, _cells) in enumerate(block)
        if j != offset
        and _verify.literal_supported(
            literal,
            chunk_text[s:e],
            chunk_text,
            (s, e),
            numeric_required=not categorical,
        )
    ]
    return matches[0] if len(matches) == 1 else None


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
    # Pipe-table bookkeeping for gr512370: a data row with fewer cells
    # than the header misaligns every column left of the gap — the
    # reader picks the wrong column and nothing downstream can see it.
    table_rows = _table_line_cells(chunk_text)
    header_cells = table_rows[0][2] if table_rows and table_rows[0][2] > 1 else None
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
        if raw.get("measurand") is None:
            out.dropped.append(
                {
                    "location": location,
                    "reason": "no_fitting_measurand",
                    "detail": (
                        f"{literal!r}: the reader found no listed measurand "
                        "whose meaning fits"
                    ),
                }
            )
            continue
        if raw.get("attribution") == "cited_work":
            out.dropped.append(
                {
                    "location": location,
                    "reason": "cited_context",
                    "detail": (
                        f"{literal!r} restates another work — a cited or "
                        "comparator value is context, not a result "
                        "(gr511459)"
                    ),
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
            # gr514627: a pipe-table chunk splits one sentence span per
            # line, so anchor '0' names the header — a literal quoted
            # from a data row reads as out-of-span. Only an anchored
            # header retries, against the unique row of that one table;
            # a wrong data-row anchor keeps dropping (the literal can't
            # move evidence between rows it didn't come from).
            row_span = _literal_table_row(
                literal, chunk_text, start, table_rows, categorical=categorical
            )
            if row_span is None:
                out.dropped.append(
                    {
                        "location": location,
                        "reason": "literal_not_in_span",
                        "detail": f"{literal!r} vs span {span_text[:120]!r}",
                    }
                )
                continue
            start, end = row_span
            span_text = chunk_text[start:end]
        # A value the sentence frames as a design target or a comparator
        # ('such that', 'intended to produce', 'targets of') is context,
        # not an outcome — it drops as cited_context whatever attribution
        # the reader gave it (gr512369).
        cue = _verify.design_hint(span_text)
        if cue is not None:
            out.dropped.append(
                {
                    "location": location,
                    "reason": "cited_context",
                    "detail": (
                        f"{literal!r} sits in design/comparator wording "
                        f"{cue!r}: {span_text[:100]!r}"
                    ),
                }
            )
            continue
        flags: list[str] = []
        spec_escalation: list[dict[str, Any]] = []
        if header_cells is not None:
            row_idx = next(
                (i for i, (s, e, _c) in enumerate(table_rows) if s <= start < e),
                None,
            )
            if (
                row_idx is not None
                and row_idx > 0
                and table_rows[row_idx][2] < header_cells
            ):
                flags.append("table_misaligned")
                spec_escalation.append(
                    {
                        "rule": "table_misaligned",
                        "row_cells": table_rows[row_idx][2],
                        "header_cells": header_cells,
                    }
                )
        unit = raw.get("unit")
        if not _verify.unit_has_text_support(unit, chunk_text):
            flags.append("unit_without_text_support")

        # The numeric channels — bound, sign, range — all check their
        # claim against what the span prints just before the literal. An
        # uncued claim is a guess (dropped), an opposite cue a misread
        # (dropped); a printed cue the model ignored stays a flag for the
        # sampler (gr511459, gr511461).
        parsed = parse_literal(literal)
        num, low, high = parsed.num, parsed.low, parsed.high
        value_form = "categorical" if raw.get("value_form") == "categorical" else None
        explicit = False
        norm_span = _verify.literal_normalize(span_text)
        positions = _verify.literal_positions(literal, span_text)
        hints = {_verify.bound_hint(norm_span[:pos]) for pos in positions}

        bound_raw = raw.get("bound")
        bound = bound_raw if bound_raw in _BOUNDS else None
        if bound_raw is not None and bound is None:
            flags.append("bad_shape")
        if bound is not None:
            if bound not in hints:
                reason = (
                    "bound_direction_mismatch"
                    if hints - {None}
                    else "unsupported_bound"
                )
                out.dropped.append(
                    {
                        "location": location,
                        "reason": reason,
                        "detail": (f"bound={bound!r} vs span {span_text[:120]!r}"),
                    }
                )
                continue
            if num is None:
                try:
                    num = float(_verify.literal_normalize(literal).replace(",", ""))
                except ValueError:
                    num = None
            if num is None:
                out.dropped.append(
                    {
                        "location": location,
                        "reason": "bad_shape",
                        "detail": f"bound on a non-numeric literal {literal!r}",
                    }
                )
                continue
            value_form = f"{bound}_bound"
            explicit = True
        elif hints - {None}:
            flags.append("bound_cue_ignored")

        # A decrease cue ('cools by', 'below the baseline') makes an
        # unsigned printed literal a negative change; a literal already
        # printed negative needs no cue (gr511461).
        sign_raw = raw.get("sign")
        sign_neg = sign_raw in (-1, "-1")
        if sign_raw is not None and sign_raw not in (-1, "-1", 1, "1"):
            flags.append("bad_shape")
        if sign_neg:
            readings = [v for v in (num, low, high) if v is not None]
            if not readings:
                flags.append("bad_shape")
            elif max(readings) > 0:
                lit_norm = _verify.literal_normalize(literal)
                if not any(
                    _verify.sign_hint(norm_span[:pos]) == -1
                    or _verify.sign_hint_after(norm_span[pos + len(lit_norm) :]) == -1
                    for pos in positions
                ):
                    out.dropped.append(
                        {
                            "location": location,
                            "reason": "unsupported_sign",
                            "detail": f"sign=-1 without a decrease cue: "
                            f"{span_text[:120]!r}",
                        }
                    )
                    continue
                num = -abs(num) if num is not None else None
                if low is not None or high is not None:
                    low, high = (
                        -abs(high) if high is not None else None,
                        -abs(low) if low is not None else None,
                    )
                explicit = True

        # A bracketed spread ('0.96 [-0.2 to -1.3]') carries the model
        # range into value_low/value_high. When the point cannot sit
        # inside it but its negation can, the bracket — verbatim — fixes
        # the sign the wording left off.
        range_raw = raw.get("range")
        spread = None
        if isinstance(range_raw, str) and range_raw.strip():
            if _verify.literal_normalize(range_raw) not in norm_span:
                flags.append("range_not_in_span")
            else:
                spread = _verify.parse_spread(range_raw)
                if spread is None:
                    flags.append("range_unparseable")
        elif range_raw is not None:
            flags.append("bad_shape")
        if spread is not None:
            if low is not None or high is not None:
                flags.append("bad_shape")
            else:
                low, high = spread
                explicit = True
        if num is not None and low is not None and high is not None:
            if low <= num <= high:
                pass
            elif low <= -num <= high:
                num = -num
                flags.append("sign_from_range")
            else:
                flags.append("point_outside_range")

        # Named context the inputs mechanism cannot carry (no measurand):
        # baseline/scenario, period, model, region. Each value must print
        # in the chunk; the dict lands on the row's conditions column.
        conditions: dict[str, str] = {}
        raw_conditions = raw.get("conditions")
        if raw_conditions is not None and not isinstance(raw_conditions, dict):
            flags.append("bad_shape")
        elif isinstance(raw_conditions, dict):
            norm_chunk = _verify.literal_normalize(chunk_text)
            for key, val in raw_conditions.items():
                if len(conditions) >= 16:
                    flags.append("bad_shape")
                    break
                if (
                    not isinstance(key, str)
                    or not key.strip()
                    or not isinstance(val, str)
                    or not val.strip()
                ):
                    flags.append("bad_shape")
                    continue
                if _verify.literal_normalize(val) not in norm_chunk:
                    flags.append("condition_not_in_chunk")
                    continue
                conditions[key.strip()] = val.strip()

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
        # The reader's form vocabulary isn't the store's (a "upper" or
        # "point" it emits used to ride into insert_measure — a DB CHECK
        # violation, not a BadInput, crashing the slice mid-run, and a
        # wrong reader "point" overrode parse_literal's own read of
        # "<1"). Only "categorical" survives — it names a non-numeric
        # literal parse_literal can't infer; every numeric form is left
        # None so the literal itself decides (gr508162). "bound" is the
        # separate, cue-checked channel for the two bound forms.
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
            "extraction": {
                "chunk": chunk_id,
                "model": model,
                "prompt": READER_PROMPT_VERSION,
            },
        }
        if spec_escalation:
            meta["escalation"] = spec_escalation
        output = MeasureSpec(
            measurand_ref_id=entry.ref_id,
            literal=literal,
            subject_ref_id=paper_ref_id,
            reported_unit=unit if isinstance(unit, str) else None,
            value_num=num if explicit else None,
            value_low=low if explicit else None,
            value_high=high if explicit else None,
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
            conditions=conditions or None,
            meta=meta,
        )
        out.proposals.append(
            RunProposal(output=output, inputs=inputs, location=location, flags=flags)
        )

    # Sibling results from one chunk that share measurand, subject and
    # conditions are indistinguishable — the region, column or sector
    # that set them apart was never written (gr512367).
    siblings: dict[tuple[Any, ...], list[RunProposal]] = {}
    for prop in out.proposals:
        key = (
            prop.output.measurand_ref_id,
            prop.output.subject or "",
            tuple(sorted((prop.output.conditions or {}).items())),
        )
        siblings.setdefault(key, []).append(prop)
    for group in siblings.values():
        if len(group) < 2:
            continue
        for prop in group:
            prop.flags.append("sibling_condition_collision")
            prop.output.meta.setdefault("escalation", []).append(
                {"rule": "sibling_condition_collision", "siblings": len(group)}
            )
    return out
