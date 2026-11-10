"""Computed-evidence producer adapters — one source contract from a frozen
producer output to a citable value (gr564183, plan td564174 under
``docs/backlog/catpath-repro-bundle.md``).

A manuscript value that came out of a computation is only as citable as the
chain someone can re-walk: which run produced it, which exact output bytes,
which field of those bytes, in which unit, by which method under which
conditions. Two contracts already guard halves of that chain —
``precis.nanopub.gates::check_computed`` pins a finding's pathway citation to
``(pathway_ref_id, content_key)`` and refuses mint when the pin moves, and a
measure row pins a number to its paper/chunk anchor. What was missing is the
shared *producer adapter* between them: a frozen JSON contract that resolves
ONE cited value to its exact output bytes and method, for any producer that
keeps such bytes. The stored-seed envelope
(``src/precis_pathway/bundle.py::build_envelope``) is the first producer with
retained, hash-pinned outputs; the concrete ``pathway_seed`` adapter below is
written against it. A generic ``external_result`` adapter covers a producer
that is not a pathway seed (an external package run behind a precis job): the
caller supplies the frozen bytes and the same selector/unit/method contract
applies. It deliberately knows nothing about the producer's internals —
gr564186 tracks which external packages actually need it, and November's
pathway package does not wait on it.

The contract is evidence, not a new claim store: it maps INTO the existing
anchors. A measure row carries it in ``meta['computed_source']`` (validated
for shape at write; the row's anchor/run semantics are untouched — a computed
row has no paper anchor and must say ``origin='computation'``). A finding's
citation keeps its computed-edge pin, and :func:`check_frozen_pins` confirms
an approved artifact's frozen pin set (``payload['computed']``, mint.py's
freeze) still covers the contract's pathway pin — a signed artifact is never
silently retargeted onto different evidence. The data-package export
(``src/precis/export/_data_package.py``) freezes whole tables per figure; a
computed-source contract may ride inside such a snapshot verbatim, but this
module never derives numbers from formatted text — only from the pinned
bytes, through the exact selector.

Verification is read-only and re-derives everything it can: producer
identity (``changed-source``), output digest (``changed-output``), selector
presence (``missing-field``, naming what IS there), and — when a caller
restates what a manuscript cites — unit and method agreement
(``unit-mismatch`` / ``method-mismatch``). Nothing here re-resolves a moved
pin onto new bytes; a moved pin is a refusal, and the fix is always to
re-derive and re-approve, exactly the standing ``check_computed`` behavior.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput

SOURCE_SCHEMA = "precis_computed_source"
SOURCE_SCHEMA_VERSION = 1

#: The concrete adapter: a stored ``autocatpath_seed`` job's evidence
#: envelope (:func:`precis_pathway.bundle.build_envelope` output, consumed as
#: a plain dict — this module never imports the pathway package).
ADAPTER_PATHWAY_SEED = "pathway_seed"

#: The generic adapter for any other producer that can hand over frozen
#: output bytes: the caller supplies the bytes and a run identity; the
#: selector/unit/method contract is identical. Optional follow-on work
#: (gr564186) decides which external packages actually run behind it.
ADAPTER_EXTERNAL_RESULT = "external_result"

_ADAPTERS = frozenset({ADAPTER_PATHWAY_SEED, ADAPTER_EXTERNAL_RESULT})
_ENCODINGS = frozenset({"json", "csv"})
_SHA256 = re.compile(r"[0-9a-f]{64}")

#: Issue kinds, one per acceptance arm: the source/key moved, the output
#: bytes moved, the selector no longer resolves, the restated unit or method
#: disagrees with the frozen contract, or the contract itself is malformed.
ISSUE_KINDS = (
    "changed-source",
    "changed-output",
    "missing-field",
    "unit-mismatch",
    "method-mismatch",
    "malformed-contract",
)


@dataclass(frozen=True, slots=True)
class SourceIssue:
    """One verification failure: a machine-routable ``kind`` plus a human
    line that names both the pinned and the observed value."""

    kind: str
    message: str


def canonical_bytes(value: Any) -> bytes:
    """Canonical v1 JSON bytes — the same spelling as
    ``src/precis_pathway/bundle.py::canonical_bytes``, so digest interop with
    envelope pins (``output_id`` / inline ``sha256``) holds by construction,
    not by convention."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def contract_fingerprint(contract: dict[str, Any]) -> str:
    """The contract's content address. A caller that freezes the contract
    (a measure row's meta, an approved mint payload) stores this alongside,
    so a hand-edited contract is visible as a fingerprint change even when
    every individual pin still verifies."""
    return hashlib.sha256(canonical_bytes(contract)).hexdigest()


