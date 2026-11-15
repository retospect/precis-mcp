"""Hand-launched repair: write a claim's missing conditions from its paper.

Step 4 of ``docs/backlog/claim-conditions-completeness.md``. The measurement
(2026-11-15, dr348633) found most electrochemical, DFT-step and photo-rate
hub sentences missing a must-state condition
(:func:`~precis.taproot.conditions.check_conditions`): the potential, its
reference electrode, the electrolyte, the light source. The evidence paper
usually states them in Methods; the passage the claim was minted from does
not. This sweep reads the paper and writes the condition into the sentence
(``aida_uri`` keys on the sentence alone, so ``scope`` is not enough).

Shape cloned from :mod:`precis.taproot.reword`: cohort query, one MEDIUM
call per hub (:func:`propose_conditions`), code post-validation, dry-run
default, ``--apply`` through
:func:`~precis.taproot.hub.refine_claim_sentence` (scope unchanged), JSONL
report. **The model is not trusted.** Per hub it answers one of:

* ``add``: the claim with ONLY the missing conditions inserted, each with a
  ``quote`` copied from the paper text it was shown.
* ``not_reported``: the paper does not state them. The sentence is then
  built *in code* (the old sentence plus
  :func:`~precis.taproot.conditions.not_reported_marker`), never taken from
  the model, so the marker phrasing cannot drift.
* ``ambiguous``: several values, or the claim mixes experiments. Nothing is
  written; the row is the report for a human.

Design choices the spec left open (all in :func:`validate_add`):

* **Insertion-only rule.** Tokens (folded: NFKC, unicode minus, casefold) of
  the old sentence must all survive in the new one, bar a small
  :data:`_CONNECTIVES` set the model may reshuffle; and every token the new
  sentence *adds* must come from an ``added[].text``, a "not reported"
  marker, or :data:`_CONNECTIVES`. A reworded clause therefore fails.
* **Quote check.** Each ``quote`` must occur verbatim in the supplied paper
  text after the same folding (subscripts, minus signs, whitespace); the
  numbers, number-attached units and reference-electrode names of the
  ``text`` must occur in its quote.
* **Progress.** ``check_conditions(new)`` must have strictly fewer missing
  items than the old one; each ``added.condition`` must be one of the gap's
  missing names.
* **No new blocking lint code** (codes the old sentence already failed are
  not held against the proposal), and the reword belt
  (:func:`~precis.taproot.reword._post_validate`) runs with the paper text
  as evidence; checks the *old* sentence already failed against that text
  are ignored, since this pass does not own them.

Cohort: the reword cohort filters (live claim hubs, not hypothesis /
report / definition, not disputed, no publish row past ``candidate``, not
human-rejected) with ``check_conditions(title)`` non-empty instead of a
blocking lint failure. ``--family`` narrows to one profile family,
``--draft`` to the hubs a draft cites. A hub with no live pinned evidence
chunk is skipped (``no-evidence``). A frozen hub, or a refused write, is a
named status, never a crash.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from precis.nanopub.term_coverage import (
    TIER_METHODS,
    PaperChunk,
    chunk_tier,
    heading_sizes,
    paper_chunks,
)
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    NOT_NONEMPIRICAL_PREDICATE_PARAMS,
)
from precis.taproot.conditions import (
    _NOT_REPORTED_RE,
    PROFILES,
    ConditionGap,
    check_conditions,
    not_reported_marker,
)
from precis.taproot.hub import (
    TitleRoundTripError,
    human_rejected,
    refine_claim_sentence,
)
from precis.taproot.reword import (
    _CHECK_LINT,
    _COHORT_SQL,
    _DIGIT_RUN_RE,
    _blocking_codes,
    _canon_numbers,
    _post_validate,
)
from precis.utils.llm.router import LlmRequest, Tier, route

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "ConditionsCandidate",
    "HubConditions",
    "propose_conditions",
    "run_conditions_sweep",
    "select_conditions_cohort",
    "validate_add",
]

#: Character budget of the paper text shown to the model (grounding
#: passage first). Split evenly over at most :data:`_MAX_PAPERS` papers.
CONTEXT_CHARS = 6000
_MAX_PAPERS = 3
_PASSAGE_CHARS = 1500
_WINDOW_CHARS = 1200
_WINDOW_LEAD = 200
#: A quote shorter than this proves nothing.
_MIN_QUOTE_CHARS = 6

# ── cohort ───────────────────────────────────────────────────────────────

#: Restrict to hubs a draft cites (same ``cites`` edge as
#: ``repair_evidence._DRAFT_CLAUSE``, keyed on the hub row ``r``).
_DRAFT_CLAUSE = """AND EXISTS (
             SELECT 1 FROM links dl
              WHERE dl.src_ref_id = %(draft)s
                AND dl.relation = 'cites'
                AND dl.dst_ref_id = r.ref_id
           )"""

#: Every cohort hub's live pinned evidence chunks: the source paper, the
#: pinned chunk and its text, one row per edge (``reword._EVIDENCE_SQL``
#: plus the source ref).
_EVIDENCE_SQL = """
    SELECT l.dst_ref_id, l.src_ref_id, c.chunk_id, c.text
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
      JOIN chunks c ON c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL
     WHERE l.relation IN ('establishes', 'corroborates')
       AND l.src_chunk_id IS NOT NULL
       AND l.dst_ref_id = ANY(%(hubs)s)
     ORDER BY l.dst_ref_id, l.link_id
