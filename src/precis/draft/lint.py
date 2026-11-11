"""Draft-write-path lint — advisory hints appended to a `draft` `put`/`edit`
`Response`, never a refusal (the write always lands; these are nudges an
authoring agent can act on or ignore). ONE exception: a write that
*introduces* a handle-shaped ``[…]`` reference resolving to nothing at all
is a hard ``BadInput`` (:func:`newly_unresolvable_tokens`, raised by the
handler before the write lands) — matching the web inline editor's 422
save-gate; tombstones and ``finding #slug`` placeholders stay advisory.
Each ``*_hint`` function inspects the
touched text (and, for the abbreviation/citation/dangling-reference checks,
what the write *changed* vs. what was already there — so re-reading a chunk
doesn't re-nag about a problem it didn't introduce) and returns either ``""``
(clean) or a ``\\n\\n``-prefixed advisory block to append to the body.

Covers: undefined/inline-only abbreviations, non-canonical citation forms
(bare ``paper:`` mentions, whole-paper vs. chunk cites, literal
``\\cite{...}``), a Taproot claim-hub cite nudge, malformed temperature/unit
notation, ``$…$`` math spans the exporters would demote to literal text
(:func:`math_form_hint`), dangling ``[...]``/``finding #slug`` references,
and — reported
distinctly from a plain dangling reference — a ``[...]`` cite whose target
is a real ref that has since been soft-deleted (a tombstone, gr265228),
a cite of a paper whose ingest flagged glyph damage
(:func:`glyph_cite_hint`, gr228652), a figure/table caption that
starts with its own float label, which the exporters' auto-numbering
would print doubled (:func:`caption_number_hint`, gr592852), and a
sentence that opens with a known lowercase identifier, which reads as a
typo at line start (:func:`sentence_start_hint`, gr592934), and an
unexplained in-house concept label — a coined term the draft uses that
resolves to a ``concept`` node with no literature provenance and no
definition, or that collides with a literature canonical term
(:func:`concept_label_hint`, gr626720).
Pure functions over an explicit :class:`~precis.store.store.Store` (no
handler `self`) — ``handlers/draft.py::DraftHandler`` is the sole caller,
wiring these into its ``put``/``edit``/``get`` bodies.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.nanopub.overview import HubOverviewRow
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Malformed temperature / unit notation the draft prose should not carry.
#: The canonical form is the literal sign spaced off the value per SI —
#: ``63 °C`` (degree sign U+00B0 + ``C``, ``°C`` being a unit symbol), a
#: range ``63–65 °C``, a tolerance ``±1 °C`` (U+00B1). An angle isn't a
#: unit symbol and stays tight (``85°``). Each pattern matches one *wrong*
#: spelling — including the unspaced ``63°C`` — so only the canonical
#: spaced form trips none of them. See ``temperature_form_hint``.
_BAD_TEMP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[℃℉]"),  # single-char degree-Celsius/Fahrenheit (U+2103/2109)
    re.compile(r"\\(?:circ|degree|textdegree|celsius|textcelsius)\b"),  # LaTeX
    re.compile(r"\^\s*\{?\s*\\?circ"),  # ^\circ / ^{\circ}
    re.compile(r"[ºᵒ⁰∘]\s*[CFcf]\b"),  # ordinal/superscript/ring + C/F
    re.compile(r"\d\s*[oO]\s*[CF]\b"),  # 'o' as degree: 63oC, 63 o C
    # SI puts a space between value and unit symbol, and `°C` is a unit
    # symbol: `63 °C`. The bare degree of an ANGLE is not a unit symbol and
    # stays tight (`85°`), so both patterns below require a trailing C/F and
    # leave angles alone.
    re.compile(r"\d°[CF]\b"),  # no space at all: 63°C
    re.compile(r"°\s+[CF]"),  # space on the wrong side: 63° C
    re.compile(r"\bdeg(?:rees?)?\.?\s*[CF]\b"),  # deg C / degrees C / degC
    re.compile(r"\bdegrees?\s+(?:celsius|fahrenheit)\b", re.IGNORECASE),  # spelt out
    re.compile(r"[+]\s*/\s*[-−]\s*\d"),  # +/- 1  (use ±)
    re.compile(r"(?<![\d.])\+-\s*\d"),  # +-1     (use ±)
)

#: ``[finding #<slug>]`` / ``citation pending — finding #<slug>`` — the
#: author-written placeholder form. Note this is NOT draft markup grammar
#: (which addresses a finding as the bare ``finding:<pub_id>`` mention): a
#: ``#<slug>`` label never autolinks and never exports.
_FINDING_MARKER = re.compile(r"finding\s+#(?P<slug>[A-Za-z][A-Za-z0-9-]+)")

#: A ``[<token>]`` prose reference that *looks* like a handle attempt — a
#: pure numeric id (the classic mistake, ``[45650]``) or a known 2-char code
#: + digits (``[me6184]``). A non-handle ``[see note]`` is not matched, so
#: prose stays untouched.
_CHUNK_REF = re.compile(r"\[(?P<h>[a-z]{2}\d+|\d+)\]")


def _bracket_number_bound_to_prose(text: str, start: int, end: int) -> bool:
    """True when the ``[<digits>]`` span ``text[start:end]`` is lexically
    fused to surrounding prose — IUPAC supramolecular nomenclature
    (``[2]rotaxane``, ``[24]crown-8``, ``calix[4]arene``, ``pillar[5]arene``,
    ``[60]fullerene``) rather than a bracketed handle attempt. Suffix form:
    the closing ``]`` is immediately followed by a letter, no space. Infix
    form: the opening ``[`` is immediately preceded by a letter. A
    standalone numeric like ``[45650]`` has neither neighbor and stays
    flagged."""
    if end < len(text) and text[end].isalpha():
        return True
    if start > 0 and text[start - 1].isalpha():
        return True
    return False


def find_whole_ref_citations(text: str) -> list[str]:
    """Bare non-chunk ``[pa<id>]``/``[pk<id>]`` handles in ``text`` — a
    citable-kind (paper/patent) reference to the *whole* document rather
    than the supporting chunk. Tolerated (a landmark/rhetorical mention of
    a whole paper is legitimate), but weaker than a ``[pc<id>]`` citation:
    it never names a specific passage, and later readers of this passage
    (a human or an editing pass) see it as a keyword-only view, never
    verbatim. ``[fi<id>]`` (finding) is never flagged — a finding has no
    internal chunks to drill to."""
    from precis.utils.mentions import BARE_BRACKET_REF_PATTERN

    out = []
    for m in BARE_BRACKET_REF_PATTERN.finditer(text or ""):
        bare = m.group("bare")
        if bare[0] in "¶§":
            continue
        parsed = handle_registry.parse(bare)
        if parsed is None:
            continue
        kind, is_chunk, _id = parsed
        # a ``~<sel>`` selector already names a specific passage — not a
        # whole-ref mention (``[pa5~3]``)
        if kind in ("paper", "patent") and not is_chunk and not m.group("sel"):
            out.append(bare)
    return out


def find_paper_cite_tokens(text: str) -> list[str]:
    """Every bare ``[pc<id>]``/``[pa<id>]``/``[pk<id>]`` handle in ``text``
    naming a paper or patent — whole-ref *or* chunk-level, unlike
    :func:`find_whole_ref_citations` which wants only the whole-ref form.
    Same ``BARE_BRACKET_REF_PATTERN`` + ``handle_registry.parse``
    extraction, reused rather than a fresh regex; deduped, appearance
    order. Feeds the Taproot claim-hub cite nudge (a ``[pc<id>]`` cite is
    exactly the form worth checking against the evidence graph)."""
    from precis.utils.mentions import BARE_BRACKET_REF_PATTERN

    seen: set[str] = set()
    out = []
    for m in BARE_BRACKET_REF_PATTERN.finditer(text or ""):
        bare = m.group("bare")
        if bare[0] in "¶§" or bare in seen:
            continue
        parsed = handle_registry.parse(bare)
        if parsed is None:
            continue
        kind, _is_chunk, _id = parsed
        if kind in ("paper", "patent"):
            seen.add(bare)
            out.append(bare)
    return out


def write_abbrev_hints(
    store: Store, slug: str, ref_id: int, new_text: str, old_text: str
) -> str:
    """Abbreviation feedback for one write, scoped to what it *introduced*
    (so editing a chunk doesn't re-nag about acronyms it already
    contained). Two disjoint hints:

    * **undefined** — acronym-shaped tokens with no definition anywhere
      in the draft (and new in this write): define or silence them.
    * **promote** — an inline ``Long Form (ABBR)`` first-use that works
      but lives only in this chunk's prose and isn't yet a glossary
      ``term``: offer to formalise it (durable across edits). The two
      never overlap — an inline-defined token isn't "undefined".
    """
    from precis.utils.abbreviations import find as _find
    from precis.utils.abbreviations import find_acronyms as _acr

    old_acr = _acr(old_text)
    undefined = [
        a for a in store.drafts.undefined_abbrevs(ref_id, new_text) if a not in old_acr
    ]
    old_pairs = _find(old_text)
    terms = store.drafts.draft_term_shorts(ref_id)
    promote = {
        short: long
        for short, long in _find(new_text).items()
        if short not in old_pairs and short not in terms
    }
    return abbrev_hint(slug, undefined) + promote_hint(slug, promote)


def promote_hint(slug: str, promote: dict[str, str]) -> str:
    """Offer to promote inline ``Long Form (ABBR)`` definitions to
    glossary ``term`` chunks — a hint, never a refusal (an inline
    first-use is correct, conventional writing; it's just fragile,
    since it lives in one chunk's prose)."""
    if not promote:
        return ""
    toks = ", ".join(promote)
    if len(promote) > 1:
        entries = ", ".join(
            f"{{'text': {long!r}, 'meta': {{'short': {short!r}}}}}"
            for short, long in promote.items()
        )
        call = f"put(kind='draft', id={slug!r}, chunk_kind='term', terms=[{entries}])"
    else:
        short, long = next(iter(promote.items()))
        call = (
            f"put(kind='draft', id={slug!r}, chunk_kind='term', "
            f"text={long!r}, meta={{'short': {short!r}}})"
        )
    return (
        f"\n\nℹ inline definition(s): {toks}. They work, but live only in "
        f"this chunk's prose — promote to the glossary so they survive edits: "
        f"{call}."
    )


def abbrev_hint(slug: str, undefined: list[str]) -> str:
    """A hint (appended to the write/edit Response) listing undefined
    abbreviations with copy-ready calls to define or silence them."""
    if not undefined:
        return ""
    toks = ", ".join(undefined)
    first = undefined[0]
    if len(undefined) > 1:
        define = (
            f"put(kind='draft', id={slug!r}, chunk_kind='term', terms=["
            f"{{'text': '<expansion>', 'meta': {{'short': {first!r}}}}}, …]) "
            f"(all of them in one call)"
        )
    else:
        define = (
            f"put(kind='draft', id={slug!r}, chunk_kind='term', "
            f"text='<expansion>', meta={{'short': {first!r}}})"
        )
    return (
        f"\n\n⚠ undefined abbreviation(s): {toks}. For each, either DEFINE it — "
        f"{define} — or, if it isn't an abbreviation, SILENCE "
        f"it: edit(kind='draft', id={slug!r}, not_abbrev=[{first!r}])."
    )


def citation_form_hint(text: str) -> str:
    """Nudge toward the canonical ``[pc<id>]`` paper-chunk citation
    when the text cites a paper by a bare ``paper:<id>`` mention —
    which resolves but is opaque, points at no specific passage, and
    exports to no ``\\cite``. The ``[pc<id>]`` handle (copied from
    ``search``/``get`` output) cites the exact supporting chunk, and
    the export engine renders the bibliography from it. Only the
    prefixed ``paper:`` form fires; bare ``[pc<id>]`` handles (the
    canonical form) are left alone."""
    from precis.utils import mentions

    seen: list[str] = []
    for m in mentions.REF_PATTERN.finditer(text):
        if m.group("kind") != "paper":
            continue
        ident = m.group("id").lstrip("#")
        suffix = m.group("chunk") or ""
        mention = f"paper:{ident}{suffix}"
        if mention not in seen:
            seen.append(mention)
    if not seen:
        return ""
    offenders = ", ".join(seen[:5])
    return (
        "\n\n⚠ cite the supporting paper *chunk* by its handle [pc<id>] "
        "(copy it from search/get output), not a bare paper: mention "
        f"(which exports to no \\cite): {offenders}."
    )


def whole_paper_cite_hint(store: Store, new_text: str, old_text: str) -> str:
    """Nudge toward the specific chunk when a bare whole-ref citation
    (``[pa<id>]``/``[pk<id>]`` — no chunk) is newly introduced in this
    write. Tolerated, never blocked — a landmark/rhetorical mention of
    a whole paper ("the Watson & Crick paper") is legitimate — but
    weaker than ``[pc<id>]`` for a specific claim: it never names a
    passage, and a later pass over this text (a reader, an editing
    agent) sees only the paper's keyword labels, never its text. Scoped
    to what this write introduced, mirroring the abbrev hint.

    A *whole-record cite* (gr514384 — a ``[pa<id>]`` naming a chunkless
    paper typed ``record_type`` ``software``/``dataset``) is exempt:
    there is no passage to drill to, so the hint would be wrong and
    train authors to ignore it. The exemption is the live predicate
    (:func:`precis.utils.record_cite.record_cite_handles`), never a
    cached flag."""
    from precis.utils.record_cite import record_cite_handles

    offenders = sorted(
        set(find_whole_ref_citations(new_text))
        - set(find_whole_ref_citations(old_text))
    )
    if offenders:
        offenders = sorted(set(offenders) - record_cite_handles(store, offenders))
    if not offenders:
        return ""
    shown = ", ".join(f"[{h}]" for h in offenders[:5])
    return (
        f"\n\n⚠ whole-paper citation, not a chunk: {shown}. Fine for a "
        "landmark/rhetorical mention of the paper itself; for a "
        "specific claim, cite the supporting chunk instead — [pc<id>] "
        "copied from search/get output, or drilled via "
        "get(kind='paper', id='<slug>~lo..hi', view='toc')."
    )


def _hub_posture_marker(row: HubOverviewRow | None) -> str:
    """`` [⚠ posture: …]`` marker for :func:`pc_cite_claim_hub_hint`, or
    ``""`` when the hub is clean (or its posture is unknown). Same
    three conditions, same ``is_refuted`` predicate, and the same
    ``disputed``/``drifted``/``refuted`` ordering as ``handlers/draft.py::
    _hygiene_hub_posture_lines`` — the nudge and the hygiene report must
    never disagree about what "bad posture" means.

    Bracketed and unpunctuated because it renders *mid-sentence*, right
    after the hub is named and before the line recommends citing it: a
    writer skimming must meet the warning before the instruction, or the
    instruction is what they act on."""
    if row is None:
        return ""
    from precis.handlers.finding import is_refuted

    conds = [
        name
        for name, on in (
            ("disputed", row.disputed),
            ("drifted", row.drifted),
            ("refuted", is_refuted(row)),
        )
        if on
    ]
    if not conds:
        return ""
    return f" [⚠ posture: {', '.join(conds)}]"


def pc_cite_claim_hub_hint(store: Store, text: str) -> str:
    """Nudge toward an existing Taproot claim hub when a paper/patent
    cite token (``[pc<id>]``/``[pa<id>]``/``[pk<id>]``) in ``text``
    names a paper that already grounds one (:func:`~precis.taproot.
    lookup.hubs_grounded_by_paper`). A ``[fi<id>]`` cite (the hub's
    kind+serial handle — the preferred form) is a *living* citation
     — it always resolves to the current derived
    originator(s), so it tracks new evidence without another edit,
    unlike a cite frozen on one paper/chunk. A NUDGE, never a
    refusal: the ``[pc<id>]``/``[pa<id>]`` cite stays exactly as valid
    as it was — this only offers a stronger alternative, or the
    ``[fi<id>>handle]`` pin to keep citing this
    exact passage while still riding the living resolution. Scoped to
    the cites actually present in ``text`` (the touched chunk), not
    the whole draft — cheap on the write path. Deduped by
    ``hub_ref_id`` — a paper grounding the same hub via two cite
    tokens in one chunk gets one nudge line.

    Each line also carries the hub's posture (:func:`_hub_posture_marker`)
    when it's ``refuted``/``disputed``/``drifted`` — steering a writer onto
    a bad-posture hub at the exact moment of citing would otherwise be
    silent. The marker sits directly after the hub is named and *before*
    the "cite this" recommendation, so a skimmer meets the warning before
    the instruction. A clean hub's line carries no marker at all
    (byte-identical to the pre-posture output). Posture is fetched in ONE ``hub_rows`` call
    for every hub collected, after the token loop — this runs on the draft
    write path, which must stay cheap. If that lookup raises, the nudge
    still renders (without posture) rather than taking down the write
    path: unlike ``finding.py::_postures``' strict mode — where a swallowed
    error would silently change a *filter's* results — this only omits
    decoration from an advisory hint, so degrading to "no posture shown"
    is honest, not a masked correctness bug. Do not "fix" this to raise."""
    from precis.taproot.lookup import hubs_grounded_by_paper
    from precis.utils.mentions import resolve_handle_target

    seen_hub_ref_ids: set[int] = set()
    hits: list[tuple[str, int, str, str]] = []
    for tok in find_paper_cite_tokens(text):
        target = resolve_handle_target(store, tok)
        if target is None:
            continue
        for hub in hubs_grounded_by_paper(store, target.dst_ref_id):
            hub_ref_id = hub["hub_ref_id"]
            if hub_ref_id in seen_hub_ref_ids:
                continue
            seen_hub_ref_ids.add(hub_ref_id)
            claim = hub["claim"] or ""
            hub_handle = handle_registry.format_handle("finding", hub_ref_id)
            hits.append((tok, hub_ref_id, hub_handle, claim))
    if not hits:
        return ""

    postures: dict[int, HubOverviewRow] = {}
    try:
        from precis.nanopub.overview import hub_rows

        postures = {
            row.ref_id: row for row in hub_rows(store, ref_ids=sorted(seen_hub_ref_ids))
        }
    except Exception:  # pragma: no cover — advisory decoration, not correctness
        log.warning("pc_cite_claim_hub_hint: posture read failed", exc_info=True)

    lines: list[str] = []
    for tok, hub_ref_id, hub_handle, claim in hits:
        marker = _hub_posture_marker(postures.get(hub_ref_id))
        lines.append(
            f"\n\n◆ taproot: {tok} grounds claim hub [{hub_handle}] "
            f'("{claim}")'
            f"{marker}"
            f" — cite [{hub_handle}] for living "
            f"resolution, or [{hub_handle}>{tok}] to pin this passage."
        )
    return "".join(lines)


def glyph_cite_hint(store: Store, text: str) -> str:
    """Warn per paper cited in ``text`` whose ingest flagged glyph damage
    (``meta.glyph_health.suspected``, gr228652): Greek/\u03bc characters may
    have been dropped or substituted in the extracted text, so quoted
    numbers/units can be off by 10^3-10^6x. Advisory only. Scoped to the
    cites present in the touched text; one batched ref fetch, deduped by ref.
    """
    from precis.utils.glyph_health import glyph_suspected
    from precis.utils.mentions import resolve_handle_target

    ids: dict[int, str] = {}
    for tok in find_paper_cite_tokens(text):
        target = resolve_handle_target(store, tok)
        if target is not None:
            ids.setdefault(target.dst_ref_id, tok)
    if not ids:
        return ""
    lines: list[str] = []
    for rid, ref in sorted(store.fetch_refs_by_ids(ids, include_deleted=False).items()):
        if ref.kind == "paper" and glyph_suspected(ref.meta):
            handle = handle_registry.try_format("paper", rid) or ids[rid]
            lines.append(
                f"\n\n\u25c6 glyphs: cites {handle}, whose extracted text may "
                "have lost Greek/\u03bc characters \u2014 check quoted "
                "numbers/units against the PDF"
            )
    return "".join(lines)


# ── In-house concept-label lint (gr626720) ─────────────────────────────
#
# A paper draft should not lean on a house-coined label the literature
# already names — or one nobody defined. ``concept_label_hint`` scans the
# touched prose for surfaces claimed by a live ``concept``/``taxon`` node
# and flags the in-house ones per the rules in its docstring.

#: Longest term-graph surface the label scan can match — concept and
#: taxon names are short term labels, never phrases.
_LABEL_MAX_WORDS = 4

#: Word shape for n-gram extraction (same shape as
#: ``utils/wordcount._WORD``: an alnum run optionally joined by an
#: internal apostrophe or hyphen, so ``g-C3N4``/``state-of-the-art``
#: stay one token).
_LABEL_WORD_RE = re.compile(r"[0-9A-Za-z]+(?:['’-][0-9A-Za-z]+)*")

#: The vocabulary-alignment convention (docs/backlog/
#: vocab-align-to-literature.md): a house term's node may name its
#: literature equivalent inside its own definition as ``(lit: …)``.
_LIT_MARKER_RE = re.compile(r"\(\s*lit\s*:\s*([^()]+)\)", re.IGNORECASE)


@dataclass(frozen=True)
class _TermNode:
    """A live ``concept``/``taxon`` node whose name or an alias surfaced
    in scanned text. ``surfaces`` is every normalized label the node
    answers to (name + aliases); ``hit_surfaces`` the subset the scanned
    text actually used. ``status`` is the taxon lifecycle marker
    (``'proposed'`` at mint, ``'systematic'`` earned) — ``""`` on kinds
    that don't carry one."""

    ref_id: int
    kind: str
    name: str
    definition: str
    status: str
    surfaces: frozenset[str]
    hit_surfaces: frozenset[str]


def _label_candidates(text: str) -> set[str]:
    """Every normalized word n-gram (1..``_LABEL_MAX_WORDS``) in ``text``
    — the candidate term surfaces probed against the term graph. Words
    are lowercased verbatim, so the joined n-grams are already in
    :func:`~precis.reading.concepts.normalize_name` form."""
    words = [w.lower() for w in _LABEL_WORD_RE.findall(text or "")]
    out: set[str] = set()
    for width in range(1, min(_LABEL_MAX_WORDS, len(words)) + 1):
        for i in range(len(words) - width + 1):
            out.add(" ".join(words[i : i + width]))
    return out


def _term_nodes(store: Store, candidates: set[str]) -> list[_TermNode]:
    """Live concept/taxon nodes claiming any ``candidates`` surface. The
    SQL is a coarse filter (``norm_name``, or a *lowered* stored alias,
    in the candidate set); the exact normalized match is re-checked in
    Python — ``lower()`` doesn't collapse internal whitespace, so the raw
    clause alone could credit a surface the stored form doesn't quite
    have."""
    if not candidates:
        return []
    from precis.reading.concepts import normalize_name

    cands = sorted(candidates)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind, title, meta FROM refs "
            "WHERE kind IN ('concept','taxon') AND retired_at IS NULL "
            "AND (meta->>'norm_name' = ANY(%s) OR EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text("
            "COALESCE(meta->'aliases', '[]'::jsonb)) a "
            "WHERE lower(a) = ANY(%s)))",
            (cands, cands),
        ).fetchall()
    out: list[_TermNode] = []
    for ref_id, kind, title, meta in rows:
        m = meta or {}
        aliases = m.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        surfaces = {normalize_name(str(a)) for a in aliases}
        surfaces.add(
            str(m.get("norm_name"))
            if m.get("norm_name")
            else normalize_name(str(m.get("name") or title or ""))
        )
        surfaces.discard("")
        hit = frozenset(surfaces & candidates)
        if not hit:
            continue
        out.append(
            _TermNode(
                ref_id=int(ref_id),
                kind=str(kind),
                name=str(m.get("name") or title or ""),
                definition=str(m.get("definition") or ""),
                status=str(m.get("status") or ""),
                surfaces=frozenset(surfaces),
                hit_surfaces=hit,
            )
        )
    return out


def _lit_sourced(store: Store, ref_ids: list[int]) -> set[int]:
    """The subset of ``ref_ids`` carrying an outbound ``derived-from``
    edge to a live paper/patent — the shipped literature-provenance
    signal (the glossary promotion pass stamps exactly this edge;
    :mod:`precis.reading.promote`). Kind-agnostic: a taxon carrying the
    edge is literature-sourced the same way a concept is."""
    if not ref_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT l.src_ref_id FROM links l "
            "JOIN refs d ON d.ref_id = l.dst_ref_id AND d.retired_at IS NULL "
            "WHERE l.relation = 'derived-from' AND l.src_ref_id = ANY(%s) "
            "AND d.kind IN ('paper', 'patent')",
            (sorted(ref_ids),),
        ).fetchall()
    return {int(r[0]) for r in rows}


