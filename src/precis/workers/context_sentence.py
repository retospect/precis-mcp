"""``context_sentence`` — one neutral paper-context sentence per source paper.

A grounding quote arrives context-free: a quoted result sentence can be
meaningless without paper-intrinsic context the quote doesn't carry (e.g.
"all numbers here are DFT-computed; no wet-lab work"). This pass writes
ONE neutral, descriptive sentence per paper characterizing its methodology
or evidence type — e.g. "Computational study; properties are
DFT-calculated, no experimental synthesis or measurement." — to
``refs.meta['context_sentence']``. ``refs.meta`` is already an allowlisted
jsonb column (per-column, not per-key — ``tests/test_schema_design.py::
JSONB_COLUMNS``), so no migration/allowlist change is needed. Full spec:
``docs/backlog/paper-context-sentence.md``.

Context, never endorsement: the sentence is descriptive of method/evidence
type only, never a claim-strength assertion. A small word-boundary
blocklist (``proof``/``definitive``/``confirms``/``demonstrates``/
``proves``/``establishes``), a ban on attribution-preamble openers ("The
authors report", "This paper presents", "We show", ...), and a ~35-word cap
are enforced IN CODE (:func:`lint_violation`) — the model is not trusted.
On a lint violation
the pass regenerates ONCE, the retry prompt carrying the rejected draft
and the violated rule so the second attempt is not a resend of an
identical request; a second violation drops the sentence entirely
("no sentence beats a bad one") and stamps
``refs.meta['context_sentence_failed']`` so the paper converges rather
than being re-billed every sweep. The stamp is VERSIONED
(``{"v": _PASS_VERSION}``; a legacy bare ``true`` reads as version 0):
a failed paper earns exactly one re-trial whenever the pass version
moves ahead of its stamp, so a fix like the lint-hint retry re-runs the
old cohort without a manual stamp clear. Only a *model verdict*
(decline, or two
lint violations) earns that stamp: a sweep where the model never answered
(dispatch failure, empty completion — e.g. the cloud rung rate-limited)
raises :class:`TransientFailure` and leaves the paper untouched for the
next sweep. Prod stamped three papers permanently failed on 2026-09-17
during a rate-limit window before this distinction existed.

**The input can be the wrong paper.** Few held papers have a
``card_abstract`` and ord 0 of a scanned journal PDF is often a
NEIGHBOURING article's reference list or front matter. Prod's first run
wrote "a review of literature regarding atmospheric chemistry and
planetary science" onto *Solid C60: a new form of carbon*, because ord
0..2 of that ref are an ozone-chemistry bibliography. The model was not
hallucinating; it described what it was handed. So :func:`_claim` no
longer takes "the first body chunk": it prefers ``card_abstract``, then
``refs.meta['abstract']``, then the paper's own retrieved method
passages — the top-k same-paper body chunks the in-paper fused
lexical+embedding search (:func:`retrieve_body_block`, the store arm
behind ``search(kind='paper', scope='pa<id>')``) returns for a fixed set
of method-seeking queries — else passes nothing. The prompt also makes
the TITLE authoritative and gives the model :data:`NO_CONTEXT` to decline
with when the text can't belong to a paper of that title — a decline is
terminal (:func:`is_decline`), never retried, since a retry that invents a
sentence is the exact failure the hatch exists to stop. A wrong context
line is worse than none: it rides along with someone's literal quote as
fact.

**The input can also be no paper at all.** A body read can still yield
only the paper's MASTHEAD — title, byline, affiliations — which has no
method content to characterize: on prod, 788 of the 841 grounding-source
papers have no ``card_abstract`` chunk, and the mastheads score 0.08-0.23
on the prose measure where real abstracts score 0.76-0.94. Asked to
describe one anyway, the model supplies the method from background recall
("NEC Corporation researchers observed helical microtubules ... using
transmission electron microscopy", written from a name and a postal
address), and ``NO_CONTEXT`` does not fire because the text *does* belong
to the paper — it is just empty. :func:`usable_context` therefore refuses
to call the model at all on such a block and stamps the NON-terminal
:data:`META_NO_ABSTRACT_KEY`: the paper needs an abstract, not a retry
(gr346458). The stamp is self-healing (gr514381): it carries the pass
version and the stamp time, and ``_claim`` re-selects a stamped paper
when its version is stale or the ref gained newer content — the block is
then re-resolved through the whole fallback chain — ``card_abstract``,
``meta.abstract``, the paper's own "Abstract" paragraph, retrieved method
passages, leading body prose — and a success clears both stamp keys, so
an abstract arriving after the stamp (or one sitting unrecognized in the
body all along) heals without an operator clear. A still-unusable
re-claim re-stamps at the current version and leaves the claim set.

To force a retry after either stamp, clear it through
``edit(kind='paper', id=<ref|[refs]>, args={'clear_stamp': '<key>'})``
(``precis.workers.retry_stamps`` is the allow-list — gr514347).

Self-contained ref-pass (shaped like ``paper_glossary``/``hub_tagline`` —
DB reads + one outbound LLM call per paper, not a pure ``WorkerHandler``).

**Population (decided, not a corpus-wide sweep):**

1. **Batch backfill** — :func:`backfill_candidate_ref_ids` selects the
   distinct grounding source ``ref_id``s of every LIVE (hub
   ``retired_at IS NULL``) ``nanopub_publish`` row, in ANY state
   (candidate through published/superseded/rejected — the artifact/
   footnote reachability set, not a publication-state filter),
   soft-delete filtered on the hub, the grounding chunk, and the source
   ref. This is also the scheduled pass's default cohort (no explicit
   ``ref_ids``).
2. **Lazy** — ``precis_web.nanopub_render._suggested_payload`` enqueues
   this pass (fire-and-forget, ``ref_ids=[ref_id]``) for a grounding
   source paper missing a sentence the moment it enters a prefill.

Whole-corpus backfill is explicitly out of scope.

**Consumers**: ``precis.nanopub.mint.approve`` freezes a copy into the
approved grounding payload (per-source, mirroring how
``contiguous_group`` is frozen); ``precis.nanopub.assemble`` emits it as
``precis:sourceContext`` when present at mint time; ``precis.export.latex``
renders it under the paper-title line in a hub footnote. Never a search/
card/UI feature — see the "explicitly NOT in scope" section of the spec.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from precis.store import SEMANTIC_DISTANCE_FLOOR
from precis.utils.embed_query import embed_query
from precis.workers.retry_stamps import register_retry_stamps

log = logging.getLogger(__name__)

__all__ = [
    "MAX_WORDS",
    "META_FAILED_KEY",
    "META_KEY",
    "META_NO_ABSTRACT_KEY",
    "NO_CONTEXT",
    "TransientFailure",
    "backfill_candidate_ref_ids",
    "is_decline",
    "lead_body_block",
    "lint_violation",
    "propose_context_sentence",
    "retrieve_body_block",
    "run_context_sentence_pass",
    "stamps_gate_claim",
    "usable_context",
]

#: ``refs.meta`` key the finished sentence is written to.
META_KEY = "context_sentence"

#: ``refs.meta`` key stamped when a paper converged on "no sentence" (both
#: the original proposal and its one regenerate failed the lint, or the
#: model declined with :data:`NO_CONTEXT`) — without this a
#: permanently-failing paper would be re-claimed and re-billed on
#: every backfill/scheduler sweep. Written as ``{"v": _PASS_VERSION}``:
#: ``_claim`` re-claims a stamped paper once when the stamp's version is
#: older than the pass's, so a pass fix self-retries the old cohort
#: (gr514381). A legacy bare ``true`` reads as version 0.
META_FAILED_KEY = "context_sentence_failed"

#: Current pass version stamped into :data:`META_FAILED_KEY`. Bump when
#: the pass's inputs or verdict logic change enough that a previously
#: failed paper deserves one re-trial; each paper retries once per
#: version, not once per bump-and-forget.
_PASS_VERSION = 1

#: ``refs.meta`` key stamped when the paper's context block carried no
#: paper prose to characterize (:func:`usable_context`) — the model was
#: never called, so this is deliberately NOT :data:`META_FAILED_KEY`,
#: which asserts a model verdict. Written as ``{"v": _PASS_VERSION,
#: "at": <utc iso>}`` (legacy bare ``true`` reads as version 0 with no
#: ``at``): ``_claim`` re-selects a stamped paper when its version is
#: stale — the one-time heal when the block-resolution logic moves — or
#: when the ref's ``updated_at`` / a live chunk's ``created_at`` is newer
#: than ``at``, the cheap "context may have arrived" signal. A
#: still-unusable re-claim re-stamps at the current version so a
#: permanently masthead-only paper converges instead of churning the
#: claim set every sweep (gr514381).
META_NO_ABSTRACT_KEY = "context_sentence_no_abstract"

#: Both stamps are operator-clearable via
#: ``edit(kind='paper', id=…, args={'clear_stamp': '<key>'})`` — forcing
#: a re-run for a cohort = clearing its stamp, no prod SQL (gr514347).
register_retry_stamps("context_sentence", META_FAILED_KEY, META_NO_ABSTRACT_KEY)

#: ``to_char`` format for the ``at`` member of a versioned stamp — UTC
#: ISO text, so ``_claim`` compares stamp-vs-row timestamps as STRINGS:
#: a malformed hand-edit degrades to a string compare rather than a
#: cast error that would kill the whole claim query.
_STAMP_TS_FMT = 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'


def _stamp_version(value: Any) -> int:
    """Pass version carried by a stamp value: ``{"v": N}`` → N; a legacy
    bare ``true`` (or anything unrecognized) → 0."""
    if isinstance(value, dict):
        v = value.get("v")
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return 0


def _stamp_at(value: Any) -> datetime | None:
    """The ``at`` recorded by :func:`_stamp_no_abstract`, parsed back to a
    tz-aware UTC datetime; ``None`` when absent or malformed — a naive
    parse counts as malformed (comparing it against the tz-aware
    ``refs.updated_at`` would raise)."""
    raw = value.get("at") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def stamps_gate_claim(meta: dict[str, Any] | None, *, ref_updated_at: Any) -> bool:
    """The stamp half of ``_claim``'s gate, mirrored in Python for
    callers that already hold the ref (``precis_web.nanopub_render``'s
    lazy enqueue prefilter). True when a current-version
    :data:`META_FAILED_KEY` stamp, or a current-version
    :data:`META_NO_ABSTRACT_KEY` stamp whose ``at`` is not older than the
    ref's ``updated_at``, blocks the paper. ``_claim``'s third arm — a
    live chunk newer than ``at`` — needs a chunk read and stays inside
    the pass: a True here is definitive on ref-level evidence, a False is
    advisory (the sweep still re-checks chunk freshness)."""
    if not meta:
        return False
    if _stamp_version(meta.get(META_FAILED_KEY)) >= _PASS_VERSION:
        return True
    no_abstract = meta.get(META_NO_ABSTRACT_KEY)
    if no_abstract is None or _stamp_version(no_abstract) < _PASS_VERSION:
        return False
    at = _stamp_at(no_abstract)
    # Malformed ``at``, or a missing/naive ``ref_updated_at``: gate
    # rather than compare — this runs in the render path and must never
    # raise on hand-edited meta.
    if at is None or getattr(ref_updated_at, "tzinfo", None) is None:
        return True
    return ref_updated_at <= at


def _stamp_no_abstract(store: Any, ref_id: int, claimed_at: Any) -> bool:
    """Write the versioned ``{"v": _PASS_VERSION, "at": now}`` stamp —
    ``at`` rendered off the statement's own ``now()``, identical to the
    ``updated_at`` bump in the same UPDATE, so ``updated_at > at`` stays
    false until a LATER write (an abstract backfill, an operator edit)
    lands and the heal arms in ``_claim`` open.

    ``claimed_at`` is the ``refs.updated_at`` :func:`_claim` read; the
    UPDATE carries ``AND updated_at = claimed_at`` so a ref touched
    between the claim read and this write is left alone — its newer
    ``updated_at`` re-arms the heal arms on the next sweep anyway.
    Returns True when the stamp landed."""
    with store.pool.connection() as conn:
        cur = conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "    %(key)s::text,"
            "    jsonb_build_object('v', %(v)s::int,"
            "        'at', to_char(now() AT TIME ZONE 'UTC', %(fmt)s))),"
            "    updated_at = now() "
            "WHERE ref_id = %(id)s AND retired_at IS NULL"
            " AND updated_at = %(claimed)s",
            {
                "key": META_NO_ABSTRACT_KEY,
                "v": _PASS_VERSION,
                "fmt": _STAMP_TS_FMT,
                "id": ref_id,
                "claimed": claimed_at,
            },
        )
        conn.commit()
        return cur.rowcount == 1


#: Hard word cap (spec: "≤ ~35 words") — enforced in code, not just prompted.
MAX_WORDS = 35

#: Abstract/body-chunk context handed to the model — a header, not the
#: whole paper (mirrors ``paper_glossary._ABSTRACT_CHARS``).
_ABSTRACT_CHARS = 1500

#: Minimum words the context block must carry BEYOND the paper's own title
#: for a method sentence to be groundable in it, and the minimum share of
#: those words that must be ordinary lowercase prose. A masthead (title +
#: byline + affiliations) clears neither: it is proper nouns, initials and
#: postcodes. Calibrated on the 841 prod grounding-source papers
#: (2026-09-18) — the 53 with a real ``card_abstract`` chunk score 0.76 to
#: 0.94, the first-body-chunk fallbacks have a median of 0.38, and the four
#: mastheads that triggered gr346458 score 0.08 to 0.23. 0.45 sits in that
#: gap, well clear of every genuine abstract in the cohort.
_MIN_CONTEXT_WORDS = 25
_MIN_PROSE_FRACTION = 0.45

#: Markup stripped before the prose measurement — the ``<sup>`` runs around
#: author affiliation marks otherwise read as lowercase prose words and
#: score a byline as an abstract (observed on ref 42558, 0.08 -> 0.40).
_TAG_RE = re.compile(r"<[^>]{1,40}>")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")

#: Claim-strength words the sentence must never carry — endorsing the
#: finding is a different pass's job (and never this one's); this sentence
#: is context, not a verdict. Word-boundary, case-insensitive: "confirmsX"
#: is not a match, "Confirms" is.
_BLOCKLIST_WORDS: tuple[str, ...] = (
    "proof",
    "definitive",
    "confirms",
    "demonstrates",
    "proves",
    "establishes",
)
_BLOCKLIST_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _BLOCKLIST_WORDS) + r")\b",
    re.IGNORECASE,
)

#: Attribution-preamble openers the sentence must never lead with — the
#: reader already knows the sentence is about the paper, so "The authors
#: report ..." / "This paper presents ..." / "We show ..." is pure filler.
#: The first alternative catches the dodge prod produced once the plain
#: forms were banned ("NEC Corporation researchers observed ..."): an
#: attribution noun anywhere in the first five words. Anchored at the
#: START of the sentence (past optional leading punctuation/quote chars),
#: case-insensitive, word-boundary after the phrase.
_PREAMBLE_RE = re.compile(
    r"^\W*(?:"
    r"(?:[^\s.,;:]+\s+){0,4}(?:researchers|scientists|investigators|co-?workers)"
    r"|the\ authors?"
    r"|this\ (?:paper|study|work|article|report|letter)"
    r"|the\ (?:paper|study|work|article|report|letter)"
    r"|the\ present\ (?:paper|study|work)"
    r"|in\ this\ (?:paper|study|work|article)"
    r"|here(?:,)?\ we"
    r"|we"
    r")\b",
    re.IGNORECASE,
)

_QUOTE_CHARS = "\"'`“”‘’"

#: Exact token the model must return when the supplied text can't ground a
#: sentence. Treated as a drop, not a sentence — see :data:`_SYS`.
NO_CONTEXT = "NO_CONTEXT"

_SYS = (
    "You write ONE neutral, descriptive sentence characterizing a scientific "
    "paper's methodology or evidence type -- context a reader needs before "
    "trusting a quote pulled from it (for example: whether a result is "
    "computational, experimental, a review, or a case report). Never assert, "
    "endorse, or judge whether the paper's claims are correct -- describe "
    "the method/evidence type only, in a neutral register. State what the "
    "paper did directly, with the method or the measured thing as the "
    "subject of the sentence -- for example: 'Elastic properties and "
    "intrinsic strength were measured by atomic force microscopy.' or "
    "'Helical microtubules of graphitic carbon were observed by "
    "transmission electron microscopy.' Never open with attribution filler "
    "such as 'The authors report', 'This paper presents', 'This study "
    "describes', 'We show', 'Here we report', or 'X researchers observed' "
    "-- no attribution subject at all, not the authors, not a group or "
    "institution name; the reader already knows the sentence is about "
    "the paper. Never use words like proof, "
    "definitive, confirms, demonstrates, proves, or establishes. At most "
    "35 words, ONE sentence, no preamble, no markdown. Reply with ONLY "
    "the sentence and nothing else.\n\n"
    "The TITLE is authoritative about what the paper is; the supplied text is "
    "machine-extracted and is sometimes NOT this paper's own prose -- a "
    "neighbouring article's reference list, front matter, or an unrelated "
    "column can bleed in. If the text does not plausibly belong to a paper "
    f"with that title, or is too thin to characterize it, reply with exactly "
    f"{NO_CONTEXT} and nothing else. Never infer the subject matter from "
    "text that contradicts the title -- a wrong context line is worse than "
    "none, because it is attached to someone's quote as fact."
)


# ── input guard (the model is only as good as what it is fed) ────────────


def usable_context(title: str, abstract: str) -> bool:
    """True when ``abstract`` plausibly carries paper prose a method
    sentence could be grounded in; False for a content-free block.

    :func:`retrieve_body_block` can still assemble a MASTHEAD-only block —
    the title, the byline, the affiliations — and on prod that was ~94% of
    the grounding cohort under the old ord-0 fallback
    (gr346458). A sentence written from a byline is the model's background
    recall, not the paper: prod produced "NEC Corporation researchers
    observed helical microtubules ... using transmission electron
    microscopy" from an input whose only content was the author's name and
    the NEC Tsukuba address. Not calling the model is the only honest
    answer, and it belongs in code rather than the prompt because the
    model's own :data:`NO_CONTEXT` escape hatch — which asks for exactly
    this judgement — demonstrably does not fire on a masthead.

    Heuristic, deliberately conservative: words beyond the title, and the
    share of them that are ordinary lowercase prose (calibration in
    :data:`_MIN_PROSE_FRACTION`). Passing is not a promise the text IS an
    abstract — it only rules out blocks that certainly are not;
    ``NO_CONTEXT`` stays the second line of defence for prose that belongs
    to a different paper."""
    title_words = {w.lower() for w in _WORD_RE.findall(title or "")}
    words = [
        w
        for w in _WORD_RE.findall(_TAG_RE.sub(" ", (abstract or "")[:_ABSTRACT_CHARS]))
        if w.lower() not in title_words
    ]
    if len(words) < _MIN_CONTEXT_WORDS:
        return False
    prose = sum(1 for w in words if w.islower() and len(w) >= 2)
    return prose / len(words) >= _MIN_PROSE_FRACTION


#: Section roles a retrieved passage must not sit under — the same
#: hearsay roles the nanopub primary-source gate rejects (references /
#: bibliographies / related-work / background: those cite the doers, they
#: are not them, so a bibliography must never become "the method"), plus
#: the acknowledgements/funding end-matter a methods query still surfaces.
#: Kept local rather than imported from ``precis.nanopub.evidence`` —
#: workers may not reach for the handlers layer evidence pulls in.
_HEARSAY_ROLE_RE = re.compile(
    r"reference|bibliograph|related.work|prior.art|background|"
    r"state.of.the.art|acknowledg|literature.cited|funding",
    re.IGNORECASE,
)
#: Citation-shaped fragments: ``1234, 56-78`` volume/page runs and ``1999)``
#: year closers. A reference list has many per chunk, prose has ~none —
#: the backstop for a bibliography chunk carrying no section role.
_CITATION_RE = re.compile(
    r"\b\d{1,4}\s*[,:]\s*\d{1,5}\s*[–-]\s*\d{1,5}\b|\b(?:19|20)\d{2}\)|\bet al\b"
)


def _end_matter(text: str, section_path: Any) -> bool:
    """True for a hearsay/end-matter chunk — by its section role
    (:data:`_HEARSAY_ROLE_RE`), or, for an un-sectioned chunk, by its
    density of citation fragments."""
    heading = " ".join(str(h) for h in (section_path or []))
    if _HEARSAY_ROLE_RE.search(heading):
        return True
    return len(_CITATION_RE.findall(text)) >= 3


#: Fixed method-seeking query legs for the in-paper retrieval fallback —
#: the spec's four groups ("we calculate / DFT / first-principles", "we
#: synthesized / measured / characterized", "experimental setup /
#: methods", "simulation details") expanded into one phrasing per leg,
#: because ``websearch_to_tsquery`` ANDs a leg's terms: a leg written as
#: "we synthesized measured characterized" would require all three
#: phrasings in one chunk. Each leg runs lexical, plus semantic when an
#: embedder is wired, inside ``ChunkStore.search_chunks_multi``'s RRF
#: fusion — the same engine the ``scope='pa<id>'`` arm of
#: ``search(kind='paper')`` drives.
_METHOD_QUERIES: tuple[str, ...] = (
    "we calculate",
    "DFT first-principles",
    "we synthesized",
    "we measured",
    "we characterized",
    "experimental setup",
    "methods",
    "simulation details",
)

#: Bound on the assembled block (spec): at most this many passages, total
#: at most :data:`_ABSTRACT_CHARS` chars. ``_RETRIEVAL_LIMIT`` over-fetches
#: so end-matter drops can still fill the passage window.
_MAX_PASSAGES = 4
_RETRIEVAL_LIMIT = 12

#: Paragraph ord window the lead-body fallback scans. An abstract sits at
#: the head of the body — on prod every "Abstract" paragraph in the
#: stamped cohort was inside the first dozen body chunks — so looking
#: deeper would only risk adopting a figure caption or a section named
#: "Abstract of ...". The leading-prose arm reads the same window: what
#: leads the body is what characterizes the paper.
_LEAD_SCAN_ORDS = 24

#: Section opener marking the paper's own abstract paragraph — matches
#: "Abstract", "ABSTRACT.", "**Abstract**" at the start of a chunk after
#: any leading non-word (markup/whitespace/punctuation) characters.
_ABSTRACT_HEAD_RE = re.compile(r"^\W*abstract\b", re.IGNORECASE)

#: Front-matter furniture a lead paragraph must not contribute — licence
#: and copyright lines, correspondence/keywords blocks, received/accepted
#: date lines, affiliation runs. Belt over the per-paragraph
#: :func:`usable_context` check: a long licence paragraph could scrape
#: the word floor, and none of it is method content (gr346458's byline
#: cohort, generalized).
_FRONT_MATTER_RE = re.compile(
    r"^\W*(?:"
    r"©|copyright|licen[cs]e|creative\s+commons|all\s+rights\s+reserved|"
    r"correspondence|keywords?|received|accepted|revised|"
    r"(?:department|university|institut\w*|laborator\w*)\s*(?:,|of\b)"
    r")",
    re.IGNORECASE,
)


def retrieve_body_block(
    store: Any, *, embedder: Any, ref_id: int
) -> tuple[str, list[int]]:
    """The paper's own method passages via the in-paper fused search.

    Runs :data:`_METHOD_QUERIES` through
    :meth:`ChunkStore.search_chunks_multi` scoped to ``ref_id`` — the
    store helper the ``scope='pa<id>'`` arm of ``search(kind='paper')``
    resolves into — so the block is the top-k same-paper body passages
    the fused lexical+embedding engine ranks first, not position ord 0.
    A ``None``/failing embedder degrades to the lexical legs
    (:func:`embed_query`). Hearsay/end-matter chunks are excluded
    (:func:`_end_matter`); survivors are assembled score-desc-then-ord,
    at most :data:`_MAX_PASSAGES` passages and :data:`_ABSTRACT_CHARS`
    chars total — deterministic, so the same paper yields the same input
    across sweeps. Returns ``(block_text, source_chunk_ids)``; the ids
    feed the pass's per-ref log line."""
    query_vecs = [
        v for q in _METHOD_QUERIES if (v := embed_query(embedder, q)) is not None
    ]
    hits = store.chunks.search_chunks_multi(
        q_texts=list(_METHOD_QUERIES),
        query_vecs=query_vecs,
        scope_ref_id=ref_id,
        limit=_RETRIEVAL_LIMIT,
        max_distance=SEMANTIC_DISTANCE_FLOOR,
    )
    passages = [
        (block, score)
        for block, _ref, score in hits
        if not _end_matter(str(block.text or ""), block.meta.get("section_path"))
    ]
    passages.sort(key=lambda t: (-t[1], t[0].ord))
    parts: list[str] = []
    chunk_ids: list[int] = []
    joined_len = 0
    for block, _score in passages[:_MAX_PASSAGES]:
        text = str(block.text or "").strip()
        if not text:
            continue
        room = _ABSTRACT_CHARS - joined_len - (2 if parts else 0)
        if room <= 0:
            break
        snippet = text[:room]
        parts.append(snippet)
        chunk_ids.append(int(block.id))
        joined_len += len(snippet) + (2 if len(parts) > 1 else 0)
    return "\n\n".join(parts), chunk_ids


