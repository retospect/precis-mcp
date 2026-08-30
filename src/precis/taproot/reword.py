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
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from precis.nanopub.gates import check_claim_sentence
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    not_hypothesis_predicate_sql,
)
from precis.taproot.hub import refine_claim_sentence
from precis.taproot.sentence_lint import _OVER_LONG_CHARS
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


@dataclass(frozen=True)
class RewordCandidate:
    """One cohort member: the hub, its failing sentence (``refs.title``),
    its ``meta.scope``, and the blocking lint codes that admitted it."""

    hub_ref_id: int
    sentence: str
    scope: dict[str, Any]
    lint_codes: tuple[str, ...]


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
       AND r.deleted_at IS NULL
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
             JOIN refs s ON s.ref_id = l.src_ref_id AND s.deleted_at IS NULL
              WHERE l.dst_ref_id = r.ref_id AND l.relation = 'contradicts'
           )
       AND NOT EXISTS (
             SELECT 1 FROM nanopub_publish np
              WHERE np.claim_ref_id = r.ref_id AND np.state <> 'candidate'
           )
       {{hub_clause}}
     ORDER BY r.ref_id
"""


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
    return out


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


def _post_validate(old: str, new: str) -> tuple[list[str], list[str]]:
    """All four in-code checks over a proposed reword — returns
    ``(checks_failed, human_details)``, both empty when the proposal is
    writable. Every check runs (a proposal can fail several); the
    explicit length check duplicates the ``over-long`` lint code on
    purpose, as the belt that survives any future re-scoping of
    ``_BLOCKING_LINT_CODES``."""
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
    checks, details = _post_validate(cand.sentence, new_sentence)
    if checks:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="rejected",
            new_sentence=new_sentence,
            lint_codes=cand.lint_codes,
            checks_failed=tuple(checks),
            reason="; ".join(details),
        )
    if not apply:
        return HubReword(
            hub_ref_id=cand.hub_ref_id,
            old_sentence=cand.sentence,
            status="reworded",
            new_sentence=new_sentence,
            lint_codes=cand.lint_codes,
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
            reason=str(exc),
        )
    return HubReword(
        hub_ref_id=cand.hub_ref_id,
        old_sentence=cand.sentence,
        status="reworded",
        new_sentence=new_sentence,
        lint_codes=cand.lint_codes,
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
         "counts": {status: int, ...}, "apply": bool, "out": str|None}
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
        "counts": dict(sorted(counts.items())),
        "apply": apply,
        "out": out_path,
    }
