"""Memory attribution gate: a number pinned to a citation must be in the cited text.

A memory that writes "~10 nm for stiff solids per websearch:170350" asserts
that the cited text says so. :func:`ungrounded_cited_numbers` checks that
claim deterministically (no LLM): every unit-bearing number
(:func:`precis.utils.numerics.numeric_spans`) that sits in the same sentence
as a citation, attributed to the *nearest* citation (a cluster of adjacent
citations pools its evidence), must appear in that evidence as the same
``(number, unit)`` pair. A parenthetical that opens right after a citation is
that citation's gloss: "paper:x (… 60 kPa …) and paper:y" pins 60 kPa to x,
whatever is nearer (the dream prose shape; the prod sample of 2026-10-10 had
it mis-pinned to the following cite in 4 of 40 rows). A chunk cite is checked
against its chunk and, failing that, the whole document: a pinpoint a few
chunks off is a reading aid, not a mis-sourced number. A bare digit run never grounds a unit-bearing claim:
a websearch body whose only "10" is the DOI prefix ``10.1098`` does not say
"10 nm" (the first dogfood write slipped through on exactly that, 2026-10-09).
Evidence that is empty is nothing to check against (no flag). A miss is an
:class:`UngroundedNumber`.

Spec and rationale: ``docs/backlog/memory-attribution-gate.md`` (the
``DREAM:speculative`` tag licenses unsourced claims, not mis-sourced ones).
The handler hook is :meth:`MemoryHandler._attribution_audit`; the retro-apply
is ``scripts/memory-attribution-audit``.

Seams for tests: :func:`_resolve_cite` and :func:`_fetch_evidence` are the
only DB touchpoints.
"""

from __future__ import annotations

import bisect
import functools
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.utils import handle_registry
from precis.utils.mentions import (
    BARE_PAPER_PATTERN,
    DRAFT_MARKUP_PATTERN,
    LINKIFY_KINDS,
    LOW_SIGNAL_KINDS,
    REF_PATTERN,
    chunk_to_pos,
    resolve_handle_ref,
    resolve_handle_target,
)
from precis.utils.numerics import _UNITS_RE_PART, numeric_spans

if TYPE_CHECKING:
    from precis.store.store import Store

#: Env switch: ``warn`` (default) tags + advises, ``reject`` raises BadInput.
GATE_ENV = "PRECIS_MEMORY_ATTRIBUTION_GATE"

#: The derived closed tag the gate maintains (``AUDIT:ungrounded-number``).
AUDIT_VALUE = "ungrounded-number"

#: Bare two-letter-code handles dream prose writes unbracketed (``pc995663``).
_BARE_HANDLE_RE = re.compile(r"\b(?:pa|pt|me|fi|pc|pk|fb)\d{3,}\b")

#: A number within this many characters of a citation, same sentence.
_WINDOW_CHARS = 200

#: "my estimate" etc. may start this many characters after the number, or end
#: this many characters before it ("my estimate: ~10 nm").
_EXEMPT_AFTER_CHARS = 60
_EXEMPT_BEFORE_CHARS = 60
_EXEMPT_RE = re.compile(
    r"my (?:own )?estimate|\(est\.\)|\(estimate\)|rough guess", re.IGNORECASE
)

#: Sentence boundary: terminal punctuation + space(s) + a sentence opener, or
#: any newline (bullets and paragraphs never share a segment). The opener
#: lookahead keeps "et al. found" whole; the stacked lookbehinds keep common
#: abbreviations ("e.g. 10 nm", "Fig. 3") from splitting on their period.
_SENTENCE_BREAK_RE = re.compile(
    r"(?<=[.!?])"
    r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bFig\.)(?<!\bFigs\.)(?<!\bEq\.)"
    r"(?<!\bet al\.)(?<!\bvs\.)(?<!\bcf\.)(?<!\bca\.)(?<!\bapprox\.)"
    r"\s+(?=[A-Z(\[\"~≈<>\-\d])|\n\s*"
)

#: Citation spans closer than this (same sentence) form one cluster whose
#: evidence is pooled: "(pc1, pc2)", "[pc1][pc2]", "pa5 / pa6".
_CLUSTER_GAP_CHARS = 12