"""


@dataclass(frozen=True)
class ConditionsCandidate:
    """One cohort hub: its sentence and the gap :func:`check_conditions`
    found. ``evidence`` is ``((paper_ref_id, chunk_id, text), ...)``, the
    pinned grounding passages in edge order."""

    hub_ref_id: int
    sentence: str
    scope: dict[str, Any]
    gap: ConditionGap
    evidence: tuple[tuple[int, int, str], ...] = ()


@dataclass(frozen=True)
class HubConditions:
    """One hub's outcome, the JSONL row (:meth:`to_row`).

    ``status``: ``added`` / ``not-reported`` (a proposal that passed
    validation; ``applied`` says whether it was written), ``ambiguous``,
    ``rejected`` (validation failed), ``no-evidence``, ``llm-failed``,
    ``frozen`` (hub already signed), ``possible-duplicate`` (the new
    pub_id belongs to another hub), ``apply-failed``.
    """

    hub_ref_id: int
    old_sentence: str
    status: str
    family: str = ""
    missing: tuple[str, ...] = ()
    verdict: str | None = None
    new_sentence: str | None = None
    added: tuple[dict[str, str], ...] = ()
    checks_failed: tuple[str, ...] = ()
    reason: str | None = None
    applied: bool = False
    needs_review: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "hub": self.hub_ref_id,
            "old": self.old_sentence,
            "new": self.new_sentence,
            "family": self.family,
            "missing": list(self.missing),
            "verdict": self.verdict,
            "status": self.status,
            "added": list(self.added),
            "checks_failed": list(self.checks_failed),
            "reason": self.reason,
            "applied": self.applied,
            "needs_review": self.needs_review,
        }


def _evidence(
    store: Store, hub_ids: Sequence[int]
) -> dict[int, tuple[tuple[int, int, str], ...]]:
    if not hub_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(_EVIDENCE_SQL, {"hubs": list(hub_ids)}).fetchall()
    out: dict[int, list[tuple[int, int, str]]] = {}
    for hub, paper, chunk, text in rows:
        if text:
            out.setdefault(int(hub), []).append((int(paper), int(chunk), str(text)))
    return {k: tuple(v) for k, v in out.items()}


def select_conditions_cohort(
    store: Store,
    *,
    hub: int | None = None,
    limit: int | None = None,
    after_ref_id: int | None = None,
    draft_ref_id: int | None = None,
    family: str | None = None,
) -> list[ConditionsCandidate]:
    """Live claim hubs whose title has a conditions gap, ``ref_id`` order.

    The structural filters are :func:`~precis.taproot.reword.
    select_reword_cohort`'s; the value filters (gap, ``--family``,
    human-rejected memo, ``limit``) run in Python, ``limit`` last, so a
    limited run is a stable prefix of the filtered cohort."""
    params: dict[str, Any] = {
        **CLAIM_HUB_PREDICATE_PARAMS,
        **NOT_HYPOTHESIS_PREDICATE_PARAMS,
        **NOT_NONEMPIRICAL_PREDICATE_PARAMS,
    }
    clauses = []
    if hub is not None:
        clauses.append("AND r.ref_id = %(hub)s")
        params["hub"] = hub
    if draft_ref_id is not None:
        clauses.append(_DRAFT_CLAUSE)
        params["draft"] = draft_ref_id
    after_clause = ""
    if after_ref_id is not None:
        after_clause = "AND r.ref_id > %(after)s"
        params["after"] = after_ref_id
    sql = _COHORT_SQL.format(hub_clause="\n".join(clauses), after_clause=after_clause)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[ConditionsCandidate] = []
    for row in rows:
        meta = dict(row[2] or {})
        if human_rejected(meta):
            continue
        sentence = str(row[1] or "").strip()
        gaps = check_conditions(sentence)
        if not gaps:
            continue
        gap = gaps[0]
        if family is not None and gap.family != family:
            continue
        scope = {str(k): str(v) for k, v in (meta.get("scope") or {}).items()}
        out.append(
            ConditionsCandidate(
                hub_ref_id=int(row[0]), sentence=sentence, scope=scope, gap=gap
            )
        )
        if limit is not None and len(out) >= limit:
            break
    ev = _evidence(store, [c.hub_ref_id for c in out])
    return [
        ConditionsCandidate(
            c.hub_ref_id, c.sentence, c.scope, c.gap, ev.get(c.hub_ref_id, ())
        )
        for c in out
    ]


# ── folding ──────────────────────────────────────────────────────────────

_MINUS_TRANS = str.maketrans({"−": "-", "–": "-", "—": "-", "‐": "-", "‑": "-", "­": ""})
_WS_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """NFKC (``K₂SO₄`` to ``K2SO4``), unicode minus/dashes to ``-``,
    whitespace collapsed, casefolded. Applied to both sides of every
    comparison, so it only makes two spellings of one thing equal."""
    out = unicodedata.normalize("NFKC", text).translate(_MINUS_TRANS)
    return _WS_RE.sub(" ", out).strip().casefold()


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", _fold(text))


# ── paper context ────────────────────────────────────────────────────────

_EC_REF = r"vs\.? ?(?:RHE|SHE|NHE|Ag/AgCl|SCE|Hg/HgO)|Ag/AgCl|\bSCE\b|\bRHE\b|Hg/HgO"
#: Keyword picks per missing condition (the spec's lists), matched on the
#: folded chunk text; ``dft-*`` families get their own ``potential`` list.
_KEYWORDS: dict[str, re.Pattern[str]] = {
    n: re.compile(p, re.IGNORECASE)
    for n, p in {
        "potential": _EC_REF + r"|applied potential|\d\s*V\b",
        "reference": _EC_REF,
        "electrolyte": r"electrolyte|\d\s*M\b|KOH|H2SO4|Na2SO4|K2SO4|PBS|KHCO3|\bpH\b",
        "light": r"lamp|\bnm\b|AM ?1\.5|\bLED\b|\d\s*mW|\d\s*W\b|illuminat|irradiat|light",
        "temperature": r"°C|\bK\b|temperature|furnace|calcin",
    }.items()
}
_DFT_POTENTIAL = re.compile(
    r"\bU\s*=|computational hydrogen electrode|\bCHE\b|applied potential|0 V\b|"
    r"constant[- ]potential|limiting potential",
    re.IGNORECASE,
)


def _keyword_rx(family: str, missing: Sequence[str]) -> re.Pattern[str]:
    parts = []
    for name in missing:
        rx = (
            _DFT_POTENTIAL
            if name == "potential" and family.startswith("dft")
            else _KEYWORDS.get(name)
        )
        if rx is not None:
            parts.append(rx.pattern)
    return re.compile("|".join(parts) or r"(?!x)x", re.IGNORECASE)


def _window(text: str, rx: re.Pattern[str]) -> str:
    m = rx.search(_fold_keep_len(text))
    start = max(0, m.start() - _WINDOW_LEAD) if m else 0
    return text[start : start + _WINDOW_CHARS]


def _fold_keep_len(text: str) -> str:
    """NFKC without changing offsets is impossible, so match on the raw
    text with subscript digits mapped one-for-one."""
    return text.translate(
        str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹−", "01234567890123456789-")
    )


class _Picker:
    """Collects chunk texts up to a character budget, once per chunk."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0
        self.seen: set[int] = set()
        self.picked: list[str] = []

    def take(self, chunk_id: int, text: str) -> None:
        if chunk_id in self.seen or self.used >= self.budget:
            return
        self.seen.add(chunk_id)
        text = text[: self.budget - self.used]
        self.picked.append(text)
        self.used += len(text)