def _lead_paragraphs(store: Any, ref_id: int) -> list[tuple[int, str, Any]]:
    """``(chunk_id, text, section_path)`` for the ref's first
    :data:`_LEAD_SCAN_ORDS` live ``paragraph`` chunks, in ord order — the
    head of the body where an abstract or lead prose would sit.
    ``section_path`` prefers the meta mirror (test seeds put it there)
    over the real column, the same pair :func:`_row_to_chunk` merges."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text, meta, section_path FROM chunks "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "AND chunk_kind = 'paragraph' AND ord >= 0 AND ord < %s "
            "ORDER BY ord",
            (ref_id, _LEAD_SCAN_ORDS),
        ).fetchall()
    return [
        (
            int(chunk_id),
            str(text or ""),
            (meta or {}).get("section_path") or section_path,
        )
        for chunk_id, text, meta, section_path in rows
    ]


def _lead_window(
    paras: list[tuple[int, str, Any]], title: str | None = None
) -> tuple[str, list[int]]:
    """Assemble ``(chunk_id, text, section_path)`` tuples into a block —
    same ``\\n\\n`` join, :data:`_MAX_PASSAGES` and :data:`_ABSTRACT_CHARS`
    bounds as :func:`retrieve_body_block`.

    Every paragraph is skipped that is end-matter (:func:`_end_matter`),
    front-matter furniture (:data:`_FRONT_MATTER_RE`), or — when
    ``title`` is given — too thin to stand on its own
    (:func:`usable_context` PER PARAGRAPH, not on the joined block: a
    byline + licence + correspondence run must not score as prose in
    aggregate, gr346458). ``title=None`` (the abstract-headed arm) keeps
    every non-end-matter paragraph — the continuations belong to the
    abstract however short they are."""
    parts: list[str] = []
    chunk_ids: list[int] = []
    joined_len = 0
    for chunk_id, text, section_path in paras:
        stripped = text.strip()
        if (
            not stripped
            or _end_matter(stripped, section_path)
            or _FRONT_MATTER_RE.match(stripped)
            or (title is not None and not usable_context(title, stripped))
        ):
            continue
        room = _ABSTRACT_CHARS - joined_len - (2 if parts else 0)
        if room <= 0 or len(parts) >= _MAX_PASSAGES:
            break
        parts.append(stripped[:room])
        chunk_ids.append(chunk_id)
        joined_len += len(stripped[:room]) + 2
    return "\n\n".join(parts), chunk_ids


def lead_body_block(
    store: Any, *, title: str, ref_id: int, abstract_only: bool = False
) -> tuple[str, list[int]]:
    """The lead-of-body context block — the last resort before a paper is
    stamped :data:`META_NO_ABSTRACT_KEY` (gr514381).

    Scans the ref's first :data:`_LEAD_SCAN_ORDS` paragraph chunks. When a
    chunk opens with "Abstract" (:data:`_ABSTRACT_HEAD_RE`) the block
    starts there — the paper's own abstract paragraph plus the following
    non-end-matter paragraphs — which is the right input for a large
    share of held papers that have no ``card_abstract`` chunk and no
    ``meta.abstract`` (prod: 11 of the stamped cohort). Otherwise the
    block is the first paragraphs that each stand on their own as prose
    (``title`` feeds that per-paragraph :func:`usable_context` check —
    front-matter furniture is dropped, not joined). With
    ``abstract_only=True`` only the headed block counts — the pass runs
    that arm before :func:`retrieve_body_block` and the plain
    leading-prose arm after it.

    Returns ``(block_text, source_chunk_ids)``; an empty block means the
    lead window was all masthead/front-matter/end-matter."""
    paras = _lead_paragraphs(store, ref_id)
    head = next(
        (
            i
            for i, (_id, text, sp) in enumerate(paras)
            if not _end_matter(text, sp) and _ABSTRACT_HEAD_RE.match(text)
        ),
        None,
    )
    if head is not None:
        return _lead_window(paras[head:])
    if abstract_only:
        return "", []
    return _lead_window(paras, title)


# ── lint (belt over the LLM) ─────────────────────────────────────────────


def lint_violation(sentence: str) -> str | None:
    """The reason ``sentence`` fails the context-sentence contract, or
    ``None`` when clean. Never raises. Three checks, all code-enforced
    (the model is not trusted): the ~35-word cap, the claim-strength
    blocklist (word-boundary, case-insensitive — see
    :data:`_BLOCKLIST_WORDS`), and a ban on leading attribution preamble
    (see :data:`_PREAMBLE_RE`)."""
    if not sentence or not sentence.strip():
        return "empty"
    words = sentence.split()
    if len(words) > MAX_WORDS:
        return f"over word cap ({len(words)} words)"
    m = _BLOCKLIST_RE.search(sentence)
    if m:
        return f"claim-strength word {m.group(0)!r}"
    m = _PREAMBLE_RE.match(sentence)
    if m:
        return f"attribution preamble {m.group(0)!r}"
    return None


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        return text[1:-1].strip()
    return text


def _clean_sentence(raw: str) -> str:
    """First line only (a model occasionally appends a trailing aside
    despite the instruction), quote-stripped, whitespace-collapsed."""
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    return " ".join(_strip_quotes(first_line.strip()).split())


# ── the LLM call ─────────────────────────────────────────────────────────


def _build_messages(
    title: str, abstract: str, retry_note: str | None = None
) -> list[dict[str, str]]:
    user = f"PAPER TITLE: {title}"
    if abstract:
        user += f"\n\nABSTRACT:\n{abstract[:_ABSTRACT_CHARS]}"
    if retry_note:
        user += f"\n\n{retry_note}"
    return [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": user},
    ]


def propose_context_sentence(
    client: Any, title: str, abstract: str, *, retry_note: str | None = None
) -> str | None:
    """One proposal call. Returns the cleaned candidate sentence, or
    ``None`` on a dispatch failure / empty completion — never raises (the
    caller counts it as a failed attempt and moves on, same contract as
    ``hub_tagline.propose_tagline``). ``retry_note`` appends a correction
    to the user turn — :func:`_generate_with_lint` uses it to tell the
    model which rule its rejected draft broke, so the retry is not a
    resend of an identical request (gr514381)."""
    try:
        out = client.complete(_build_messages(title, abstract, retry_note))
    except Exception:
        log.warning("context_sentence: propose call failed", exc_info=True)
        return None
    text = getattr(out, "text", "") or ""
    sentence = _clean_sentence(text)
    return sentence or None


def is_decline(sentence: str) -> bool:
    """True when the model used its :data:`NO_CONTEXT` escape hatch — the
    supplied text didn't plausibly belong to a paper with that title (a
    neighbouring article's reference list bleeding into a scan is the
    common shape). Terminal: never retried, because a retry that invents
    a sentence anyway is exactly the outcome the escape hatch prevents."""
    return sentence.strip().rstrip(".").upper() == NO_CONTEXT


class TransientFailure(RuntimeError):
    """The model never delivered a verdict this sweep (dispatch failure or
    empty completion on the attempts that mattered). The caller must NOT
    stamp :data:`META_FAILED_KEY` — the paper stays claimable and the next
    sweep retries; only a decline or two real lint violations converge."""


def _generate_with_lint(
    client: Any, title: str, abstract: str, *, ref_id: int | None = None
) -> str | None:
    """Propose + lint; on a violation, regenerate ONCE; a second violation
    drops the sentence ("no sentence beats a bad one" — spec) by returning
    ``None``. Raises :class:`TransientFailure` when neither attempt was a
    model verdict — a ``None`` from :func:`propose_context_sentence` is an
    outage, not an opinion, and must not converge the paper.

    The retry carries the rejection back to the model — the dropped draft
    and the rule it broke — because a resend of the identical request
    invites the identical answer (prod's 36266 echoed the same blocklisted
    word twice, gr514381).

    Every drop is logged with the rejected text. Without that the pass
    emits only a batch count, and diagnosing a converging paper means
    replaying the claim query and the model call by hand against prod
    (gr346458)."""
    sentence = propose_context_sentence(client, title, abstract)
    if sentence is not None and is_decline(sentence):
        log.info("context_sentence: ref_id=%s model declined (NO_CONTEXT)", ref_id)
        return None
    reason = lint_violation(sentence) if sentence is not None else None
    if sentence is not None and reason is None:
        return sentence
    retry_note = None
    if sentence is not None:
        log.info(
            "context_sentence: ref_id=%s lint rejected %r (%s); regenerating",
            ref_id,
            sentence,
            reason,
        )
        retry_note = (
            f"Your previous sentence was rejected: {sentence[:300]!r} — "
            f"{reason}. Write a different sentence that obeys every rule."
        )
    retry = propose_context_sentence(client, title, abstract, retry_note=retry_note)
    if retry is not None and is_decline(retry):
        log.info(
            "context_sentence: ref_id=%s model declined (NO_CONTEXT) on retry", ref_id
        )
        return None
    if retry is not None and lint_violation(retry) is None:
        return retry
    if sentence is not None and retry is not None:
        log.warning(
            "context_sentence: ref_id=%s dropped — lint rejected both attempts "
            "(%r: %s | %r: %s)",
            ref_id,
            sentence,
            lint_violation(sentence),
            retry,
            lint_violation(retry),
        )
        return None  # two real lint violations — the model's verdict
    raise TransientFailure("no model reply this sweep")


# ── DB: backfill cohort + claim + write ──────────────────────────────────

#: Distinct grounding source ref_ids of every live nanopub_publish row, any
#: state. ``hub``/``src`` are both soft-delete filtered; the grounding
#: chunk_id must resolve to a live, non-retired chunk.
#:
#: The ``CASE`` around the cast is load-bearing, not ceremony: the frozen
#: payload is reviewer-submitted JSON, and the mint gate only asserts
#: ``int(chunk_id)`` works — which accepts a JSON *float* (``12.0``) that
#: ``'12.0'::bigint`` then rejects. A bare cast would fail the WHOLE query
#: (not skip the row) on one such passage, starving the entire backfill
#: cohort. ``CASE`` fixes evaluation order — a non-integral literal yields
#: NULL and simply doesn't join, where a ``WHERE``-clause regex guard
#: would not be ordering-guaranteed.
_BACKFILL_SQL = r"""
    SELECT DISTINCT c.ref_id
      FROM nanopub_publish p
      JOIN refs hub ON hub.ref_id = p.claim_ref_id AND hub.retired_at IS NULL
      CROSS JOIN LATERAL
           jsonb_array_elements(COALESCE(p.grounding -> 'passages', '[]'::jsonb))
               AS passage
      JOIN chunks c
        ON c.chunk_id = CASE
               WHEN passage ->> 'chunk_id' ~ '^[0-9]+$'
               THEN (passage ->> 'chunk_id')::bigint
           END
       AND c.retired_at IS NULL
      JOIN refs src ON src.ref_id = c.ref_id AND src.retired_at IS NULL
     WHERE p.grounding IS NOT NULL
     ORDER BY c.ref_id
