"""Skill write-fence filter — :func:`render` + :func:`is_write_call`.

gr512233 slice 1 (``docs/backlog/read-scope-skill-fencing.md``): one
source file per skill, marked with write fences, filtered at serve time
by the connection scope (:func:`precis.runtime.scope.current_scope`).

Markers — authoring rule: mark the section, not the line, unless a read
section holds one stray write line.

- **Whole skill**: front-matter ``scope: write``
  (``SkillFrontmatter.scope``). The handler serves a stub; ``render``
  never sees the file.
- **Section**: a trailing `` {write}`` on an H2/H3 heading drops the
  body up to the next heading of the same or higher level (a deeper
  subsection dies with its marked parent). The heading lines stay —
  ``~N`` chunk addresses minted in write scope must resolve to the
  same section in read scope, so the slot keeps its heading and the
  dropped body becomes the pointer (round-2 review). On a
  ``chunk_by_h2`` alias group — stacked same-level headings separated
  only by blank lines — a marker on any heading marks the whole group.
- **Lines inside a read section**: an own-line ``<!-- write -->`` /
  ``<!-- /write -->`` pair. Unbalanced or nested pairs raise
  ``ValueError`` — the slice-5 ratchet gates them out of the corpus, so
  a malformed marker must fail loudly rather than silently half-serve.

Markers are stripped on serve in BOTH scopes: a write-scope serve of a
marked file is byte-identical to the file before it was marked. A
read-scope serve replaces each dropped run with one pointer line
(:data:`_POINTER`) naming the cut.

Exempt, never marked: the create-only ``put(kind='gripe', text=...)``
with no ``id=``/``link=``/``rel=``/``tags=``/``prio=``/``new=``/
``observation=`` — the write:none channel takes ``text=`` only
(``runtime.dispatch._ro_gripe_create``, ``handlers/gripe.py``).
``is_write_call`` encodes the same exemption for the slice-2 surfaces
(``Next:`` rows, hint bus, ``next=``).

Lines inside a ``` code fence are never headings — a ``# comment`` in
a ```python block is code, not an H1 that ends a section or names a
pointer. Marker lines DO still count inside a code fence (a stray
fenced line inside an example block drops as authored); the slice-5
lint is the guard against that authoring error.
"""

from __future__ import annotations

import re

from precis.runtime.scope import Scope

#: H2/H3 heading, capture = (hashes, text).
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*?)\s*$")

#: Any ATX heading — a ``#``/``####`` line is not fenceable itself but
#: still terminates (H1) or sits inside (H4+) a dropped section.
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s")

#: Heading carrying the trailing ``{write}`` section marker.
_HEADING_MARK_RE = re.compile(r"^(#{2,3})\s+(.*?)\s*\{write\}\s*$")

_MARK_TAIL_RE = re.compile(r"\s*\{write\}\s*$")

_OPEN = "<!-- write -->"
_CLOSE = "<!-- /write -->"

#: Call-shaped mutating-verb lead: ``put(``, ``edit(``, ``delete(``,
#: ``tag(``, ``link(``.
_VERB_LEAD_RE = re.compile(r"\b(?:put|edit|delete|tag|link)\s*\(")

#: ``put( kind='gripe'`` / ``put( kind="gripe"`` — the verb + kind of the
#: read-only filing carve-out. ``\s*`` spans the multi-line call shape
#: skills use.
_GRIPTE_KIND_RE = re.compile(r"\bput\s*\(\s*kind\s*=\s*['\"]gripe['\"]")

#: Kwargs that make a gripe ``put`` more than the ``text=``-only create
#: the write:none channel accepts (``handlers/gripe.py``'s refused list).
_WRITE_SHAPING_ARG_RE = re.compile(
    r"\b(?:id|link|unlink|rel|tags|untags|prio|new|observation)\s*="
)


def _pointer(heading: str) -> str:
    """One line per dropped run — names what was cut so the reader can
    ask a write-capable caller instead of guessing the recipe doesn't
    exist."""
    return (
        "(write recipe omitted: this connection is read-only. "
        f'"{heading}" needs a write connection; to report a problem: '
        "put(kind='gripe', text='...'))"
    )


def is_write_call(call: str) -> bool:
    """True when ``call`` leads with a mutating verb.

    The create-only gripe filing (``put(kind='gripe', text=...)`` with no
    write-shaping kwarg) is the read-only carve-out — ``False`` here, so
    the slice-2 ``Next:``/hint/``next=`` filters keep it visible.
    """
    if not _VERB_LEAD_RE.search(call):
        return False
    if _GRIPTE_KIND_RE.search(call) and not _WRITE_SHAPING_ARG_RE.search(call):
        return False
    return True