def build_context(
    store: Store, cand: ConditionsCandidate, *, budget: int = CONTEXT_CHARS
) -> str:
    """The paper text shown to the model and checked against its quotes:
    per evidence paper, the grounding passage(s) first, then the
    Methods/Experimental chunks and the chunks carrying condition keywords
    for the missing items (many papers carry only the title in
    ``section_path``, so Methods tier alone misses them), capped."""
    papers = list(dict.fromkeys(p for p, _, _ in cand.evidence))[:_MAX_PAPERS]
    if not papers:
        return ""
    per_paper = budget // len(papers)
    chunks, abstracts = paper_chunks(store, papers)
    sizes = heading_sizes(chunks)
    rx = _keyword_rx(cand.gap.family, cand.gap.missing)
    blocks: list[str] = []
    for i, paper in enumerate(papers, 1):
        pick = _Picker(per_paper)
        for p, cid, text in cand.evidence:
            if p == paper:
                pick.take(cid, text[:_PASSAGE_CHARS])
        ranked: list[tuple[int, int, int, PaperChunk]] = []
        for c in chunks:
            if c.ref_id != paper or c.chunk_id in pick.seen:
                continue
            tier = chunk_tier(
                kind=c.kind,
                section_path=c.section_path,
                ord_=c.ord,
                text=c.text,
                abstract=abstracts.get(c.ref_id),
                heading_chunks=sizes.get((c.ref_id, tuple(c.section_path)), 0),
            )
            score = len(rx.findall(_fold_keep_len(c.text)))
            is_methods = tier == TIER_METHODS
            if score or is_methods:
                # keyword hits in a methods chunk first, then hits
                # elsewhere, then methods chunks with none.
                rank = 0 if (score and is_methods) else 1 if score else 2
                ranked.append((rank, -score, c.ord, c))
        for _, _, _, c in sorted(ranked, key=lambda t: t[:3]):
            pick.take(c.chunk_id, _window(c.text, rx))
        blocks.append(f"[paper {i}]\n" + "\n\n".join(pick.picked))
    return "\n\n".join(blocks)


