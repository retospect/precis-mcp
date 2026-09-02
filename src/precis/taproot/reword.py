"""LLM batch reword for claim hubs blocked by sentence lints.

The 2026-08-27 corpus audit found 356 of 1,490 live claim hubs failing
ONLY the blocking sentence lints (``no-epistemic-mode`` 578 and
``no-evidence-verb`` 511 dominate; spot-checks were true positives).
Approve stays reword-on-demand — a hub gets its grammar fixed when
someone actually wants to publish it — and this pass is the batch door
for paying that debt down *ahead* of demand. It is the LLM half of the
lint-debt split: ``precis taproot lint --fix``
(:func:`~precis.taproot.notation.normalize_notation`) owns the
deterministically-fixable notation codes; this pass owns the
admissibility codes no mechanical rewrite can fix (name the method,
name an evidence verb, one assertion). Non-goal: changing what the
gates enforce — this pays debt under the current bar.

Cohort (:func:`select_reword_cohort`): live strict claim hubs — a
``finding`` carrying unexpired ``TAPROOT:claim`` + ``STATUS:canonical``
tags — whose ``refs.title`` fails any blocking lint code
(:func:`~precis.nanopub.gates.check_claim_sentence`, which applies
``gates._BLOCKING_LINT_CODES`` and the artifact-type exemptions).
Excluded, each for its own reason:

* **hypothesis hubs** (``refs.meta.artifact_type = 'hypothesis'``, the
  same predicate the widening pass uses) — exempt from the epistemic
  pair by type; rewording one to "name the method that showed this"
  would manufacture exactly the false attribution the exemption exists
  to prevent.
* **disputed hubs** (a live inbound ``contradicts`` edge in ``links``)
  — never reword mid-dispute; adjudication is by artifacts, not edits.
* **hubs with any ``nanopub_publish`` row past ``candidate``** —
  ``reviewed``/``signed``/onward froze the claim sha (a wording change
  there is a re-review or a supersede, per ``nanopub/mint.py``), and a
  terminal ``rejected``/``retracted``/``superseded`` row is review
  history a batch pass must not write over.
* **hubs with a non-empty ``meta.taproot_rejected`` memo** — the mint
  gates refuse such a hub outright (``rejected-memo``), so polishing
  its wording buys nothing; truthiness matches the gate's read exactly
  (an empty ``{}`` memo left by a sha-reopen does not exclude).

Per hub, ONE MEDIUM-tier call (:func:`propose_reword`) proposes the
rewrite. **The model is not trusted**: before any write the proposal is
re-validated in code (:func:`_post_validate`) — it must pass the same
blocking-lint filter, preserve every numeric/unit token of the original
(digit runs compared grouping-insensitively, unit attachment via
:func:`~precis.utils.numerics.extract_numerics`), introduce no citation
marker the original lacked (``[N]``, ``et al.``, parenthetical
author-year), and fit the over-long budget. Any failure skips the hub,
counted and reported with the named checks. NO-REWORD is an expected
verdict, not a failure: a definition or a "study happened" report is
not a claim, and rewording it into one would invent a finding.

Those four checks all compare the proposal to the *previous sentence* —
self-consistency, never grounding. Two more (2026-09-01) compare it to
the hub's own pinned evidence passages, which the cohort query now
carries:

* **numeric grounding** (blocking, :data:`_CHECK_GROUNDING`) — every
  quantity-shaped digit run in the proposal must appear somewhere in a
  pinned passage. Motivating defect: a nanobud hub whose sentence said
  "an opening angle of approximately 19°" while its own paper says
  ≈20° — the 19° was carried over from a *different* paper's hub, and
  the old→new preservation check dutifully protected it. Quantity shape
  (:data:`_QUANTITY_RE`) is deliberately narrower than the preservation
  check's digit run: a run touching a word character or a hyphen is
  nomenclature (``6-311G``, ``C60``, ``sp2``, ``B3LYP``), not a
  measurement, and grounding it against prose would re-run the
  ``hyphen-numeric-range`` false-positive treadmill.
* **mode grounding** (advisory, :attr:`HubReword.warnings`) — every
  specific epistemic-mode token the proposal names
  (:func:`~precis.taproot.sentence_lint.find_epistemic_modes`) should
  appear in a pinned passage. This guards the failure the lint itself
  *incentivises*: ``no-epistemic-mode`` demands a method, so the model
  is under pressure to supply one it cannot see. Advisory on purpose —
  the passage-side match is a word-prefix stem (so the passage's "cone
  wall model" grounds the claim's "modelling"), which is loose in both
  directions and has had no corpus dry run; promote it to blocking only
  once one has measured its false-positive rate, the discipline
  :data:`~precis.taproot.sentence_lint.EPISTEMIC_MODE_TOKENS` itself
  was grown under.

A hub with no live pinned passage at all is not silently exempt: both
checks no-op and the row carries a ``no pinned evidence`` warning, so
the report never reads as "grounded" when nothing was checked.

"Grounding" here is claim-against-its-passages, a different axis from
:mod:`precis.taproot.grounding`, which asks whether a *chunk* is body
prose at all. Neither subsumes the other, and neither is a substitute
for :mod:`~precis.taproot.verify_edges`: whether a passage actually
*supports* a claim is a semantic judgment no regex reaches. These two
catch the mechanical half — a number or a method the passage does not
contain — which is precisely the half an LLM proposal fabricates.

Apply (``apply=True``; dry-run is the default and writes nothing) goes
through :func:`precis.taproot.hub.refine_claim_sentence` — the single
retitle door, so ``refs.title``, the ``finding_body`` chunk, and the
``pub_id`` alias stay in sync. A per-hub JSONL report (``out=``)
mirrors :mod:`~precis.taproot.repair_evidence`'s row-per-outcome shape.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from precis.nanopub.gates import check_claim_sentence
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    not_hypothesis_predicate_sql,
)
from precis.taproot.hub import refine_claim_sentence
from precis.taproot.sentence_lint import (
    _OVER_LONG_CHARS,
    GENERIC_EPISTEMIC_HEADS,
    find_epistemic_modes,
)
from precis.utils.llm.router import LlmRequest, Tier, route
from precis.utils.numerics import extract_numerics

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "HubReword",
    "RewordCandidate",
    "propose_reword",
    "run_reword_sweep",
    "select_reword_cohort",
]

#: One hub's outcome. ``reworded`` is the only status that ever writes
#: (and only with ``apply=True`` — ``applied`` distinguishes a dry-run
#: proposal from a written one). ``no-reword`` is the model's expected
#: "this is not a rewordable claim" verdict. ``rejected`` names a
#: post-validation failure (see ``HubReword.checks_failed``).
#: ``llm-failed`` is a dead or malformed dispatch; ``apply-failed`` is
#: :func:`~precis.taproot.hub.refine_claim_sentence` refusing the write
#: (e.g. the new pub_id already belongs to a different live ref — a
#: dedup/merge candidate, a human call).
RewordStatus = Literal[
    "reworded",
    "no-reword",
    "rejected",
    "llm-failed",
    "apply-failed",
]

#: ``HubReword.checks_failed`` vocabulary, in check order.
_CHECK_LINT = "lint"
_CHECK_NUMERIC = "numeric"
_CHECK_CITATION = "citation"
_CHECK_OVER_LONG = "over-long"
_CHECK_GROUNDING = "numeric-grounding"


@dataclass(frozen=True)
class RewordCandidate:
    """One cohort member: the hub, its failing sentence (``refs.title``),
    its ``meta.scope``, the blocking lint codes that admitted it, and the
    text of its live pinned evidence passages (``evidence``, empty when
    the hub has none — see :func:`_grounding_warnings`)."""

    hub_ref_id: int
    sentence: str
    scope: dict[str, Any]
    lint_codes: tuple[str, ...]
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class HubReword:
    """One hub's sweep outcome — the JSONL row (:meth:`to_row`) carries
    proposal and verdict alike, so a dry run is a reviewable artifact."""

    hub_ref_id: int
    old_sentence: str
    status: RewordStatus
    new_sentence: str | None = None
    lint_codes: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str | None = None
    applied: bool = False

    def to_row(self) -> dict[str, Any]:
        """The JSONL row — proposal (dry-run) or record (``apply``)."""
        return {
            "hub": self.hub_ref_id,
            "old": self.old_sentence,
            "new": self.new_sentence,
            "status": self.status,
            "lint_codes": list(self.lint_codes),
            "checks_failed": list(self.checks_failed),
            "warnings": list(self.warnings),
            "reason": self.reason,
            "applied": self.applied,
        }


# ── cohort selection ────────────────────────────────────────────────────

#: The "not a conjecture" clause (shared with hub_refine/chase_trigger).
_NOT_HYPOTHESIS_SQL = not_hypothesis_predicate_sql()

#: Structural cohort filters. The two tag ``EXISTS`` clauses are
#: :func:`~precis.taproot.canon.claim_hub_predicate_sql` PLUS the
#: ``rt.expires_at`` guard that helper deliberately omits (its hot dedup
#: path skips it; a corpus sweep must not count an expired tag — the
#: same guard ``taproot lint``'s cohort query carries). The value-shaped
#: filters (blocking-lint failure, the ``taproot_rejected`` truthiness)
#: run in Python — SQL cannot evaluate the linter, and jsonb truthiness
#: (non-empty object vs the ``{}`` a sha-reopen leaves) is exactly the
#: Python ``bool()`` the mint gate applies.
_COHORT_SQL = f"""
    SELECT r.ref_id, r.title, r.meta
      FROM refs r
     WHERE r.kind = 'finding'
       AND r.retired_at IS NULL
       AND EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = r.ref_id
                AND t.namespace = %(taproot_ns)s AND t.value = %(taproot_claim)s
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
           )
       AND EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = r.ref_id
                AND t.namespace = %(status_ns)s AND t.value = %(status_canonical)s
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
           )
       AND {_NOT_HYPOTHESIS_SQL}
       AND NOT EXISTS (
             SELECT 1 FROM links l
             JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
              WHERE l.dst_ref_id = r.ref_id AND l.relation = 'contradicts'
           )
       AND NOT EXISTS (
             SELECT 1 FROM nanopub_publish np
              WHERE np.claim_ref_id = r.ref_id AND np.state <> 'candidate'
           )
       {{hub_clause}}
     ORDER BY r.ref_id