def concept_label_hint(
    store: Store,
    slug: str,
    ref_id: int,
    new_text: str,
    old_text: str = "",
    chunk_kind: str | None = None,
) -> str:
    """Advisory ⚠ for unexplained in-house concept labels in draft prose
    (gr626720 — the small consumer of the concept-identity contract).

    A "label" is a surface a live ``concept``/``taxon`` node claims —
    its name or an alias — appearing in the touched text. A claimant is
    **canonical** when it carries literature provenance — a
    ``derived-from`` edge to a paper/patent (the shipped signal;
    :mod:`precis.reading.promote`) — or, for a ``taxon``, the earned
    ``status='systematic'`` (:func:`precis.taxonomy.select.promote`).
    Everything else is **in-house**: an unsourced ``concept``, a
    ``proposed`` taxon (an in-house proposal, not the literature's
    word), or any node whose definition carries the ``(lit: …)`` marker
    the vocabulary-alignment convention uses for "the literature's word
    is X". Provenance is checked on every claimant — including the ones
    an in-house label's *other* surfaces pull in. For each newly
    introduced in-house label:

    * a literature claimant on the *same* surface, or any second
      claimant at all — an ambiguous equivalent: flag for review, never
      pick a side;
    * the literature canonical named by a ``(lit: …)`` marker, or a
      literature claimant on one of the node's *other* surfaces — flag
      and prefer the canonical (the shared surface may be coincidental,
      so this reads as a review suggestion);
    * no definition anywhere (node ``meta.definition`` empty, no draft
      glossary ``term`` leaf for the used surface) — flag as
      unexplained, with the copy-ready glossary call;
    * defined — a deliberately introduced term, allowed.

    Aliases are never rewritten — the hint only names. Scoped to the
    surfaces this write introduced (mirroring the abbrev hint) and to
    prose chunk kinds (a ``term`` leaf IS a definition; ``code`` is
    verbatim payload). Advisory only, same as every hint in this module.

    What waits on the concept-identity contract (me626712): true
    sense-level equivalence — an in-house label for a thing the
    literature names *differently* with no shared surface can't be seen
    yet, and per-alias provenance will sharpen "in-house" from node-level
    to per-label."""
    from precis.reading.concepts import normalize_name
    from precis.utils.wordcount import PROSE_CHUNK_KINDS

    if (
        chunk_kind is not None
        and chunk_kind not in PROSE_CHUNK_KINDS
        and chunk_kind != "figure"
    ):
        return ""
    candidates = _label_candidates(new_text)
    if not candidates:
        return ""
    nodes = _term_nodes(store, candidates)
    if not nodes:
        return ""
    old_surfaces: set[str] = set()
    if old_text:
        for n in _term_nodes(store, _label_candidates(old_text)):
            old_surfaces |= n.hit_surfaces
    claims: dict[str, list[_TermNode]] = {}
    used_by: dict[int, set[str]] = {}
    for n in nodes:
        for s in n.hit_surfaces:
            claims.setdefault(s, []).append(n)
        fresh = set(n.hit_surfaces) - old_surfaces
        if fresh:
            used_by[n.ref_id] = fresh
    if not used_by:
        return ""
    lit_ids = _lit_sourced(store, [n.ref_id for n in nodes])

    def _lit_marker(n: _TermNode) -> str | None:
        m = _LIT_MARKER_RE.search(n.definition)
        return m.group(1).strip() if m else None

    def _is_lit(n: _TermNode) -> bool:
        # A ``(lit: …)`` marker keeps the node in-house — it names the
        # literature word rather than being it. Without one, canonical
        # means literature provenance (``derived-from`` → paper/patent)
        # or, for a taxon, the earned ``systematic`` status — a
        # ``proposed`` taxon is an in-house proposal like an unsourced
        # concept.
        if _lit_marker(n) is not None:
            return False
        return n.ref_id in lit_ids or (n.kind == "taxon" and n.status == "systematic")

    inhouse = [n for n in nodes if n.ref_id in used_by and not _is_lit(n)]
    if not inhouse:
        return ""

    # Claimants of an in-house node's *other* surfaces — "X, aka Y" where
    # the canonical registry knows Y. One batched probe for all in-house
    # nodes, then intersected per node below. Provenance follows the
    # probe: a lit-sourced claimant reachable only through an alias is
    # still a canonical.
    other_surfaces = {s for n in inhouse for s in n.surfaces if s not in candidates}
    extra = _term_nodes(store, other_surfaces)
    lit_ids |= _lit_sourced(store, [n.ref_id for n in extra])
    by_id = {n.ref_id: n for n in nodes + extra}

    draft_terms: set[str] | None = None

    def _draft_defines(surface: str) -> bool:
        nonlocal draft_terms
        if draft_terms is None:
            draft_terms = {
                normalize_name(k) for k in store.drafts.defined_terms(ref_id)
            }
        return surface in draft_terms

    # One ambiguous-equivalent line per collided surface, naming every
    # claimant — review picks a side, never the lint.
    lines: list[str] = []
    ambiguous: set[str] = set()
    for n in inhouse:
        ambiguous |= {
            s
            for s in used_by[n.ref_id]
            if len({r.ref_id for r in claims.get(s, ())}) > 1
        }
    for s in sorted(ambiguous):
        shown = ", ".join(
            f"'{r.name}' [{handle_registry.format_handle(r.kind, r.ref_id)}]"
            for r in sorted(claims[s], key=lambda r: r.ref_id)[:4]
        )
        lines.append(
            f"\n\n⚠ ambiguous label '{s}': claimed by {shown} — review "
            "which the draft means; on a true collision the literature "
            "term wins."
        )

    for n in inhouse:
        used = sorted(s for s in used_by[n.ref_id] if s not in ambiguous)
        if not used:
            continue
        surface = used[0]
        handle = handle_registry.format_handle(n.kind, n.ref_id)
        label = f"'{surface}' [{handle}]"
        canon = [
            d
            for d in by_id.values()
            if d.ref_id != n.ref_id and _is_lit(d) and d.surfaces & n.surfaces
        ]
        marker = _lit_marker(n)
        if marker or canon:
            if canon:
                d = sorted(canon, key=lambda d: d.ref_id)[0]
                dh = handle_registry.format_handle(d.kind, d.ref_id)
                what = f"'{d.name}' [{dh}]"
                what += f" (+{len(canon) - 1} more)" if len(canon) > 1 else ""
            else:
                what = f"'{marker}'"
            lines.append(
                f"\n\n⚠ in-house label {label}: a literature canonical "
                f"exists — {what}. Prefer it, or confirm the equivalence "
                "is real (a shared name can be a coincidence)."
            )
            continue
        if n.definition.strip() or any(_draft_defines(s) for s in used):
            continue
        lines.append(
            f"\n\n⚠ unexplained in-house label {label}: define it — "
            f"put(kind='draft', id={slug!r}, chunk_kind='term', "
            f"text='<definition>', meta={{'short': {surface!r}}}) — or use "
            "the literature term if one exists."
        )
    if not lines:
        return ""
    out = "".join(lines[:5])
    if len(lines) > 5:
        out += f"\n  (+{len(lines) - 5} more in-house label(s))"
    return out


