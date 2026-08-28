"""Draft-write-path lint — advisory hints appended to a `draft` `put`/`edit`
`Response`, never a refusal (the write always lands; these are nudges an
authoring agent can act on or ignore). Each ``*_hint`` function inspects the
touched text (and, for the abbreviation/citation/dangling-reference checks,
what the write *changed* vs. what was already there — so re-reading a chunk
doesn't re-nag about a problem it didn't introduce) and returns either ``""``
(clean) or a ``\\n\\n``-prefixed advisory block to append to the body.

Covers: undefined/inline-only abbreviations, non-canonical citation forms
(bare ``paper:`` mentions, whole-paper vs. chunk cites, literal
``\\cite{...}``), a Taproot claim-hub cite nudge, malformed temperature/unit
notation, and dangling ``[...]``/``finding #slug`` references. Pure
functions over an explicit :class:`~precis.store.store.Store` (no handler
`self`) — ``handlers/draft.py::DraftHandler`` is the sole caller, wiring
these into its ``put``/``edit``/``get`` bodies.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.nanopub.overview import HubOverviewRow
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Malformed temperature / unit notation the draft prose should not carry.
#: The canonical form is the literal sign with no space — ``63°C`` (degree
#: sign U+00B0 + ``C``), a range ``63–65°C``, a tolerance ``±1°C`` (U+00B1).
#: Each pattern matches one *wrong* spelling so the canonical ``63°C`` (no
#: space, real ° / ± signs) trips none of them. See ``temperature_form_hint``.
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
        if kind in ("paper", "patent") and not is_chunk:
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
    short, long = next(iter(promote.items()))
    return (
        f"\n\nℹ inline definition(s): {toks}. They work, but live only in "
        f"this chunk's prose — promote to the glossary so they survive edits: "
        f"put(kind='draft', id={slug!r}, chunk_kind='term', text={long!r}, "
        f"meta={{'short': {short!r}}})."
    )


def abbrev_hint(slug: str, undefined: list[str]) -> str:
    """A hint (appended to the write/edit Response) listing undefined
    abbreviations with copy-ready calls to define or silence them."""
    if not undefined:
        return ""
    toks = ", ".join(undefined)
    first = undefined[0]
    return (
        f"\n\n⚠ undefined abbreviation(s): {toks}. For each, either DEFINE it — "
        f"put(kind='draft', id={slug!r}, chunk_kind='term', text='<expansion>', "
        f"meta={{'short': {first!r}}}) — or, if it isn't an abbreviation, SILENCE "
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


def whole_paper_cite_hint(new_text: str, old_text: str) -> str:
    """Nudge toward the specific chunk when a bare whole-ref citation
    (``[pa<id>]``/``[pk<id>]`` — no chunk) is newly introduced in this
    write. Tolerated, never blocked — a landmark/rhetorical mention of
    a whole paper ("the Watson & Crick paper") is legitimate — but
    weaker than ``[pc<id>]`` for a specific claim: it never names a
    passage, and a later pass over this text (a reader, an editing
    agent) sees only the paper's keyword labels, never its text. Scoped
    to what this write introduced, mirroring the abbrev hint."""
    offenders = sorted(
        set(find_whole_ref_citations(new_text))
        - set(find_whole_ref_citations(old_text))
    )
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


def dangling_chunk_tokens(store: Store, text: str) -> list[str]:
    """The ``[<handle>]`` references in ``text`` that resolve to nothing —
    a pure numeric id (``[45650]``) or a known type-code prefix that no
    store row backs. A bare ``[ab12]`` with an unknown code is left as
    literal prose, not flagged. Order-preserving, deduped."""
    seen: list[str] = []
    dangling: list[str] = []
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
        try:
            if store.resolve_handle(h) is not None:
                continue
        except Exception:  # pragma: no cover — store hiccup, don't nag
            continue
        dangling.append(h)
    return dangling


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
    autonomous planner minting a forward reference is warned, not stalled."""
    old_bad = set(dangling_chunk_tokens(store, old_text))
    chunk = [h for h in dangling_chunk_tokens(store, new_text) if h not in old_bad]
    old_find = set(dangling_finding_tokens(store, old_text))
    find = [s for s in dangling_finding_tokens(store, new_text) if s not in old_find]
    return chunk, find


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