#: Text-evidence loading cap per ref (whichever hits first).
_EVIDENCE_MAX_CHUNKS = 200
_EVIDENCE_MAX_BYTES = 400_000

#: Evidence memo bound (refs).
_EVIDENCE_CACHE_MAX = 512

#: Targets whose grounding evidence is the ``numerics`` column + title (large
#: documents). Every other kind is checked against its chunk text.
_NUMERICS_ONLY_KINDS = frozenset({"paper", "patent"})

#: Units the ``numerics`` column cannot see: PDF-derived text writes micro as
#: ``$\mu$ m`` or the Greek letter, which :data:`precis.utils.numerics._UNITS`
#: does not tokenise, so a paper's numerics never hold "2 µm" however often
#: the paper says it. A claim in these units is unverifiable against
#: numerics-only evidence, which is not the same as ungrounded: no flag.
_NUMERICS_BLIND_UNITS = frozenset({"µm", "µA", "µM", "µg", "µs"})

#: "2010s", "1980s": a decade, not seconds. ``numeric_spans`` reads the glued
#: ``s`` as the unit; only the four-digit year shape is excluded.
_DECADE_RE = re.compile(r"(?:1[89]|20)\d\ds")

_NUMBER_PART_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: "10-20 nm", "10–20 nm", "10 to 20 nm": a range sharing one trailing unit.
_RANGE_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)"
    rf"\s*({_UNITS_RE_PART})(?!\w)"
)

#: Spelling variants of one unit that numerics._UNITS lists separately. No
#: conversion: A (ampere) is deliberately not Å.
_UNIT_CANON = {
    "um": "µm",
    "uA": "µA",
    "uM": "µM",
    "ug": "µg",
    "us": "µs",
    "μm": "µm",  # Greek mu (U+03BC), not the micro sign (U+00B5)
    "μA": "µA",
    "μM": "µM",
    "μg": "µg",
    "μs": "µs",
    "cm−1": "cm-1",
    "Angstrom": "Å",
}


@functools.cache
def _reword():
    # Lazy: taproot.reword pulls the nanopub/LLM stack; keep it off the
    # handlers' import path. Cached so the import statement runs once.
    from precis.taproot import reword

    return reword


@dataclass(frozen=True)
class Evidence:
    """What a cited target carries: ``(unit, digit-run)`` pairs, bare digit
    runs (only a unit-less claim may match on those), and whether there was
    any text to check against at all."""

    units: frozenset[tuple[str, str]] = frozenset()
    runs: frozenset[str] = frozenset()
    has_text: bool = False
    #: Some of this came from a ``numerics`` column (see
    #: :data:`_NUMERICS_BLIND_UNITS`).
    numerics_only: bool = False

    @property
    def usable(self) -> bool:
        return self.has_text or bool(self.units) or bool(self.runs)

    def __or__(self, other: Evidence) -> Evidence:
        return Evidence(
            self.units | other.units,
            self.runs | other.runs,
            self.has_text or other.has_text,
            self.numerics_only or other.numerics_only,
        )


def _quantities(text: str) -> list[tuple[str, int, int]]:
    """``numeric_spans`` plus ranges: "10-20 nm" gives "10 nm" and "20 nm"
    (never "-20 nm"); the range replaces the spans it covers."""
    spans = [
        (t, s, e)
        for t, s, e in numeric_spans(text)
        if not _DECADE_RE.fullmatch(text, s, e)
    ]
    ranges = list(_RANGE_RE.finditer(text))
    if not ranges:
        return spans
    out = [
        (t, s, e)
        for t, s, e in spans
        if not any(r.start() <= s and e <= r.end() for r in ranges)
    ]
    for r in ranges:
        unit = r.group(3)
        sep = "" if unit == "%" else " "
        for num in (r.group(1), r.group(2)):
            out.append((f"{num}{sep}{unit}", r.start(), r.end()))
    out.sort(key=lambda t: (t[1], t[2]))
    return out


def _split_token(token: str) -> tuple[str, str]:
    m = _NUMBER_PART_RE.match(token)
    if m is None:
        return token, ""
    return m.group(0), _UNIT_CANON.get(
        token[m.end() :].strip(), token[m.end() :].strip()
    )


