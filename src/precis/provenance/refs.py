"""Workspace-code db references — find, lint, expand (gr514049 layers 2-3).

A workspace script carries ``# <handle> (<why>)`` comments — layer 1
shipped as skill text (829d68a10). This module is the check + the
export expansion the convention promised:

* :func:`find_annotations` — the parser. A ``#`` comment is an
  annotation iff its **first** whitespace token is a universal handle
  (``<2-letter code><digits>``, optional ``~<sel>``); more handle
  tokens may follow, then free text (the gloss). This accepts the
  shipped ``# fi349615 (why)`` form while refusing prose that merely
  mentions a handle (``# see gr513665 for history`` is not an
  annotation — its first token is ``see``). ``# lcsc:C6186`` /
  ``# ds:<slug>`` are not handles either — external references stay
  prose. A token shaped like a handle with an unknown code IS an
  annotation — a typo must be flagged, not silently ignored.
* :func:`lint_references` — resolve every handle; unresolved ones are
  reported with their line, never dropped. ``resolve`` is injected so
  tests stub it; production passes ``Store.resolve_handle``.
* :func:`expand_references` — pure text→text expansion for export
  (``mode='block'`` default: a numbered ``# ==== precis references
  ====`` comment block appended at EOF, lines keep ``# [N]``;
  ``'inline'``: a ``#|`` detail block under each annotated line;
  ``'sidecar'``: like ``block`` plus a ``REFERENCES.md`` text in
  ``ExpandResult.files``). Idempotent — a prior expansion is stripped
  back to handles first, then regenerated — and
  :func:`strip_references` is its inverse. Unresolved handles become
  ``# (unresolved: <handle>)`` — kept in place, never dropped, and
  stripped back to the bare handle on import so a re-export retries.
  *Internal* handles (gripe, todo, memory, … — kinds that mean
  nothing outside this instance) are replaced by ``(internal ref, not
  exported)`` so no internal id leaks; that rewrite is one-way.
* :class:`StoreRefResolver` — the db-backed ``cite(handle)`` building
  the one-line citation + capped verbatim excerpt per kind
  (``docs/backlog/workspace-db-references.md``'s excerpt table).
"""

from __future__ import annotations

import hashlib
import io
import re
import tokenize
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from precis.errors import BadInput
from precis.utils import handle_registry

#: One handle token: 2-letter code + digits, optional ``~<selector>``.
_HANDLE = r"[a-z]{2}\d+(?:~[A-Za-z0-9][A-Za-z0-9_.\-]*)?"
#: The leading handle run of an annotation comment (1+ handle tokens).
#: The lookahead keeps ``pa5x``/``pa5:`` from parsing as ``pa5`` + a
#: one-char gloss — a handle must end at a token boundary.
_RUN_RE = re.compile(rf"^({_HANDLE}(?:\s+{_HANDLE})*)(?=\s|$)")

#: Verbatim excerpt cap per the spec's excerpt table — and the licence
#: guardrail: ``include_excerpts`` defaults to False (citation-only
#: export); verbatim paper/patent/datasheet text opts in explicitly.
EXCERPT_CAP = 400

#: Sentinels bracketing a generated ``block``-mode references section.
_BLOCK_OPEN = "# ==== precis references ===="
_BLOCK_CLOSE = "# ==== /precis references ===="
#: Marker prefix for generated ``inline``-mode detail lines.
_INLINE_MARK = "#|"
_SIDECAR_NAME = "REFERENCES.md"

#: ``[N] <handle> | … | sha256:<digest> | <date>Z`` — the machine-read
#: entry line, shared by the sentinel block (as ``# [N] …``) and the
#: sidecar (as ``- [N] …``).
_ENTRY_RE = re.compile(
    r"\[(\d+)\]\s+([a-z]{2}\d+(?:~\S+)?)\s*\|.*?\bsha256:([0-9a-f]+)"
)
#: The ``(unresolved: <handle>)`` marker — strip restores the handle.
_UNRES_RE = re.compile(r"\(unresolved:\s*([a-z]{2}\d+(?:~\S+)?)\)")
#: The internal-handle placeholder — deliberately NOT a handle shape
#: (never re-parses as an annotation) and not reversible.
_INTERNAL = "(internal ref, not exported)"
#: A handle-shaped token anywhere in a gloss (word-bounded, so ``ch4``
#: inside ``xch4y`` is not one) — scrubbed to ``_INTERNAL`` on export.
_GLOSS_HANDLE_RE = re.compile(rf"(?<![A-Za-z0-9]){_HANDLE}(?![A-Za-z0-9])")