class _Miss(ValueError):
    """A selector did not resolve — the message names what is available."""


def _available(container: Any) -> str:
    if isinstance(container, dict):
        keys = sorted(str(k) for k in container)
        shown = ", ".join(keys[:8])
        return f"available keys: {shown}{', …' if len(keys) > 8 else ''}"
    if isinstance(container, list):
        return f"the list has {len(container)} entr{'y' if len(container) == 1 else 'ies'} (index 0..{len(container) - 1})"
    return f"the value there is a {type(container).__name__} scalar"


def _select_json(data: Any, selector: str) -> Any:
    """Apply an exact dot-path selector (``states.s1.energy``; a numeric
    component indexes a list). Any divergence raises :class:`_Miss` naming
    the available keys/indices — a missing selector refusal must read as a
    map, not a bare no."""
    current = data
    prefix: list[str] = []
    for component in selector.split("."):
        if not component:
            raise _Miss(f"empty path component in {selector!r}")
        prefix.append(component)
        where = ".".join(prefix)
        if isinstance(current, dict):
            if component not in current:
                raise _Miss(
                    f"{where!r}: {component!r} not found — {_available(current)}"
                )
            current = current[component]
        elif isinstance(current, list):
            if not component.isdigit() or int(component) >= len(current):
                raise _Miss(
                    f"{where!r}: {component!r} is not a valid index — "
                    f"{_available(current)}"
                )
            current = current[int(component)]
        else:
            raise _Miss(
                f"{where!r}: cannot descend into a {type(current).__name__} scalar"
            )
    return current


def _select_csv(text: str, selector: str, row: int | None) -> Any:
    """Apply a CSV selector: the selector IS the exact column header; ``row``
    is the 0-based data-row index (default 0). A numeric cell reads as a
    finite float, anything else as its stripped string."""
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if not rows:
        raise _Miss("the CSV has no rows")
    header = [h.strip() for h in rows[0]]
    if selector not in header:
        raise _Miss(
            f"column {selector!r} not in the header — "
            f"available columns: {', '.join(header)}"
        )
    index = header.index(selector)
    which = 0 if row is None else row
    data_rows = rows[1:]
    if which >= len(data_rows):
        raise _Miss(
            f"row {which} of column {selector!r} — the CSV has "
            f"{len(data_rows)} data row(s) (index 0..{len(data_rows) - 1})"
        )
    cell = data_rows[which][index].strip()
    try:
        number = float(cell)
    except ValueError:
        return cell
    if not math.isfinite(number):
        raise _Miss(f"row {which} of column {selector!r} is not a finite number")
    return number


def _select(data: Any, encoding: str, selector: str, row: int | None) -> Any:
    if encoding == "json":
        return _select_json(data, selector)
    return _select_csv(data, selector, row)


def _resolved_value(raw: Any) -> tuple[float | None, str | None]:
    """The selected value as (number, text): exactly one arm set."""
    if isinstance(raw, bool):
        return None, "true" if raw else "false"
    if isinstance(raw, (int, float)):
        number = float(raw)
        if not math.isfinite(number):
            raise BadInput(
                f"the selector resolves to a non-finite number ({raw!r}) — "
                "a citable value must be a finite number or a string"
            )
        return number, None
    if isinstance(raw, str):
        return None, raw
    raise BadInput(
        f"the selector resolves to a {type(raw).__name__}, not a number or "
        "string — select one exact field (a whole object is not a value)"
    )


def _require(condition: bool, message: str, **kwargs: Any) -> None:
    """Raise :class:`BadInput` with ``message`` (plus the caller's refusal
    fields, always ``next=``) when ``condition`` is false."""
    if not condition:
        raise BadInput(message, **kwargs)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