def _runs(text: str) -> list[str]:
    rw = _reword()
    return rw._DIGIT_RUN_RE.findall(rw._canon_grounding(text))


def _canon_runs(text: str) -> set[str]:
    return set(_runs(text))


def evidence_from_text(text: str, *, has_text: bool | None = None) -> Evidence:
    """:class:`Evidence` of free text (or of numerics tokens joined by space)."""
    units: set[tuple[str, str]] = set()
    for tok, _s, _e in _quantities(text):
        number, unit = _split_token(tok)
        units.update((unit, r) for r in _runs(number))
    return Evidence(
        frozenset(units),
        frozenset(_canon_runs(text)),
        bool(text.strip()) if has_text is None else has_text,
    )


def _grounded(token: str, ev: Evidence) -> bool:
    """Is the claim ``token`` ("10 nm") present in ``ev``?

    A unit-bearing claim needs the same ``(number, unit)`` pair in the
    evidence; its bare digit runs do not count (DOIs, years and ref ids make
    every small integer "present" otherwise). Only a unit-less claim, which
    :func:`_quantities` does not produce today, falls back to the runs.
    Decimal-prefix tolerance / integer exactness come from the reword check.
    """
    number, unit = _split_token(token)
    claim = _runs(number)
    if unit:
        have = {r for u, r in ev.units if u == unit}
    else:
        have = set(ev.runs)
    is_grounded = _reword()._is_grounded
    return all(is_grounded(r, have) for r in claim)


@dataclass(frozen=True)
class UngroundedNumber:
    token: str  # as written, e.g. "10 nm"
    cite: str  # the citation token it sits next to, e.g. "websearch:170350"
    ref_id: int | None  # resolved target, None when the cite does not resolve

    def advisory(self) -> str:
        """The one-line ack/BadInput form (spec §2)."""
        return (
            f'ungrounded: "{self.token}" near {self.cite} — not in the cited '
            'text; drop the attribution or write "(my estimate)"'
        )


@dataclass
class AttributionCache:
    """Cross-call memo: handle resolution + evidence per target.

    The backfill passes one instance across thousands of memories so each
    cited paper/websearch is read once. Evidence is an LRU bounded to
    ``_EVIDENCE_CACHE_MAX`` targets.
    """

    resolved: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    evidence: OrderedDict[tuple[int, int | None], Evidence] = field(
        default_factory=OrderedDict
    )

    def get_evidence(self, key: tuple[int, int | None]) -> Evidence | None:
        ev = self.evidence.get(key)
        if ev is not None:
            self.evidence.move_to_end(key)
        return ev

    def put_evidence(self, items: dict[tuple[int, int | None], Evidence]) -> None:
        for k, v in items.items():
            self.evidence[k] = v
            self.evidence.move_to_end(k)
        while len(self.evidence) > _EVIDENCE_CACHE_MAX:
            self.evidence.popitem(last=False)


def gate_mode() -> str:
    """``warn`` (default) or ``reject``; anything unrecognised is ``warn``."""
    v = os.environ.get(GATE_ENV, "warn").strip().lower()
    return "reject" if v == "reject" else "warn"


# --- citation spans ---------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    cite: str  # shown form
    key: str  # resolution key: ("h", handle) | ("r", ident, chunk)
    drop_if_unresolved: bool = False