"""


def backfill_candidate_ref_ids(conn: Any) -> list[int]:
    """Distinct grounding source ``ref_id``s of every live ``nanopub_publish``
    row (see :data:`_BACKFILL_SQL`) — the batch-backfill selection, and the
    scheduled pass's default cohort when no explicit ``ref_ids`` are given."""
    rows = conn.execute(_BACKFILL_SQL).fetchall()
    return [int(r[0]) for r in rows]


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None
) -> list[tuple[int, str, str, list[int], bool, bool, Any]]:
    """``(ref_id, title, block, block_chunk_ids, no_abstract_stamped,
    failed_stamped, claimed_at)``
    for up to ``limit`` still-unsentenced live papers. ``ref_ids``, when
    given, IS the candidate set (the lazy single-paper enqueue, or a
    targeted backfill/test run); ``None`` sweeps
    :func:`backfill_candidate_ref_ids`. Excludes a paper already carrying
    a sentence and one that converged on "no sentence" AT THE CURRENT
    pass version — a :data:`META_FAILED_KEY` stamp older than
    :data:`_PASS_VERSION` (a legacy bare ``true`` counts as version 0)
    earns exactly one re-trial (gr514381).
    :data:`META_NO_ABSTRACT_KEY` papers are claimed only when the stamp
    is stale-versioned (the one-time heal for new resolution logic) or
    the ref gained content since ``at`` — ``refs.updated_at`` or a live
    chunk's ``created_at`` newer than the stamp. Timestamp arms compare
    :data:`_STAMP_TS_FMT` STRINGS so a malformed hand-edit degrades to a
    string compare, not a cast error that kills the whole claim. Stamped
    rows sort last so a permanently-thin cohort can't starve fresh
    claims; ``no_abstract_stamped``/``failed_stamped`` tell the pass
    which stamp keys are present (cleared on a verdict, so the clear
    write is skipped entirely on an unstamped paper), and ``claimed_at``
    is the ``updated_at`` this claim read — the stamp write's race guard
    (:func:`_stamp_no_abstract`).

    ``block`` here is only the two authoritative sources — a
    :func:`usable_context` ``card_abstract`` chunk (whose ``chunk_id``
    lands in ``block_chunk_ids`` for the per-ref log) or a prose
    ``refs.meta['abstract']``. The body fallbacks need the store, not
    this connection, so the pass resolves them per row (see
    :func:`lead_body_block` and :func:`retrieve_body_block`)."""
    candidates = ref_ids if ref_ids is not None else backfill_candidate_ref_ids(conn)
    if not candidates:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, card.chunk_id, card.text,
               r.meta->>'abstract',
               r.meta ? %(no_abstract_key)s,
               r.meta ? %(failed_key)s,
               r.updated_at
          FROM refs r
          LEFT JOIN LATERAL (
               SELECT chunk_id, text FROM chunks
                WHERE ref_id = r.ref_id AND chunk_kind = 'card_abstract'
                  AND retired_at IS NULL
                ORDER BY ord LIMIT 1
               ) card ON TRUE
         WHERE r.ref_id = ANY(%(ids)s)
           AND r.retired_at IS NULL
           AND r.meta->>%(key)s IS NULL
           AND COALESCE((r.meta->%(failed_key)s->>'v')::int, 0)
               < %(pass_version)s
           AND (
                 NOT (r.meta ? %(no_abstract_key)s)
                 OR COALESCE((r.meta->%(no_abstract_key)s->>'v')::int, 0)
                    < %(pass_version)s
                 OR to_char(r.updated_at AT TIME ZONE 'UTC', %(ts_fmt)s)
                    > r.meta->%(no_abstract_key)s->>'at'
                 OR EXISTS (
                      SELECT 1 FROM chunks nc
                       WHERE nc.ref_id = r.ref_id
                         AND nc.retired_at IS NULL
                         AND to_char(nc.created_at AT TIME ZONE 'UTC',
                                     %(ts_fmt)s)
                             > r.meta->%(no_abstract_key)s->>'at'
                    )
               )
         ORDER BY (r.meta ? %(no_abstract_key)s), r.ref_id
         LIMIT %(limit)s
        """,
        {
            "ids": list(candidates),
            "key": META_KEY,
            "failed_key": META_FAILED_KEY,
            "no_abstract_key": META_NO_ABSTRACT_KEY,
            "pass_version": _PASS_VERSION,
            "ts_fmt": _STAMP_TS_FMT,
            "limit": limit,
        },
    ).fetchall()
    out: list[tuple[int, str, str, list[int], bool, bool, Any]] = []
    for (
        ref_id,
        title,
        card_id,
        card_text,
        meta_abstract,
        has_no_abstract,
        has_failed,
        claimed_at,
    ) in rows:
        title = str(title or "")
        block = ""
        chunk_ids: list[int] = []
        if card_text and usable_context(title, str(card_text)):
            block = str(card_text)
            chunk_ids = [int(card_id)]
        if not block and meta_abstract and usable_context(title, str(meta_abstract)):
            block = str(meta_abstract)
        out.append(
            (
                int(ref_id),
                title,
                block,
                chunk_ids,
                bool(has_no_abstract),
                bool(has_failed),
                claimed_at,
            )
        )
    return out


def run_context_sentence_pass(
    store: Any,
    *,
    client: Any,
    batch_size: int = 8,
    ref_ids: list[int] | None = None,
    embedder: Any = None,
) -> dict[str, int]:
    """One claim → retrieve → generate → lint → write cycle. Returns
    ``{claimed, ok, failed}``.

    ``ref_ids`` optionally restricts the sweep to specific papers (lazy
    enqueue / targeted backfill / tests); ``None`` sweeps
    :func:`backfill_candidate_ref_ids` — the live-publish-row grounding
    cohort. Never raises on a single paper's failure; a dropped/lint-failed
    sentence bumps ``failed`` and stamps :data:`META_FAILED_KEY` at
    :data:`_PASS_VERSION` so the paper is not re-claimed until the pass
    logic next moves. ``embedder`` feeds the semantic legs
    of the body-passage retrieval (:func:`retrieve_body_block`); ``None``
    degrades that search to its lexical legs.

    The context block is resolved in order — ``card_abstract``,
    ``meta.abstract``, the paper's own "Abstract" paragraph, retrieved
    method passages, leading body prose (:func:`lead_body_block`,
    :func:`retrieve_body_block`) — and the first source whose text is
    :func:`usable_context` wins. A paper whose sources are all
    masthead-thin stamps :data:`META_NO_ABSTRACT_KEY` before any model
    call; the stamp is versioned and self-healing so a later-arriving
    abstract re-runs the chain on the next sweep, and a written sentence
    clears both stamp keys (gr514381).

    Two outcomes converge a paper WITHOUT a model verdict and so stay out
    of ``failed``, which counts verdicts: a content-free context block
    (:func:`usable_context`) stamps :data:`META_NO_ABSTRACT_KEY` before any
    call, and a sweep the model never answered
    (:class:`TransientFailure`) stamps nothing at all. Both are logged."""
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = deferred = no_abstract = 0
    for (
        ref_id,
        title,
        block,
        chunk_ids,
        was_no_abstract,
        was_failed,
        claimed_at,
    ) in rows:
        try:
            if not usable_context(title, block):
                # The paper's own "Abstract" paragraph wins over generic
                # method-passage retrieval (it IS the context), and both
                # beat a body that leads with prose.
                block, chunk_ids = lead_body_block(
                    store, title=title, ref_id=ref_id, abstract_only=True
                )
            if not usable_context(title, block):
                block, chunk_ids = retrieve_body_block(
                    store, embedder=embedder, ref_id=ref_id
                )
            if not usable_context(title, block):
                block, chunk_ids = lead_body_block(store, title=title, ref_id=ref_id)
            log.info(
                "context_sentence: ref_id=%s context block %s",
                ref_id,
                f"chunks={chunk_ids}"
                if chunk_ids
                else ("meta.abstract" if block else "empty"),
            )
            if not usable_context(title, block):
                # No paper prose to characterize — calling the model here
                # buys background recall dressed as provenance (gr346458).
                # Re-stamp at the current version on every unusable
                # outcome: the fresh ``at`` is what lets the heal arms in
                # _claim tell "new content arrived" from "still nothing"
                # (gr514381). The write no-ops when the ref moved since
                # the claim read — that bump already re-arms the heal.
                _stamp_no_abstract(store, ref_id, claimed_at)
                no_abstract += 1
                continue
            try:
                sentence = _generate_with_lint(client, title, block, ref_id=ref_id)
            except TransientFailure:
                deferred += 1
                continue
            if sentence is None:
                store.update_ref(
                    ref_id, meta_patch={META_FAILED_KEY: {"v": _PASS_VERSION}}
                )
                if was_no_abstract:
                    # The paper DID have usable context this sweep — the
                    # no-abstract stamp is stale, only the model verdict
                    # should remain.
                    store.clear_ref_meta_keys(
                        ref_id, [META_NO_ABSTRACT_KEY], source="context_sentence"
                    )
                failed += 1
                continue
            store.update_ref(ref_id, meta_patch={META_KEY: sentence})
            stamped = [
                key
                for key, present in (
                    (META_NO_ABSTRACT_KEY, was_no_abstract),
                    (META_FAILED_KEY, was_failed),
                )
                if present
            ]
            if stamped:
                store.clear_ref_meta_keys(ref_id, stamped, source="context_sentence")
            ok += 1
        except Exception:
            log.exception("context_sentence: failed ref_id=%s", ref_id)
            failed += 1
    if deferred:
        log.warning(
            "context_sentence: %d paper(s) deferred — no model reply "
            "(rate limit / dispatch failure); left unstamped for the next sweep",
            deferred,
        )
    if no_abstract:
        log.info(
            "context_sentence: %d paper(s) skipped — context block carries no "
            "paper prose (masthead/front matter); needs an abstract backfill, "
            "not a retry",
            no_abstract,
        )
    return {"claimed": len(rows), "ok": ok, "failed": failed}
