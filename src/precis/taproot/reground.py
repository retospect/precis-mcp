"""Taproot atom re-grounding — "no source, no atom" (T1b prerequisite).

Design: ``docs/backlog/taproot-atom-regrounding.md``. A ``split``-verdict
hub's atoms (:mod:`precis.taproot.migrate` Phase 1) are extracted from the
hub's claim sentence *alone* and can carry content no shown source
states. This module sits between that verdict and
:func:`~precis.taproot.apply_migrate.apply_dry_run`'s placement: per
atom, find the hub's candidate source papers, rank passages, verify (LLM)
support, producing a grounding record (paper, chunk, verbatim quote,
optional bound) or a named ungrounded reason.

**Pipeline:**

1. :func:`collect_source_papers` — a hub's candidate papers via both
   provenance shapes :mod:`precis.taproot.apply_migrate` reads (inbound
   evidence, outbound ``derived-from`` lineage); reuses that module's own
   SQL helpers rather than duplicating them.
2. :func:`candidate_passages` — pure, no DB/model: ranks a paper's body
   chunks against one atom by normalized content-word overlap (shares
   :mod:`precis.taproot.migrate`'s tokenization/notation-folding),
   excluding hearsay sections (references/bibliography/related-work/
   prior-art/background/state-of-the-art/literature-review).
   Embedding-similarity ranking is out of scope
   (:func:`_embedding_similarity_hook`'s TODO).
3. :func:`verify_atoms` — the hub-level orchestrator: one LLM call per
   (hub, paper), batching every atom against that paper's top-``k``
   candidates in a single dispatch (bounds cost). Same MEDIUM-tier
   format-flake-guard posture as
   :func:`~precis.taproot.canon.extract_claim_strict_medium` — a dead
   dispatch or persistently unparseable reply raises
   :class:`RegroundingUnavailable`, never a silent "unsupported". The
   real call (:func:`verify_atoms_batch`) is injectable via
   ``verify_batch_fn``, so tests never touch the network.

**Post-validation happens in code, not the prompt.** Every quote the
model returns is markup-stripped, whitespace-collapsed, and
unicode/notation-folded (:func:`_fold_quote_text`), then must (a) appear
verbatim in the *claimed* chunk and (b) be unique across every
non-hearsay body chunk of that paper (:func:`_validate_quote`). Either
failure rejects only that one support claim — the atom may still ground
via another passage or paper.

**Four ungrounded reasons**: ``"no-passage"`` (no candidate had
anything), ``"hearsay-only"`` (matching material sat in an excluded
section — a re-point candidate for the doer-paper hunt),
``"verify-rejected"`` (candidates existed, LLM found none supporting),
``"quote-validation-failed"`` (model claimed support but the quote never
survived :func:`_validate_quote` — a flake/hallucination signal, distinct
from a clean rejection).

**Quote/snip storage stays out of the DB** (open design call,
``claim-publication-nanopub-ots.md``): a :class:`GroundedRecord`'s
``quote`` lives only in the CLI's JSONL run artifact; what lands in the
DB (via ``apply_migrate``) is the evidence edge's chunk anchor
(``meta.source_handle``) — the passage's identity, not its text.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.taproot.canon import CanonicalClaim, _parse_json_object
from precis.taproot.grounding import has_grounding_prose
from precis.taproot.migrate import _content_words, _normalize_number_text
from precis.utils.llm.router import LlmRequest, Tier, route

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "AtomGrounding",
    "AtomVerifyResult",
    "GroundedRecord",
    "HubGroundingResult",
    "PaperChunk",
    "RegroundingUnavailable",
    "candidate_passages",
    "collect_source_papers",
    "is_hearsay_section",
    "verify_atoms",
    "verify_atoms_batch",
]

#: Default top-k candidate passages per atom x paper (design doc's "Cost
#: shape" — bound the passage-candidate count per atom before scaling up).
_DEFAULT_TOP_K = 6

#: Ported verbatim from the dry-run-49 dossier's ``make_dossier.py``
#: (``~/precis-experiments/taproot-dryrun100-2026-08-15/make_dossier.py``'s
#: ``_HEARSAY_SECTION``) — the mint-side check has no query-time section
#: filter (0118 dropped the index), so this stays a Python-side regex.
_HEARSAY_SECTION_RE = re.compile(
    r"(?i)reference|bibliograph|related work|prior art|background|"
    r"state of the art|literature review"
)

_WS_RE = re.compile(r"\s+")

#: Simple markup :func:`_fold_quote_text` strips before whitespace/notation
#: folding — applied to *both* sides of a quote match (a model's quote and
#: the chunk's stored text), so symmetric stripping is safe: a chunk's
#: ``**2350**`` and a model's plain ``2350`` fold to the same thing.
#: Conservative on purpose (calibration findings,
#: ``docs/backlog/taproot-atom-regrounding.md``) — only the three markup
#: shapes actually seen in stored chunk text:
#: ``<sup>X</sup>``/``<sub>X</sub>`` -> ``X`` (superscript/subscript HTML,
#: e.g. exponents/isotope labels the ingest pipeline sometimes preserves),
#: ``[text](url-or-#anchor)`` -> ``text`` (markdown links — footnote/anchor
#: targets, never the visible content), and bare ``**``/``*`` emphasis
#: markers dropped outright.
_SUP_SUB_RE = re.compile(r"(?is)<(sup|sub)>(.*?)</\1>")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def is_hearsay_section(section_path: str | None) -> bool:
    """True iff ``section_path`` (already ``' > '``-joined, see
    :func:`_fetch_body_chunks`) names a citations/prior-art/background
    section — the paper *cites* the work, it didn't do it."""
    return bool(section_path and _HEARSAY_SECTION_RE.search(section_path))


