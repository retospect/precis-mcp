"""TOON (Token-Oriented Object Notation) serialiser / deserialiser.

The format we emit is the flat homogeneous-rows shape from
`docs/design/b10-toon-output.md` §"Format spec":

    col1<TAB>col2<TAB>col3
    val1<TAB>val2<TAB>val3
    val1<TAB>val2<TAB>val3

That's TOON's tabular-section form, the part that gives the
~40 % token saving over JSON on lists of homogeneous records.
We deliberately do *not* implement TOON's hierarchical syntax
(nested objects, length-prefixed sub-arrays); ADR 0002 routes
those through JSON instead.

The audience is an LLM, not a parser. The dump rules are tuned
for token frugality and human-/agent-readability rather than
strict RFC 4180 round-trippability:

* A cell is wrapped in ``"..."`` **only** when it contains the
  separator, a newline, or a carriage return — characters that
  would otherwise confuse the column / row structure the LLM
  reads off the wire.
* Bare double quotes inside cells are passed through verbatim.
  An LLM reads ``The "Bayes Factor" Revisited`` as a single cell
  far more readily than the RFC 4180 escape ``"The ""Bayes
  Factor"" Revisited"``, and pure-`"` content does not break
  the columnar shape so the wrapper is unjustified token cost.
* All-empty rows are emitted as the empty string — they aren't
  force-quoted to ``""`` to "preserve" the row across a
  ``load(dump(rows))`` round-trip. We don't parse our own output
  in production.

``None`` renders as the empty string; booleans as ``true``/
``false``; floats via ``repr`` to keep precision when displayed.

``load()`` retains the full RFC 4180 grammar (it understands
``""``-escaped quotes inside wrapped cells) so any well-formed
TOON input parses; it just won't see the unnecessary escapes
from our own dumps. Round-trip is best-effort for cells we
control: any cell starting with a literal ``"`` is the only
known shape that ``load`` cannot reverse, since the leading
quote is indistinguishable from a wrapper open. That trade is
intentional — see ``docs/design/b10-toon-output.md``.

The implementation is pure Python with no external dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Cells containing any of these characters need wrapping; without
# the wrapper a bare line break or tab would split the row mid-
# cell and an LLM would mis-count columns. The separator itself is
# added to the trigger set at runtime via the ``sep`` argument.
# Note: ``"`` is *not* a trigger — see the module docstring for
# the LLM-audience reasoning.
_QUOTE_TRIGGERS_FIXED = ("\n", "\r")


def dump(
    data: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    sep: str = "\t",
    schema: list[str] | None = None,
) -> str:
    """Serialise *data* as a TOON document.

    Parameters
    ----------
    data
        Either a list of row mappings or a single mapping (treated
        as a 1-row table). Each value must be one of the supported
        scalar types (``None``, ``bool``, ``int``, ``float``,
        ``str``) or convertible via ``str()``.
    sep
        Column delimiter. Defaults to ``"\\t"`` per ADR 0002.
    schema
        Optional explicit column order. When supplied:

        * Columns are emitted in this order.
        * Keys present in rows but absent from *schema* are dropped.
        * Columns listed in *schema* but missing from a row render
          as empty cells.

    Returns
    -------
    str
        The serialised document. No trailing newline.

    Notes
    -----
    Heterogeneous keys across rows union in first-seen order when
    no *schema* is given; that keeps the column ordering stable
    across reruns even if individual rows omit some columns.
    """
    # Normalise to a list of mappings up front. The bare-dict case
    # is a convenience so callers don't have to wrap single records
    # in `[...]`.
    if isinstance(data, Mapping):
        rows: list[Mapping[str, Any]] = [data]
    elif isinstance(data, list):
        for i, row in enumerate(data):
            if not isinstance(row, Mapping):
                raise TypeError(
                    f"toon.dump: rows must be mappings; "
                    f"got {type(row).__name__} at index {i}"
                )
        rows = data
    else:
        raise TypeError(
            f"toon.dump: expected list of mappings or a single mapping; "
            f"got {type(data).__name__}"
        )

    columns = _resolve_columns(rows, schema)

    # Special case: empty list with a pinned schema still emits a
    # header so downstream callers see the column shape.
    if not rows:
        if columns:
            return sep.join(_encode_cell(c, sep) for c in columns)
        return ""

    header_line = sep.join(_encode_cell(c, sep) for c in columns)
    body_lines = [
        sep.join(_encode_cell(row.get(c), sep) for c in columns) for row in rows
    ]
    # All-empty rows render as the empty string; we deliberately do
    # not force-quote them to ``""`` to preserve a load round-trip.
    # The LLM consumer doesn't care, and ``""`` would just spend
    # tokens on shape the wire format does not need to carry.
    return "\n".join([header_line, *body_lines])


def load(text: str, *, sep: str = "\t") -> list[dict[str, str]]:
    """Parse a TOON document back to a list of dicts.

    Cells always come back as ``str`` — TOON is a transport, not
    a schema language. ``None``-typed empties round-trip as the
    empty string.

    Tolerates ``\\r\\n`` line endings and trailing blank lines (the
    common shape when piping output through `cat` or similar).
    Returns ``[]`` for empty/whitespace-only input. A header-only
    document also returns ``[]`` (no concrete rows to surface).
    """
    if not text or not text.strip():
        return []

    # Token-by-token scan over the raw string. We can't use `str.split`
    # because quoted fields may contain embedded newlines. The parser
    # walks character-by-character, tracking whether we're inside a
    # `"..."` quoted field and whether the current quote is being
    # escaped by a following `"`.
    records = _tokenise(text, sep=sep)
    if not records:
        return []

    header = records[0]
    rows = records[1:]

    # Header-only docs are valid but produce no data rows.
    if not rows:
        return []

    return [
        {header[i]: row[i] if i < len(row) else "" for i in range(len(header))}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_columns(
    rows: list[Mapping[str, Any]],
    schema: list[str] | None,
) -> list[str]:
    """Resolve the column order for serialisation.

    Pinned ``schema`` wins; otherwise we union the keys across all
    rows in first-seen order (Python's dict preserves insertion
    order since 3.7, so the union is deterministic).
    """
    if schema is not None:
        return list(schema)
    seen: dict[str, None] = {}
    for row in rows:
        for k in row:
            if k not in seen:
                seen[k] = None
    return list(seen)


def _encode_cell(value: Any, sep: str) -> str:
    """Render a single cell value to its serialised string form."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        text = repr(value)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)

    if any(t in text for t in _QUOTE_TRIGGERS_FIXED) or sep in text:
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text


