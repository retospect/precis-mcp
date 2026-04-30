"""Anchored edit resolver — pure, no I/O.

Implements the ``mode='edit'`` / ``mode='insert'`` primitives shared
across every R/W file kind. The resolution algorithm is the core of
``docs/edit-protocol-spec.md``: content selects, anchors disambiguate,
``match`` policy validates uniqueness.

Three responsibilities:

1. :class:`EditOp` — typed spec for one edit (literal find + optional
   anchors + match policy).
2. :func:`find_candidates` — locate every occurrence of ``find`` in a
   buffer, filter by anchors, return ranges with line numbers.
3. :func:`apply_edit` — resolve candidates against the match policy
   and return the spliced buffer plus the line ranges that changed.

This module is **pure**: it only operates on strings. Atomic writes,
re-ingest, AST gates, and ruff live in the per-kind handler. The
handler builds an :class:`EditOp` from the agent's call, hands it +
the buffer to :func:`apply_edit`, then runs its own validation +
write pipeline.

Minimal v1 scope (per spec § Implementation order):

- Literal ``find`` only — no regex.
- Single edit per call — no ``edits=[...]`` batch.
- No ``dry_run`` — handler may add it as a per-handler kwarg.
- No ``expect_lines`` assertion.

These are deliberately deferred to keep the v1 surface small. The
schema and error shapes leave room for them without breaking changes.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Literal

from precis.errors import BadInput

MatchPolicy = Literal["unique", "first", "all", "nth"]
InsertWhere = Literal["before", "after"]


__all__ = [
    "DRY_RUN_MODES",
    "EditCandidate",
    "EditOp",
    "EditResult",
    "apply_edit",
    "classify_diff_hunks",
    "find_candidates",
    "format_unified_diff",
    "normalize_dry_run",
    "render_dry_run_full",
    "render_dry_run_header",
    "select_candidates",
]


#: Recognised values for ``dry_run`` on ``put``. The bool ``True``
#: is normalised to ``"diff"`` so callers don't have to pick a
#: string. ``"full"`` returns the post-edit region(s) verbatim
#: instead of a diff; useful when the agent wants to see the
#: result in its natural form.
DRY_RUN_MODES = ("diff", "full")


# ---------------------------------------------------------------------------
# EditOp — typed spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditOp:
    """One edit operation against a buffer.

    Args:
        op: ``"edit"`` (replace ``find`` with ``text``) or
            ``"insert"`` (insert ``text`` immediately ``before`` /
            ``after`` the matched ``find``, leaving ``find`` itself
            untouched).
        find: Exact literal text to locate. Required.
        text: Replacement text (for ``"edit"``) or text to insert
            (for ``"insert"``). May be empty for ``"edit"`` to delete
            the matched text. Required.
        before: Optional anchor — the bytes immediately preceding a
            candidate match must equal this string. Default ``""``
            (no anchor).
        after: Optional anchor — the bytes immediately following a
            candidate match must equal this string. Default ``""``
            (no anchor).
        where: For ``op='insert'`` only. ``"before"`` puts ``text``
            in front of the matched span; ``"after"`` puts it behind.
        match: How many candidate matches the call requires.
            ``"unique"`` (default) — exactly one. ``"first"`` — take
            the earliest. ``"all"`` — replace every candidate.
            ``"nth"`` — pick the Nth (1-indexed) candidate; requires
            ``nth=``.
        nth: 1-indexed pick when ``match='nth'``.
        region_label: Caller-supplied human label for the search
            region (e.g. ``"notes/foo.md~intro"``). Used in error
            messages so the agent knows where the search ran.
        base_line: 1-indexed line number in the *outer* file that the
            buffer's line 1 corresponds to. Lets error messages cite
            absolute file lines when the resolver was given a sub-
            region. Defaults to 1 (buffer is the whole file).
    """

    op: Literal["edit", "insert"]
    find: str
    text: str
    before: str = ""
    after: str = ""
    where: InsertWhere | None = None
    match: MatchPolicy = "unique"
    nth: int | None = None
    region_label: str = ""
    base_line: int = 1

    def __post_init__(self) -> None:
        # We're frozen so we have to validate via object.__setattr__ -
        # but we don't actually mutate, just check.
        if self.op not in ("edit", "insert"):
            raise BadInput(
                f"unknown edit op {self.op!r}",
                options=["edit", "insert"],
                next="mode='edit' or mode='insert'",
            )
        if not self.find:
            raise BadInput(
                "find= is required and must be non-empty",
                next="add find='<exact text to locate>'",
            )
        if self.match not in ("unique", "first", "all", "nth"):
            raise BadInput(
                f"unknown match policy {self.match!r}",
                options=["unique", "first", "all", "nth"],
                next="match='unique' (default), 'first', 'all', or 'nth'",
            )
        if self.match == "nth":
            if self.nth is None or self.nth < 1:
                raise BadInput(
                    f"match='nth' requires nth= as a positive int; got {self.nth!r}",
                    next="nth=1 selects the first candidate",
                )
        elif self.nth is not None:
            raise BadInput(
                f"nth= is only valid with match='nth' (got match={self.match!r})",
                next="drop nth= or set match='nth'",
            )
        if self.op == "insert":
            if self.where not in ("before", "after"):
                raise BadInput(
                    "mode='insert' requires where='before' or where='after'",
                    options=["before", "after"],
                    next="add where='before' or where='after'",
                )
            if self.match == "all":
                # Inserting at every match is rarely what the caller
                # actually wants; force them to be explicit with a
                # single-target policy. Unblock by passing nth=
                # iteratively or first=.
                raise BadInput(
                    "mode='insert' does not allow match='all' "
                    "(would insert at every occurrence)",
                    next="use match='unique' (default) or match='nth' with nth=N",
                )
        else:  # op == "edit"
            if self.where is not None:
                raise BadInput(
                    f"where= is only valid for mode='insert' (got mode={self.op!r})",
                    next="drop where=",
                )


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditCandidate:
    """One concrete location where ``find`` matched (after anchor filter).

    Byte offsets are into the *buffer the resolver was given*. Line
    numbers are absolute to the outer file when the caller supplied
    ``base_line`` on the EditOp; otherwise they are 1-indexed within
    the buffer.
    """

    start: int  # byte offset of the matched span (inclusive)
    end: int  # byte offset just past the matched span (exclusive)
    line_no: int  # 1-indexed line of the start
    line_text: str  # the line itself (for error context)


@dataclass(frozen=True, slots=True)
class EditResult:
    """Outcome of a successful edit.

    Args:
        new_buffer: The post-edit buffer. May equal the input if the
            edit was effectively a no-op (rare; we error on no-op).
        edited_spans: One ``(start_line, end_line)`` tuple per
            candidate that was changed. Line numbers are absolute
            (i.e. include ``op.base_line``). For ``mode='edit'`` the
            range is the matched span; for ``mode='insert'`` the
            range is the inserted text's lines.
        n_matches: How many candidates were considered (after anchor
            filter, before match policy collapsed them).
    """

    new_buffer: str
    edited_spans: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    n_matches: int = 0


# ---------------------------------------------------------------------------
# Candidate finding
# ---------------------------------------------------------------------------


def _line_starts(buffer: str) -> list[int]:
    """Byte offsets of the first character of every line.

    Always begins with 0 (line 1's offset). Length equals the number
    of lines in the buffer (counting a final trailing newline as
    closing the last line, not opening a new empty one).
    """
    starts = [0]
    for i, ch in enumerate(buffer):
        if ch == "\n":
            starts.append(i + 1)
    # If the buffer ends in '\n', the trailing offset points past the
    # end and there's no real line N+1; trim it.
    if starts and starts[-1] == len(buffer) and len(starts) > 1:
        starts.pop()
    return starts


def _line_of(buffer: str, offset: int, line_starts: list[int]) -> tuple[int, str]:
    """Return ``(1-indexed line number, line text)`` for a byte offset.

    The line text excludes the trailing newline.
    """
    # Binary search for the largest line_start <= offset.
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    line_idx = lo
    line_start = line_starts[line_idx]
    nl = buffer.find("\n", line_start)
    line_end = nl if nl != -1 else len(buffer)
    return line_idx + 1, buffer[line_start:line_end]


def find_candidates(buffer: str, op: EditOp) -> list[EditCandidate]:
    """Return every literal+anchor-filtered candidate for ``op.find``.

    No match-policy collapse is applied — that's :func:`select_candidates`.
    """
    candidates: list[EditCandidate] = []
    if not buffer:
        return candidates

    line_starts = _line_starts(buffer)

    start = 0
    flen = len(op.find)
    blen = len(op.before)
    alen = len(op.after)
    while True:
        idx = buffer.find(op.find, start)
        if idx == -1:
            break
        end = idx + flen

        # Anchor filter: the bytes immediately before/after must match
        # the supplied anchors verbatim. Empty anchor = no constraint.
        if blen and (idx < blen or buffer[idx - blen : idx] != op.before):
            start = idx + 1
            continue
        if alen and (end + alen > len(buffer) or buffer[end : end + alen] != op.after):
            start = idx + 1
            continue

        line_no_local, line_text = _line_of(buffer, idx, line_starts)
        line_no_abs = line_no_local + (op.base_line - 1)
        candidates.append(
            EditCandidate(
                start=idx,
                end=end,
                line_no=line_no_abs,
                line_text=line_text,
            )
        )
        # Advance past this match's start so overlapping matches are
        # still seen (e.g. find='aa' in 'aaaa' yields 3 matches, not 2).
        start = idx + 1

    return candidates


# ---------------------------------------------------------------------------
# Match-policy selection (with sharp errors)
# ---------------------------------------------------------------------------


def _format_candidate(c: EditCandidate, max_ctx: int = 80) -> str:
    """Render one candidate as a single line for error messages."""
    text = c.line_text
    if len(text) > max_ctx:
        # Truncate with ellipsis but try to keep the match visible.
        text = text[: max_ctx - 1].rstrip() + "…"
    return f"  L{c.line_no}  {text}"


def _nearest_lines(
    buffer: str, find: str, *, base_line: int, k: int = 3
) -> list[tuple[int, str, float]]:
    """Best-effort fuzzy: find lines that look like ``find``.

    Returns up to ``k`` ``(line_no, line_text, ratio)`` triples sorted
    by similarity. Used only for the "not found" error footer to give
    the agent something concrete to fix.

    Strategy: for each line, compute the best :class:`difflib.SequenceMatcher`
    ratio between ``find`` and any same-length-window substring of the
    line. This surfaces typos (``dpoamine`` → ``dopamine``) without
    being penalised by surrounding context the line carries.
    """
    lines = buffer.splitlines()
    if not lines:
        return []
    flen = len(find)
    scored: list[tuple[float, int, str]] = []
    # Cap at 2000 lines for big files; that's already plenty of context.
    for i, line in enumerate(lines[:2000]):
        if not line.strip():
            continue
        # Tokens are the natural unit: try the line itself, plus each
        # whitespace-separated word, plus same-length sliding windows.
        candidates = [line] + line.split()
        if len(line) >= flen and len(line) - flen <= 200:
            # Bound the sliding window to keep this O(N) per line.
            step = max(1, (len(line) - flen) // 50 + 1)
            for j in range(0, len(line) - flen + 1, step):
                candidates.append(line[j : j + flen])
        best = max(difflib.SequenceMatcher(None, find, c).ratio() for c in candidates)
        if best >= 0.55:
            scored.append((best, i + base_line, line))
    scored.sort(reverse=True)
    return [(ln, txt, r) for r, ln, txt in scored[:k]]


def select_candidates(
    candidates: list[EditCandidate],
    op: EditOp,
    *,
    buffer: str,
) -> list[EditCandidate]:
    """Apply the match policy. Raises :class:`BadInput` on any policy
    violation, including:

    - zero candidates (with up to 3 fuzzy nearest lines),
    - ``match='unique'`` with ≥2 candidates (lists every candidate),
    - ``match='nth'`` with ``nth`` out of range.

    The returned list is what the splice step should mutate.
    """
    region = op.region_label or "the buffer"
    if not candidates:
        nearest = _nearest_lines(buffer, op.find, base_line=op.base_line)
        hint_lines: list[str] = []
        if nearest:
            hint_lines.append("Nearest matches in the region:")
            for ln, text, ratio in nearest:
                preview = text if len(text) <= 80 else text[:77].rstrip() + "…"
                hint_lines.append(f"  L{ln}  {preview}  ({ratio:.0%} similar)")
        suggestion = "widen id= to a larger region, or copy the exact text from get(... view='raw')"
        body = f"find={op.find!r} not found in {region}"
        if hint_lines:
            body += "\n" + "\n".join(hint_lines)
        raise BadInput(body, next=suggestion)

    if op.match == "unique":
        if len(candidates) > 1:
            listing = "\n".join(_format_candidate(c) for c in candidates[:10])
            extra = (
                f"\n  …and {len(candidates) - 10} more" if len(candidates) > 10 else ""
            )
            raise BadInput(
                f"find={op.find!r} has {len(candidates)} matches in {region} "
                f"(match='unique' requires exactly 1):\n{listing}{extra}",
                next=(
                    "narrow with before='...' / after='...', "
                    "or pick a policy: match='all' / match='nth' with nth=N"
                ),
            )
        return candidates

    if op.match == "first":
        return [candidates[0]]

    if op.match == "all":
        return list(candidates)

    # match == "nth"
    assert op.nth is not None  # validated in EditOp.__post_init__
    if op.nth > len(candidates):
        raise BadInput(
            f"match='nth' nth={op.nth} but only {len(candidates)} "
            f"match{'es' if len(candidates) != 1 else ''} for {op.find!r} in {region}",
            next=f"reduce nth (1..{len(candidates)}) or use match='all'",
        )
    return [candidates[op.nth - 1]]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_edit(buffer: str, op: EditOp) -> EditResult:
    """Resolve ``op`` against ``buffer`` and return the spliced result.

    Steps:
      1. Validate the EditOp shape (already done at construction).
      2. Find candidate spans (literal find + anchor filter).
      3. Apply the match policy (errors on ambiguous / not found).
      4. Splice every chosen candidate, end-to-start so byte offsets
         remain valid as we mutate.

    Raises :class:`BadInput` on any policy or input violation. Pure —
    does not touch disk.
    """
    candidates = find_candidates(buffer, op)
    chosen = select_candidates(candidates, op, buffer=buffer)

    # Splice end-to-start so earlier offsets don't shift.
    new_buffer = buffer
    edited_spans: list[tuple[int, int]] = []
    for c in sorted(chosen, key=lambda x: x.start, reverse=True):
        if op.op == "edit":
            new_buffer = new_buffer[: c.start] + op.text + new_buffer[c.end :]
            # Compute the post-edit line span. The edit text replaces
            # the matched span; figure out how many lines that adds.
            lines_added = op.text.count("\n")
            line_start_abs = c.line_no
            line_end_abs = line_start_abs + lines_added
            edited_spans.append((line_start_abs, line_end_abs))
        else:  # insert
            insert_at = c.start if op.where == "before" else c.end
            new_buffer = new_buffer[:insert_at] + op.text + new_buffer[insert_at:]
            # The inserted text may add lines; report the inserted span.
            insert_line_no, _ = _line_of(buffer, insert_at, _line_starts(buffer))
            line_start_abs = insert_line_no + (op.base_line - 1)
            lines_added = op.text.count("\n")
            line_end_abs = line_start_abs + lines_added
            edited_spans.append((line_start_abs, line_end_abs))

    if new_buffer == buffer:
        # The edit was a literal no-op — same bytes in, same bytes out.
        # Treat as an error so callers can't accidentally write a
        # nothing-burger and assume their edit landed.
        raise BadInput(
            f"edit produced no change to {op.region_label or 'the buffer'} "
            f"(find=text=text='{op.text}')",
            next="check that find= and text= are different",
        )

    # Reverse so spans are returned in document order.
    edited_spans.reverse()
    return EditResult(
        new_buffer=new_buffer,
        edited_spans=tuple(edited_spans),
        n_matches=len(candidates),
    )


# ---------------------------------------------------------------------------
# Dry-run rendering — diff and full views
# ---------------------------------------------------------------------------


def normalize_dry_run(value: bool | str | None) -> str | None:
    """Coerce a ``dry_run=`` argument to one of ``DRY_RUN_MODES`` or None.

    - ``False`` / ``None`` → None (no dry-run; do the real write)
    - ``True`` → ``"diff"`` (the default verbose shape)
    - ``"diff"`` / ``"full"`` → as-is
    - Anything else → :class:`BadInput`
    """
    if value is None or value is False:
        return None
    if value is True:
        return "diff"
    if isinstance(value, str) and value in DRY_RUN_MODES:
        return value
    raise BadInput(
        f"dry_run must be bool or one of {list(DRY_RUN_MODES)}; got {value!r}",
        options=list(DRY_RUN_MODES),
        next="dry_run=True (alias for 'diff') or dry_run='full'",
    )


def format_unified_diff(
    pre: str,
    post: str,
    *,
    file_label: str,
    n_context: int = 3,
) -> str:
    """Return a unified diff between ``pre`` and ``post``.

    Standard ``difflib.unified_diff`` output with the ``--- a/<label>``
    / ``+++ b/<label>`` headers most diff viewers and models expect.
    Uses ``keepends=True`` so trailing-newline differences show up
    correctly.

    When ``pre == post`` returns the empty string (no diff is the
    diff). Callers should treat that as a no-op signal.
    """
    diff = difflib.unified_diff(
        pre.splitlines(keepends=True),
        post.splitlines(keepends=True),
        fromfile=f"a/{file_label}",
        tofile=f"b/{file_label}",
        n=n_context,
    )
    out = "".join(diff)
    # `unified_diff` may emit a final no-newline marker which is
    # already standard; nothing to fix up.
    return out


def classify_diff_hunks(
    pre: str,
    post: str,
    edited_spans: tuple[tuple[int, int], ...],
) -> tuple[int, int]:
    """Count diff hunks that overlap vs don't overlap ``edited_spans``.

    Returns ``(within, outside)``. ``within`` are hunks the agent's
    own edit produced; ``outside`` are hunks introduced by a
    downstream pass (e.g. ``ruff check --fix`` removing an unused
    import outside the agent's spans). Used in the dry-run header to
    flag "incidental" formatter changes so the agent isn't surprised.

    Hunks are detected via :func:`difflib.SequenceMatcher`'s
    ``get_opcodes`` over the line tokens — same data underpinning
    :func:`format_unified_diff` but cheaper to interpret.
    """
    pre_lines = pre.splitlines(keepends=True)
    post_lines = post.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(a=pre_lines, b=post_lines, autojunk=False)
    within = 0
    outside = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # Hunk's pre-line span is 1-indexed inclusive.
        # Empty pre-side (insertion) → use the line *before* the insertion as anchor.
        if i1 == i2:
            hunk_start = max(1, i1)
            hunk_end = hunk_start
        else:
            hunk_start = i1 + 1
            hunk_end = i2
        overlaps = any(
            not (hunk_end < span_start or hunk_start > span_end)
            for span_start, span_end in edited_spans
        )
        if overlaps:
            within += 1
        else:
            outside += 1
    return within, outside


def render_dry_run_header(
    *,
    region_label: str,
    edited_spans: tuple[tuple[int, int], ...],
    match_policy: str,
    extras: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Render the standard dry-run header lines.

    ``extras`` are ``(label, value)`` pairs the kind-specific caller
    wants to show — e.g. ``[("ast.parse", "ok"), ("ruff", "1 unrelated change")]``.
    The handler returns these in addition to the universal "spans"
    + the diff/full body that follows.
    """
    n_spans = len(edited_spans)
    span_str = ", ".join(f"L{a}-{b}" if a != b else f"L{a}" for a, b in edited_spans)
    if not span_str:
        span_str = "(none)"
    lines = [
        f"DRY RUN — would edit {region_label}",
        f"spans:        {n_spans}   ({span_str})  match={match_policy!r}",
    ]
    if extras:
        max_label = max(len(label) for label, _ in extras)
        for label, value in extras:
            lines.append(f"{label:<{max_label}}  {value}")
    return lines


def render_dry_run_full(
    post_buffer: str,
    *,
    edited_spans: tuple[tuple[int, int], ...],
    region_label: str,
    n_context: int = 2,
) -> str:
    """Render the post-edit content of every edited span verbatim.

    Each span is shown as a short header (``# <region_label>  L42-L46``)
    followed by ``n_context`` lines of context above and below the
    edited range. Spans are emitted in document order.

    Whole-file edits with no concrete spans fall back to a hint
    pointing the caller at ``view='raw'`` to see the full result.
    """
    if not edited_spans:
        return "(no edited spans — use view='raw' to see the post-edit file)"
    lines = post_buffer.splitlines()
    out: list[str] = []
    for i, (start, end) in enumerate(edited_spans):
        if i > 0:
            out.append("")
        ctx_start = max(1, start - n_context)
        ctx_end = min(len(lines), end + n_context)
        span_label = f"L{start}-L{end}" if start != end else f"L{start}"
        out.append(f"# {region_label}  ({span_label}, post-edit)")
        for ln in range(ctx_start, ctx_end + 1):
            marker = ">" if start <= ln <= end else " "
            out.append(f"{marker} {ln:>4}  {lines[ln - 1]}")
    return "\n".join(out)