# ── the LLM call ─────────────────────────────────────────────────────────

_PROMPT = """\
You are completing a stored scientific claim sentence so an expert could
replicate it. The sentence is missing experimental or computational
conditions that its source paper states elsewhere (usually Methods).

CLAIM:
{sentence}

MISSING CONDITIONS ({family}):
{gaps}

PAPER TEXT (the only source you may use):
{paper_text}

Decide:
  - "add": the paper text states the missing condition(s) for THIS
    measurement or calculation. Return the claim with ONLY the missing
    conditions inserted (a short phrase such as "at -0.5 V vs RHE in
    0.5 M K2SO4"). Do not reword, reorder, drop or add anything else; keep
    every number and unit of the claim exactly. For each condition you
    add, give "condition" (one of the missing names above), "text" (the
    phrase you inserted), and "quote": a span copied CHARACTER FOR
    CHARACTER from the paper text above that states it. Every number,
    unit and reference electrode in "text" must appear in "quote".
    If the paper states SOME missing conditions but not others, still
    answer "add": insert only the stated ones (in "added") and list the
    names of the unstated ones in "not_reported"; a fixed marker for
    those is appended for you (do not write it yourself).
  - "not_reported": the paper text does not state the missing
    condition(s) at all. Return no sentence; a fixed marker is added for
    you.
  - "ambiguous": the paper reports several different values (several
    potentials, electrolytes, lamps...) and the claim does not say which
    applies, or the claim mixes experiments. Change nothing.

Never guess a condition. A value only counts if the paper text states it
for the quantity the claim reports.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "add" | "not_reported" | "ambiguous",
  "sentence": "<the claim with the conditions inserted>" | null,
  "added": [{{"condition": "<missing name>", "text": "<inserted phrase>", "quote": "<verbatim span>"}}],
  "not_reported": ["<missing name the paper does not state>"],
  "reason": "<one short sentence>"
}}
"""

#: The injectable LLM seam: ``(candidate, paper_text)`` to the parsed JSON
#: dict, or ``None`` on dispatch failure.
ProposeFn = Callable[[ConditionsCandidate, str], dict[str, Any] | None]


