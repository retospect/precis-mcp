"""Context and transport budgets for complete agent-facing results.

Over-cap results return a **fitted body** — the head that fits cut at a
line boundary, then an outline of what remains and a footer naming the
exact targeted read for it. There is still no pagination and no cursor:
search, section selectors and targeted views are the
context-management surface.

Policy, once, in :func:`fit_result_body` — classified by the dispatch
chokepoint (:func:`read_shape`):

Default: every read comes back whole up to ``PRECIS_MAX_BODY_BYTES``
(24576); size is a logged meter (``tool_calls.result_bytes``), never a
refusal. The context cap ``PRECIS_TOOL_RESULT_CONTEXT_BYTES`` is opt-in
(unset or 0 = off); when set it binds the open-ended shapes below.

* **explicit** — the caller named exactly what it wanted (a ``~``
  selector, a ``/<view>`` id suffix, a list of ids). Skips the context
  cap; only ``PRECIS_MAX_BODY_BYTES`` bounds it, and over that the
  leading lines fit plus a ``rest:``/``next:`` line naming what to ask
  for next.
* **extent** — a fisheye render: the node block first, then ring rows
  in rank order until the cap; dropped rows are named by handle.
* **list** — a search / list view: the top rows that fit, then ``N
  more matches not shown; narrow q or lower page_size``.
* **single** — a whole-ref read: the head, the ref's outline (handler
  ``budget_outline`` when it supplies one, else the body's own
  headings), and ``next:`` the targeted read.

Two ledger signals, both counted per session by
``tool_ledger.sessions_rollup`` (block-navigation slice 2): a **fitted**
result (the dispatch chokepoint sees the body change, and records the
row ``error_type='ResultBudgetFitted'`` on an ``ok`` outcome) and the
last-resort **refusal** — nothing fits, so :func:`fit_result_body`
raises ``ResultBudgetExceeded`` (a ``BadInput``) and the row reads
``error_type='ResultBudgetExceeded'``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from precis.errors import ResultBudgetExceeded

#: Not a default cap any more — the size budget handlers pre-trim to, and
#: the value opt-in context limiting is usually set to.
DEFAULT_CONTEXT_BODY_BYTES = 4096
DEFAULT_MAX_BODY_BYTES = 24576

#: ``id='x/<view>'`` suffixes that count as an explicit part-read (the
#: bounded path-view grammar of skill/paper-style ids). A ``~`` selector
#: is explicit only when its tail matches a selector grammar — a URL id
#: like ``https://host/~user/page`` must not skip the context cap.
_EXPLICIT_PATH_VIEWS: frozenset[str] = frozenset({"toc", "cite", "bib"})

#: Selector tails after ``~``: ``4``, ``4..11``, comma lists, draft
#: ``dc<N>`` cards, file ``L42``/``L42-58`` line reads.
_TILDE_NUMERIC = re.compile(r"\d+(?:\.\.\d+)?(?:,\d+(?:\.\.\d+)?)*")
_TILDE_STRUCT = re.compile(r"dc\d+|L\d+(?:-\d+)?")

#: Kinds whose ``~`` tail may be a section slug (memory ``~<slug>`` /
#: ``~body``, skill/markdown ``~<heading-slug>``).
_TILDE_SLUG_KINDS: frozenset[str] = frozenset(
    {"memory", "skill", "markdown", "md", "file"}
)
_TILDE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def _tilde_is_selector(kind: str | None, ident: str) -> bool:
    """True when the tail after the last ``~`` is a selector, not a URL."""
    sel = ident.rpartition("~")[2].strip()
    if not sel or "/" in sel or "~" in sel:
        return False
    if _TILDE_NUMERIC.fullmatch(sel) or _TILDE_STRUCT.fullmatch(sel):
        return True
    if kind in _TILDE_SLUG_KINDS:
        return bool(_TILDE_SLUG.fullmatch(sel))
    return False


#: Universal handles look like ``pa259454`` / ``me474312`` — two lower-
#: case letters plus digits. Used to name dropped extent rows.
_HANDLE_RE = re.compile(r"\b[a-z]{2}\d{2,}\b")

#: Section-heading lines the generic outline fallback extracts.
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)

#: Hard cap on outline lines appended to a truncated read — an outline
#: longer than this is itself truncated with a ``…`` marker.
_OUTLINE_LINE_CAP = 40

#: One outline line never exceeds this many characters — a heading that
#: is itself paragraphs long (quest titles are prose) must not blow the
#: trailer budget.
_OUTLINE_LINE_CHARS = 160


def _cap_line(ln: str) -> str:
    return ln if len(ln) <= _OUTLINE_LINE_CHARS else ln[:_OUTLINE_LINE_CHARS] + "…"


def body_bytes(text: str) -> int:
    """UTF-8 byte size — the unit both caps count in."""
    return len(text.encode("utf-8"))


def size_cell(n_bytes: int) -> str:
    """Outline/toc size label: ``<n>kB`` at ≥ 1 kB, ``""`` below.

    Sub-kB rows get no size — the number only earns its column when it
    steers the choice (same kB unit the caps and the truncated marker
    report in). Round-to-nearest like ``round()``; a 0-byte part still
    prints nothing.
    """
    if n_bytes < 1024:
        return ""
    return f"{max(1, round(n_bytes / 1024))}kB"


def read_shape(verb: str, args: dict[str, Any]) -> str:
    """Classify a verb call into the fit policy's four read shapes.

    Runs on the *incoming* args — before ``_invoke_handler`` pops
    ``extent=``/``ring=`` — so the classifier sees the caller's spelling.
    """
    if verb == "search":
        return "list"
    if verb != "get":
        return "single"
    idv = args.get("id")
    view = args.get("view")
    extras = args.get("__extras__")
    extent = args.get("extent")
    if extent is None and isinstance(extras, dict):
        extent = extras.get("extent")
    if extent is not None:
        return "extent"
    if view is not None:
        from precis.handlers._eye import is_eye_view

        if is_eye_view(str(view)):
            return "extent"
        # Any other named view is a whole-view read — the caller asked
        # for a view, not a bounded part of one. Over cap it gets the
        # single-ref treatment: head + the view's own targeted hint.
        return "single"
    if isinstance(idv, list):
        return "explicit"
    if isinstance(idv, str):
        s = idv.strip()
        if s.startswith("/"):
            return "list"
        if "~" in s and _tilde_is_selector(args.get("kind"), s):
            return "explicit"
        tail = s.rpartition("/")[2]
        if "/" in s and tail in _EXPLICIT_PATH_VIEWS:
            return "explicit"
        return "single"
    if args.get("q") is not None:
        return "list"
    if idv is None:
        return "list"
    return "single"


def _env_cap(name: str, default: int, *, zero_disables: bool = False) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return 0 if zero_disables else default
    return value


def result_context_cap() -> int:
    """Byte budget a handler fits a structured render to (gr540851).

    ``PRECIS_TOOL_RESULT_CONTEXT_BYTES`` when an operator sets it to a
    positive value, else the transport cap — size under that is never
    cut. Handlers that fit by structure (draft level map) read this
    instead of :data:`DEFAULT_CONTEXT_BODY_BYTES`.
    """
    explicit = _env_cap("PRECIS_TOOL_RESULT_CONTEXT_BYTES", 0, zero_disables=True)
    return explicit or _env_cap("PRECIS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)


def _fit_head(body: str, byte_cap: int) -> tuple[str, list[str]]:
    """Largest leading run of whole lines that fits ``byte_cap``.

    Returns ``(head, dropped_lines)``. A first line that alone exceeds
    the cap is cut at the byte boundary — the only mid-line cut —
    so a head always exists when ``byte_cap`` is positive.
    """
    lines = body.split("\n")
    used = 0
    kept: list[str] = []
    for line in lines:
        cost = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + cost > byte_cap and kept:
            break
        if used + cost > byte_cap and not kept:
            # One huge first line: cut it at the byte boundary — but a
            # non-positive budget yields no head at all (the refusal case).
            # Reserve the ellipsis's 3 bytes so head + tail stays ≤ cap.
            raw = line.encode("utf-8")[: max(0, byte_cap - 3)]
            if raw:
                kept.append(raw.decode("utf-8", errors="ignore").rstrip() + "…")
            break
        kept.append(line)
        used += cost
    dropped = lines[len(kept) :] if len(kept) < len(lines) else []
    return "\n".join(kept), dropped


def _heading_outline(body: str) -> str:
    """Generic outline: the body's own ``#``-headings, each with the
    byte size of the section it opens — the shape a kind with no
    selector grammar still exposes."""
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return ""
    lines = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sz = size_cell(len(body[m.start() : end].encode("utf-8")))
        lines.append(_cap_line(f"{m.group(1)}\t{sz}" if sz else m.group(1)))
    return "\n".join(lines)


def _cap_outline(outline: str) -> str:
    lines = [_cap_line(ln) for ln in outline.split("\n") if ln.strip()]
    if len(lines) > _OUTLINE_LINE_CAP:
        kept = lines[:_OUTLINE_LINE_CAP]
        kept.append(f"… {len(lines) - _OUTLINE_LINE_CAP} more")
        return "\n".join(kept)
    return "\n".join(lines)


def fit_result_body(
    body: str,
    *,
    verb: str,
    kind: str | None,
    context_limited: bool,
    targeted_hint: str | None,
    read: str = "single",
    outline: str | Callable[[], str | None] | None = None,
) -> str:
    """Fit ``body`` to the budgets that bind this read; return the result.

    Never raises for size alone unless nothing fits — ``BadInput`` is
    the transport cap's last resort (a sub-line head under
    ``PRECIS_MAX_BODY_BYTES`` is a refusal only because there is no
    honest head to show).
    """
    size = len(body.encode("utf-8"))
    # Unset = no context cap: the transport cap is the only default
    # bound (gr540851 — size is a meter, not a refusal). An explicit
    # positive PRECIS_TOOL_RESULT_CONTEXT_BYTES still binds; 0 or unset
    # or unparseable means off.
    context_cap = (
        _env_cap("PRECIS_TOOL_RESULT_CONTEXT_BYTES", 0, zero_disables=True)
        if context_limited
        else 0
    )
    frame_cap = _env_cap("PRECIS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    if read == "explicit":
        cap = frame_cap
        cap_name = f"PRECIS_MAX_BODY_BYTES ({frame_cap} bytes)"
    elif context_cap and size > context_cap:
        cap = context_cap
        cap_name = f"PRECIS_TOOL_RESULT_CONTEXT_BYTES ({context_cap} bytes)"
    elif size > frame_cap:
        cap = frame_cap
        cap_name = f"PRECIS_MAX_BODY_BYTES ({frame_cap} bytes)"
    else:
        return body
    if size <= cap:
        return body

    # Compose the trailer first so the head budget accounts for it.
    outline_block = ""
    if read == "single":
        # Handlers may pass a thunk — the outline's body load only pays
        # when the cap actually binds (reached here only when size > cap).
        text_src = outline() if callable(outline) else outline
        text = _cap_outline(text_src) if text_src else _heading_outline(body)
        if text:
            # Bound the map so the trailer can't crowd out the head —
            # beyond half the cap it stops paying.
            block_lines: list[str] = []
            block_used = 0
            for ln in text.split("\n"):
                cost = len(ln.encode("utf-8")) + 3
                if block_used + cost > cap // 2:
                    if block_lines:
                        block_lines.append("  …")
                        break
                    continue  # a single oversized line just isn't listed
                block_lines.append(f"  {ln}")
                block_used += cost
            if block_lines:
                outline_block = "\n\noutline:\n" + "\n".join(block_lines)

    def _next_call() -> str:
        return targeted_hint or (
            f"search(kind={kind!r}, q='<more specific terms>', page_size=5)"
            if kind
            else "search(q='<more specific terms>', page_size=5)"
        )

    marker = "\n\n… truncated: showing {shown} of {total} bytes"
    if read == "list":
        # Dropped lines aren't all result rows (a rendered list ends in
        # trailer lines) and row boundaries aren't reliably delimitable —
        # the honest count is lines.
        tail_fmt = marker + "\n{more} more lines not shown; narrow q or lower page_size"
    elif read == "extent":
        tail_fmt = (
            marker + "\ndropped {more} rows: {dropped} — read one: get(id='{first}')"
        )
    elif read == "explicit":
        tail_fmt = (
            f"\n\n… truncated: showing {{shown}} of {{total}} bytes; cap {cap_name}\n"
            "rest: {next_call}"
        )
    else:
        tail_fmt = marker + "{outline}\nnext: {next_call}"

    # Iterative fit: the marker reports the shown count, which itself
    # has a byte width — converge twice; widths settle on pass one.
    head = ""
    dropped: list[str] = []
    shown = 0
    for _ in range(3):
        tail = tail_fmt.format(
            shown=shown,
            total=size,
            outline=outline_block,
            next_call=_next_call(),
            more=len([ln for ln in dropped if ln.strip()]),
            dropped=_name_dropped(dropped)[0],
            first=_name_dropped(dropped)[1],
        )
        head, dropped = _fit_head(body, max(0, cap - len(tail.encode("utf-8"))))
        shown = len(head.encode("utf-8"))
    if not shown:
        raise ResultBudgetExceeded(
            f"{verb}: result is {size} bytes and even a head will not fit "
            f"under {cap_name}",
            next=_next_call(),
        )
    names, first = _name_dropped(dropped)
    tail = tail_fmt.format(
        shown=shown,
        total=size,
        outline=outline_block,
        next_call=_next_call(),
        more=len([ln for ln in dropped if ln.strip()]),
        dropped=names,
        first=first,
    )
    if len((head + tail).encode("utf-8")) > cap:
        # ``shown`` gained a digit after the head was cut — re-cut once
        # against the final trailer width (can only shrink, so it settles).
        head, dropped = _fit_head(body, max(0, cap - len(tail.encode("utf-8"))))
        shown = len(head.encode("utf-8"))
        if not shown:
            raise ResultBudgetExceeded(
                f"{verb}: result is {size} bytes and even a head will not fit "
                f"under {cap_name}",
                next=_next_call(),
            )
        names, first = _name_dropped(dropped)
        tail = tail_fmt.format(
            shown=shown,
            total=size,
            outline=outline_block,
            next_call=_next_call(),
            more=len([ln for ln in dropped if ln.strip()]),
            dropped=names,
            first=first,
        )
    return head + tail


def _name_dropped(dropped: list[str]) -> tuple[str, str]:
    """``(rendered names, first handle)`` for an extent footer — the
    universal handles inside the rows that didn't fit."""
    seen: list[str] = []
    for ln in dropped:
        for h in _HANDLE_RE.findall(ln):
            if h not in seen:
                seen.append(h)
    if not seen:
        return "…", "…"
    names = ", ".join(seen[:8]) + (" …" if len(seen) > 8 else "")
    return names, seen[0]


__all__ = [
    "DEFAULT_CONTEXT_BODY_BYTES",
    "DEFAULT_MAX_BODY_BYTES",
    "body_bytes",
    "fit_result_body",
    "read_shape",
    "result_context_cap",
    "size_cell",
]