"""


#: Every cohort hub's live pinned evidence passages, one row per edge.
#: Same relations and pin requirement as ``verify_edges._COHORT_SQL`` --
#: ``contradicts`` is excluded there because a sweep must not certify a
#: dispute, and here because a contradicting passage is the *last* text a
#: claim should be allowed to source its numbers from. Support status is
#: deliberately NOT filtered: grounding asks "does the paper say this
#: number", which a stripped or never-verified edge answers just as well.
_EVIDENCE_SQL = """
    SELECT l.dst_ref_id, c.text
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
      JOIN chunks c ON c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL
     WHERE l.relation IN ('establishes', 'corroborates')
       AND l.src_chunk_id IS NOT NULL
       AND l.dst_ref_id = ANY(%(hubs)s)
     ORDER BY l.dst_ref_id, l.link_id
"""


def _evidence_by_hub(
    store: Store, hub_ref_ids: Sequence[int]
) -> dict[int, tuple[str, ...]]:
    """``{hub_ref_id: (passage_text, ...)}`` for the given hubs. Hubs with
    no live pinned passage are absent, not empty -- the caller distinguishes
    "checked and grounded" from "nothing to check against"."""
    if not hub_ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(_EVIDENCE_SQL, {"hubs": list(hub_ref_ids)}).fetchall()
    out: dict[int, list[str]] = {}
    for hub_ref_id, text in rows:
        if text:
            out.setdefault(int(hub_ref_id), []).append(str(text))
    return {k: tuple(v) for k, v in out.items()}


def _blocking_codes(sentence: str) -> tuple[str, ...]:
    """The sentence's failing blocking lint codes, first-hit order —
    :func:`~precis.nanopub.gates.check_claim_sentence` under the strict
    ``claim`` artifact type (hypotheses never reach this: the cohort SQL
    excludes them)."""
    codes: list[str] = []
    for violation in check_claim_sentence(sentence, artifact_type="claim"):
        code = violation.message.split(":", 1)[0].strip()
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def select_reword_cohort(
    store: Store, *, hub: int | None = None, limit: int | None = None
) -> list[RewordCandidate]:
    """The rewordable cohort, ``ref_id`` order.

    ``hub`` restricts to one hub (it still has to qualify); ``limit``
    applies in Python, **after** the lint and rejected-memo filters, so
    a limited run is a stable prefix of the filtered cohort rather than
    of the candidate scan (the :func:`~precis.taproot.repair_evidence.
    select_prose_less_evidence_edges` precedent)."""
    params: dict[str, Any] = {
        **CLAIM_HUB_PREDICATE_PARAMS,
        **NOT_HYPOTHESIS_PREDICATE_PARAMS,
    }
    hub_clause = ""
    if hub is not None:
        hub_clause = "AND r.ref_id = %(hub)s"
        params["hub"] = hub
    sql = _COHORT_SQL.format(hub_clause=hub_clause)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[RewordCandidate] = []
    for row in rows:
        meta = dict(row[2] or {})
        if meta.get("taproot_rejected"):
            continue
        sentence = str(row[1] or "").strip()
        codes = _blocking_codes(sentence)
        if not codes:
            continue
        scope = {str(k): str(v) for k, v in (meta.get("scope") or {}).items()}
        out.append(
            RewordCandidate(
                hub_ref_id=int(row[0]),
                sentence=sentence,
                scope=scope,
                lint_codes=codes,
            )
        )
        if limit is not None and len(out) >= limit:
            break
    evidence = _evidence_by_hub(store, [c.hub_ref_id for c in out])
    return [replace(cand, evidence=evidence.get(cand.hub_ref_id, ())) for cand in out]


# ── the LLM call ────────────────────────────────────────────────────────

_PROMPT_REWORD = """\
You are rewording a stored scientific claim sentence so it passes a
mechanical admissibility lint, WITHOUT changing what it claims.