#: Kinds that export a citation + verbatim chunk excerpt (the spec's
#: excerpt table): external-world documents a reader can re-fetch
#: without this db.
_EXCERPT_KINDS = frozenset({"paper", "patent", "datasheet", "edgar", "cfp", "news"})
#: Kinds that export a citation only — objects whose identity a reader
#: can still name; the data itself rides in the design export.
_POINTER_KINDS = frozenset(
    {"structure", "part", "material", "component", "rxn", "make", "cad", "pcb"}
)
#: Everything else is internal: operational/instance-local kinds whose
#: handles must not leave the instance (gripe, todo, memory, quest,
#: draft, markdown, plaintext, tex, plan, message, job, alert,
#: agentlog, fleet, llm, skill, tag, folder, profile, checklist, orcid,
#: measure, figure, mermaid, oracle, concept, taxon, anki, citation,
#: md, python, conv, pres). A kind absent from both sets fails closed —
#: never leaked.


@dataclass(frozen=True)
class Annotation:
    """One ``# <handle>… [gloss]`` annotation comment found in a script."""

    #: 1-based line of the ``#`` comment.
    lineno: int
    #: 0-based column of the ``#`` — where a rewrite splices.
    col: int
    #: The handle tokens, in order (selectors kept: ``pc10~x4km``).
    handles: tuple[str, ...]
    #: Free text after the handle run (a leading ``|`` separator — the
    #: designed-for extension marker — is stripped); ``""`` when none.
    gloss: str
    #: True when code precedes the comment on its line (a trailing
    #: annotation); False for a standalone comment line.
    trailing: bool


@dataclass(frozen=True)
class LintIssue:
    """One annotation handle that did not resolve."""

    lineno: int
    handle: str
    #: ``unresolved`` = resolver returned None; ``selector`` = the base
    #: chunk handle resolved but its ``~<sel>`` did not parse.
    reason: str = "unresolved"


def find_annotations(source: str) -> list[Annotation]:
    """Find annotation comments in Python source.

    Tokenize-based, so ``#`` inside strings/docstrings never counts.
    A comment is an annotation iff its first whitespace token is a
    handle (``[a-z]{2}\\d+[~sel]``); further handle tokens extend the
    run, the rest is gloss. A comment whose first token is anything
    else — prose, ``lcsc:``, ``ds:``, ``[N]``, ``#|`` — is not an
    annotation, so expanded output never re-parses as annotations.
    Raises ``tokenize.TokenizeError``/``IndentationError`` on source
    that does not tokenize — callers vet ``compile()`` first (the
    import op already does).
    """
    lines = source.splitlines()
    out: list[Annotation] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        body = tok.string[1:].strip()
        m = _RUN_RE.match(body)
        if m is None:
            continue
        gloss = body[m.end() :].strip()
        if gloss.startswith("|"):
            gloss = gloss[1:]
        prefix = lines[tok.start[0] - 1][: tok.start[1]]
        out.append(
            Annotation(
                lineno=tok.start[0],
                col=tok.start[1],
                handles=tuple(m.group(1).split()),
                gloss=gloss.strip(),
                trailing=bool(prefix.strip()),
            )
        )
    return out