def _cite_spans(body: str) -> list[_Span]:
    """Citation spans in priority order (markup, kind:id, bare handle, bare
    paper key); a later span overlapping an earlier one is dropped."""
    spans: list[_Span] = []

    def _free(start: int, end: int) -> bool:
        return all(end <= s.start or start >= s.end for s in spans)

    for m in DRAFT_MARKUP_PATTERN.finditer(body):
        if m.group("auth") is not None:
            token = m.group("auth").strip()
        elif m.group("tgt") is not None:
            token = m.group("tgt").strip()
        elif m.group("bare") is not None:
            token = m.group("bare")
        else:
            continue
        # A ``[text](https://…)`` / ``[¶h]`` is not a registry handle.
        if not handle_registry.is_well_formed(handle_registry.normalize(token)):
            continue
        spans.append(_Span(m.start(), m.end(), token, "h:" + token))
    for m in REF_PATTERN.finditer(body):
        kind = m.group("kind")
        if kind not in LINKIFY_KINDS or kind in LOW_SIGNAL_KINDS:
            continue
        if not _free(m.start(), m.end()):
            continue
        spans.append(
            _Span(
                m.start(),
                m.end(),
                m.group(0),
                f"r:{m.group('id').lstrip('#')}|{m.group('chunk') or ''}",
            )
        )
    for m in _BARE_HANDLE_RE.finditer(body):
        if _free(m.start(), m.end()):
            spans.append(_Span(m.start(), m.end(), m.group(0), "h:" + m.group(0)))
    for m in BARE_PAPER_PATTERN.finditer(body):
        if not _free(m.start(), m.end()):
            continue
        slug, _, suffix = m.group(0).partition("~")
        spans.append(
            _Span(
                m.start(),
                m.end(),
                m.group(0),
                f"r:{slug}|{('~' + suffix) if suffix else ''}",
                # ``abc12`` is usually just a word; an unresolved bare key is
                # not reported (it is not evidence anyone cited anything).
                drop_if_unresolved=True,
            )
        )
    spans.sort(key=lambda s: s.start)
    return spans


def _resolve_cite(
    store: Store, span: _Span, cache: AttributionCache
) -> tuple[int | None, int | None]:
    """``(ref_id, chunk_ord)`` of a citation, or ``(None, None)``."""
    hit = cache.resolved.get(span.key)
    if hit is not None:
        return hit
    ref_id: int | None = None
    pos: int | None = None
    if span.key.startswith("h:"):
        tgt = resolve_handle_target(store, span.key[2:])
        if tgt is not None:
            ref_id, pos = tgt.dst_ref_id, tgt.dst_pos
    else:
        ident, _, chunk = span.key[2:].partition("|")
        ref = resolve_handle_ref(store, ident)
        if ref is not None and getattr(ref, "retired_at", None) is None:
            ref_id, pos = int(ref.id), chunk_to_pos(chunk or None)
    cache.resolved[span.key] = (ref_id, pos)
    return ref_id, pos


# --- evidence ---------------------------------------------------------------


def _fetch_evidence(
    store: Store, targets: set[tuple[int, int | None]]
) -> dict[tuple[int, int | None], Evidence]:
    """:class:`Evidence` for each ``(ref_id, chunk_ord|None)``.

    Batched: one query for ref kinds/titles, one per evidence shape. A
    chunk cite reads that chunk and its ±1 neighbours (the caller pools the
    ``(ref_id, None)`` document evidence with it); a ref cite reads the
    ``numerics`` column (paper/patent; a ref with no numerics at all has
    nothing to check against) or chunk text, capped per ref (other kinds:
    websearch / perplexity / web / memory bodies).
    """
    out: dict[tuple[int, int | None], Evidence] = {t: Evidence() for t in targets}
    ref_ids = sorted({r for r, _ in targets})
    chunk_targets = sorted((r, p) for r, p in targets if p is not None)
    ref_targets = sorted(r for r, p in targets if p is None)
    with store.pool.connection() as conn:
        if chunk_targets:
            rows = conn.execute(
                "SELECT t.r, t.p, c.text FROM unnest(%s::bigint[], %s::int[]) "
                "AS t(r, p) JOIN chunks c ON c.ref_id = t.r "
                "AND c.ord BETWEEN t.p - 1 AND t.p + 1 AND c.retired_at IS NULL",
                ([r for r, _ in chunk_targets], [p for _, p in chunk_targets]),
            ).fetchall()
            for r, p, text in rows:
                out[(int(r), int(p))] |= evidence_from_text(text or "")
        if ref_targets:
            meta = {
                int(rid): (kind, title)
                for rid, kind, title in conn.execute(
                    "SELECT ref_id, kind, title FROM refs WHERE ref_id = ANY(%s)",
                    (ref_ids,),
                ).fetchall()
            }
            numeric_ids = [
                r
                for r in ref_targets
                if meta.get(r, ("", ""))[0] in _NUMERICS_ONLY_KINDS
            ]
            numeric_set = set(numeric_ids)
            text_ids = [r for r in ref_targets if r not in numeric_set]
            for r in text_ids:
                out[(r, None)] |= evidence_from_text(meta.get(r, ("", ""))[1] or "")
            if numeric_ids:
                tokens: dict[int, list[str]] = {r: [] for r in numeric_ids}
                for rid, n in conn.execute(
                    "SELECT DISTINCT c.ref_id, n FROM chunks c, "
                    "unnest(c.numerics) AS n "
                    "WHERE c.ref_id = ANY(%s) AND c.retired_at IS NULL",
                    (numeric_ids,),
                ).fetchall():
                    if n:
                        tokens[int(rid)].append(n)
                for rid, toks in tokens.items():
                    if not toks:
                        continue  # numerics = [] everywhere: nothing to check
                    ev = evidence_from_text(" ; ".join(toks), has_text=True)
                    ev = Evidence(ev.units, ev.runs, True, numerics_only=True)
                    title = meta.get(rid, ("", ""))[1] or ""
                    out[(rid, None)] |= ev | evidence_from_text(title, has_text=False)
            if text_ids:
                spent: dict[int, int] = {}
                for rid, text in conn.execute(
                    "SELECT ref_id, text FROM ("
                    " SELECT ref_id, text, ord, row_number() OVER ("
                    "  PARTITION BY ref_id ORDER BY ord) AS rn FROM chunks "
                    " WHERE ref_id = ANY(%s) AND retired_at IS NULL) q "
                    "WHERE rn <= %s ORDER BY ref_id, ord",
                    (text_ids, _EVIDENCE_MAX_CHUNKS),
                ).fetchall():
                    rid = int(rid)
                    used = spent.get(rid, 0)
                    if used >= _EVIDENCE_MAX_BYTES:
                        continue
                    text = (text or "")[: _EVIDENCE_MAX_BYTES - used]
                    spent[rid] = used + len(text.encode("utf-8"))
                    out[(rid, None)] |= evidence_from_text(text)
    return out