# ── build: producer → contract ─────────────────────────────────────────


def _coerce_literal(
    literal: str, number: float | None, text: str | None, *, selector: str
) -> None:
    """The printed literal must BE the resolved value, not an approximation
    of it — a contract whose literal disagrees with its own bytes would
    publish a number the evidence does not state (the computed analogue of
    the field-containment gate)."""
    if number is not None:
        try:
            stated = float(literal.strip())
        except ValueError:
            raise BadInput(
                f"literal {literal!r} is not the numeric value the selector "
                f"resolves to ({number:g}) — the literal is the printed value "
                "the manuscript cites",
                next=f"pass literal='{number:g}' (or select a field that "
                f"holds the value {literal!r})",
            ) from None
        if stated != number:
            raise BadInput(
                f"literal {literal!r} states {stated:g} but selector "
                f"{selector!r} resolves to {number:g} — cite the value the "
                "bytes actually hold, or select the field that holds it",
            )
    elif text is not None and literal != text:
        raise BadInput(
            f"literal {literal!r} is not the value the selector resolves to "
            f"({text!r}) — the literal must be the printed value, verbatim",
        )


def _require_selector_unit_method(
    *, selector: str | None, unit: str | None, method: str | None, encoding: str
) -> str:
    """The three omissions the acceptance calls out, each an actionable
    refusal rather than a silent NULL: a value citation without its exact
    field selector, its unit, or its method cannot be re-checked by anyone."""
    grammar = (
        "a dot-path like 'states.s1.energy' (a numeric component indexes a list)"
        if encoding == "json"
        else "the exact column header"
    )
    _require(
        isinstance(selector, str) and bool(selector.strip()),
        "a computed source needs its exact field selector — without it the "
        "value cannot be traced into the output bytes",
        next=f"pass selector=<{grammar}> naming the field the value is read from",
    )
    _require(
        isinstance(unit, str) and bool(unit.strip()),
        "a computed source needs its unit — a bare number cannot be cited "
        "across unit systems",
        next="pass unit=<the unit the literal is printed in, e.g. 'eV'>",
    )
    _require(
        isinstance(method, str) and bool(method.strip()),
        "a computed source needs its method — the computation that produced "
        "the value is half of what a reader re-runs",
        next="pass method='<the method/conditions label, e.g. 'mace:small relaxation'>",
    )
    # the three _require calls above refuse every non-string/blank case
    assert isinstance(selector, str)
    return selector.strip()


def _contract(
    *,
    producer: dict[str, Any],
    path: str,
    encoding: str,
    sha256: str,
    selector: str,
    row: int | None,
    literal: str,
    unit: str,
    method: str,
    conditions: dict[str, Any] | None,
    parsed: Any,
) -> dict[str, Any]:
    number, text = _resolved_value(parsed)
    _coerce_literal(literal, number, text, selector=selector)
    conds = conditions or {}
    return {
        "schema": SOURCE_SCHEMA,
        "schema_version": SOURCE_SCHEMA_VERSION,
        "producer": producer,
        "output": {
            "path": path,
            "encoding": encoding,
            "sha256": sha256,
            "selector": selector,
            "row": row,
        },
        "value": {
            "literal": literal.strip(),
            "unit": unit.strip(),
            "number": number,
            "text": text,
        },
        "method": {
            "name": method.strip(),
            "conditions": conds,
            "conditions_sha256": hashlib.sha256(canonical_bytes(conds)).hexdigest(),
        },
    }