def _resolve_base(resolve: Any, handle: str) -> tuple[Any, str | None]:
    """Resolve ``handle``, tolerating a free selector on a record handle.

    ``Store.resolve_handle`` takes a bare handle or a chunk handle with
    a table selector; a record handle with a ``~<sel>`` (``pa5~p12``,
    ``pa5~chunk3`` — citation detail, not part of the address) resolves
    its record instead. A chunk handle with a malformed selector is NOT
    tolerated: ``pc10~bogus`` reports ``selector`` — the handle is real
    but the citation detail is junk.
    """
    resolved = resolve(handle)
    if resolved is not None:
        return resolved, None
    base, sel = handle_registry.split_selector(handle)
    if sel is None:
        return None, None
    parsed = handle_registry.parse(base)
    if parsed is None:
        return None, None
    if parsed[1]:
        return None, "selector"
    return resolve(base), None


def lint_references(source: str, *, resolve: Any) -> list[LintIssue]:
    """Every annotation handle that fails ``resolve``, with its line.

    ``resolve`` is ``handle -> resolved | None`` — production passes
    ``Store.resolve_handle``; tests pass a stub. Issues are listed in
    source order, one per (line, handle) — nothing is dropped or
    deduped away.
    """
    issues: list[LintIssue] = []
    for ann in find_annotations(source):
        for h in ann.handles:
            resolved, why = _resolve_base(resolve, h)
            if resolved is None:
                issues.append(LintIssue(ann.lineno, h, why or "unresolved"))
    return issues


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefCitation:
    """One handle's export citation — what the resolver hands
    :func:`expand_references`. ``internal`` marks a kind that must not
    leave the instance; ``citation``/``excerpt`` are ignored then."""

    handle: str
    kind: str
    #: One-line self-contained citation ("Smith et al. (2020). …
    #: doi:…"); "" when nothing citable exists.
    citation: str = ""
    #: Verbatim excerpt, already capped at :data:`EXCERPT_CAP`.
    excerpt: str | None = None
    internal: bool = False


@dataclass
class ExpandReport:
    """What one expansion did — the export echo."""

    #: Handles with no entry in the prior expansion.
    added: list[str] = field(default_factory=list)
    #: Handles whose digest changed since the prior expansion.
    changed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    internal: list[str] = field(default_factory=list)
    #: ``# [N]`` comments whose ``N`` no entry defined (kept verbatim).
    orphaned: int = 0


@dataclass(frozen=True)
class ExpandResult:
    """Expanded source plus optional sidecar files and the echo."""

    source: str
    files: dict[str, str] = field(default_factory=dict)
    report: ExpandReport = field(default_factory=ExpandReport)


class _Resolver(Protocol):
    def cite(self, handle: str) -> RefCitation | None: ...


def _digest(citation: str, excerpt: str | None) -> str:
    """The staleness digest recorded on an entry: sha256 of the exact
    exported content, so a re-ingested chunk shows as a mismatch."""
    return hashlib.sha256(f"{citation}\n{excerpt or ''}".encode()).hexdigest()[:12]


def _entry_line(n: int, handle: str, cit: RefCitation, today: str) -> str:
    return (
        f"# [{n}] {handle} | {cit.citation or '(no citation)'} "
        f"| sha256:{_digest(cit.citation, cit.excerpt)} | {today}"
    )


def _parse_entries(lines: list[str]) -> tuple[dict[int, str], dict[str, str]]:
    """``[N] → handle`` and ``handle → digest`` from a sentinel block's
    or a sidecar's entry lines (same ``_ENTRY_RE`` grammar)."""
    num_to_handle: dict[int, str] = {}
    digests: dict[str, str] = {}
    for ln in lines:
        m = _ENTRY_RE.search(ln)
        if m is not None:
            num_to_handle[int(m.group(1))] = m.group(2)
            digests[m.group(2)] = m.group(3)
    return num_to_handle, digests


def _take_block(
    lines: list[str],
) -> tuple[list[str], dict[int, str], dict[str, str]]:
    """Remove a sentinel references block; return (clean lines,
    num→handle, handle→digest). Removes the one separator blank line
    the expansion added before the open sentinel."""
    open_i = close_i = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == _BLOCK_OPEN and open_i < 0:
            open_i = i
        elif s == _BLOCK_CLOSE and open_i >= 0:
            close_i = i
            break
    if open_i < 0 or close_i < 0:
        return lines, {}, {}
    num_to_handle, digests = _parse_entries(lines[open_i : close_i + 1])
    out = lines[:open_i] + lines[close_i + 1 :]
    if open_i > 0 and lines[open_i - 1].strip() == "":
        del out[open_i - 1]
    return out, num_to_handle, digests