def pin_hint(store: Store, new_text: str, old_text: str = "") -> str:
    """Pin warnings for a write (G3 coverage, G4 pin rule — see
    :mod:`precis.nanopub.pin_lint`): a ``[fi<hub>>pc<chunk>]`` /
    ``[fi<hub>+pc<chunk>]`` whose pinned passage(s) no longer carry a term
    the pinned sentence names, or whose pinned paper is outside the hub's
    frozen grounding. Fires only on what this write *changed* — a new pin,
    or a sentence edit that uncovered a term — so re-saving a chunk never
    re-nags about pins it left alone. Advisory; a failing check degrades to
    no hint rather than taking the write path down."""
    if "[fi" not in new_text:
        return ""
    try:
        from precis.nanopub import pin_lint

        lines = [
            line
            for f in pin_lint.edit_findings(store, new_text, old_text)
            for line in pin_lint.render_finding(f)
        ]
    except Exception:  # pragma: no cover — advisory, not correctness
        log.warning("pin_hint failed", exc_info=True)
        return ""
    return "".join(f"\n\n⚠ pin: {line}" for line in lines)


def literal_cite_hint(text: str) -> str:
    r"""Flag a literal ``\cite{...}`` / ``\citequote{...}`` typed into a
    draft body. In a draft you cite by writing the supporting paper-
    chunk handle inline (``[pc<id>]``); the export engine emits the
    ``\cite`` + bibliography, so a hand-written cite key resolves to
    nothing. Fires only on draft chunks — a real ``.tex`` *file* keeps
    its literal ``\cite`` as source (see precis-tex-help)."""
    if re.search(r"\\cite(?:quote|p|t|alp|author|year)?\s*\{", text):
        return (
            "\n\n⚠ you typed a literal \\cite/\\citequote in the draft. "
            "Cite by the supporting paper-chunk handle inline instead: "
            "[pc<id>] (copy it from search/get output). The export engine "
            "writes the \\cite and the bibliography; \\cite/\\citequote "
            "are export-only output, never authored in a draft."
        )
    return ""