def _gap_lines(gap: ConditionGap) -> str:
    hints = {g.name: g.hint for p in PROFILES for g in p.groups}
    return "\n".join(f"  - {n}: {hints.get(n, n)}" for n in gap.missing)


def propose_conditions(
    cand: ConditionsCandidate, paper_text: str
) -> dict[str, Any] | None:
    """One MEDIUM-tier proposal; ``None`` on dispatch failure (recorded as
    ``llm-failed``; a model that never ran is never a verdict)."""
    prompt = _PROMPT.format(
        sentence=cand.sentence,
        family=cand.gap.family,
        gaps=_gap_lines(cand.gap),
        paper_text=paper_text,
    )
    res = route(
        LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="taproot:conditions-repair")
    )
    if res.error:
        log.warning("taproot-conditions: proposal failed: %s", res.error)
        return None
    return res.data


# ── post-validation ──────────────────────────────────────────────────────

#: Words the model may add or move without it counting as rewording.
_CONNECTIVES = frozenset(
    [
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "under",
        "using",
        "via",
        "vs",
        "with",
        "was",
        "were",
        "is",
        "are",
        "as",
        "from",
        "during",
        "after",
        "before",
        "when",
        "while",
    ]
)
_REF_RE = re.compile(r"ag/agcl|hg/hgo|\bs?sce\b|\brhe\b|\bshe\b|\bnhe\b")
_NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zµμ%°ω][\w%°./-]*)")
_MAX_INSERT_CHARS = 200


def _missing_count(sentence: str) -> int:
    return sum(len(g.missing) for g in check_conditions(sentence))


_SIGNED_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _quote_supports(text: str, quote: str) -> str | None:
    """Why ``text`` is not backed by ``quote`` (``None`` when it is): every
    content word of ``text`` occurs in the quote, every number occurs with
    its sign, each number-attached unit matches whole, and each reference
    electrode is named. The model must copy the paper, not paraphrase it."""
    t, q = _fold(_canon_numbers(text)), _fold(_canon_numbers(quote))
    q_words = set(re.findall(r"\w+", q))
    for word in re.findall(r"\w+", t):
        if word not in _CONNECTIVES and word not in q_words:
            return f"word {word!r} of {text!r} is not in its quote"
    q_nums = set(_SIGNED_NUM_RE.findall(q))
    for num in _SIGNED_NUM_RE.findall(t):
        if num not in q_nums:
            return f"number {num!r} of {text!r} is not in its quote (sign included)"
    q_runs = set(_DIGIT_RUN_RE.findall(q))
    for run in _DIGIT_RUN_RE.findall(t):
        if run not in q_runs:
            return f"number {run!r} of {text!r} is not in its quote"
    for num, unit in _NUM_UNIT_RE.findall(t):
        unit = unit.rstrip(".,;:)/-")
        if not unit:
            continue
        pat = r"(?<![\d.])" + re.escape(num) + r"\s*" + re.escape(unit) + r"(?!\w)"
        if not re.search(pat, q):
            return f"{num} {unit} of {text!r} is not in its quote"
    for ref in _REF_RE.findall(t):
        if ref not in q:
            return f"reference electrode {ref!r} of {text!r} is not in its quote"
    return None


def _insert_only(old: str, new: str, allowed: set[str]) -> str | None:
    """``None`` when ``new`` is ``old`` plus contiguous insertions (the old
    token SEQUENCE survives; inserted tokens are ``allowed``); otherwise why
    not. Connective-only deletions/replacements are tolerated."""
    a, b = _tokens(old), _tokens(new)
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        gone, came = a[i1:i2], b[j1:j2]
        if any(t not in _CONNECTIVES for t in gone):
            return "old word(s) dropped, changed or reordered: " + " ".join(gone)
        if any(t not in allowed for t in came):
            return "word(s) not from added text: " + " ".join(came)
    return None