# --- the check --------------------------------------------------------------


def _paren_groups(body: str) -> list[tuple[int, int]]:
    """``(open, close)`` of every balanced round-bracket pair."""
    stack: list[int] = []
    out: list[tuple[int, int]] = []
    for i, ch in enumerate(body):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            out.append((stack.pop(), i))
    return out


def _cite_parenthetical(
    groups: list[tuple[int, int]], spans: list[_Span], start: int, end: int
) -> tuple[int, int, int] | None:
    """``(open, close, span index)`` of the innermost parenthetical holding
    ``[start, end)`` that opens within :data:`_CLUSTER_GAP_CHARS` of a
    citation's end — that citation's gloss. ``None`` when the number is in
    no such group."""
    enclosing = sorted(
        ((o, c) for o, c in groups if o < start and end <= c), reverse=True
    )
    for o, c in enclosing:
        for i, sp in enumerate(spans):
            if 0 <= o - sp.end <= _CLUSTER_GAP_CHARS:
                return o, c, i
    return None


def _clusters(spans: list[_Span], seg_of_span: list[int]) -> list[int]:
    """Cluster id per span: neighbours in one sentence within
    ``_CLUSTER_GAP_CHARS`` characters share an id."""
    ids: list[int] = []
    for i, sp in enumerate(spans):
        if (
            i
            and seg_of_span[i] == seg_of_span[i - 1]
            and sp.start - spans[i - 1].end <= _CLUSTER_GAP_CHARS
        ):
            ids.append(ids[-1])
        else:
            ids.append(ids[-1] + 1 if ids else 0)
    return ids