#: A bibliographic identifier typed into prose (gripe 454749): an arXiv id
#: (new ``2604.13729`` or old ``hep-th/9901001`` form, ``arXiv`` prefix
#: required so a bare decimal never trips it) or a DOI.
_BARE_ID_RE = re.compile(
    r"\barxiv\s*:?\s*(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
    r"|\b10\.\d{4,9}/[^\s\])>,;]+",
    re.IGNORECASE,
)


def bare_identifier_hint(text: str) -> str:
    """Advisory ⚠ for a raw arXiv id / DOI in draft prose. An identifier typed
    inline is a dead string: it does not resolve, join the cite graph, follow a
    preprint to its published version, or get caught by cite-drift detection —
    the ``[pa<id>]``/``[pc<id>]`` handle does all four, and the export renders
    the bibliography entry itself. A hint, never a refusal: a chunk *about*
    identifier formats may quote one verbatim and can ignore it."""
    found: list[str] = []
    for m in _BARE_ID_RE.finditer(text):
        ident = m.group(0).rstrip(".")
        if ident not in found:
            found.append(ident)
    if not found:
        return ""
    shown = ", ".join(repr(f) for f in found[:5])
    return (
        f"\n\n⚠ bare identifier in prose: {shown}. Cite the paper by handle "
        "([pa<id>] / [pc<id>]) instead — the export renders the identifier and "
        "link, and a raw id never resolves or tracks the published version. "
        "(Ignore if this chunk quotes an identifier format on purpose.)"
    )


def temperature_form_hint(text: str) -> str:
    r"""Nudge toward the canonical plain-text temperature/unit notation
    when the prose carries a malformed spelling: a superscript or
    single-character degree (``℃``, ``63ºC``), a missing or misplaced
    space (``63°C`` / ``63° C``), an ``o``-as-degree (``63oC``), LaTeX
    (``^\circ`` / ``\degree``), the spelt-out "degrees Celsius", or
    ``+/-`` for a tolerance. The wanted form is the literal Unicode sign
    spaced off the value — ``63 °C``, a range ``63–65 °C``, a tolerance
    ``±1 °C`` — so the canonical spelling trips none of the patterns and
    fires no hint. A hint, never a refusal: the write still lands.

    The spacing follows SI: a space separates a value from a unit symbol,
    and ``°C`` is a unit symbol. The degree of an **angle** is not, and
    stays tight (``85°``) — see the "percent / degrees" row of the
    ``precis-notation-canon`` skill, which governs claim sentences under
    the same rule so the two surfaces cannot disagree."""
    offenders: list[str] = []
    for pat in _BAD_TEMP_PATTERNS:
        for m in pat.finditer(text):
            snippet = m.group(0).strip()
            if snippet and snippet not in offenders:
                offenders.append(snippet)
    if not offenders:
        return ""
    shown = ", ".join(repr(o) for o in offenders[:5])
    return (
        "\n\n⚠ temperature/unit formatting: write the literal sign spaced "
        "off the value — `63 °C` (space, degree sign `°`, then `C`), a range "
        "`63–65 °C`, a tolerance `±1 °C` (the `±` sign). An angle keeps no "
        "space (`85°`). No superscript, no `℃`, no "
        'LaTeX (`^\\circ`, `\\degree`), no spelt-out "degrees Celsius", '
        f"no `+/-`. Found: {shown}."
    )