def validate_add(
    old: str,
    new: str,
    added: Sequence[dict[str, str]],
    paper_text: str,
    *,
    missing: Sequence[str],
    unstated: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """Every in-code check on an ``add`` (or code-built ``not_reported``)
    proposal. Returns ``(checks_failed, details)``, both empty when it is
    writable. Never trusts the model: see the module docstring.

    ``unstated`` are the conditions the code (not the model) marks "not
    reported"; the only marker ``new`` may carry is exactly
    ``not_reported_marker(unstated)``."""
    checks: list[str] = []
    details: list[str] = []

    def fail(check: str, detail: str) -> None:
        checks.append(check)
        details.append(detail)

    haystack = _fold(paper_text)
    added_tokens: set[str] = set()
    for item in added:
        cond = str(item.get("condition") or "").strip().lower()
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if cond not in missing:
            fail("unknown-condition", f"{cond!r} is not a missing condition")
        added_tokens.update(_tokens(text))
        if len(_fold(quote)) < _MIN_QUOTE_CHARS or _fold(quote) not in haystack:
            fail("quote-not-in-paper", f"quote for {cond!r} is not in the paper text")
            continue
        why = _quote_supports(text, quote)
        if why:
            fail("quote-mismatch", why)

    # the marker: exactly the code-built one, or none
    expected = not_reported_marker(unstated) if unstated else None
    found = [m.group(0) for m in _NOT_REPORTED_RE.finditer(new)]
    if found != ([expected] if expected else []):
        fail("bad-marker", f"marker(s) {found!r}, expected {expected!r}")
    body = new.replace(expected, "") if expected else new

    # insertion-only: the old token sequence survives, only contiguous insertions
    why = _insert_only(old, body, added_tokens | _CONNECTIVES)
    if why:
        fail("rewrites-old", why)
    if len(new) - len(old) > _MAX_INSERT_CHARS:
        fail("too-long-insertion", f"inserts {len(new) - len(old)} characters")

    # progress
    if _missing_count(new) >= _missing_count(old):
        fail(
            "no-progress",
            f"missing conditions {_missing_count(old)} -> {_missing_count(new)}",
        )

    # the reword belt, judged only on what the old sentence did not already fail
    base_checks, _ = _post_validate(old, old, evidence=(paper_text,))
    belt, belt_details = _post_validate(old, new, evidence=(paper_text,))
    for check, detail in zip(belt, belt_details, strict=False):
        if check == _CHECK_LINT or check in base_checks:
            continue
        fail(check, detail)
    gained = [c for c in _blocking_codes(new) if c not in _blocking_codes(old)]
    if gained:
        fail(_CHECK_LINT, f"new blocking lint code(s): {', '.join(gained)}")
    return checks, details


def _keyword_hits(
    store: Store, cand: ConditionsCandidate, names: Sequence[str]
) -> list[str]:
    """Conditions of ``names`` whose keyword regex hits ANY live body chunk
    of ANY evidence paper (not just the capped context)."""
    papers = list(dict.fromkeys(p for p, _, _ in cand.evidence))
    chunks, _ = paper_chunks(store, papers)
    hits = []
    for name in names:
        rx = _keyword_rx(cand.gap.family, [name])
        if any(rx.search(_fold_keep_len(c.text)) for c in chunks):
            hits.append(name)
    return hits


def _with_marker(old: str, names: Sequence[str]) -> str:
    """``old`` with the "not reported" marker before its terminal period."""
    body = old.rstrip()
    stem = body[:-1] if body.endswith(".") else body
    return f"{stem} {not_reported_marker(names)}."


# ── the sweep ────────────────────────────────────────────────────────────


def _clean_added(raw: Any) -> tuple[dict[str, str], ...]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            out.append(
                {k: str(item.get(k) or "") for k in ("condition", "text", "quote")}
            )
    return tuple(out)


def _conditions_one(
    store: Store, cand: ConditionsCandidate, fn: ProposeFn, *, apply: bool
) -> tuple[HubConditions, int]:
    """One hub: ``(outcome, llm_calls)``. Never raises on a model failure
    or a refused write."""

    def row(status: str, **kw: Any) -> HubConditions:
        return HubConditions(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status=status,
            family=cand.gap.family,
            missing=cand.gap.missing,
            **kw,
        )

    paper_text = build_context(store, cand)
    if not paper_text.strip():
        return row("no-evidence", reason="no live pinned evidence chunk to read"), 0
    data = fn(cand, paper_text)
    if not isinstance(data, dict):
        return row("llm-failed", reason="LLM call failed or returned no JSON"), 1
    verdict = str(data.get("verdict") or "").strip().lower()
    reason = str(data.get("reason") or "").strip() or None
    added = _clean_added(data.get("added"))
    if verdict == "ambiguous":
        return row("ambiguous", verdict=verdict, reason=reason), 1
    unstated: list[str] = []
    if verdict == "not_reported":
        unstated = list(cand.gap.missing)
        new = cand.sentence
        added = ()
        status = "not-reported"
    elif verdict == "add":
        new = str(data.get("sentence") or "").strip()
        status = "added"
        raw = data.get("not_reported")
        unstated = [
            n
            for n in dict.fromkeys(
                str(x).strip().lower() for x in (raw if isinstance(raw, list) else [])
            )
            if n in cand.gap.missing
        ]
        if not new or not added:
            return (
                row(
                    "rejected",
                    verdict=verdict,
                    added=added,
                    new_sentence=new or None,
                    checks_failed=("malformed",),
                    reason="add verdict without a sentence and added items",
                ),
                1,
            )
    else:
        return row("llm-failed", reason=f"malformed response (verdict={verdict!r})"), 1

    if unstated:
        # "not reported" is checked in code, never trusted: a keyword hit
        # anywhere in the evidence papers means a human looks.
        hit = _keyword_hits(store, cand, unstated)
        if hit:
            return (
                row(
                    "ambiguous",
                    verdict=verdict,
                    reason=f"paper mentions {', '.join(hit)} terms; human check",
                ),
                1,
            )
        new = _with_marker(new, unstated)

    checks, details = validate_add(
        cand.sentence,
        new,
        added,
        paper_text,
        missing=cand.gap.missing,
        unstated=unstated,
    )
    if checks:
        return (
            row(
                "rejected",
                verdict=verdict,
                new_sentence=new,
                added=added,
                checks_failed=tuple(dict.fromkeys(checks)),
                reason="; ".join(details),
            ),
            1,
        )
    common: dict[str, Any] = {
        "verdict": verdict,
        "new_sentence": new,
        "added": added,
        "reason": reason,
    }
    if not apply:
        return row(status, **common), 1
    try:
        res = refine_claim_sentence(
            store, cand.hub_ref_id, new, set_by="conditions-sweep"
        )
    except (ValueError, TitleRoundTripError) as exc:
        # HubFrozenError is a ValueError; so is the pub_id collision.
        from precis.taproot.hub import HubFrozenError

        msg = str(exc)
        if isinstance(exc, HubFrozenError):
            kind = "frozen"
        elif "dedup/merge candidate" in msg:
            kind = "possible-duplicate"
        else:
            kind = "apply-failed"
        return row(kind, **{**common, "reason": msg}), 1
    return (
        row(
            status,
            applied=True,
            needs_review=len(res.get("needs_review") or []),
            **common,
        ),
        1,
    )


def run_conditions_sweep(
    store: Store,
    *,
    apply: bool = False,
    limit: int | None = None,
    hub: int | None = None,
    after_ref_id: int | None = None,
    draft_ref_id: int | None = None,
    family: str | None = None,
    out: str | Path | IO[str] | None = None,
    propose_fn: ProposeFn | None = None,
) -> dict[str, Any]:
    """Select the cohort, propose + validate per hub, and (``apply=True``
    only) write through the retitle door. Dry-run (default) writes nothing.

    Returns ``{"cohort", "processed", "applied", "llm_calls", "counts",
    "apply", "out", "last_ref_id"}`` (``counts``: status to hubs)."""
    cands = select_conditions_cohort(
        store,
        hub=hub,
        limit=limit,
        after_ref_id=after_ref_id,
        draft_ref_id=draft_ref_id,
        family=family,
    )
    fn = propose_fn or propose_conditions
    results: list[HubConditions] = []
    calls = 0
    # One flushed row per hub, so a crash mid --apply keeps the record.
    with ExitStack() as stack:
        sink: IO[str] | None
        if isinstance(out, str | Path):
            sink = stack.enter_context(Path(out).open("w", encoding="utf-8"))
        else:
            sink = out
        for cand in cands:
            res, n = _conditions_one(store, cand, fn, apply=apply)
            results.append(res)
            calls += n
            if sink is not None:
                sink.write(json.dumps(res.to_row(), ensure_ascii=False) + "\n")
                sink.flush()
    out_path = str(out) if isinstance(out, str | Path) else None
    return {
        "cohort": len(cands),
        "processed": len(results),
        "applied": sum(1 for r in results if r.applied),
        "llm_calls": calls,
        "counts": dict(sorted(Counter(r.status for r in results).items())),
        "apply": apply,
        "out": out_path,
        "last_ref_id": max((c.hub_ref_id for c in cands), default=None),
    }