def build_pathway_source(
    *,
    envelope: dict[str, Any],
    path: str,
    selector: str | None,
    literal: str,
    unit: str | None,
    method: str | None,
    conditions: dict[str, Any] | None = None,
    row: int | None = None,
) -> dict[str, Any]:
    """Freeze one cited value against a stored seed envelope.

    ``envelope`` is a detached :func:`precis_pathway.bundle.build_envelope`
    result (plain dict; the caller retains its canonical bytes — rereading a
    mutable ref cannot recover the snapshot the contract pins). ``path`` is
    one of the envelope's recorded output names (e.g.
    ``outputs/partial.json``); ``selector`` the exact field within it. The
    producer identity pins (job, run stamps, pathway content key,
    ``output_id``, ``bundle_id``) are read from the envelope, never supplied.
    Missing selector/unit/method, an unrecorded output, a selector that does
    not resolve, or a literal that disagrees with the resolved value each
    refuse with the fix named."""
    _require(
        isinstance(envelope, dict)
        and envelope.get("schema") == "precis_pathway_seed_envelope"
        and envelope.get("schema_version") == 1,
        "build_pathway_source needs a build_envelope result "
        "(schema 'precis_pathway_seed_envelope', version 1)",
        next="call precis_pathway.bundle.build_envelope with selected stored "
        "snapshots and pass its detached dict",
    )
    outputs = envelope.get("outputs")
    entries = (
        [o for o in outputs if isinstance(o, dict) and o.get("name") == path]
        if isinstance(outputs, list)
        else []
    )
    names = (
        sorted(str(o.get("name")) for o in outputs if isinstance(o, dict))
        if isinstance(outputs, list)
        else []
    )
    _require(
        len(entries) == 1,
        f"envelope records no output named {path!r}"
        + (f" — recorded outputs: {', '.join(names)}" if names else ""),
        next="pass path=<one of the recorded output names above>",
    )
    entry = entries[0]
    encoding = entry.get("encoding")
    _require(
        encoding == "json",
        f"output {path!r} is stored as {encoding!r}, not 'json' — text outputs "
        "carry no field to select",
        next="select one of the JSON outputs",
    )
    sel = _require_selector_unit_method(
        selector=selector, unit=unit, method=method, encoding="json"
    )
    try:
        parsed = _select_json(entry.get("data"), sel)
    except _Miss as miss:
        raise BadInput(
            f"selector {sel!r} does not resolve in {path!r}: {miss}",
            next="pass selector=<one of the available keys above>",
        ) from None
    run = envelope.get("run") or {}
    context = envelope.get("pathway_context") or {}
    producer = {
        "adapter": ADAPTER_PATHWAY_SEED,
        "job_id": run.get("job_id"),
        "job_type": run.get("job_type"),
        "content_key": run.get("content_key"),
        "seed": run.get("seed"),
        "model_index": run.get("model_index"),
        "model": run.get("model"),
        "pathway_ref_id": context.get("pathway_ref_id"),
        "pathway_content_key": context.get("content_key"),
        "output_id": envelope.get("output_id"),
        "bundle_id": envelope.get("bundle_id"),
    }
    return _contract(
        producer=producer,
        path=path,
        encoding="json",
        sha256=str(entry.get("sha256")),
        selector=sel,
        row=row,
        literal=literal,
        unit=unit or "",
        method=method or "",
        conditions=conditions,
        parsed=parsed,
    )