def _strip_markup(text: str) -> str:
    """Conservative markup stripping — see :data:`_SUP_SUB_RE`/
    :data:`_MD_LINK_RE` for the exact shapes. Never touches a bare
    ``[...]`` that isn't followed by ``(...)`` (e.g. ``"[Fe] complex"``),
    so ordinary bracketed text is left alone."""
    text = _SUP_SUB_RE.sub(lambda m: m.group(2), text)
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    return text.replace("**", "").replace("*", "")


def _fold_quote_text(text: str) -> str:
    """Markup-stripping (:func:`_strip_markup`) + whitespace-collapse + the
    same unicode/notation folding :mod:`precis.taproot.migrate`'s gates use
    (:func:`~precis.taproot.migrate._normalize_number_text`) — the
    normalization both sides of a quote match go through, so ``10^4`` in a
    model's quote matches ``10⁴`` in the chunk's stored text, and
    ``**2350**`` in a chunk matches a model's plain ``2350``."""
    return _WS_RE.sub(" ", _normalize_number_text(_strip_markup(text))).strip()


# ── PaperChunk — the DB-independent shape candidate_passages/verify_atoms
# operate over ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaperChunk:
    """One live body chunk of a candidate source paper — the DB-independent
    shape :func:`candidate_passages` ranks and :func:`verify_atoms`
    verifies against. ``section_path`` is already ``' > '``-joined (SQL
    ``array_to_string``), matching the dossier generator's own shape."""

    chunk_id: int
    chunk_ord: int
    section_path: str | None
    text: str