def _tokenise(text: str, *, sep: str) -> list[list[str]]:
    """Scan *text* into a list of records, each a list of cell strings.

    A record is a top-level (i.e. not inside a quoted field) line.
    Quoted fields may contain the separator, newlines, and `""`-
    escaped double quotes.

    Two rules govern record termination:

    * **Trailing newline tolerance** — a document ending in `\\n`
      does not gain a phantom empty row. Internally tracked via
      ``cell_started``: only flush a record when the current cell
      has been touched (any character, separator, or opening
      quote).
    * **Blank line tolerance** — a literal `\\n\\n` mid-document is
      treated as a single record break, not "row + empty row".
      ``dump`` never emits truly-blank rows (an all-empty row in a
      single-column table is force-quoted to ``""``), so this rule
      is purely defensive against handwritten input.
    """
    records: list[list[str]] = []
    fields: list[str] = []
    cell: list[str] = []
    in_quotes = False
    # ``cell_started`` flips to True the moment we touch the
    # current cell — a non-special character, an opening quote, or
    # the end of the previous cell (which begins the next one).
    # We use it to distinguish "just hit a sep then EOF" (legit
    # trailing empty cell) from "blank trailing line" (skip).
    cell_started = False
    i = 0
    n = len(text)
    sep_len = len(sep)

    def _commit_cell() -> None:
        nonlocal cell_started
        fields.append("".join(cell))
        cell.clear()
        cell_started = False

    def _commit_record() -> None:
        records.append(list(fields))
        fields.clear()

    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                # Doubled-quote inside a quoted field is an escape;
                # otherwise the quote closes the field.
                if i + 1 < n and text[i + 1] == '"':
                    cell.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            cell.append(ch)
            i += 1
            continue

        # Not in quotes — normal mode.
        if ch == '"' and not cell:
            # A quote at the very start of a cell opens a quoted
            # field. Quotes mid-cell are treated as literals (no
            # standard says otherwise; our `dump` never emits one).
            in_quotes = True
            cell_started = True
            i += 1
            continue
        if text.startswith(sep, i):
            _commit_cell()
            # The next cell is now in progress (even if it ends up
            # empty), so a subsequent newline must still flush it.
            cell_started = True
            i += sep_len
            continue
        if ch == "\r":
            # `\r\n` collapses to a record break; lone `\r` does too.
            if cell_started or fields:
                _commit_cell()
                _commit_record()
            i += 1
            if i < n and text[i] == "\n":
                i += 1
            continue
        if ch == "\n":
            if cell_started or fields:
                _commit_cell()
                _commit_record()
            i += 1
            continue
        cell.append(ch)
        cell_started = True
        i += 1

    # Flush any trailing cell / record. The ``cell_started`` guard
    # keeps us from emitting a phantom row when the document ends
    # with a newline.
    if cell_started or fields:
        _commit_cell()
        _commit_record()

    return records


__all__ = ["dump", "load"]