def _rewrite_ref_comments(
    lines: list[str], num_to_handle: dict[int, str]
) -> tuple[list[str], int]:
    """Restore handles inside comments: ``(unresolved: h)`` → ``h``
    (needs no map — the marker is self-describing) and ``# [N]`` tokens
    → the block map's handles. A ``[N]`` with no entry leaves the whole
    comment verbatim and counts as orphaned."""
    orphaned = 0
    source = "\n".join(lines)
    edits: dict[int, tuple[int, str]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        body = _UNRES_RE.sub(lambda m: m.group(1), tok.string[1:].strip())
        if re.match(r"\[\d+\]", body):
            parts: list[str] = []
            rest = body
            ok = True
            while m := re.match(r"\[(\d+)\]\s*", rest):
                n = int(m.group(1))
                if n not in num_to_handle:
                    ok = False
                    break
                parts.append(num_to_handle[n])
                rest = rest[m.end() :]
            if ok and parts:
                body = " ".join(parts) + (" " + rest if rest else "")
            else:
                orphaned += 1
                continue
        elif body == tok.string[1:].strip():
            continue
        edits[tok.start[0]] = (tok.start[1], "# " + body)
    for lineno, (col, comment) in edits.items():
        lines[lineno - 1] = lines[lineno - 1][:col] + comment
    return lines, orphaned


def _strip_full(
    text: str, sidecar_text: str | None
) -> tuple[str, dict[str, int], dict[str, str], int]:
    """Strip a prior expansion; also return the old numbering
    (handle→N) and digests so a re-expansion keeps entry numbers stable
    and can echo "changed"."""
    lines = text.split("\n")
    lines = [ln for ln in lines if not re.match(r"^\s*#\|", ln)]
    lines, num_to_handle, digests = _take_block(lines)
    if sidecar_text is not None:
        n2h, d2 = _parse_entries(sidecar_text.split("\n"))
        num_to_handle = {**n2h, **num_to_handle}
        digests = {**d2, **digests}
    lines, orphaned = _rewrite_ref_comments(lines, num_to_handle)
    handle_to_num = {h: n for n, h in num_to_handle.items()}
    return "\n".join(lines), handle_to_num, digests, orphaned


def strip_references(text: str, *, sidecar_text: str | None = None) -> str:
    """Inverse of :func:`expand_references`: generated content out,
    bare handles back.

    * ``inline``: ``#|`` lines deleted (handles were never rewritten).
    * ``block``/``sidecar``: the sentinel block dropped and ``# [N]``
      comments rewritten to ``# <handle>`` via the block's own map. A
      ``[N]`` with no entry — or a ``# [N]`` comment with no block at
      all — is left verbatim (it is prose, not ours). For a sidecar
      expansion the map lives in ``REFERENCES.md``; pass it as
      ``sidecar_text`` or the ``[N]`` comments stay as they are.
      Internal handles were dropped at export and do not come back.
    """
    clean, _h2n, _dig, _orph = _strip_full(text, sidecar_text)
    return clean


def _render_tokens(
    ann: Annotation,
    numbers: dict[str, int] | None,
    cites: dict[str, RefCitation | None],
) -> str:
    """The replacement comment body: resolved handles → ``[N]`` (or
    kept verbatim for ``inline``), unresolved → ``(unresolved: h)``,
    internal → the placeholder. Gloss kept verbatim."""
    parts: list[str] = []
    for h in ann.handles:
        cit = cites.get(h)
        if cit is None:
            parts.append(f"(unresolved: {h})")
        elif cit.internal:
            parts.append(_INTERNAL)
        elif numbers is not None:
            parts.append(f"[{numbers[h]}]")
        else:
            parts.append(h)
    if ann.gloss:
        # A handle named inside the gloss is an address too: scrub it to
        # the same placeholder, or ``(see gr5137)`` leaks gr5137 out.
        parts.append(_GLOSS_HANDLE_RE.sub(_INTERNAL, ann.gloss))
    return " ".join(parts)


def expand_references(
    text: str,
    *,
    resolver: _Resolver,
    mode: str = "block",
    include_excerpts: bool = False,
    today: str | None = None,
    sidecar_text: str | None = None,
) -> ExpandResult:
    """Expand every annotation to a self-contained citation.

    Pure: text in, text out (plus ``REFERENCES.md`` for ``mode=
    'sidecar'``). Prior expansions are stripped first, so re-export
    recomputes — idempotent by construction — and digests from the old
    block feed ``report.changed``. ``today`` (UTC ``YYYY-MM-DDZ``) is
    injectable for tests.
    """
    if mode not in ("block", "inline", "sidecar"):
        raise BadInput(
            f"expand_references: unknown mode {mode!r}",
            options=["block", "inline", "sidecar"],
        )
    today = today or (datetime.now(UTC).date().isoformat() + "Z")
    report = ExpandReport()
    clean, handle_to_num, old_digests, report.orphaned = _strip_full(text, sidecar_text)
    trailing_nl = clean.endswith("\n") or clean == ""
    anns = find_annotations(clean)
    cites: dict[str, RefCitation | None] = {}
    for ann in anns:
        for h in ann.handles:
            if h not in cites:
                cites[h] = resolver.cite(h)
    for h, cit in cites.items():
        if cit is None:
            report.unresolved.append(h)
        elif cit.internal:
            report.internal.append(h)

    # Numbering: an already-cited handle keeps its old [N]; new ones
    # take the lowest free numbers, in first-appearance order.
    numbers: dict[str, int] = {}
    used: set[int] = set()
    for h, cit in cites.items():
        if cit is None or cit.internal:
            continue
        n = handle_to_num.get(h)
        if n is not None and n not in used:
            numbers[h] = n
            used.add(n)
    free = 1
    for h, cit in cites.items():
        if cit is None or cit.internal or h in numbers:
            continue
        while free in used:
            free += 1
        numbers[h] = free
        used.add(free)

    entries: list[tuple[int, str, RefCitation]] = []
    for h, n in numbers.items():
        cit = cites[h]
        assert cit is not None
        entries.append((n, h, cit))
        if h not in old_digests:
            report.added.append(h)
        elif old_digests[h] != _digest(cit.citation, cit.excerpt):
            report.changed.append(h)
    entries.sort(key=lambda e: e[0])

    lines = clean.split("\n")
    if mode == "inline":
        for ann in anns:
            lines[ann.lineno - 1] = (
                lines[ann.lineno - 1][: ann.col]
                + "# "
                + _render_tokens(ann, None, cites)
            )
        inserts: dict[int, list[str]] = {}
        for ann in anns:
            # ``#|`` detail lines align with the statement's indent —
            # comment lines never affect INDENT/DEDENT, so this is
            # safe even inside a bracketed call.
            indent = re.match(r"\s*", lines[ann.lineno - 1]).group(0)  # type: ignore[union-attr]
            extra: list[str] = []
            for h in ann.handles:
                cit = cites.get(h)
                if cit is None:
                    extra.append(f"{indent}{_INLINE_MARK} (unresolved: {h})")
                    continue
                if cit.internal:
                    continue
                ex = cit.excerpt if include_excerpts else None
                extra.append(
                    f"{indent}{_INLINE_MARK} {cit.citation or '(no citation)'} "
                    f"| sha256:{_digest(cit.citation, cit.excerpt)} | {today}"
                )
                if ex:
                    extra.append(f'{indent}{_INLINE_MARK} "{ex}"')
            if extra:
                inserts[ann.lineno] = extra
        out: list[str] = []
        for i, ln in enumerate(lines, start=1):
            out.append(ln)
            out.extend(inserts.get(i, []))
        if trailing_nl and (not out or out[-1] != ""):
            out.append("")
        return ExpandResult(source="\n".join(out), report=report)

    # block / sidecar: rewrite the comment bodies.
    for ann in anns:
        lines[ann.lineno - 1] = (
            lines[ann.lineno - 1][: ann.col]
            + "# "
            + _render_tokens(ann, numbers, cites)
        )

    files: dict[str, str] = {}
    if mode == "sidecar":
        md = [f"# References — expanded {today} (precis gr514049)", ""]
        for n, h, cit in entries:
            ex = cit.excerpt if include_excerpts else None
            md.append(
                f"- [{n}] {h} | {cit.citation or '(no citation)'} "
                f"| sha256:{_digest(cit.citation, cit.excerpt)} | {today}"
            )
            if ex:
                md.append(f"  > {ex}")
            md.append("")
        files[_SIDECAR_NAME] = "\n".join(md).rstrip() + "\n"
    elif entries:
        # No entries → no block: every annotation was unresolved or
        # internal, and an empty REFERENCES section is noise.
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", _BLOCK_OPEN]
        for n, h, cit in entries:
            ex = cit.excerpt if include_excerpts else None
            lines.append(_entry_line(n, h, cit, today))
            if ex:
                lines.append(f"#     > {ex}")
        lines.append(_BLOCK_CLOSE)
        if trailing_nl:
            lines.append("")
    return ExpandResult(source="\n".join(lines), files=files, report=report)


# ---------------------------------------------------------------------------
# The db-backed resolver
# ---------------------------------------------------------------------------


def _cap_excerpt(text: str | None) -> str | None:
    """Whitespace-collapsed verbatim excerpt, capped at EXCERPT_CAP on
    a word boundary."""
    if not text:
        return None
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CAP:
        return flat
    return flat[: EXCERPT_CAP - 1].rsplit(" ", 1)[0] + "…"


def _paper_citation(ref: Any, doi: str | None) -> str:
    """``Wang & Li (2020). "Title". doi:10.…`` — author surnames +
    ``et al.`` past two."""
    authors = (
        getattr(ref, "authors", None)
        or (getattr(ref, "meta", None) or {}).get("authors")
        or []
    )
    names: list[str] = []
    for a in authors[:2]:
        name = str(
            (a.get("family") or a.get("name") or "") if isinstance(a, dict) else a
        ).strip()
        if name:
            names.append(
                name.split(",")[0].strip() if "," in name else name.split()[-1]
            )
    who = " & ".join(names) + (" et al." if len(authors) > 2 else "")
    year = getattr(ref, "year", None)
    head = f"{who} ({year})." if who and year else who
    bits = [b for b in (head, f'"{ref.title}".' if getattr(ref, "title", None) else "")]
    if doi:
        bits.append(f"doi:{doi}")
    return " ".join(bits) or str(getattr(ref, "slug", "") or "")


class StoreRefResolver:
    """``cite(handle)`` against a live store — the citation + excerpt
    table of ``docs/backlog/workspace-db-references.md``.

    Duck-typed on the store slice it reads: ``resolve_handle``,
    ``get_ref``, ``identifiers_for_refs``, ``chunks.get_chunk`` /
    ``list_chunks_for_ref`` / ``chunk_pages``. A miss at any level
    degrades the entry (citation without excerpt), never raises —
    except a handle that resolves to nothing, which returns ``None``
    (unresolved).
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def cite(self, handle: str) -> RefCitation | None:
        base, sel = handle_registry.split_selector(handle)
        parsed = handle_registry.parse(handle) or handle_registry.parse(base)
        if parsed is None:
            return None
        kind, is_chunk, _pk = parsed
        if kind != "finding" and kind not in _EXCERPT_KINDS | _POINTER_KINDS:
            return RefCitation(handle=handle, kind=kind, internal=True)
        resolved = self._store.resolve_handle(handle) or (
            self._store.resolve_handle(base) if sel and not is_chunk else None
        )
        if resolved is None:
            return None
        ref = self._store.get_ref(kind=resolved.kind, id=resolved.ref_id)
        if ref is None:
            return None
        citation, want_excerpt = self._citation(resolved.kind, ref, resolved)
        excerpt = None
        if want_excerpt:
            text, _page = self._excerpt(resolved, is_chunk, sel)
            excerpt = _cap_excerpt(text)
        return RefCitation(
            handle=handle, kind=resolved.kind, citation=citation, excerpt=excerpt
        )

    def _citation(self, kind: str, ref: Any, resolved: Any) -> tuple[str, bool]:
        """(one-line citation, wants an excerpt) per kind."""
        meta = getattr(ref, "meta", None) or {}
        if kind == "paper":
            doi = None
            try:
                ids = self._store.identifiers_for_refs([resolved.ref_id])
                doi = (ids.get(resolved.ref_id) or {}).get("doi")
            except Exception:
                doi = None
            return _paper_citation(ref, doi), True
        if kind == "patent":
            from precis.export._patent_cite import format_patent_bibliography_entry

            return format_patent_bibliography_entry(ref), True
        if kind == "datasheet":
            head = (
                " ".join(
                    p
                    for p in (
                        str(meta.get("manufacturer") or ""),
                        str(meta.get("mfr_part") or ""),
                    )
                    if p
                ).strip()
                or ref.title
                or str(ref.slug or "")
            )
            cite = f"{head} datasheet"
            if meta.get("revision"):
                cite += f", rev {meta['revision']}"
            return cite, True
        if kind == "part":
            return f"LCSC {ref.slug or resolved.public_id} — {ref.title}", False
        if kind == "finding":
            status = meta.get("status") or ""
            tail = f" (status: {status})" if status else ""
            return f'finding: "{ref.title}"{tail}', True
        if kind == "structure":
            bits = [f"structure {ref.slug or resolved.public_id}"]
            if meta.get("version") is not None:
                bits.append(f"v{meta['version']}")
            atoms = meta.get("atom_count") or meta.get("n_atoms")
            if atoms is not None:
                bits.append(f"{atoms} atoms")
            if meta.get("generator"):
                bits.append(f"generator {meta['generator']}")
            return ", ".join(bits), False
        # edgar/cfp/news + the pointer kinds (material/component/rxn/
        # make/cad/pcb): title line + the kind's public id.
        who = ref.title or str(ref.slug or resolved.public_id)
        return (
            f"{who} ({kind}:{resolved.cite_public_id or resolved.public_id})",
            kind in _EXCERPT_KINDS,
        )

    def _excerpt(
        self, resolved: Any, is_chunk: bool, sel: str | None
    ) -> tuple[str | None, int | None]:
        """The verbatim chunk text for a handle: its own chunk for a
        chunk handle; ``~N`` the chunk at pos N, ``~pN`` the chunk
        covering page N, ``~<slug>`` the named chunk; a record handle
        with no selector takes the ref's first body chunk (a paper's
        abstract). ``(text, page_first | None)``."""
        chunks = self._store.chunks
        rid = resolved.ref_id
        if is_chunk and resolved.chunk_ord is not None:
            row = chunks.get_chunk(rid, pos=resolved.chunk_ord)
            page = (
                chunks.chunk_pages(rid, [resolved.chunk_ord]).get(resolved.chunk_ord)
                if row
                else None
            )
            return (row.text if row else None), page
        if sel:
            if m := re.fullmatch(r"p(\d+)", sel):
                return self._chunk_on_page(rid, int(m.group(1)))
            if sel.isdigit():
                row = chunks.get_chunk(rid, pos=int(sel))
                return (row.text if row else None), None
            row = chunks.get_chunk(rid, slug=sel)
            return (row.text if row else None), None
        rows = chunks.list_chunks_for_ref(rid)
        return (rows[0].text if rows else None), None

    def _chunk_on_page(self, ref_id: int, page: int) -> tuple[str | None, int | None]:
        """``~pN``: the chunk covering page N — the last chunk with
        ``page_first <= N``."""
        rows = self._store.chunks.list_chunks_for_ref(ref_id)
        pages = self._store.chunks.chunk_pages(ref_id, [r.ord for r in rows])
        best: Any | None = None
        for row in rows:
            p = pages.get(row.ord)
            if p is not None and p <= page:
                best = row
        return (best.text if best else None), (pages.get(best.ord) if best else None)