def build_external_source(
    *,
    producer: str,
    run_key: str,
    data: bytes,
    path: str,
    encoding: str,
    selector: str | None,
    literal: str,
    unit: str | None,
    method: str | None,
    conditions: dict[str, Any] | None = None,
    row: int | None = None,
) -> dict[str, Any]:
    """Freeze one cited value against an arbitrary producer's output bytes.

    The generic adapter behind any non-pathway producer (an external package
    run recorded by a precis job, gr564186): the caller hands over the
    frozen bytes, the digest is computed here, and the selector/unit/method
    contract is byte-for-byte the pathway adapter's. ``encoding`` is
    ``json`` (``selector`` is a dot-path) or ``csv`` (``selector`` is the
    exact column header; ``row`` the 0-based data-row index, default 0). The
    same refusals as :func:`build_pathway_source`."""
    _require(
        isinstance(producer, str) and bool(producer.strip()),
        "an external source needs its producer's name — 'some run' cannot "
        "be cited back to anything",
        next="pass producer='<the package/job name that produced the bytes>'",
    )
    _require(
        isinstance(run_key, str) and bool(run_key.strip()),
        "an external source needs its run identity — the envelope/run the "
        "bytes belong to",
        next="pass run_key='<the job id or envelope id the bytes were frozen from>'",
    )
    _require(
        isinstance(data, bytes) and bool(data),
        "an external source needs the producer's frozen output bytes",
        next="pass data=<the exact bytes the value is read from>",
    )
    _require(
        encoding in _ENCODINGS,
        f"encoding {encoding!r} is not json or csv",
        next="pass encoding='json' (selector is a dot-path) or 'csv' "
        "(selector is the exact column header)",
    )
    _require(
        isinstance(path, str) and bool(path.strip()),
        "an external source needs the output's logical path (its name in the "
        "run's output set)",
        next="pass path='outputs/result.json' (the name the bytes are filed under)",
    )
    if encoding == "json":
        try:
            parsed_doc: Any = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BadInput(
                f"the bytes are not valid UTF-8 JSON: {exc}",
                next="pass the producer's raw JSON output bytes",
            ) from None
        sel = _require_selector_unit_method(
            selector=selector, unit=unit, method=method, encoding="json"
        )
        try:
            parsed = _select_json(parsed_doc, sel)
        except _Miss as miss:
            raise BadInput(
                f"selector {sel!r} does not resolve in {path!r}: {miss}",
                next="pass selector=<one of the available keys above>",
            ) from None
    else:
        sel = _require_selector_unit_method(
            selector=selector, unit=unit, method=method, encoding="csv"
        )
        try:
            parsed = _select_csv(data.decode("utf-8"), sel, row)
        except UnicodeDecodeError as exc:
            raise BadInput(
                f"the bytes are not valid UTF-8 CSV: {exc}",
                next="pass the producer's raw CSV output bytes",
            ) from None
        except _Miss as miss:
            raise BadInput(
                f"selector {sel!r} does not resolve in {path!r}: {miss}",
                next="pass selector=<one of the available columns above>",
            ) from None
    return _contract(
        producer={
            "adapter": ADAPTER_EXTERNAL_RESULT,
            "name": producer.strip(),
            "run_key": run_key.strip(),
        },
        path=path.strip(),
        encoding=encoding,
        sha256=hashlib.sha256(data).hexdigest(),
        selector=sel,
        row=row,
        literal=literal,
        unit=unit or "",
        method=method or "",
        conditions=conditions,
        parsed=parsed,
    )


# ── validate + verify: contract → standing ─────────────────────────────


def validate_contract(contract: Any) -> list[str]:
    """Structural problems with a contract dict, as short strings; empty
    when well-formed. Deliberately shape-only: it never touches a producer,
    so the measure write path can validate a caller-supplied contract
    without the frozen envelope in hand."""
    if not isinstance(contract, dict):
        return ["not an object"]
    out: list[str] = []
    if contract.get("schema") != SOURCE_SCHEMA:
        out.append(f"schema is {contract.get('schema')!r}, not {SOURCE_SCHEMA!r}")
    if contract.get("schema_version") != SOURCE_SCHEMA_VERSION:
        out.append(
            f"schema_version is {contract.get('schema_version')!r}, not {SOURCE_SCHEMA_VERSION}"
        )
    producer = contract.get("producer")
    if not isinstance(producer, dict):
        out.append("producer is not an object")
    else:
        adapter = producer.get("adapter")
        if adapter not in _ADAPTERS:
            out.append(
                f"producer.adapter is {adapter!r}, not one of {sorted(_ADAPTERS)}"
            )
        elif adapter == ADAPTER_PATHWAY_SEED:
            for key in ("job_id", "pathway_ref_id"):
                if not isinstance(producer.get(key), int) or isinstance(
                    producer.get(key), bool
                ):
                    out.append(f"producer.{key} is not an int")
            for key in ("output_id", "bundle_id"):
                if not _is_sha256(producer.get(key)):
                    out.append(f"producer.{key} is not a sha256 hex string")
            if not isinstance(producer.get("pathway_content_key"), str):
                out.append("producer.pathway_content_key is not a string")
        else:
            for key in ("name", "run_key"):
                if not isinstance(producer.get(key), str) or not producer.get(key):
                    out.append(f"producer.{key} is missing or not a string")
    output = contract.get("output")
    if not isinstance(output, dict):
        out.append("output is not an object")
    else:
        if not isinstance(output.get("path"), str) or not output.get("path"):
            out.append("output.path is missing or not a string")
        if output.get("encoding") not in _ENCODINGS:
            out.append(f"output.encoding is not one of {sorted(_ENCODINGS)}")
        if not _is_sha256(output.get("sha256")):
            out.append("output.sha256 is not a sha256 hex string")
        if not isinstance(output.get("selector"), str) or not output.get("selector"):
            out.append("output.selector is missing or not a string")
        if output.get("row") is not None and (
            not isinstance(output.get("row"), int)
            or isinstance(output.get("row"), bool)
        ):
            out.append("output.row is not an int")
    value = contract.get("value")
    if not isinstance(value, dict):
        out.append("value is not an object")
    else:
        if not isinstance(value.get("literal"), str) or not value.get("literal"):
            out.append("value.literal is missing or not a string")
        if not isinstance(value.get("unit"), str) or not value.get("unit"):
            out.append("value.unit is missing or not a string")
        number, text = value.get("number"), value.get("text")
        if (number is None) == (text is None):
            out.append("exactly one of value.number / value.text must be set")
        if number is not None and (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(float(number))
        ):
            out.append("value.number is not a finite number")
    method = contract.get("method")
    if not isinstance(method, dict):
        out.append("method is not an object")
    else:
        if not isinstance(method.get("name"), str) or not method.get("name"):
            out.append("method.name is missing or not a string")
        if not isinstance(method.get("conditions"), dict):
            out.append("method.conditions is not an object")
        if not _is_sha256(method.get("conditions_sha256")):
            out.append("method.conditions_sha256 is not a sha256 hex string")
    return out