def ungrounded_cited_numbers(
    store: Store, body: str, *, cache: AttributionCache | None = None
) -> list[UngroundedNumber]:
    """Numbers written next to a citation that the cited text does not carry.

    Includes ``ref_id=None`` entries for citations that do not resolve —
    callers treat those as informational, never as a grounding failure
    (see :func:`grounding_failures`).
    """
    if not body:
        return []
    cache = cache if cache is not None else AttributionCache()
    spans = _cite_spans(body)
    if not spans:
        return []
    numbers = [
        (tok, s, e)
        for tok, s, e in _quantities(body)
        if all(e <= sp.start or s >= sp.end for sp in spans)
    ]
    if not numbers:
        return []

    breaks = [0] + [m.end() for m in _SENTENCE_BREAK_RE.finditer(body)]

    def _segment(pos: int) -> int:
        return bisect.bisect_right(breaks, pos) - 1

    seg_of_span = [_segment(sp.start) for sp in spans]
    cluster_of = _clusters(spans, seg_of_span)
    groups = _paren_groups(body)

    def _dist(sp: _Span, s: int, e: int) -> int:
        return sp.start - e if sp.start >= e else s - sp.end

    # Attribute each number to its citation: inside a gloss, the citation the
    # gloss belongs to unless a citation inside the gloss is nearer; else the
    # nearest in-window citation in the same sentence.
    picks: list[tuple[str, int, int]] = []  # (token, number start, span index)
    for tok, s, e in numbers:
        if _exempt(body, s, e):
            continue
        cands: list[tuple[int, int]] = []
        scope = _cite_parenthetical(groups, spans, s, e)
        if scope is not None:
            o, c, opener = scope
            cands.append((s - o, opener))
            for i, sp in enumerate(spans):
                if i != opener and sp.start > o and sp.end <= c:
                    dist = _dist(sp, s, e)
                    if dist <= _WINDOW_CHARS:
                        cands.append((dist, i))
        else:
            seg = _segment(s)
            for i, (sp, sp_seg) in enumerate(zip(spans, seg_of_span, strict=True)):
                if sp_seg != seg:
                    continue
                dist = _dist(sp, s, e)
                if dist <= _WINDOW_CHARS:
                    cands.append((dist, i))
        if cands:
            picks.append((tok, s, min(cands)[1]))
    if not picks:
        return []

    # A pick checks against its whole cluster's pooled evidence.
    members: dict[int, list[int]] = {}
    for i in {i for _t, _s, i in picks}:
        members[i] = [j for j, c in enumerate(cluster_of) if c == cluster_of[i]]
    resolved = {
        j: _resolve_cite(store, spans[j], cache) for js in members.values() for j in js
    }
    need: set[tuple[int, int | None]] = set()
    for r, p in resolved.values():
        if r is None:
            continue
        for key in {(r, p), (r, None)}:
            if cache.get_evidence(key) is None:
                need.add(key)
    if need:
        cache.put_evidence(_fetch_evidence(store, need))

    out: list[UngroundedNumber] = []
    seen: set[tuple[str, str]] = set()
    for tok, _s, i in picks:
        sp = spans[i]
        live: list[tuple[int, int | None]] = []
        for j in members[i]:
            rid, pos = resolved[j]
            if rid is not None:
                live.append((rid, pos))
        if not live:
            if sp.drop_if_unresolved or (tok, sp.cite) in seen:
                continue
            seen.add((tok, sp.cite))
            out.append(UngroundedNumber(tok, sp.cite, None))
            continue
        ev = Evidence()
        for rid, pos in live:
            for key in {(rid, pos), (rid, None)}:
                got = cache.get_evidence(key)
                if got is not None:
                    ev = ev | got
        if not ev.usable or _grounded(tok, ev):
            continue  # nothing to check against, or the number is there
        if ev.numerics_only and _split_token(tok)[1] in _NUMERICS_BLIND_UNITS:
            continue  # the numerics column cannot see this unit: unverifiable
        if (tok, sp.cite) in seen:
            continue
        seen.add((tok, sp.cite))
        ref_id = resolved[i][0] if resolved[i][0] is not None else live[0][0]
        out.append(UngroundedNumber(tok, sp.cite, ref_id))
    return out


def grounding_failures(misses: list[UngroundedNumber]) -> list[UngroundedNumber]:
    """The misses that are real grounding failures (resolved citation)."""
    return [m for m in misses if m.ref_id is not None]


def _exempt(body: str, start: int, end: int) -> bool:
    """The number carries an own-estimate phrase just after or just before it."""
    m = _EXEMPT_RE.search(body, end, end + _EXEMPT_AFTER_CHARS + 24)
    if m is not None and m.start() - end <= _EXEMPT_AFTER_CHARS:
        return True
    lo = max(0, start - _EXEMPT_BEFORE_CHARS)
    return _EXEMPT_RE.search(body[lo:start]) is not None