SENTENCE:
{sentence}

SCOPE (structured context; may name the method or material):
{scope_json}

FAILING LINT CODES: {lint_codes}

Rewrite the sentence into admissible claim shape:
  - Name the method/technique that established the finding (the
    epistemic mode: DFT, TEM, nanoindentation, "transport
    measurements", ...) and use a controlled evidence verb: one of
    predicts / finds / shows / measures / observes / demonstrates /
    calculates / estimates / reveals / confirms / identifies /
    indicates.
  - Exactly ONE assertion: no ", and" / "; " clause joins, no em dash.
  - Simple present tense, active voice, terminal period, at most
    {max_chars} characters.
  - PRESERVE VERBATIM every quantity, unit, material name, and scope
    condition of the original. Never add, drop, round, or convert a
    number or a unit.
  - Add NO citation: no [N] markers, no "et al.", no author-year.
  - Invent NOTHING. If neither the sentence nor the scope names the
    method, answer NO-REWORD rather than guess one.

Answer NO-REWORD when the sentence is not a rewordable claim: a
definition, a bibliography entry, "a study happened" prose that states
no finding, or a claim whose method is stated nowhere.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "reword" | "no-reword",
  "sentence": "<the rewritten sentence>" | null,
  "reason": "<one short sentence>"
}}
"""

#: The injectable LLM seam — ``(sentence, scope, lint_codes)`` to the
#: parsed JSON dict, or ``None`` on failure (the ``_chase_llm`` contract).
ProposeFn = Callable[[str, dict[str, Any], Sequence[str]], dict[str, Any] | None]


def propose_reword(
    sentence: str, scope: dict[str, Any], lint_codes: Sequence[str]
) -> dict[str, Any] | None:
    """One MEDIUM-tier reword proposal. Returns the parsed JSON dict or
    ``None`` on dispatch failure — the caller records ``llm-failed`` and
    moves on; a model that never ran is never a verdict."""
    prompt = _PROMPT_REWORD.format(
        sentence=sentence,
        scope_json=json.dumps(scope, sort_keys=True),
        lint_codes=", ".join(lint_codes) or "(none)",
        max_chars=_OVER_LONG_CHARS,
    )
    res = route(LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="taproot:reword"))
    if res.error:
        log.warning("taproot-reword: reword hook failed: %s", res.error)
        return None
    return res.data


# ── post-validation (belt over the LLM) ─────────────────────────────────

#: Unicode superscript digits/minus folded to ASCII, and grouping
#: separators between digits stripped, before digit runs are compared —
#: so a mechanically-renotated number (``10,000`` vs ``10 000``) still
#: counts as preserved while a dropped or rounded one never does.
_SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_GROUP_SEP_RE = re.compile(r"(?<=\d)[,\s\u00a0\u2009\u202f](?=\d)")
_DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")

#: Citation-marker shapes the reword must not introduce. The ``[N]``
#: form requires a non-alphanumeric follower so chemical nomenclature
#: like ``[2]pseudorotaxane`` never trips it; all three are additionally
#: excused when the exact marker text already appears in the original
#: (the belt bans *introducing* a citation, not preserving a token).
_CITATION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\d+(?:\s*[,;–—-]\s*\d+)*\](?![A-Za-z0-9])"),
    re.compile(r"\bet\s+al\b\.?", re.IGNORECASE),
    re.compile(
        r"\(\s*[A-Z][\w'’-]+(?:\s+(?:and|&)\s+[A-Z][\w'’-]+)?"
        r"(?:\s+et\s+al\.?)?,?\s+(?:19|20)\d\d[a-z]?\s*\)"
    ),
)


def _canon_numbers(text: str) -> str:
    return _GROUP_SEP_RE.sub("", text.translate(_SUPERSCRIPT_TRANS))


def _missing_numeric_tokens(old: str, new: str) -> list[str]:
    """Numeric material of ``old`` absent from ``new``: every digit run
    (grouping-insensitive), plus every ``<number> <unit>`` token
    (:func:`~precis.utils.numerics.extract_numerics` — unit attachment
    must survive, not just the digits)."""
    old_c, new_c = _canon_numbers(old), _canon_numbers(new)
    new_runs = set(_DIGIT_RUN_RE.findall(new_c))
    missing = [
        run
        for run in dict.fromkeys(_DIGIT_RUN_RE.findall(old_c))
        if run not in new_runs
    ]
    new_units = set(extract_numerics(new_c))
    missing += [
        tok
        for tok in extract_numerics(old_c)
        if tok not in new_units and tok not in missing
    ]
    return missing


def _new_citation_markers(old: str, new: str) -> list[str]:
    markers = [m.group(0) for rx in _CITATION_RES for m in rx.finditer(new)]
    return [m for m in dict.fromkeys(markers) if m not in old]


#: A digit run that reads as a *quantity*: bounded on both sides by
#: something that is neither a word character, a hyphen, nor a decimal
#: point. Deliberately narrower than :data:`_DIGIT_RUN_RE` (which the
#: old-to-new preservation check uses, and which must stay greedy):
#:
#: * a run touching a letter or a hyphen is nomenclature -- ``6-311G``,
#:   ``C60``, ``sp2``, ``B3LYP``, ``Fe-ZSM-5`` -- and demanding a passage
#:   restate it is the ``hyphen-numeric-range`` treadmill in a new place;
#: * the ``.`` in both guards stops the optional fraction backtracking
#:   into a bogus run: without it ``0.7-1.3`` yields ``"0"`` (greedy
#:   ``0.7`` fails the hyphen guard, then ``0`` passes it) and ``"3"``,
#:   neither of which any passage would contain. 2026-09-01 corpus dry
#:   run: this pair was ~a third of the blocking hits.
_QUANTITY_RE = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.-])")

#: Superscript/subscript digits are DROPPED, not folded to ASCII, before
#: grounding. :func:`_canon_numbers` folds them (right for old-vs-new,
#: where both sides fold identically and cancel), but here the fold is
#: asymmetric and invents tokens: ``~100x (10^2)`` became ``"102"`` and
#: ``3 x 10^4`` became ``"104"``, matched nothing, and blocked the hub.
#: Dropping instead leaves ``10``, which the passage's mantissa grounds --
#: exponent surface forms (``10^-6``, ``1e-6``, ``x 10 -6``) vary too
#: widely between claim and passage to compare at all.
_SUPERSCRIPT_DIGITS = str.maketrans("", "", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻₀₁₂₃₄₅₆₇₈₉")

#: The mantissa ``10`` of scientific notation goes with its exponent. A
#: claim's ``10¹¹ bits/cm²`` left a bare ``10`` that its own passage --
#: which writes the value out as ``100,000,000,000`` -- does not contain.
#: Nothing is grounded by the literal ten in ``10^n``, so drop it.
_SCI_TEN_RE = re.compile(r"10(?=[⁰¹²³⁴⁵⁶⁷⁸⁹⁻])")

#: Trailing fractional zeros carry no information and papers are not
#: consistent about them: a claim's ``0.7`` must ground on a passage's
#: ``0.70``, and ``4.0`` on ``4``. Applied after separators are stripped,
#: so ``0.700`` -> ``0.7`` and ``4.0`` -> ``4``.
_TRAILING_FRAC_ZERO_RE = re.compile(r"(?<=\d)(\.\d*?)0+\b")
_BARE_POINT_RE = re.compile(r"(?<=\d)\.(?!\d)")


def _canon_grounding(text: str) -> str:
    """Grounding-side number canon: grouping separators stripped (so a
    passage's ``10,000`` grounds a claim's ``10 000``), scientific-notation
    mantissas and super/subscripts dropped, trailing fractional zeros
    normalized away. Both sides go through it, so it only ever makes two
    spellings of the same value compare equal."""
    out = _GROUP_SEP_RE.sub("", text)
    out = _SCI_TEN_RE.sub("", out).translate(_SUPERSCRIPT_DIGITS)
    return _BARE_POINT_RE.sub("", _TRAILING_FRAC_ZERO_RE.sub(r"\1", out))


#: Word-prefix length a mode token is stemmed to before the passage is
#: searched, so an inflection gap is not a miss ("modelling" grounds on
#: "cone wall model", "measurements" on "measured"). Short tokens (the
#: acronyms) are searched whole.
_MODE_STEM_CHARS = 5

#: The advisory-warning texts, so the caller can match on a stable prefix.
_WARN_NO_EVIDENCE = "no pinned evidence: grounding unchecked"
_WARN_UNGROUNDED_MODE = "epistemic mode absent from every pinned passage"


def _ungrounded_quantities(new: str, evidence: Sequence[str]) -> list[str]:
    """Quantity-shaped digit runs of ``new`` that no pinned passage
    contains. Empty ``evidence`` returns ``[]`` -- nothing to check
    against is reported as a warning, never as a failure.

    The passage side matches on ANY digit run, not just quantity-shaped
    ones, so a claim's ``150`` grounds on a passage's ``150 Ohm/sq`` and
    on its ``Fig. 150`` alike. That asymmetry is on purpose: this check
    blocks a write, so it errs toward passing."""
    if not evidence:
        return []
    seen: set[str] = set()
    for text in evidence:
        seen.update(_DIGIT_RUN_RE.findall(_canon_grounding(text)))
    return [
        run
        for run in dict.fromkeys(_QUANTITY_RE.findall(_canon_grounding(new)))
        if not _is_grounded(run, seen)
    ]


def _is_grounded(run: str, passage_runs: set[str]) -> bool:
    """Whether one claim-side digit run is present in the passages.

    Exact, except that a **decimal** run also grounds on a longer run it
    prefixes: a claim's ``1.3`` is a rounded reading of a passage's
    ``1.33``, and blocking on that is the pedantry that gets a check
    disbelieved. Integers get no such tolerance -- ``19`` must not ground
    on ``1900``, and the motivating defect (a claim saying 19° whose paper
    says 20°) is exactly an integer."""
    if run in passage_runs:
        return True
    if "." not in run:
        return False
    return any(other.startswith(run) for other in passage_runs)


def _mode_probes(token: str) -> tuple[str, ...]:
    """What counts as a passage naming this mode: the token as written,
    plus a word-prefix stem of its last word when that word is long
    enough to stem safely."""
    lowered = token.lower()
    probes = {lowered}
    last = lowered.rsplit(" ", 1)[-1].strip("-")
    if len(last) > _MODE_STEM_CHARS:
        probes.add(last[:_MODE_STEM_CHARS])
    return tuple(sorted(probes))


def _ungrounded_modes(new: str, evidence: Sequence[str]) -> list[str]:
    """Epistemic-mode tokens ``new`` names that no pinned passage does.
    Advisory: see the module docstring on why this is not blocking.

    A generic head (:data:`~precis.taproot.sentence_lint.
    GENERIC_EPISTEMIC_HEADS`) is grounded by ANY specific technique in
    the passage — "calculations" against "the SCC-DFTB algorithm" is not
    a gap. Not the reverse: a claim naming molecular dynamics still warns
    against a passage that only says "simulations", because *which*
    method is the part a reader is being asked to trust."""
    if not evidence:
        return []
    haystack = " \n".join(evidence).lower()
    passage_has_specific = any(
        token.lower() not in GENERIC_EPISTEMIC_HEADS
        for token in find_epistemic_modes(haystack)
    )
    out: list[str] = []
    for token in find_epistemic_modes(new):
        if passage_has_specific and token.lower() in GENERIC_EPISTEMIC_HEADS:
            continue
        if not any(
            re.search(r"\b" + re.escape(probe), haystack)
            for probe in _mode_probes(token)
        ):
            out.append(token)
    return out


def _grounding_warnings(new: str, evidence: Sequence[str]) -> list[str]:
    """The advisory half of grounding -- never blocks a write, always
    lands in the JSONL row so a sweep is reviewable."""
    if not evidence:
        return [_WARN_NO_EVIDENCE]
    modes = _ungrounded_modes(new, evidence)
    if modes:
        return [f"{_WARN_UNGROUNDED_MODE}: {', '.join(modes)}"]
    return []


def _post_validate(
    old: str, new: str, *, evidence: Sequence[str] = ()
) -> tuple[list[str], list[str]]:
    """All five blocking in-code checks over a proposed reword — returns
    ``(checks_failed, human_details)``, both empty when the proposal is
    writable. Every check runs (a proposal can fail several); the
    explicit length check duplicates the ``over-long`` lint code on
    purpose, as the belt that survives any future re-scoping of
    ``_BLOCKING_LINT_CODES``.

    The first four read ``old`` — self-consistency. ``evidence`` (the
    hub's pinned passage texts) adds the fifth, numeric grounding, which
    reads the *sources* instead; it no-ops when the sequence is empty, so
    a caller with no passages to hand gets the historical behaviour."""
    checks: list[str] = []
    details: list[str] = []
    still_failing = _blocking_codes(new)
    if still_failing:
        checks.append(_CHECK_LINT)
        details.append(f"still fails blocking lints: {', '.join(still_failing)}")
    missing = _missing_numeric_tokens(old, new)
    if missing:
        checks.append(_CHECK_NUMERIC)
        details.append(f"drops numeric/unit token(s): {', '.join(missing)}")
    markers = _new_citation_markers(old, new)
    if markers:
        checks.append(_CHECK_CITATION)
        details.append(f"introduces citation marker(s): {', '.join(markers)}")
    if len(new) > _OVER_LONG_CHARS:
        checks.append(_CHECK_OVER_LONG)
        details.append(f"{len(new)} chars exceeds the {_OVER_LONG_CHARS}-char budget")
    ungrounded = _ungrounded_quantities(new, evidence)
    if ungrounded:
        checks.append(_CHECK_GROUNDING)
        details.append(
            "quantit(y/ies) absent from every pinned passage: " + ", ".join(ungrounded)
        )
    return checks, details


# ── the sweep ───────────────────────────────────────────────────────────


def _reword_one(
    store: Store, cand: RewordCandidate, propose_fn: ProposeFn, *, apply: bool
) -> HubReword:
    """One hub: propose, post-validate, and (``apply=True`` only) write
    through the retitle door. Never raises on a model failure or a
    refused write — both are named statuses; a batch pass reports, it
    does not crash on hub 137 of 356."""
    data = propose_fn(cand.sentence, cand.scope, cand.lint_codes)
    if not isinstance(data, dict):
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="llm-failed",
            lint_codes=cand.lint_codes,
            reason="LLM call failed or returned no JSON object",
        )
    verdict = str(data.get("verdict") or "").strip().lower()
    reason = str(data.get("reason") or "").strip() or None
    if verdict == "no-reword":
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="no-reword",
            lint_codes=cand.lint_codes,
            reason=reason,
        )
    new_sentence = str(data.get("sentence") or "").strip()
    if verdict != "reword" or not new_sentence:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="llm-failed",
            lint_codes=cand.lint_codes,
            reason=f"malformed response (verdict={verdict!r})",
        )
    checks, details = _post_validate(
        cand.sentence, new_sentence, evidence=cand.evidence
    )
    warnings = tuple(_grounding_warnings(new_sentence, cand.evidence))
    if checks:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="rejected",
            new_sentence=new_sentence,
            lint_codes=cand.lint_codes,
            checks_failed=tuple(checks),
            warnings=warnings,
            reason="; ".join(details),
        )
    if not apply:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="reworded",
            new_sentence=new_sentence,
            lint_codes=cand.lint_codes,
            warnings=warnings,
            reason=reason,
        )
    try:
        refine_claim_sentence(
            store, cand.hub_ref_id, new_sentence, set_by="reword-sweep"
        )
    except ValueError as exc:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="apply-failed",
            new_sentence=new_sentence,
            lint_codes=cand.lint_codes,
            warnings=warnings,
            reason=str(exc),
        )
    return HubReword(
        hub_ref_id=cand.hub_ref_id,
        old_sentence=cand.sentence,
        status="reworded",
        new_sentence=new_sentence,
        lint_codes=cand.lint_codes,
        warnings=warnings,
        reason=reason,
        applied=True,
    )


def _write_rows(
    rows: Sequence[dict[str, Any]], out: str | Path | IO[str] | None
) -> str | None:
    """JSONL report — one row per processed hub. Returns the path written
    (``None`` for a stream or no ``out``)."""
    if out is None:
        return None
    if isinstance(out, str | Path):
        path = Path(out)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)
    for row in rows:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return None


def run_reword_sweep(
    store: Store,
    *,
    apply: bool = False,
    limit: int | None = None,
    hub: int | None = None,
    out: str | Path | IO[str] | None = None,
    propose_fn: ProposeFn | None = None,
) -> dict[str, Any]:
    """The sweep: select the cohort, propose + post-validate per hub, and
    (``apply=True`` only) reword through the retitle door.

    ``apply=False`` (the default) computes and reports every proposal
    and writes NOTHING. ``limit`` caps the cohort (a stable prefix, so
    the first real run can be small); ``hub`` scopes to one hub;
    ``out`` (path or text stream) gets the per-hub JSONL rows;
    ``propose_fn`` is the injectable LLM seam (tests; default
    :func:`propose_reword`).

    Returns the summary the CLI prints::

        {"cohort": int, "processed": int, "applied": int,
         "warned": int, "counts": {status: int, ...}, "apply": bool,
         "out": str|None}

    ``warned`` counts hubs carrying an advisory grounding warning (an
    unseen epistemic mode, or no pinned passage to check against). It is
    a review pointer into the JSONL, never a gate.
    """
    candidates = select_reword_cohort(store, hub=hub, limit=limit)
    fn = propose_fn or propose_reword
    results = [_reword_one(store, cand, fn, apply=apply) for cand in candidates]
    out_path = _write_rows([r.to_row() for r in results], out)
    counts = Counter(r.status for r in results)
    return {
        "cohort": len(candidates),
        "processed": len(results),
        "applied": sum(1 for r in results if r.applied),
        "warned": sum(1 for r in results if r.warnings),
        "counts": dict(sorted(counts.items())),
        "apply": apply,
        "out": out_path,
    }