def house_style_hint(text: str, chunk_kind: str | None = None) -> str:
    """Advisory ⚠ for prose that breaks the plain-prose house style: an
    em-dash, ``**bold**``, ``*italic*``, ``_italic_`` or a ``--`` double
    hyphen outside math, code, URLs and handle brackets
    (``utils/house_style.py``). Each line names the span, its position and
    the fix. A hint, never a refusal: the write still lands. A known
    ``chunk_kind`` outside prose and figure captions (table, equation,
    code, …) is skipped, matching the hygiene view's scan."""
    from precis.utils.house_style import find_style_flags
    from precis.utils.wordcount import PROSE_CHUNK_KINDS

    if (
        chunk_kind is not None
        and chunk_kind not in PROSE_CHUNK_KINDS
        and chunk_kind != "figure"
    ):
        return ""
    flags = find_style_flags(text)
    if not flags:
        return ""
    lines = [
        f"  - {f.rule} at {f.line}:{f.col} `{f.snippet[:40]}`: {f.fix}"
        for f in flags[:5]
    ]
    more = f"\n  (+{len(flags) - 5} more)" if len(flags) > 5 else ""
    return (
        f"\n\n⚠ house style: {len(flags)} plain-prose violation(s) "
        "(no em-dash, no **bold**/*italic*/_italic_, no `--`):\n"
        + "\n".join(lines)
        + more
    )


#: Built-in lowercase-by-nature identifiers — product/tool names a draft
#: sentence may legitimately open with, which read as a typo lowercased
#: at line start (gr592934). The draft's glossary ``term`` shorts join
#: this set via :func:`lowercase_identifiers`.
LOWERCASE_IDENTIFIERS: frozenset[str] = frozenset(
    {"autocatpath", "catpath", "land_slice", "precis"}
)

#: Identifier shape for a lowercase-by-nature glossary short: two or more
#: chars of lowercase/digit/underscore/hyphen — ``land_slice``,
#: ``g-c3n4``. ``GFET`` (uppercase is a fine sentence start), ``e.g.``
#: (dotted) and a single letter (too FP-prone — "a", "i") never qualify.
_LOWERCASE_IDENT = re.compile(r"[a-z][a-z0-9_-]+")


def lowercase_identifiers(store: Store, ref_id: int) -> frozenset[str]:
    """The draft's known lowercase identifiers — the element list
    :func:`~precis.utils.house_style.find_sentence_start_identifiers`
    checks sentence openers against: the built-in
    :data:`LOWERCASE_IDENTIFIERS` plus every identifier-shaped
    all-lowercase glossary ``term`` short."""
    ids = set(LOWERCASE_IDENTIFIERS)
    ids.update(
        s
        for s in store.drafts.draft_term_shorts(ref_id)
        if _LOWERCASE_IDENT.fullmatch(s)
    )
    return frozenset(ids)


def sentence_start_hint(
    store: Store,
    ref_id: int,
    text: str,
    old_text: str = "",
    chunk_kind: str | None = None,
) -> str:
    """Advisory ⚠ for a sentence that opens with a known lowercase
    identifier (gr592934): "autocatpath, the reaction engine, …" reads
    as a typo in the export and house style forbids an italic/bold
    rescue, so the fix is a leading 'The <identifier> …' or `` `…` ``
    inline code — both exporters render the code span (``\\texttt`` in
    the PDF, Consolas in docx), keeping the lowercase form true. Scoped
    to identifiers this write introduced (mirroring the abbrev hint).
    A hint, never a refusal. Same prose-only chunk-kind skip as
    :func:`house_style_hint`."""
    from precis.utils.house_style import find_sentence_start_identifiers
    from precis.utils.wordcount import PROSE_CHUNK_KINDS

    if (
        chunk_kind is not None
        and chunk_kind not in PROSE_CHUNK_KINDS
        and chunk_kind != "figure"
    ):
        return ""
    ids = lowercase_identifiers(store, ref_id)
    new = {f.snippet for f in find_sentence_start_identifiers(text, ids)}
    if old_text:
        new -= {f.snippet for f in find_sentence_start_identifiers(old_text, ids)}
    if not new:
        return ""
    shown = ", ".join(sorted(new)[:5])
    return (
        f"\n\n⚠ sentence opens with lowercase identifier(s): {shown}. "
        "A lowercase-by-nature name reads as a typo at line start — open "
        "with 'The <identifier> …' or set it in `backticks` (renders "
        "\\texttt in the PDF, Consolas in docx)."
    )


def math_form_hint(new_text: str, old_text: str = "") -> str:
    r"""Advisory ⚠ for ``$…$`` math spans the LaTeX exporter would DEMOTE
    to escaped literal prose — unbalanced ``{ }`` braces, or prose/currency
    accidentally paired between two ``$`` — plus a stray unpaired ``$``.
    The judgment is delegated to ``export/latex.py::lint_math_spans``, the
    exporter's own demotion predicates, so this warning and the export
    behaviour can never disagree. Scoped to what this write *introduced*
    (a complaint already present in ``old_text`` is not re-nagged),
    mirroring the abbrev hint. A hint, never a refusal."""
    from precis.export.latex import lint_math_spans

    old = set(lint_math_spans(old_text)) if old_text else set()
    complaints = [c for c in lint_math_spans(new_text) if c not in old]
    if not complaints:
        return ""
    shown = "; ".join(complaints[:5])
    return (
        f"\n\n⚠ math that won't render: {shown}. The LaTeX/docx exporters "
        "demote such spans to escaped literal text instead of math — fix "
        "the span, or escape a literal dollar as \\$."
    )


#: An intra-draft chunk cross-ref: legacy ``[¶base58]`` or ``[dc<id>]``
#: (``[¶dc123]`` too). Exports render it as ``\cref`` ("section 1.2").
_XREF_STRICT_RE = re.compile(r"\[(?:¶[A-Za-z0-9]+|dc\d+)\]")

#: Words (or a bracket/comma) a cross-ref may directly follow without
#: reading as a bare, dangling pointer.
_XREF_LEAD_ENDS: tuple[str, ...] = (
    "subsection",
    "section",
    "sec.",
    "see",
    "fig.",
    "figure",
    "eq.",
    "equation",
    "(",
    ",",
)


def bare_xref_hint(new_text: str, old_text: str = "") -> str:
    """Advisory ⚠ for a ``[¶…]`` / ``[dc<id>]`` cross-ref dropped into prose
    with no lead-in ("... lattice [¶dc123]."): the export prints a bare
    "section 1.4.3" with no sentence role. The lead-in wants "see", a
    "section"/"figure"/"equation" word, an opening bracket or a comma. A
    cross-ref after another cross-ref (``[¶a] and [¶b]``) is exempt.
    Scoped to refs this write introduced. A hint, never a refusal."""
    already = set(_XREF_STRICT_RE.findall(old_text)) if old_text else set()
    bare: list[str] = []
    for m in _XREF_STRICT_RE.finditer(new_text):
        token = m.group(0)
        lead = new_text[: m.start()].rstrip().lower()
        if not lead or lead.endswith(_XREF_LEAD_ENDS):
            continue
        if re.search(r"\]\s*(?:and|or|&)?$", lead):
            continue
        if token in already or token in bare:
            continue
        bare.append(token)
    if not bare:
        return ""
    return "".join(
        f"\n\n⚠ bare cross-ref {t}: write 'see [¶…]' or '([¶…])'" for t in bare[:5]
    )


#: A figure/table caption that opens with its own float label — both
#: exporters prepend the assigned number themselves ("Figure 3: …" /
#: "Table 2: …"), so an authored "Figure 1b. …"/"Table 0. …" prints
#: doubled and can never match the exporter's count (gr592852).
_CAPTION_SELF_LABEL_RE = re.compile(r"^\s*(?:figure|fig\.?|table)\s*\d", re.IGNORECASE)


def caption_self_label(caption: str) -> str | None:
    """The float label a caption opens with ("Figure 1b", "table 0"), or
    ``None`` — shared by the write-path hint and the hygiene report."""
    m = _CAPTION_SELF_LABEL_RE.match(caption or "")
    return m.group(0).strip() if m else None


def table_caption_text(meta: dict[str, Any] | None, text: str | None) -> str:
    """A table chunk's legend as the exporters see it — canonical
    ``meta.caption``, else the ``**…**`` lead-in or a raw ``\\caption{}``
    in the text (:func:`table_payload`). ``""`` when there is none."""
    from precis.utils.table_data import table_payload

    return (table_payload(meta, text) or {}).get("caption") or ""