def _inline_bytes(entry: dict[str, Any]) -> bytes:
    if entry.get("encoding") == "utf-8":
        data = entry.get("data")
        return data.encode("utf-8") if isinstance(data, str) else b""
    return canonical_bytes(entry.get("data"))


def _verify_pathway_identity(
    contract: dict[str, Any], envelope: dict[str, Any]
) -> list[SourceIssue]:
    """Every identity pin the contract froze from the envelope, re-read from
    the candidate envelope now. A moved pin is ``changed-source`` naming both
    values — the refusal IS the designed behavior; nothing retargets."""
    producer = contract["producer"]
    issues: list[SourceIssue] = []
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != "precis_pathway_seed_envelope"
        or envelope.get("schema_version") != 1
    ):
        return [
            SourceIssue(
                "changed-source",
                "the candidate evidence is not a precis_pathway_seed_envelope "
                "v1 — the frozen producer identity cannot be re-checked against it",
            )
        ]
    run = envelope.get("run") or {}
    context = envelope.get("pathway_context") or {}
    observed = {
        "job_id": run.get("job_id"),
        "job_type": run.get("job_type"),
        "content_key": run.get("content_key"),
        "seed": run.get("seed"),
        "model_index": run.get("model_index"),
        "model": run.get("model"),
        "pathway_ref_id": context.get("pathway_ref_id"),
        "pathway_content_key": context.get("content_key"),
        "output_id": envelope.get("output_id"),
        "bundle_id": envelope.get("bundle_id"),
    }
    for key, pinned in producer.items():
        if key == "adapter":
            continue
        current = observed.get(key)
        if current != pinned:
            issues.append(
                SourceIssue(
                    "changed-source",
                    f"producer pin {key} moved: the contract froze "
                    f"{pinned!r}, the stored envelope now records {current!r} — "
                    "re-derive the source from the current envelope and "
                    "re-approve; a moved pin is never silently retargeted",
                )
            )
    return issues


class _BytesMoved(ValueError):
    """Internal carrier: the pinned bytes no longer parse under their frozen
    encoding — converted to a ``changed-output`` :class:`SourceIssue` at the
    catch site (issues are values, not exceptions)."""