def _fetch_body_chunks(store: Store, paper_ref_id: int) -> list[PaperChunk]:
    """Every live body chunk (``ord >= 0``, ``retired_at IS NULL``) of one
    candidate source paper, in ``ord`` order — the read side
    :func:`verify_atoms` calls per candidate paper (injectable as
    ``fetch_body_chunks_fn`` for tests)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, array_to_string(section_path, ' > '), text "
            "FROM chunks WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL "
            "ORDER BY ord",
            (paper_ref_id,),
        ).fetchall()
    return [
        PaperChunk(
            chunk_id=int(r[0]),
            chunk_ord=int(r[1]),
            section_path=str(r[2]) if r[2] else None,
            text=str(r[3] or ""),
        )
        for r in rows
    ]


# ── collect_source_papers — both provenance shapes, reusing apply_migrate
# ──────────────────────────────────────────────────────────────────────


def collect_source_papers(store: Store, hub_ref_id: int) -> list[int]:
    """A hub's candidate source papers — both provenance shapes
    :mod:`precis.taproot.apply_migrate` reads: inbound evidence edges and
    outbound ``derived-from`` lineage. Sorted, deduped ``ref_id`` list —
    empty means the hub is hanging (no structural provenance), which
    :func:`verify_atoms` and :func:`~precis.taproot.apply_migrate.
    apply_dry_run` both treat specially: nothing to re-ground against,
    never a reason to withhold an atom.

    Reuses :mod:`precis.taproot.apply_migrate`'s own read helpers
    (``_fetch_evidence_edges``/``_fetch_lineage_links``) rather than
    duplicating their SQL.
    """
    from precis.taproot.apply_migrate import _fetch_evidence_edges, _fetch_lineage_links

    evidence_ids = {e.paper_ref_id for e in _fetch_evidence_edges(store, hub_ref_id)}
    lineage_ids = {ln.paper_ref_id for ln in _fetch_lineage_links(store, hub_ref_id)}
    return sorted(evidence_ids | lineage_ids)


# ── candidate_passages — pure ranking, no DB/model ──────────────────────


def _overlap_score(atom_sentence: str, chunk_text: str) -> float:
    """Normalized content-word overlap: the fraction of the atom's content
    words (after :mod:`precis.taproot.migrate`'s stopword-drop + unicode/
    notation folding) also present in the chunk's text. ``0.0`` when the
    atom has no content words at all (never divides by zero)."""
    atom_words = _content_words(_normalize_number_text(atom_sentence))
    if not atom_words:
        return 0.0
    chunk_words = _content_words(_normalize_number_text(chunk_text))
    return len(atom_words & chunk_words) / len(atom_words)


def _embedding_similarity_hook(
    atom_sentence: str, chunks: Sequence[PaperChunk], embedder: Any
) -> dict[int, float]:
    """TODO — embedding-similarity passage ranking is explicitly OUT of
    scope for this build (``docs/backlog/taproot-atom-regrounding.md``):
    ``chunk_id -> cosine-similarity-ish score``, meant to blend with (or
    replace) :func:`_overlap_score` in :func:`candidate_passages` once an
    embedder is threaded through this module. Not called anywhere —
    :func:`candidate_passages` is lexical-overlap-only today."""
    raise NotImplementedError(
        "embedding-similarity passage ranking is not built for this pass — "
        "candidate_passages() is lexical-overlap-only "
        "(docs/backlog/taproot-atom-regrounding.md)"
    )


def candidate_passages(
    atom_sentence: str,
    chunks: Sequence[PaperChunk],
    *,
    k: int = _DEFAULT_TOP_K,
) -> list[PaperChunk]:
    """The top-``k`` candidate passages for one atom out of one paper's
    body chunks — pure function, no DB/model call.

    Excludes two chunk kinds before ranking, both answering *this passage
    cannot be evidence*: a hearsay ``section_path``
    (:func:`is_hearsay_section`) and a chunk with no assertion at all
    (:func:`~precis.taproot.grounding.has_grounding_prose` — a title/author
    front-matter block, gripe 245842). Front matter is the more dangerous
    of the two: short and dense with the atom's own topic words, so it
    *wins* :func:`_overlap_score` against real body prose.

    Ranks the rest by :func:`_overlap_score` descending (ties by
    ``chunk_ord`` ascending), keeping only chunks scoring above zero.
    ``chunks`` is expected to already be one paper's live body chunks
    (:func:`_fetch_body_chunks`'s contract) — not re-checked here.
    """
    scored = [
        (c, _overlap_score(atom_sentence, c.text))
        for c in chunks
        if not is_hearsay_section(c.section_path) and has_grounding_prose(c.text)
    ]
    scored = [(c, s) for c, s in scored if s > 0]
    scored.sort(key=lambda cs: (-cs[1], cs[0].chunk_ord))
    return [c for c, _ in scored[:k]]


def _hearsay_only_signal(atom_sentence: str, all_chunks: Sequence[PaperChunk]) -> bool:
    """True iff *some* prose chunk in ``all_chunks`` — hearsay sections
    included — would score as a candidate for this atom. Called only when
    :func:`candidate_passages` (which excludes hearsay) came back empty for
    this atom x paper, to tell "nothing in this paper matches at all"
    (``no-passage``) apart from "the only matching material sat in an
    excluded section" (``hearsay-only``) — the delta the design doc's
    apply-integration section names.

    Prose-less chunks are excluded here too, and deliberately: they are the
    OTHER thing :func:`candidate_passages` drops, so counting them would
    report a paper whose only lexical match is its own title page as
    ``hearsay-only`` — naming the wrong exclusion. Such a paper has no usable
    passage, which is exactly ``no-passage``."""
    return any(
        _overlap_score(atom_sentence, c.text) > 0
        for c in all_chunks
        if has_grounding_prose(c.text)
    )


# ── verify_atoms_batch — the real LLM call, MEDIUM tier ─────────────────

#: Cap each passage's text in the verify prompt — several passages per
#: paper, several atoms, all in one call, so a generous-but-bounded per-
#: passage excerpt keeps the prompt from growing unboundedly with a large
#: chunk.
_PASSAGE_EXCERPT_CHARS = 1200

_VERIFY_SYS = (
    "You are a skeptical scientific evidence auditor, checking whether "
    "specific passages actually assert specific claims. Reply with ONLY "
    "the requested JSON object, no prose."
)

_VERIFY_PROMPT = """\
For each numbered ATOM below, decide whether ANY ONE of the numbered \
PASSAGES (all from the same source paper) asserts its content, read alone \
-- verification, not extraction: you may only point at a passage that \
already states what the atom says, never combine passages, infer beyond \
what is written, or use outside knowledge.

ATOMS:
{atoms}

PASSAGES (ord = the passage's position in this paper):
{passages}

For each atom, decide:
- supported: true only if exactly one passage, read alone, asserts the \
atom's content.
- chunk_ord: the ord of that single best supporting passage (null if \
unsupported).
- quote: the MINIMAL verbatim span copied EXACTLY from that passage that \
supports the atom. It MUST be ONE CONTIGUOUS span -- a single unbroken run \
of characters exactly as printed in that one passage. NEVER stitch two \
separate spans together (with an ellipsis, a joining word, or anything \
else), and never paraphrase. null if unsupported.
- bound: only when the atom names a numeric quantity -- does the quote's \
value read as an exact measurement, an explicit upper bound, an explicit \
lower bound, or an approximation ("~", "about", "up to", "on the order \
of")? One of "exact"/"upper"/"lower"/"approx". Omit or null when the atom \
names no quantity.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdicts": [
    {{
      "atom_index": <int>,
      "supported": true | false,
      "chunk_ord": <int> | null,
      "quote": "<minimal verbatim span>" | null,
      "bound": "exact" | "upper" | "lower" | "approx" | null
    }}
  ]
}}
"""

#: Wall-clock ceiling for one batched (hub x paper) verify call -- several
#: atoms and passages in one prompt, so a little more headroom than
#: extract_claim_strict_medium's single-sentence call.
_VERIFY_TIMEOUT_S = 300.0

#: Pause before retrying a dispatch error -- mirrors
#: extract_claim_strict_medium's format-flake backoff (host-load
#: correlated flakes, not worth an immediate retry). Tests monkeypatch this
#: to 0.
_FLAKE_RETRY_BACKOFF_S = 5.0

_BOUND_VALUES = ("exact", "upper", "lower", "approx")


@dataclass(frozen=True)
class AtomVerifyResult:
    """One atom's raw verdict from one (hub, paper) :func:`verify_atoms_batch`
    call -- pre-post-validation (:func:`_validate_quote` still has to check
    the quote before this becomes a :class:`GroundedRecord`)."""

    atom_index: int
    supported: bool
    chunk_ord: int | None
    quote: str | None
    bound: str | None


class RegroundingUnavailable(RuntimeError):
    """Raised by :func:`verify_atoms_batch` when the dispatch itself failed
    (infra error, timeout, or a persistently unparseable reply) rather than
    the model judging every atom unsupported. Mirrors
    :class:`~precis.taproot.canon.ExtractionUnavailable`/
    :class:`~precis.taproot.directed.QualifyUnavailable`: conflating "the
    model never ran" with "nothing is grounded" is exactly the silent
    failure mode this strict posture exists to prevent."""


def _format_atoms(atoms: Sequence[CanonicalClaim]) -> str:
    lines = []
    for i, atom in enumerate(atoms):
        scope_bits = ", ".join(f"{k}={v}" for k, v in sorted(atom.scope.items()))
        suffix = f" [scope: {scope_bits}]" if scope_bits else ""
        lines.append(f"{i}. {atom.sentence}{suffix}")
    return "\n".join(lines)


def _format_passages(passages: Sequence[PaperChunk]) -> str:
    return "\n\n".join(
        f"[ord {c.chunk_ord}] {c.text[:_PASSAGE_EXCERPT_CHARS]}" for c in passages
    )


def _parse_verify_payload(data: Any, n_atoms: int) -> list[AtomVerifyResult] | None:
    """Parse one dispatch reply's payload into
    :class:`AtomVerifyResult`\\ s, or ``None`` when the reply isn't the
    expected ``{"verdicts": [...]}`` shape at all (the caller's format-
    flake retry trigger). Individual malformed entries are skipped rather
    than failing the whole batch (bias-safe: a stray bad ``verdicts[]``
    entry from the model degrades that one atom, not the call).

    A ``"supported": true`` entry missing either ``chunk_ord`` or ``quote``
    is coerced to unsupported here (both are required to ever build a
    :class:`GroundedRecord` downstream) -- never a supported result with a
    half-filled locator.
    """
    if not isinstance(data, dict):
        return None
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        return None
    results: list[AtomVerifyResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("atom_index")
        if (
            not isinstance(idx, int)
            or isinstance(idx, bool)
            or not (0 <= idx < n_atoms)
        ):
            continue
        supported = item.get("supported") is True
        raw_chunk_ord = item.get("chunk_ord")
        chunk_ord = (
            raw_chunk_ord
            if isinstance(raw_chunk_ord, int) and not isinstance(raw_chunk_ord, bool)
            else None
        )
        raw_quote = item.get("quote")
        quote = (
            raw_quote.strip()
            if isinstance(raw_quote, str) and raw_quote.strip()
            else None
        )
        raw_bound = item.get("bound")
        bound = raw_bound if raw_bound in _BOUND_VALUES else None
        if supported and (chunk_ord is None or quote is None):
            supported = False
        results.append(AtomVerifyResult(idx, supported, chunk_ord, quote, bound))
    return results


def verify_atoms_batch(
    atoms: Sequence[CanonicalClaim],
    passages: Sequence[PaperChunk],
    *,
    tier: Tier = Tier.MEDIUM,
) -> list[AtomVerifyResult]:
    """One (hub, paper) verify dispatch: every atom against every one of
    ``passages`` (the union of each atom's own :func:`candidate_passages`
    top-``k`` for this paper), batched into a single call at ``tier`` --
    one call per hub x paper, not per atom, per the design doc's cost
    shape.

    ``tier`` defaults to MEDIUM; a caller wanting a cohort re-verified
    higher binds it and passes the result as ``verify_atoms``'
    ``verify_batch_fn`` -- the per-call tier seam
    :mod:`precis.taproot.repair_evidence` uses to re-audit edges whose
    original verdict anchored no passage.

    Same format-flake guard as
    :func:`~precis.taproot.canon.extract_claim_strict_medium`: a dispatch
    timeout raises :class:`RegroundingUnavailable` immediately (never
    retried); any other dispatch error, or an unparseable/wrong-shape
    reply (:func:`_parse_verify_payload` returning ``None``), is retried
    once. A repeat of either raises :class:`RegroundingUnavailable` --
    persistently broken output is infra-grade failure, never silently
    "nothing supported".
    """
    if not atoms or not passages:
        return []
    prompt = _VERIFY_PROMPT.format(
        atoms=_format_atoms(atoms), passages=_format_passages(passages)
    )
    for attempt in range(2):
        res = route(
            LlmRequest(
                tier=tier,
                messages=[
                    {"role": "system", "content": _VERIFY_SYS},
                    {"role": "user", "content": prompt},
                ],
                prompt=prompt,
                source="taproot:reground-verify",
                timeout_s=_VERIFY_TIMEOUT_S,
            )
        )
        if res.error:
            if res.timed_out:
                raise RegroundingUnavailable(res.error)
            if attempt == 0:
                log.info(
                    "taproot-reground: verify dispatch failed: %s -- "
                    "retrying once in %.0fs (format-flake guard)",
                    res.error,
                    _FLAKE_RETRY_BACKOFF_S,
                )
                time.sleep(_FLAKE_RETRY_BACKOFF_S)
                continue
            raise RegroundingUnavailable(res.error)

        data = res.data if isinstance(res.data, dict) else None
        if data is None:
            data = _parse_json_object(res.text)
        parsed = _parse_verify_payload(data, len(atoms))
        if parsed is not None:
            return parsed
        if attempt == 0:
            log.info(
                "taproot-reground: verify reply had no parseable "
                "{'verdicts': [...]} JSON -- retrying once (format-flake guard)"
            )
            continue
        raise RegroundingUnavailable("no parseable verify JSON")
    raise RegroundingUnavailable("unreachable")  # pragma: no cover


VerifyBatchFn = Callable[
    [Sequence[CanonicalClaim], Sequence[PaperChunk]], list[AtomVerifyResult]
]
CollectPapersFn = Callable[["Store", int], list[int]]
FetchChunksFn = Callable[["Store", int], list[PaperChunk]]


# ── post-validation — the anti-hallucination backstop ───────────────────


def _validate_quote(
    quote: str, claimed_chunk: PaperChunk, non_hearsay_chunks: Sequence[PaperChunk]
) -> bool:
    """The mechanical check every model-returned quote must pass before it
    becomes a :class:`GroundedRecord`: after :func:`_fold_quote_text`
    (whitespace-collapse + notation folding) it must (a) appear as a
    substring of the *claimed* chunk's own folded text, and (b) appear in
    exactly one of the paper's non-hearsay body chunks (uniqueness --
    mirrors the mint gate's own uniqueness check, done here instead so it
    fails at re-grounding time, not at mint)."""
    quote_folded = _fold_quote_text(quote)
    if not quote_folded:
        return False
    if quote_folded not in _fold_quote_text(claimed_chunk.text):
        return False
    hits = sum(
        1 for c in non_hearsay_chunks if quote_folded in _fold_quote_text(c.text)
    )
    return hits == 1


# ── output shape ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GroundedRecord:
    """One verified, quote-validated support link for an atom -- one
    (paper, passage) pair out of possibly several (an atom may be
    supported by more than one paper)."""

    paper_ref_id: int
    chunk_id: int
    chunk_ord: int
    quote: str
    #: ``"exact"``/``"upper"``/``"lower"``/``"approx"``, or ``None`` when
    #: the atom names no quantity scope.
    bound: str | None = None


@dataclass(frozen=True)
class AtomGrounding:
    """One atom's :func:`verify_atoms` outcome: grounded (one or more
    :class:`GroundedRecord`\\ s) or ungrounded with a named ``reason`` --
    ``"no-passage"`` / ``"hearsay-only"`` / ``"verify-rejected"`` /
    ``"quote-validation-failed"`` (module docstring), or ``None`` when the
    atom's hub carried no candidate papers at all (verify never ran -- see
    :attr:`HubGroundingResult.paper_ref_ids`)."""

    atom: CanonicalClaim
    records: tuple[GroundedRecord, ...] = ()
    reason: str | None = None

    @property
    def grounded(self) -> bool:
        return bool(self.records)


@dataclass(frozen=True)
class HubGroundingResult:
    """The full :func:`verify_atoms` result for one hub. ``paper_ref_ids``
    empty means this hub is hanging -- no structural paper provenance at
    all (:func:`collect_source_papers`) -- the signal callers use to tell
    "nothing to re-ground against, atoms may still place hanging" apart
    from "papers exist but this atom isn't in any of them, withhold it"."""

    hub_ref_id: int
    paper_ref_ids: tuple[int, ...]
    atoms: tuple[AtomGrounding, ...]


# ── verify_atoms — the hub-level orchestrator ───────────────────────────


def verify_atoms(
    store: Store,
    hub_ref_id: int,
    atoms: Sequence[CanonicalClaim],
    *,
    top_k: int = _DEFAULT_TOP_K,
    verify_batch_fn: VerifyBatchFn = verify_atoms_batch,
    fetch_body_chunks_fn: FetchChunksFn = _fetch_body_chunks,
    collect_papers_fn: CollectPapersFn = collect_source_papers,
) -> HubGroundingResult:
    """Re-ground every atom of one ``split``-verdict hub against its
    candidate source papers -- the module's top-level entry point.

    1. :func:`collect_source_papers` -- empty means the hub is hanging:
       every atom gets ``reason=None`` (nothing evaluated, never
       "ungrounded"), no LLM call.
    2. Per candidate paper: fetch live body chunks, rank each atom's
       :func:`candidate_passages`, and if any atom has candidates, one
       batched :func:`verify_atoms_batch` call verifies every atom against
       that paper's union of candidates. **Reply-level flake guard**: an
       all-atoms-unsupported reply despite non-empty candidates is
       retried once (same atoms/passages); a consistent all-reject on
       retry stands.
    3. Every ``supported`` verdict is post-validated
       (:func:`_validate_quote`) before becoming a :class:`GroundedRecord`
       -- a bad quote is simply not added; other papers/atoms are
       unaffected.
    4. An atom with zero records across every paper gets one of the
       module docstring's four reasons (:func:`_hearsay_only_signal`
       decides ``"hearsay-only"``; a failed :func:`_validate_quote` or
       unknown ``chunk_ord`` decides ``"quote-validation-failed"``).

    Raises whatever ``verify_batch_fn`` raises (default
    :class:`RegroundingUnavailable`) -- a caller processing many hubs
    should catch this per-hub, same isolation as
    :func:`~precis.taproot.migrate.dry_run`.
    """
    paper_ids = collect_papers_fn(store, hub_ref_id)
    if not paper_ids:
        return HubGroundingResult(
            hub_ref_id=hub_ref_id,
            paper_ref_ids=(),
            atoms=tuple(AtomGrounding(atom=a) for a in atoms),
        )
    if not atoms:
        return HubGroundingResult(
            hub_ref_id=hub_ref_id, paper_ref_ids=tuple(paper_ids), atoms=()
        )

    n = len(atoms)
    records: list[list[GroundedRecord]] = [[] for _ in range(n)]
    had_any_candidate = [False] * n
    had_hearsay_signal = [False] * n
    had_validation_failure = [False] * n

    for paper_id in paper_ids:
        chunks = fetch_body_chunks_fn(store, paper_id)
        if not chunks:
            continue
        non_hearsay_chunks = [
            c for c in chunks if not is_hearsay_section(c.section_path)
        ]
        per_atom_candidates = [
            candidate_passages(a.sentence, chunks, k=top_k) for a in atoms
        ]
        for i, cands in enumerate(per_atom_candidates):
            if cands:
                had_any_candidate[i] = True
            elif _hearsay_only_signal(atoms[i].sentence, chunks):
                had_hearsay_signal[i] = True

        union_by_ord: dict[int, PaperChunk] = {}
        for cands in per_atom_candidates:
            for c in cands:
                union_by_ord[c.chunk_ord] = c
        if not union_by_ord:
            continue

        passages = sorted(union_by_ord.values(), key=lambda c: c.chunk_ord)
        results = verify_batch_fn(atoms, passages)
        if not any(r.supported for r in results):
            # Reply-level flake guard (calibration findings, docs/backlog/
            # taproot-atom-regrounding.md): a degraded LLM reply
            # wholesale-rejects every atom of a batch that DID have
            # candidate passages. One retry recovers the flake; a
            # consistent all-reject stands on the second call.
            log.info(
                "taproot-reground: hub %s paper %s -- every atom came back "
                "unsupported against %d candidate passage(s), retrying once "
                "(reply-level flake guard)",
                hub_ref_id,
                paper_id,
                len(passages),
            )
            results = verify_batch_fn(atoms, passages)
        by_ord = {c.chunk_ord: c for c in passages}
        for r in results:
            if not r.supported or not r.quote:
                continue
            if not (0 <= r.atom_index < n):
                continue
            claimed = by_ord.get(r.chunk_ord) if r.chunk_ord is not None else None
            if claimed is None or not _validate_quote(
                r.quote, claimed, non_hearsay_chunks
            ):
                had_validation_failure[r.atom_index] = True
                continue
            records[r.atom_index].append(
                GroundedRecord(
                    paper_ref_id=paper_id,
                    chunk_id=claimed.chunk_id,
                    chunk_ord=claimed.chunk_ord,
                    quote=r.quote,
                    bound=r.bound,
                )
            )

    atom_groundings: list[AtomGrounding] = []
    for i, atom in enumerate(atoms):
        recs = tuple(records[i])
        if recs:
            atom_groundings.append(AtomGrounding(atom=atom, records=recs))
        elif not had_any_candidate[i]:
            reason = "hearsay-only" if had_hearsay_signal[i] else "no-passage"
            atom_groundings.append(AtomGrounding(atom=atom, reason=reason))
        elif had_validation_failure[i]:
            atom_groundings.append(
                AtomGrounding(atom=atom, reason="quote-validation-failed")
            )
        else:
            atom_groundings.append(AtomGrounding(atom=atom, reason="verify-rejected"))

    return HubGroundingResult(
        hub_ref_id=hub_ref_id,
        paper_ref_ids=tuple(paper_ids),
        atoms=tuple(atom_groundings),
    )