def _heading_text(line: str) -> str:
    """Heading text without ``##`` or a trailing ``{write}``."""
    m = _HEADING_MARK_RE.match(line) or _HEADING_RE.match(line)
    if m:
        return m.group(2).strip()
    return line.lstrip("#").strip()


def strip_heading_mark(line: str) -> str:
    """Remove a trailing `` {write}`` from a heading line, preserving
    the rest of the line byte-for-byte."""
    if _HEADING_MARK_RE.match(line):
        return _MARK_TAIL_RE.sub("", line)
    return line


def _scan(lines: list[str], *, drop: bool) -> tuple[list[str], list[str]]:
    """Single pass over ``lines``; returns ``(kept, dropped_headings)``.

    ``drop=True`` removes marked content and emits pointer lines;
    ``drop=False`` keeps content and strips markers only.

    Lines inside a ```` ``` ```` code fence are never headings — a
    ``# comment`` in a ```python block is code, and treating it as an
    H1 would end a dropped section early (the section's remaining
    write lines would leak into a read serve) or hijack the pointer's
    heading name. Marker lines still count inside code fences, so a
    stray fenced line inside an example block drops as authored.
    """
    out: list[str] = []
    dropped: list[str] = []
    in_fence = False
    last_heading = ""
    n = len(lines)
    in_code = [False] * n
    inside = False
    for k, code_line in enumerate(lines):
        if code_line.strip().startswith("```"):
            inside = not inside
        else:
            in_code[k] = inside
    i = 0
    while i < n:
        line = lines[i]
        s = line.strip()
        if s == _OPEN:
            if in_fence:
                raise ValueError("nested <!-- write --> marker")
            in_fence = True
            if drop:
                name = last_heading or "(intro)"
                out.append(_pointer(name))
                dropped.append(name)
            i += 1
            continue
        if s == _CLOSE:
            if not in_fence:
                raise ValueError("<!-- /write --> without <!-- write -->")
            in_fence = False
            i += 1
            continue
        if in_fence:
            if not drop:
                out.append(strip_heading_mark(line))
            i += 1
            continue
        m = None if in_code[i] else _HEADING_RE.match(line)
        if m is None:
            out.append(line)
            if not in_code[i] and _ANY_HEADING_RE.match(line):
                last_heading = _heading_text(line)
            i += 1
            continue
        # A heading line. Collect its alias group: consecutive headings
        # of the SAME level with only blank lines between (the
        # ``chunk_by_h2`` stack). Blanks between the headings stay inside
        # the emitted range, so write scope is byte-identical.
        level = len(m.group(1))
        stack: list[int] = []
        j = i
        while j < n:
            sj = lines[j].strip()
            if not sj:
                j += 1
                continue
            mj = None if in_code[j] else _HEADING_RE.match(lines[j])
            if mj is not None and len(mj.group(1)) == level:
                stack.append(j)
                j += 1
                continue
            break
        if drop and any(_HEADING_MARK_RE.match(lines[k]) for k in stack):
            heading = _heading_text(lines[stack[0]])
            # The slot stays: emit the group's heading lines (stripped)
            # so ``slug~N`` addresses the same section in both scopes —
            # only the dropped body is replaced by the pointer.
            for k in range(stack[0], stack[-1] + 1):
                out.append(strip_heading_mark(lines[k]))
            out.append("")
            out.append(_pointer(heading))
            out.append("")
            dropped.append(heading)
            last_heading = _heading_text(lines[stack[-1]])
            # Drop to the next heading of the same or higher level —
            # deeper headings inside the section are content and go too.
            k = stack[-1] + 1
            while k < n:
                mk = None if in_code[k] else _ANY_HEADING_RE.match(lines[k])
                if mk is not None and len(mk.group(1)) <= level:
                    break
                k += 1
            i = k
            continue
        for k in range(stack[0], stack[-1] + 1):
            out.append(strip_heading_mark(lines[k]))
        last_heading = _heading_text(lines[stack[-1]])
        i = stack[-1] + 1
    if in_fence:
        raise ValueError("unclosed <!-- write --> marker")
    return out, dropped


def render(text: str, scope: Scope) -> tuple[str, list[str]]:
    """Filter skill ``text`` for ``scope`` → ``(body, dropped_headings)``.

    ``write`` scope strips markers only — the result is byte-identical
    to the file before marking. ``read`` scope drops each marked run
    and leaves one pointer line in its place; a dropped *section* keeps
    its heading lines so ``slug~N`` addresses stay stable across
    scopes. ``dropped_headings`` names each cut (the marked section's
    first heading, or the enclosing heading for an inline fence —
    ``"(intro)"`` before the first heading).
    """
    out, dropped = _scan(text.split("\n"), drop=scope == "read")
    return "\n".join(out), dropped