def _parse_pinned_bytes(contract: dict[str, Any], raw: bytes) -> Any:
    """The pinned bytes parsed for selection, or :class:`_BytesMoved` when
    they no longer parse — bytes under a moved digest are different bytes,
    and the refusal says so."""
    if contract["output"]["encoding"] == "json":
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _BytesMoved(
                f"the output bytes under {contract['output']['path']!r} no "
                f"longer parse as JSON ({exc}) — the pinned digest moved to "
                "different bytes",
            ) from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BytesMoved(
            f"the output bytes under {contract['output']['path']!r} are no "
            f"longer UTF-8 ({exc}) — the pinned digest moved to different bytes",
        ) from None


def verify_source(
    contract: dict[str, Any],
    *,
    envelope: dict[str, Any] | None = None,
    data: bytes | None = None,
    unit: str | None = None,
    method: str | None = None,
    conditions: dict[str, Any] | None = None,
) -> list[SourceIssue]:
    """Re-derive everything checkable and report what no longer stands.
    Empty return: the cited value traces to these exact bytes, this unit,
    this method. ``unit`` / ``method`` / ``conditions`` are what a caller
    (a manuscript block, an export) restates NOW: disagreement with the
    frozen contract is a mismatch issue, never a silent adoption.

    A pathway contract needs the candidate ``envelope``; an external one the
    candidate ``data`` bytes. Both are read-only inputs — verification never
    mutates, re-freezes, or substitutes."""
    problems = validate_contract(contract)
    if problems:
        return [SourceIssue("malformed-contract", p) for p in problems]
    issues: list[SourceIssue] = []
    adapter = contract["producer"]["adapter"]
    if adapter == ADAPTER_PATHWAY_SEED:
        if envelope is None:
            raise BadInput(
                "verifying a pathway_seed source needs the candidate envelope",
                next="pass envelope=<the build_envelope result to check against>",
            )
        issues += _verify_pathway_identity(contract, envelope)
        path = contract["output"]["path"]
        outputs = envelope.get("outputs")
        entries = (
            [o for o in outputs if isinstance(o, dict) and o.get("name") == path]
            if isinstance(outputs, list)
            else []
        )
        if len(entries) != 1:
            issues.append(
                SourceIssue(
                    "changed-output",
                    f"the envelope no longer records exactly one output named "
                    f"{path!r} — the frozen output is gone",
                )
            )
            raw = None
        else:
            raw = _inline_bytes(entries[0])
    else:
        if data is None:
            raise BadInput(
                "verifying an external_result source needs the candidate bytes",
                next="pass data=<the producer's frozen output bytes>",
            )
        raw = data
    if raw is not None:
        observed_sha = hashlib.sha256(raw).hexdigest()
        digest_ok = observed_sha == contract["output"]["sha256"]
        if not digest_ok:
            issues.append(
                SourceIssue(
                    "changed-output",
                    f"output {contract['output']['path']!r} digest moved: the "
                    f"contract pins {contract['output']['sha256'][:12]}…, the "
                    f"bytes now hash {observed_sha[:12]}… — the cited output "
                    "changed; re-derive the source and re-approve",
                )
            )
        try:
            parsed = _parse_pinned_bytes(contract, raw)
            resolved = _select(
                parsed,
                contract["output"]["encoding"],
                contract["output"]["selector"],
                contract["output"].get("row"),
            )
        except _Miss as miss:
            issues.append(
                SourceIssue(
                    "missing-field",
                    f"selector {contract['output']['selector']!r} no longer "
                    f"resolves in {contract['output']['path']!r}: {miss}",
                )
            )
        except _BytesMoved as moved:
            issues.append(SourceIssue("changed-output", str(moved)))
        else:
            if not digest_ok:
                pass  # a moved digest already refuses above — no pile-on
            else:
                # the bytes stand: the value the contract claims must be the
                # value they hold
                number, text = _resolved_value(resolved)
                value = contract["value"]
                if number is not None and (
                    value["number"] is None or float(value["number"]) != number
                ):
                    issues.append(
                        SourceIssue(
                            "changed-output",
                            f"selector {contract['output']['selector']!r} resolves "
                            f"to {number:g} in the pinned bytes but the contract "
                            f"claims {value['number']!r} — the contract was edited "
                            "or its pins moved; re-derive it from the envelope",
                        )
                    )
                if text is not None and value["text"] != text:
                    issues.append(
                        SourceIssue(
                            "changed-output",
                            f"selector {contract['output']['selector']!r} resolves "
                            f"to {text!r} in the pinned bytes but the contract "
                            f"claims {value['text']!r} — the contract was edited "
                            "or its pins moved; re-derive it from the envelope",
                        )
                    )
    cited_unit = contract["value"]["unit"]
    if unit is not None and unit.strip() != cited_unit:
        issues.append(
            SourceIssue(
                "unit-mismatch",
                f"the unit now cited is {unit.strip()!r} but the contract "
                f"froze {cited_unit!r} — restate the value in the frozen unit "
                "or re-derive the source; a unit change is never adopted silently",
            )
        )
    cited_method = contract["method"]["name"]
    if method is not None and method.strip() != cited_method:
        issues.append(
            SourceIssue(
                "method-mismatch",
                f"the method now cited is {method.strip()!r} but the contract "
                f"froze {cited_method!r} — the value was produced by the "
                "frozen method; re-derive the source under its own name",
            )
        )
    if conditions is not None:
        digest = hashlib.sha256(canonical_bytes(conditions)).hexdigest()
        if digest != contract["method"]["conditions_sha256"]:
            issues.append(
                SourceIssue(
                    "method-mismatch",
                    "the conditions now cited hash "
                    f"{digest[:12]}… but the contract froze "
                    f"{contract['method']['conditions_sha256'][:12]}… — the "
                    "run's conditions moved; re-derive the source from the "
                    "run that actually produced it",
                )
            )
    return issues