def caption_number_hint(
    text: str = "",
    chunk_kind: str | None = None,
    caption: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """Advisory ⚠ for a figure/table caption that begins with its own
    float label ("Figure 1b. …", "Table 0. …"): the exporters prepend
    "Figure N:"/"Table N:" themselves, so the authored label prints
    doubled ("Figure 3: Figure 1b. …"). ``text`` is the written chunk
    text (a figure's caption IS its text); ``caption`` the resolved table
    legend when the caller already has it — else a table's legend is
    recovered the same way the exporters do (:func:`table_payload`: the
    canonical ``meta.caption``, else the ``**…**`` lead-in or a raw
    ``\\caption{}`` in the text). A hint, never a refusal: the write
    still lands."""
    if chunk_kind not in ("figure", "table"):
        return ""
    cap = caption
    if cap is None:
        cap = text if chunk_kind == "figure" else table_caption_text(meta, text)
    if caption_self_label(cap) is None:
        return ""
    shown = cap.strip()[:60]
    noun = "Figure" if chunk_kind == "figure" else "Table"
    return (
        f"\n\n⚠ caption starts with its own float label ({shown!r}): the "
        f"export prepends '{noun} N:' itself, so this prints doubled "
        f"('{noun} 3: {shown}'). Drop the authored number — reference the "
        "float from prose with [dc<chunk-id>], which renders the assigned "
        "number, so a re-ordered draft never goes stale."
    )


def title_heading_dup_hint(
    store: Store, ref_id: int, chunk_id: int, chunk_kind: str | None, text: str
) -> str:
    """Advisory ⚠ when a heading chunk that sits first in reading order
    repeats the draft title (case-insensitive, whitespace-normalised): the
    exporter prints the title once already, so the heading shows it twice.
    A hint, never a refusal."""
    if chunk_kind != "heading":
        return ""

    def norm(t: str) -> str:
        return " ".join((t or "").split()).casefold()

    try:
        ref = store.get_ref(kind="draft", id=int(ref_id))
        order = store.drafts.reading_order(int(ref_id))
    except Exception:  # pragma: no cover — advisory, not correctness
        log.warning("title_heading_dup_hint failed", exc_info=True)
        return ""
    title = norm(getattr(ref, "title", "") or "")
    if not title or not order or order[0].chunk_id != chunk_id:
        return ""
    if norm(text) != title:
        return ""
    return (
        "\n\n⚠ duplicate title: this first heading repeats the draft title "
        f"({text.strip()[:60]!r}), so the export shows the title twice. "
        "Delete the heading or rename it to a real section."
    )


def dangling_finding_tokens(store: Store, text: str) -> list[str]:
    """The ``finding #slug`` markers in ``text`` that resolve to no live
    finding ref — the placeholder slugs a reader could mistake for a real
    citation. Order-preserving, deduped."""
    from precis.utils import mentions

    seen: list[str] = []
    dangling: list[str] = []
    for m in _FINDING_MARKER.finditer(text):
        slug = m.group("slug")
        if slug in seen:
            continue
        seen.append(slug)
        ref = mentions.resolve_handle_ref(store, slug)
        if ref is None or getattr(ref, "kind", None) != "finding":
            dangling.append(slug)
    return dangling


def dangling_finding_hint(store: Store, text: str) -> str:
    """Flag ``[finding #slug]`` markers that resolve to no finding ref
    (Fix C). The author leaves these as 'citation pending' placeholders;
    on a verbatim read they're indistinguishable from a real, linked
    citation. Resolve each marker's slug against the finding store and
    warn about the ones that don't land — so a reader can't mistake a
    placeholder for a live citation."""
    dangling = dangling_finding_tokens(store, text)
    if not dangling:
        return ""
    toks = ", ".join(f"#{s}" for s in dangling)
    return (
        f"\n\n⚠ unresolved finding reference(s): {toks}. These resolve to "
        "no finding ref — they're 'citation pending' placeholders, not live "
        "citations, and won't autolink or export. For each, either create "
        "the finding (put(kind='finding', …)) and cite it by its handle "
        "(finding:<pub_id>), or remove the marker."
    )


def _classify_chunk_ref_tokens(
    store: Store, text: str
) -> tuple[list[str], list[tuple[str, int]]]:
    """One pass over ``_CHUNK_REF`` handle-attempt tokens in ``text``,
    split into ``(fully-unresolvable, tombstone)``:

    * **fully-unresolvable** — a pure numeric id (``[45650]``) or a known
      type-code prefix that backs no store row at all (:func:`dangling_
      chunk_tokens`'s return).
    * **tombstone** — ``(handle, ref_id)`` for a handle naming a *real*,
      ref-level target (e.g. ``[fi42]``) that ``store.resolve_handle``
      can't resolve because the ref has been soft-deleted (gr265228),
      not because it never existed. Reported distinctly: the token still
      *looks* like a live citation, so lumping it in with "resolves to
      nothing" would understate the failure — a reader (or a conversion
      agent scanning for "does this chunk already carry a grounding
      cite?") can't tell a tombstone from a real one without this.
      Scoped to ref-level handles (chunk-level tombstones — e.g. a
      retired paper chunk under a still-live paper — fall through to
      the plain dangling bucket, same as before this split).

    A bare ``[ab12]`` with an unknown code is left as literal prose,
    flagged as neither. Order-preserving, deduped.

    A numeric match lexically bound to surrounding prose is IUPAC
    supramolecular nomenclature, not a handle attempt: ``[2]rotaxane``
    (suffix — ``]`` directly followed by a letter) or ``calix[4]arene``
    (infix — ``[`` directly preceded by a letter). Those are skipped
    (gr263760); a standalone ``[45650]`` bounded by space/punctuation is
    still flagged. Non-numeric handles are unaffected — a handle prefix
    like ``pa``/``dc`` never collides with a chemical name here."""
    seen: list[str] = []
    dangling: list[str] = []
    tombstones: list[tuple[str, int]] = []
    for m in _CHUNK_REF.finditer(text):
        h = m.group("h").strip()
        if h in seen:
            continue
        seen.append(h)
        # Only nag on a real handle attempt: a pure numeric, or a known
        # type-code prefix. A bare ``[ab12]`` (unknown code) is left as
        # literal prose, not flagged.
        if not h.isdigit():
            try:
                handle_registry.kind_for_code(h[:2])
            except KeyError:
                continue
        elif _bracket_number_bound_to_prose(text, m.start(), m.end()):
            continue
        try:
            if store.resolve_handle(h) is not None:
                continue
        except Exception:  # pragma: no cover — store hiccup, don't nag
            continue
        # Unresolved: nothing there at all, or a real ref-level row
        # that's been soft-deleted underneath the cite (a tombstone).
        parsed = handle_registry.parse(h)
        if parsed is not None:
            kind, is_chunk, pk = parsed
            if not is_chunk:
                try:
                    target = store.fetch_refs_by_ids([pk], include_deleted=True).get(pk)
                except Exception:  # pragma: no cover — store hiccup
                    target = None
                if (
                    target is not None
                    and target.kind == kind
                    and target.retired_at is not None
                ):
                    tombstones.append((h, pk))
                    continue
        dangling.append(h)
    return dangling, tombstones


def dangling_chunk_tokens(store: Store, text: str) -> list[str]:
    """The ``[<handle>]`` references in ``text`` that resolve to *nothing
    at all* — see :func:`_classify_chunk_ref_tokens`. A handle naming a
    real but soft-deleted ref-level target is reported separately by
    :func:`tombstone_chunk_tokens`, not here."""
    dangling, _ = _classify_chunk_ref_tokens(store, text)
    return dangling


def tombstone_chunk_tokens(store: Store, text: str) -> list[tuple[str, int]]:
    """``[(handle, ref_id), ...]`` for ``[<handle>]`` references in
    ``text`` naming a real ref-level target that has been soft-deleted
    (gr265228) — see :func:`_classify_chunk_ref_tokens`."""
    _, tombstones = _classify_chunk_ref_tokens(store, text)
    return tombstones


def dangling_chunk_hint(store: Store, text: str) -> str:
    """Flag ``[<handle>]`` references that resolve to nothing. A handle is
    a ref to *something* (a chunk ``dc<id>``, a memory ``me<id>``, a paper
    chunk ``pc<id>``, …); an LLM that writes a numeric id (``[45650]``) or
    a typo'd handle produces a dead link. Warn here so the author fixes it
    to a handle the outline / search actually shows."""
    dangling = dangling_chunk_tokens(store, text)
    if not dangling:
        return ""
    toks = ", ".join(f"[{h}]" for h in dangling)
    return (
        f"\n\n⚠ unresolved reference(s): {toks}. A `[…]` reference must be a "
        "handle that resolves to something (a chunk `dc<id>`, a memory "
        "`me<id>`, a paper chunk `pc<id>`, …), not a numeric id — use the "
        "handle the outline / search shows, or remove the reference."
    )


def tombstone_chunk_hint(store: Store, text: str) -> str:
    """Flag ``[<handle>]`` references whose target is a real ref that has
    been soft-deleted (gr265228) — distinct from :func:`dangling_chunk_hint`'s
    "resolves to nothing" case: the token still LOOKS like a live citation
    (a conversion agent scanning the chunk for "already cited?" reads it as
    grounded), but the ref underneath it is gone, so the prose it decorates
    is silently ungrounded. One line per offender, matching the audit's
    wording (gr265228)."""
    tombstones = tombstone_chunk_tokens(store, text)
    if not tombstones:
        return ""
    lines = [
        f"\n\n⚠ cite target {h} is deleted (tombstone) — the ref was "
        "soft-deleted; the citation still renders but grounds nothing. "
        "Re-point it to the successor (if this hub was merged/superseded) "
        "or remove the cite."
        for h, _ref_id in tombstones
    ]
    return "".join(lines)


def newly_dangling(
    store: Store, new_text: str, old_text: str
) -> tuple[list[str], list[str]]:
    """``(newly-broken chunk-ref tokens, newly-broken finding slugs)`` — the
    references that resolve to nothing in ``new_text`` and were *not* already
    dead in ``old_text``. Pre-existing dead refs are the author's standing
    debt, not this edit's regression, so they are excluded.

    This is the shared core of the inline-editor validation gate
    (``docs/backlog/draft-inline-editor.md``): the web editor turns the same
    old-vs-new diff into a **hard** save-block ("comes back at you if you
    broke something serious"), while the MCP/CLI edit path
    (`dangling_edit_hint`) surfaces it as a **non-blocking** ⚠ so an
    autonomous planner minting a forward reference is warned, not stalled.

    A tombstone cite (:func:`tombstone_chunk_tokens` — a handle naming a
    real but soft-deleted ref-level target, gr265228) folds into the
    ``chunk`` leg here too: this diff gate's job is "did the edit newly
    break a reference", and a tombstone is broken by that measure even
    though it's reported with its own distinct wording on the read path
    (:func:`tombstone_chunk_hint`)."""

    def _broken(text: str) -> list[str]:
        dangling, tombstones = _classify_chunk_ref_tokens(store, text)
        return dangling + [h for h, _ref_id in tombstones]

    old_bad = set(_broken(old_text))
    chunk = [h for h in _broken(new_text) if h not in old_bad]
    old_find = set(dangling_finding_tokens(store, old_text))
    find = [s for s in dangling_finding_tokens(store, new_text) if s not in old_find]
    return chunk, find


def newly_unresolvable_tokens(
    store: Store, new_text: str, old_text: str = ""
) -> list[str]:
    """The handle-shaped ``[…]`` references this write *introduces* that
    resolve to **nothing at all** — the hard-``BadInput`` subset of the
    reference lints (``DraftHandler._raise_on_new_dangling_refs``), the
    MCP-side twin of the web inline editor's 422 save-gate. Deliberately
    narrower than :func:`newly_dangling`'s chunk leg: a **tombstone** (a
    real ref soft-deleted underneath the cite, gr265228) stays an advisory
    hint — the reference was valid when written and blocking the edit
    would trap the author — and ``finding #slug`` placeholders stay
    advisory too (they are deliberate forward references). Pre-existing
    dead refs in ``old_text`` never block: they are standing debt, not
    this write's regression.

    The tombstone carve-out extends to **chunk-level** handles, which
    :func:`_classify_chunk_ref_tokens` leaves in the plain dangling
    bucket (its own split is ref-level only). A ``[pc<id>]`` whose paper
    was soft-deleted stops resolving exactly like a ``[fi<id>]`` whose
    finding was — same "valid when written" rationale, so it must get the
    same advisory treatment, or a structural edit (splitting a chunk,
    pasting the prose that carries the cite into a new one) would be
    unable to land without deleting a citation.
    :meth:`~precis.store._chunks_ops.ChunksOps.chunk_owner_kind` supplies
    the split the resolver can't: a chunk row that exists under a
    kind-matching (if retired) ref is a tombstone; a handle naming no
    chunk row at all is a typo, and still blocks."""
    old_dangling, _ = _classify_chunk_ref_tokens(store, old_text)
    new_dangling, _ = _classify_chunk_ref_tokens(store, new_text)
    old_bad = set(old_dangling)
    out: list[str] = []
    for h in new_dangling:
        if h in old_bad:
            continue
        parsed = handle_registry.parse(h) if not h.isdigit() else None
        if parsed is not None and parsed[1]:
            kind, _is_chunk, chunk_id = parsed
            try:
                owner = store.chunks.chunk_owner_kind(chunk_id)
            except Exception:  # pragma: no cover — store hiccup, don't block
                owner = None
            if owner == kind:
                continue  # real chunk, retired source — a tombstone
        out.append(h)
    return out


# ── [fi] cite-fit audit — does the cited hub's claim support the prose? ──
#
# The pc-side lints above check *form*; this one checks the draft↔hub seam
# the mint gates never see: a `[fi<id>]` cite whose hub asserts something
# *adjacent* to the draft's sentence (hub: characterization, draft:
# production scale — the fi236297/dc2445854 class). It needs an LLM
# judgment per cite, so unlike every hint above it is NOT wired into the
# synchronous write path — callers are explicit audit passes (sweeps, a
# future hygiene/audit view), with `judge_fn` injected the same way the
# taproot backfill cascade injects `dedup_judge`.

#: An `[fi<id>]` cite grounds the prose since the previous cite (of any
#: kind) or the chunk start — the backfill's "a claim is whatever a
#: citation grounds" rule. The judged tail is capped: the grounded
#: assertion sits adjacent to its cite, and an unbounded paragraph head
#: only pads the judge's context.
_FIT_SEGMENT_CAP = 800

_FI_HANDLE_RE = re.compile(r"fi\d+$")

FitVerdictKind = Literal["supports", "partial", "adjacent", "unrelated", "error"]


class FitVerdict(TypedDict):
    """One cite-fit judgment for a (segment, hub-claim) pair."""

    verdict: FitVerdictKind
    confidence: float
    rationale: str


FitJudgeFn = Callable[[str, str], FitVerdict]


@dataclass(frozen=True)
class FiCiteFit:
    """One judged `[fi<id>]` cite: the hub's claim sentence vs the draft
    segment the cite grounds."""

    token: str  # "fi236297"
    hub_ref_id: int
    claim: str  # the hub's claim sentence (refs.title)
    segment: str  # the draft prose this cite grounds (marker-stripped)
    verdict: FitVerdict


def fi_cite_segments(text: str) -> list[tuple[str, int, str]]:
    """``(token, hub_ref_id, segment)`` for every ``[fi<id>]`` cite (bare
    or pinned ``[fi<id>>pc<id>]``) in ``text``, in order.

    Segmentation mirrors ``taproot/backfill.py::segment_cite_groups``'s
    rules with `fi` as the anchor kind: each cite grounds the
    marker-stripped prose since the previous cite token (of any kind) or
    the chunk start; contiguous fi cites (whitespace-only gap —
    ``[fi1][fi2]``) share one segment (one claim, several hubs); a prefix
    cite (empty segment, no run to share) grounds nothing and is skipped.
    """
    from precis.utils.draft_markup import strip_markers
    from precis.utils.mentions import DRAFT_MARKUP_PATTERN

    out: list[tuple[str, int, str]] = []
    prev_end = 0
    run_segment: str | None = None  # live only across an unbroken fi run
    for m in DRAFT_MARKUP_PATTERN.finditer(text or ""):
        bare = m.groupdict().get("bare")
        is_fi = bool(bare) and bool(_FI_HANDLE_RE.match(bare or ""))
        if not is_fi:
            prev_end = m.end()
            run_segment = None
            continue
        assert bare is not None
        span = strip_markers(text[prev_end : m.start()]).strip()
        span = re.sub(r"^[\s.,;:!?)—–-]+", "", span)
        prev_end = m.end()
        if span:
            run_segment = span[-_FIT_SEGMENT_CAP:]
        elif run_segment is None:
            continue  # prefix cite — grounds nothing
        out.append((bare, int(bare[2:]), run_segment))
    return out


_FIT_JUDGE_SYS = (
    "You are a strict citation auditor for a scientific draft. Reply with "
    "ONLY the requested JSON object, no prose."
)

_FIT_JUDGE_PROMPT = """\
A draft cites a claim hub as the supporting evidence for the assertion at
the END of this segment (the prose immediately before the citation is what
the cite grounds). Judge whether the HUB CLAIM supports that assertion —
not whether the two share a topic or a source paper.

- "supports": the hub claim states or directly entails the draft's assertion.
- "partial": the same fact, but the hub is weaker, narrower, or hedged where
  the draft is not (citation inflation).
- "adjacent": same paper/topic, DIFFERENT assertion — the hub reports one
  finding (e.g. a characterization) while the draft asserts another (e.g. a
  production capability). The classic paper-proxy cite.
- "unrelated": the hub claim has no bearing on the draft's assertion.

DRAFT SEGMENT: {segment}

HUB CLAIM: {claim}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "supports" | "partial" | "adjacent" | "unrelated",
  "confidence": <float 0.0-1.0>,
  "rationale": "<one sentence>"
}}
"""


def _coerce_fit_verdict(data: object, *, default_rationale: str) -> FitVerdict:
    """Normalize raw model JSON into a :class:`FitVerdict`. Bias-safe both
    ways: a malformed/missing verdict degrades to ``"error"`` at confidence
    0.0 — never a silent ``"supports"`` (a masked bad cite) and never a
    false flag (an invented ``"adjacent"``)."""
    verdict: FitVerdictKind = "error"
    confidence = 0.0
    rationale = default_rationale
    if isinstance(data, dict):
        raw = data.get("verdict")
        if raw in ("supports", "partial", "adjacent", "unrelated"):
            verdict = raw
        raw_conf = data.get("confidence")
        if isinstance(raw_conf, int | float) and not isinstance(raw_conf, bool):
            confidence = max(0.0, min(1.0, float(raw_conf)))
        raw_rat = data.get("rationale")
        if isinstance(raw_rat, str) and raw_rat.strip():
            rationale = raw_rat.strip()
    return FitVerdict(verdict=verdict, confidence=confidence, rationale=rationale)


def cite_fit_judge(segment: str, claim: str) -> FitVerdict:
    """One bounded pairwise cite-fit judgment — MEDIUM tier, mirroring
    ``taproot/canon.py::dedup_judge``'s dispatch + degrade discipline."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    prompt = _FIT_JUDGE_PROMPT.format(segment=segment, claim=claim)
    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            messages=[
                {"role": "system", "content": _FIT_JUDGE_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="draft:cite-fit",
        )
    )
    if res.error:
        log.warning("cite_fit_judge dispatch failed: %s", res.error)
        return _coerce_fit_verdict(
            None, default_rationale=f"dispatch error: {res.error}"
        )
    data = res.data
    if data is None:
        try:
            data = json.loads(res.text or "")
        except (json.JSONDecodeError, TypeError):
            m = re.search(r"\{.*\}", res.text or "", re.DOTALL)
            data = None
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
    return _coerce_fit_verdict(data, default_rationale="unparseable model output")


def fi_cite_fit_report(
    store: Store, text: str, judge_fn: FitJudgeFn = cite_fit_judge
) -> list[FiCiteFit]:
    """Judge every ``[fi<id>]`` cite in ``text`` against the hub claim it
    resolves to. Tokens that resolve to no live finding are skipped — the
    dangling-reference lints own that failure. Duplicate (token, segment)
    pairs are judged once."""
    segs = fi_cite_segments(text)
    if not segs:
        return []
    refs = store.fetch_refs_by_ids(sorted({hub_id for _, hub_id, _ in segs}))
    out: list[FiCiteFit] = []
    seen: set[tuple[str, str]] = set()
    for token, hub_id, segment in segs:
        ref = refs.get(hub_id)
        if ref is None or getattr(ref, "kind", None) != "finding":
            continue
        claim = (ref.title or "").strip()
        if not claim:
            continue
        key = (token, segment)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            FiCiteFit(
                token=token,
                hub_ref_id=hub_id,
                claim=claim,
                segment=segment,
                verdict=judge_fn(segment, claim),
            )
        )
    return out


def fi_cite_fit_hint(rows: list[FiCiteFit]) -> str:
    """Advisory ⚠ lines for the cite-fit failures in ``rows`` —
    ``adjacent``/``unrelated`` verdicts only (``partial`` is a review-pass
    concern, not a wrong cite; ``error`` is infrastructure, not evidence).
    Empty when every judged cite fits."""
    bad = [r for r in rows if r.verdict["verdict"] in ("adjacent", "unrelated")]
    if not bad:
        return ""
    lines = []
    for r in bad:
        tail = r.segment[-160:]
        lines.append(
            f"\n\n⚠ cite-fit ({r.verdict['verdict']}): [{r.token}] — the hub "
            f'claims "{r.claim}" but the prose it grounds asserts '
            f'"…{tail}" ({r.verdict["rationale"]}). An adjacent hub is a '
            "paper-proxy, not support: if the source paper backs the draft's "
            "assertion, mint THAT claim (put(kind='finding', …, supporters=…)) "
            "and cite it; otherwise re-ground or drop the cite."
        )
    return "".join(lines)


def dangling_edit_hint(store: Store, new_text: str, old_text: str) -> str:
    """Advisory ⚠ naming the references *this edit* newly broke (see
    `newly_dangling`). Empty when the edit introduced no dead refs."""
    chunk, find = newly_dangling(store, new_text, old_text)
    if not chunk and not find:
        return ""
    parts: list[str] = []
    if chunk:
        parts.append(", ".join(f"[{h}]" for h in chunk))
    if find:
        parts.append(", ".join(f"finding #{s}" for s in find))
    toks = "; ".join(parts)
    return (
        f"\n\n⚠ this edit introduced unresolved reference(s): {toks}. Each "
        "resolves to nothing — fix it to a handle that lands (a chunk "
        "`dc<id>` / memory `me<id>` / paper chunk `pc<id>`, or a live "
        "`finding:<pub_id>`), or drop the reference. Only refs *this edit* "
        "broke are flagged; pre-existing dead refs elsewhere are left alone."
    )


@dataclass(frozen=True, slots=True)
class DriftedCite:
    """One ``cites`` edge whose stamped hub version has moved on —
    docs/backlog/cite-pins-hub-version.md. Enough to render "was X, now
    Y, cited at dc<id>": ``stamped_title`` is the title the citing prose
    was written against and ``current_title`` is the hub's title today;
    ``stamped_title`` is ``None`` when it can't be recovered (see
    :func:`find_drifted_cites`)."""

    dc: str  # citing chunk handle, e.g. "dc4821" (or "pe4821" for a plan)
    hub_ref_id: int
    stamped_pub_id: str
    current_pub_id: str
    stamped_title: str | None
    current_title: str


def find_drifted_cites(store: Store, draft: str | int) -> tuple[list[DriftedCite], int]:
    """Every ``cites`` edge from ``draft``'s chunks to a finding hub whose
    stamped ``cited_pub_id`` (``handlers/draft.py::sync_draft_links``) no
    longer matches the hub's live pub_id
    (:meth:`~precis.store._identifiers_ops.IdentifiersMixin.
    current_pub_ids`) — i.e. the hub was reworded since this passage cited
    it. Returns ``(drifted, unstamped)``: ``unstamped`` is the count of
    ``cites`` edges to a finding hub carrying no ``cited_pub_id`` at all
    (every edge written before this feature existed) — **not** drift,
    just unknown, so it's reported as a separate count rather than mixed
    into the list.

    ``draft`` accepts a numeric ref id, a numeric string, or a slug —
    resolved the same way :meth:`Store.get_ref` resolves any other
    slug-addressed kind. Raises :class:`~precis.errors.NotFound` if it
    doesn't resolve to a live draft.

    Each :class:`DriftedCite` carries both titles for the "was X, now Y"
    message. ``stamped_title`` is the ``cited_title`` pinned alongside
    ``cited_pub_id`` at stamp time (``handlers/draft.py::
    sync_draft_links``) — present for every edge stamped since that pin
    was added. An edge stamped *before* the title pin existed carries a
    ``cited_pub_id`` but no ``cited_title``; for those, ``stamped_title``
    is ``None`` and a caller must say the cite is stale without quoting
    the old text, never invent an empty string for "what it used to say".
    """
    ref_id = _resolve_draft_ref_id(store, draft)
    links = store.links_for(ref_id, direction="out", relation="cites")
    if not links:
        return [], 0
    dst_ids = {link.dst_ref_id for link in links}
    refs_by_id = store.fetch_refs_by_ids(list(dst_ids), include_deleted=False)
    hub_ids = {
        rid
        for rid, ref in refs_by_id.items()
        if ref is not None and ref.kind == "finding"
    }
    if not hub_ids:
        return [], 0
    current_pub_ids = store.current_pub_ids(hub_ids)

    drifted: list[DriftedCite] = []
    unstamped = 0
    for link in links:
        if link.dst_ref_id not in hub_ids or link.src_chunk_id is None:
            continue
        stamped_pub_id = (link.meta or {}).get("cited_pub_id")
        if not stamped_pub_id:
            unstamped += 1
            continue
        current_pub_id = current_pub_ids.get(link.dst_ref_id)
        if current_pub_id is None or current_pub_id == stamped_pub_id:
            continue
        hub_ref = refs_by_id.get(link.dst_ref_id)
        stamped_title = (link.meta or {}).get("cited_title")
        drifted.append(
            DriftedCite(
                dc=handle_registry.format_handle(
                    "draft", link.src_chunk_id, chunk=True
                ),
                hub_ref_id=link.dst_ref_id,
                stamped_pub_id=str(stamped_pub_id),
                current_pub_id=current_pub_id,
                stamped_title=(
                    str(stamped_title) if isinstance(stamped_title, str) else None
                ),
                current_title=(hub_ref.title if hub_ref is not None else ""),
            )
        )
    return drifted, unstamped


def _resolve_draft_ref_id(store: Store, draft: str | int) -> int:
    """``draft`` (ref id / numeric string / slug) → live draft ref_id, for
    :func:`find_drifted_cites`. Mirrors the ``isinstance``-then-slug
    dispatch :meth:`Store.get_ref` already documents; kept private here
    since every other draft-resolution helper is handler-layer
    (``DraftHandler._resolve_draft_any``, which additionally enforces the
    machine-owned-target guard that this pure read has no business
    applying)."""
    from precis.errors import NotFound

    ref_id: int | None
    if isinstance(draft, int):
        ref_id = draft
    else:
        s = str(draft).strip()
        ref_id = int(s) if s.isdigit() else None
    if ref_id is not None:
        ref = store.get_ref(kind="draft", id=ref_id)
        if ref is None:
            raise NotFound(f"no draft with ref_id={ref_id}")
        return ref.id
    ref = store.get_ref(kind="draft", id=str(draft).strip())
    if ref is None:
        raise NotFound(f"no draft {draft!r}")
    return ref.id