def require_verified(contract: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """:func:`verify_source` as a refusal: returns the contract when it
    stands, raises :class:`BadInput` naming every issue otherwise — the
    idiom a handler or export step uses when a stale source must stop the
    world, not just render a notice."""
    issues = verify_source(contract, **kwargs)
    if issues:
        raise BadInput(
            "; ".join(f"{i.kind}: {i.message}" for i in issues),
            next="re-derive the source from the stored producer evidence with "
            "build_pathway_source / build_external_source and re-approve",
        )
    return contract


def check_frozen_pins(
    contract: dict[str, Any], pins: list[dict[str, Any]] | None
) -> list[SourceIssue]:
    """Does an approved artifact's frozen computed pin set still cover this
    contract? ``pins`` is ``payload['computed']`` from the mint freeze
    (:func:`precis.nanopub.gates.frozen_computed_pins` over a stored
    grounding). A pathway contract's ``(pathway_ref_id,
    pathway_content_key)`` must appear among the frozen pins — approve froze
    exactly the standing edges, so a contract whose pin is absent (or never
    pinned) names an artifact that was approved over different evidence.
    External contracts carry no pathway pin and always pass. Read-only;
    never retargets."""
    problems = validate_contract(contract)
    if problems:
        return [SourceIssue("malformed-contract", p) for p in problems]
    producer = contract["producer"]
    if producer["adapter"] != ADAPTER_PATHWAY_SEED:
        return []
    frozen = [
        pin
        for pin in (pins or [])
        if isinstance(pin, dict)
        and pin.get("pathway_ref_id") == producer["pathway_ref_id"]
        and pin.get("content_key") == producer["pathway_content_key"]
    ]
    if frozen:
        return []
    return [
        SourceIssue(
            "changed-source",
            f"no frozen computed pin covers pathway ref "
            f"{producer['pathway_ref_id']} at content key "
            f"{producer['pathway_content_key']!r} — the approved artifact's "
            "pin set cites different evidence; reopen and re-approve over "
            "this source, never retarget a signed artifact silently",
        )
    ]


__all__ = [
    "ADAPTER_EXTERNAL_RESULT",
    "ADAPTER_PATHWAY_SEED",
    "ISSUE_KINDS",
    "SOURCE_SCHEMA",
    "SOURCE_SCHEMA_VERSION",
    "SourceIssue",
    "build_external_source",
    "build_pathway_source",
    "canonical_bytes",
    "check_frozen_pins",
    "contract_fingerprint",
    "require_verified",
    "validate_contract",
    "verify_source",
]
