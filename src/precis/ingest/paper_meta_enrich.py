"""Paper metadata enrichment — one Crossref (+OpenAlex) fetch per paper.

Legacy ``refs.authors`` rows are flat ``{"name"}`` strings in mixed
formats that no parser can reliably fix, but Crossref/OpenAlex return the
given/family split authoritatively for any paper with a DOI, and their
responses carry more than we currently keep: Crossref's document type
(``entry_type`` — ``journal-article``, ``proceedings-article``,
``book-chapter``, ``posted-content``, ``dissertation``, verbatim),
retraction/correction notices (``update-to``), ISSN, per-author ORCID
iDs, and (via OpenAlex) the PubMed/OpenAlex id cluster.
:func:`enrich_paper` is the per-ref choke point; :mod:`precis.workers.
paper_meta_enrich` is the scheduled batch sweep that claims rows and
calls it.

What lands where:

* **authors** — wholesale replaced via :func:`precis.utils.authors.
  normalize_authors` on the Crossref author list (also flushes junk
  entries) when a DOI resolves; a best-effort comma-split heuristic on
  the *existing* flat strings otherwise (no network). Skipped entirely
  when ``refs.human_verified_at`` is set — a hand correction is never
  clobbered (a verified paper still gets its other meta fields filled).
* **``refs.title`` / ``refs.year``** — filled only when the ref carries
  no real value of its own: a blank title or the ``PLACEHOLDER_TITLE``
  sentinel (:func:`precis.identity.is_placeholder_title`), and a NULL
  year. A DOI-only acquire mints its stub title-less and yearless
  (``Store.acquire_paper_stub``), and this pass was for a long while the
  only thing that ever looked the DOI up — so the row sat at
  ``(no title)`` forever while its *abstract* and byline filled in. The
  placeholder guard is what lets this run corpus-wide: a real title is
  by definition not a placeholder, so a PDF-derived title or a human's
  Meta-tab correction is never clobbered (and needs no ``verified``
  special case — there is nothing to protect in a blank). When Crossref
  can't supply the title (miss, fetch error, no DOI), the paper's own
  front matter is the last resort (gr658887): a chunk-0 title head
  followed by a multi-name byline — the same signature the junk-title
  repair cascade's front-matter leg persists — is written under the
  same placeholder guard and breadcrumbed as a ``title_front_matter``
  event, so the DOI'd-placeholder class the repair cascade delegates
  here doesn't sit blank forever.
* **``meta.entry_type`` / ``meta.issn`` / ``meta.abstract`` /
  ``meta.volume`` / ``meta.number`` / ``meta.pages``** — filled only when
  the ref doesn't already carry one (never clobbers a better existing
  value, or a human edit made via the web Meta tab's entry_type form on
  a paper this pass hasn't visited yet). ``number`` is Crossref's
  ``issue``; ``pages`` is Crossref's ``page`` verbatim. These are what
  the LaTeX ``.bib`` / EndNote exporters read. Papers visited before
  these fields existed get them via ``precis enrich-rearm`` (clears the
  visited stamp).
* **``meta.journal``** — the one corrective field. Crossref's
  ``container-title`` is authoritative for a DOI-registered work, so a
  stored value that differs is replaced (gr606459 — S2's ``venue`` is
  lossy: it minted "Applications of Surface Science" for a paper in
  "Applied Surface Science", "Angewandte Chemie" for "Angewandte Chemie
  International Edition"). The displaced string is kept in
  ``meta.journal_aliases`` so the S2 name still resolves for a human;
  a ``human_verified_at`` ref keeps its journal (a hand correction
  wins). A blank fills the same as the other fields.
* **``ref_identifiers``** — OpenAlex's id cluster (``openalex``,
  ``pubmed``, ``mag``) is pulled *only* when a DOI resolved via Crossref
  (an OpenAlex-only fetch adds identifiers, nothing else in scope here)
  and only actually inserted for schemes the ref doesn't already carry
  (``insert_ref_identifiers`` is first-write-wins).
* **per-author ``openalex_author_id``** — the same OpenAlex fetch's
  ``authorships`` block is merged onto the Crossref author list
  (:func:`_merge_openalex_author_ids`): every (Crossref, OpenAlex)
  pair is graded by :func:`_author_name_score` — full family+given
  agreement (the whole given string, middle names included) outranks a
  surname-plus-initial compatibility, initials-only agreement is thin
  evidence however it is punctuated ("J.-A." ties "J. A.", never
  outranks it), and a real given-name disagreement at any position is
  a non-match — and a pair merges only when it is the unambiguous best
  match on *both* sides.
  Position is never evidence, so a reordered byline can no longer shift
  one author's ids onto a same-initial co-author (gr662344); also
  backfills ``orcid`` when Crossref's own byline lacked one for that
  author. Crossref stays the source of truth for names/order — never
  reordered or dropped.
* **``refs.retraction_status/reason/url``** — a conservative read of the
  same Crossref response's ``update-to`` array (see
  :mod:`precis.ingest.provenance` for the full notice-ref-minting
  pipeline this deliberately doesn't replicate; this pass only sets the
  three columns).
* **``cite_key``** — when the author/year write above first makes a
  junk-mint key's record mintable (``anon*``/``user*``/``the*``, born
  before the byline arrived), it is re-keyed through
  ``paper_rekey.remint_junk_cite_key``; the displaced key stays
  resolvable as a ``cite_key_alias`` redirect (gr523888 — the
  ``paper-remint-junk`` sweep used to refill daily because nothing
  re-minted at this point).
* **per-author ORCID** — kept on the author dict, and a minimal
  ``kind='orcid'`` stub ref is minted (if missing) + linked to the paper
  via the ``authored`` relation (ADR 0039). Deliberately does **not**
  call the full ORCID Public API resolve
  (:func:`precis.ingest.orcid.fetch_record`) — that's a per-author
  network round-trip (and needs its own OAuth credentials) this pass's
  one-fetch-per-paper budget doesn't afford at corpus scale. A later
  ``get(kind='orcid', id=...)`` fleshes the stub out on demand.

Provenance + idempotency: every visited ref gets
``meta.authors_resolved_at`` stamped (regardless of outcome — a
Crossref miss/error still counts as "visited" so it isn't retried every
pass forever); ``meta.authors_source`` (``crossref`` | ``heuristic``) is
set only when authors were actually replaced. A visit that read a
placeholder title also pins ``meta.title_recovery_v`` at
:data:`_TITLE_RECOVERY_VERSION` (gr658887): the front-matter title leg
postdates most of the stamped placeholder backlog, so
``_claim_batch``'s recovery leg re-selects a stamped placeholder only
while its pin is below the current version — each still-blank row gets
exactly one recovery per version, never a pass-every-hour retry. The
worker pass otherwise selects on ``authors_resolved_at IS NULL``, so a
second run over the same rows is a no-op.

Ordering: the stamp is written **last**, only after the authors/meta
update, the ORCID mint+link, and the card rebuild have all completed —
not up front. And for a placeholder visit the recovered *title* is
written in that same final transaction, not with the earlier
authors/meta update (gr658887 review): the still-placeholder title IS
the ``_claim_batch`` recovery leg's claim key — the row's old
``authors_resolved_at`` stamp is already set — so a title committed
before the pin would falsify the leg's title predicate on a mid-way
failure and wedge the row (real title, old stamp, pin never written,
cards still showing the sentinel). Deferring the write keeps the row
claimable until every recovery side effect has landed; the card rebuild
reads the pending title via a parameter instead of the column. A ref
left unstamped (and, for a placeholder, unpinned and still
sentinel-titled) after a mid-way failure (network hiccup, a raised
exception in card rebuild, …) is simply retried next pass rather than
silently wedged half-enriched forever. Every side effect here is
itself safe to repeat (wholesale author replace, fill-blanks-only meta,
first-write-wins identifiers, get-or-create ORCID mint + idempotent
link, idempotent card rewrite) — a retry re-fetching Crossref once more
is the acceptable cost.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis.identity import is_placeholder_title
from precis.ingest import orcid as orcid_api
from precis.ingest.cards import ensure_abstract_card, rewrite_cards
from precis.ingest.crossref import _normalize as _crossref_normalize
from precis.ingest.crossref import fetch_message as _fetch_crossref_message
from precis.ingest.openalex_meta import _authorships as _openalex_authorships
from precis.ingest.openalex_meta import _short_id as _short_openalex_id
from precis.ingest.openalex_meta import fetch_openalex_work
from precis.ingest.provenance import (
    RetractionStatus,
    classify_update_type,
    dominant_status,
)
from precis.store import Store
from precis.utils.authors import author_display, author_names, normalize_authors
from precis.utils.rate_limit import RateLimited
from precis.utils.title_views import title_plain

log = logging.getLogger(__name__)

#: meta key marking a ref as visited by this pass — the idempotency gate.
#: Distinct from ``SOURCE_KEY`` because a human-verified paper is visited
#: (its other meta fields filled) without its authors ever being touched.
RESOLVED_AT_KEY = "authors_resolved_at"
#: meta key recording where the current ``refs.authors`` value came from.
SOURCE_KEY = "authors_source"
#: meta key pinning the placeholder-title recovery version a placeholder
#: was last swept at (gr658887). Stamped together with
#: :data:`RESOLVED_AT_KEY` on any visit that read a placeholder title —
#: the ``_claim_batch`` recovery leg re-selects a stamped placeholder
#: only while its pin is below :data:`_TITLE_RECOVERY_VERSION`, so a
#: still-blank dead end is retried once per version, never every pass.
#: The pin, the stamp and the recovered title land in one transaction
#: (see the module docstring's "Ordering" note): the placeholder title
#: is the recovery leg's other predicate, so committing it earlier would
#: wedge a row whose remaining side effects never ran.
#: Bump the version when the title-fill legs learn a new trick (mirrors
#: ``paper_hygiene._TITLE_REPAIR_VERSION``).
TITLE_RECOVERY_KEY = "title_recovery_v"
#: Current placeholder-title recovery version (see
#: :data:`TITLE_RECOVERY_KEY`). v1: the front-matter title leg.
_TITLE_RECOVERY_VERSION = 1

#: Raw (non-normalized) Crossref message fetch — injectable for tests.
CrossrefRawFn = Callable[[str, str], "dict[str, Any] | None"]
#: OpenAlex work-object fetch — injectable for tests.
OpenAlexFn = Callable[..., "dict[str, Any] | None"]


@dataclass
class EnrichOutcome:
    """What one :func:`enrich_paper` call actually did, for logging/tests."""

    ref_id: int
    authors_source: str | None = None
    entry_type: str | None = None
    retraction_status: str | None = None
    extra_identifiers: int = 0
    orcid_links: int = 0
    title_filled: str | None = None
    title_recased: str | None = None
    year_filled: int | None = None
    #: gr606459 — the Crossref ``container-title`` that displaced a
    #: different stored ``meta.journal`` (the old string survives in
    #: ``meta.journal_aliases``). ``None`` when journal was filled into
    #: a blank, matched, or left alone on a verified ref.
    journal_corrected: str | None = None
    skipped_authors: bool = False
    cards_rebuilt: bool = False
    cite_key_reminted: str | None = None


#: Shortest all-caps title the recase repair considers (gr353794); below
#: this a title is more likely an acronym or formula than a shouted heading.
_ALLCAPS_MIN_LEN = 25


def _clean_title(title: str) -> str:
    """The plain title view (gr515227): markup stripped, entities decoded,
    math unwrapped — comparison-only, never stored."""
    return title_plain(title)


def _title_key(title: str) -> str:
    """Case-, whitespace-, punctuation- and markup-insensitive title key."""
    return " ".join(re.sub(r"[\W_]+", " ", _clean_title(title)).casefold().split())


def registry_recase(local: str | None, registry: str | None) -> str | None:
    """The registry title when it is ``local`` with only the casing wrong.

    Never a heuristic case-fixer (it mangles CO2/NaCl/pH): the registry's
    own string is the only source of casing. Applies only when ``local`` is
    ALL CAPS (``local == local.upper()``, longer than
    :data:`_ALLCAPS_MIN_LEN`), the registry string is *not* itself all caps,
    and the two agree after :func:`_title_key` normalisation. A substantive
    difference returns ``None`` (left for a human).

        >>> registry_recase("DFT STUDY OF CO2 ON NACL SURFACES", "DFT Study of CO2 on NaCl Surfaces")
        'DFT Study of CO2 on NaCl Surfaces'
        >>> registry_recase("DFT STUDY OF CO2 ON NACL SURFACES", "Something else entirely here") is None
        True
    """
    local = (local or "").strip()
    # gr515227: the returned value is stored — keep the registry string
    # raw (markup and all); only the *comparison* normalises.
    registry = (registry or "").strip()
    if not local or not registry or len(local) <= _ALLCAPS_MIN_LEN:
        return None
    if local != local.upper() or registry == registry.upper():
        return None
    if _title_key(local) != _title_key(registry):
        return None
    return registry


def _orcid_lookup(msg: dict[str, Any]) -> dict[tuple[str, str], str]:
    """``{(family_lower, given_lower): bare_orcid}`` off a raw Crossref
    message's ``author`` array.

    Used to reattach ORCID iDs onto the already-normalized
    (:func:`precis.ingest.crossref._normalize`) author list without
    re-deriving its affiliation-filtering / editor-fallback logic here.
    """
    out: dict[tuple[str, str], str] = {}
    for a in msg.get("author") or []:
        family = (a.get("family") or "").strip().lower()
        given = (a.get("given") or "").strip().lower()
        orcid_url = (a.get("ORCID") or "").strip()
        if not orcid_url or (not family and not given):
            continue
        out[(family, given)] = orcid_url.rsplit("/", 1)[-1].strip()
    return out


def _journal_aliases_with(
    meta: dict[str, Any], *, displaced: str, canonical: str
) -> list[str]:
    """``meta.journal_aliases`` plus the just-displaced ``journal`` value.

    Order-stable and deduped: prior aliases keep their place, the newly
    displaced string appends; the new canonical journal is never listed
    as its own alias.
    """
    raw = meta.get("journal_aliases")
    out: list[str] = []
    for a in raw if isinstance(raw, list) else []:
        s = str(a).strip() if isinstance(a, str) else ""
        if s and s != canonical and s not in out:
            out.append(s)
    if displaced != canonical and displaced not in out:
        out.append(displaced)
    return out


def _extract_retraction(
    msg: dict[str, Any],
) -> tuple[RetractionStatus, str | None, str | None] | None:
    """Conservative retraction read off ``message.update-to``.

    Returns ``(status, reason, url)`` or ``None`` when no entry maps to a
    known ``refs.retraction_status`` value (see
    :func:`precis.ingest.provenance.classify_update_type` for the
    vocabulary) — never guesses.
    """
    statuses: list[RetractionStatus] = []
    notice_dois: list[str] = []
    for entry in msg.get("update-to") or []:
        raw_type = entry.get("type")
        if not raw_type:
            continue
        cls = classify_update_type(str(raw_type))
        if cls is None:
            continue
        _severity, status, _relation = cls
        if status is None:
            continue
        statuses.append(status)
        doi_val = entry.get("DOI")
        if doi_val:
            notice_dois.append(str(doi_val).strip().lower())
    if not statuses:
        return None
    applied = dominant_status(statuses)
    if applied is None:
        return None
    reason = "; ".join(notice_dois) if notice_dois else None
    url = f"https://doi.org/{notice_dois[0]}" if notice_dois else None
    return applied, reason, url


def _openalex_extra_ids(work: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(scheme, value, source)`` triples for identifiers only OpenAlex
    supplies here (PubMed, OpenAlex, MAG). The reconstructed abstract /
    topics land in ``meta.openalex`` via
    :func:`precis.ingest.openalex_meta.enrich_ref` separately (a distinct
    pass) — this only pulls the id cluster.
    """
    ids = work.get("ids") or {}
    out: list[tuple[str, str, str]] = []
    oa_id = _short_openalex_id(work.get("id") or ids.get("openalex"))
    if oa_id:
        out.append(("openalex", oa_id, "openalex"))
    pmid = ids.get("pmid")
    if pmid:
        bare = str(pmid).rstrip("/").rsplit("/", 1)[-1].strip()
        if bare:
            out.append(("pubmed", bare, "openalex"))
    mag = ids.get("mag")
    if mag:
        out.append(("mag", str(mag).strip(), "openalex"))
    return out


def _oa_family_name(display_name: str) -> str:
    """The surname an OpenAlex ``display_name`` implies — its last token,
    lowercased. Empty string when the name is blank."""
    tokens = display_name.split()
    return tokens[-1].strip().lower() if tokens else ""


def _crossref_family_name(entry: dict[str, Any]) -> str:
    """The surname a normalized Crossref author entry implies — ``family``,
    else the last token of a ``{"name"}``-shape entry (a mononym still
    matches a surname-only OpenAlex row)."""
    family = str(entry.get("family") or "").strip().lower()
    if family:
        return family
    return _oa_family_name(str(entry.get("name") or ""))


def _given_keys(name: str) -> list[tuple[str, str]]:
    """Per-letter-run ``(full, initial)`` given-name keys off a name
    string, lowercased: ``"Bryan R."`` → ``[("bryan", "b"), ("r",
    "r")]``; ``"Yi-Tao"`` → ``[("yi", "y"), ("tao", "t")]``; ``"Y.-T."``
    → ``[("y", "y"), ("t", "t")]``. One key per alphabetic run, not per
    whitespace token, so a compressed-initials form (``"J.-A."``)
    yields exactly the keys of its spaced form (``"J. A."``) — the same
    initials-only evidence must score identically however it is
    punctuated, or the tighter-written twin wins an ambiguity it should
    tie and takes the id (gr662344 review). Every run is kept — a
    middle name or second initial is evidence too: "John Alan" and
    "John Brian" must conflict, not score as equal first names.
    """
    return [(part, part[0]) for part in re.findall(r"[^\W\d_]+", name.casefold())]


def _crossref_given_keys(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Given-name keys off a normalized Crossref entry — every token of
    ``given``, else all but the last (family) token of a
    ``{"name"}``-shape entry (a mononym exposes no given name)."""
    given = str(entry.get("given") or "").strip()
    if given:
        return _given_keys(given)
    tokens = str(entry.get("name") or "").split()
    return _given_keys(" ".join(tokens[:-1])) if len(tokens) > 1 else []


def _oa_given_keys(display_name: str) -> list[tuple[str, str]]:
    """Given-name keys off an OpenAlex ``display_name`` — all but the
    last (family) token, matching :func:`_oa_family_name`."""
    tokens = display_name.split()
    return _given_keys(" ".join(tokens[:-1])) if len(tokens) > 1 else []


def _author_name_score(entry: dict[str, Any], authorship: dict[str, str]) -> int:
    """Grade how well a Crossref author entry and an OpenAlex authorship
    row name the same person (gr662344):

    * ``2`` — family names and the *whole* given name agree,
      tokenization aside ("Jie Xu" / "Jie Xu", "Jean-Luc" / "Jean Luc"),
      with a real (non-initial) name part on *both* sides — initials-only
      agreement stays thin however the initials are punctuated.
      Strong enough to stand alone.
    * ``1`` — family names agree and the given evidence is compatible
      but thin: a side exposes no given name (or fewer given names than
      the other), a bare initial ("B. Goldsmith" / "Bryan R.
      Goldsmith"), a compressed-initials form ("Y.-T." / "Yi-Tao Dai"),
      or initials matching initials ("J.-A." / "J. A." — identical
      keys, identical score, so same-initial twins tie rather than the
      tighter-written one winning). Compatible, but never proof against
      a same-initial rival — which is why only an *unambiguous* match
      may merge.
    * ``0`` — family names differ, or a shared given-name position has
      a real name or initial disagreement on the two sides: "Jie Xu" /
      "Jun Xu", "John Alan Smith" / "John Brian Smith" — two different
      people, not one ambiguous match.
    """
    oa_name = str(authorship.get("name") or "")
    family = _crossref_family_name(entry)
    if not family or family != _oa_family_name(oa_name):
        return 0
    cr_keys = _crossref_given_keys(entry)
    oa_keys = _oa_given_keys(oa_name)
    if not cr_keys or not oa_keys:
        return 1  # a side exposes no given name — family-only evidence
    cr_cat = "".join(full for full, _ in cr_keys)
    oa_cat = "".join(full for full, _ in oa_keys)
    if cr_cat == oa_cat:
        # Letters agree run for run; the agreement is only as real as its
        # weakest side — initials on either side ("J.-A.", "J. A. N.")
        # are thin evidence even when they spell a rival's real name.
        cr_real = any(len(full) > 1 for full, _ in cr_keys)
        oa_real = any(len(full) > 1 for full, _ in oa_keys)
        return 2 if cr_real and oa_real else 1
    for (cr_full, cr_init), (oa_full, oa_init) in zip(cr_keys, oa_keys):
        if cr_full in (oa_full, oa_init) or oa_full == cr_init:
            continue  # equal or abbreviation agreement ("B."↔"Bryan")
        return 0  # real given names disagree — different people
    return 1  # compatible but thin — extra given names went unchecked


def _merge_openalex_author_ids(
    crossref_authors: list[dict[str, Any]], authorships: list[dict[str, str]]
) -> None:
    """Merge OpenAlex identity onto the already-normalized Crossref author
    list, in place: ``openalex_author_id`` always, ``orcid`` only when the
    Crossref entry doesn't already carry one (docs/backlog/
    precis.utils.authors module docstring).

    Every (Crossref entry, OpenAlex row) pair is graded by
    :func:`_author_name_score`, and a pair merges only when it is the
    unambiguous best match on *both* sides — the unique highest score in
    its row and in its column (gr662344). Position is deliberately not
    evidence: a blind zip shifted every ORCID/OpenAlex id one row off on
    a preprint whose byline was reordered for the published version, and
    a positional surname+initial check still pairs a reordered "Jie Xu"
    with "Jun Xu". Mutual uniqueness is the whole guard: a full-name
    agreement pairs even across a reorder, an initial-only pairing
    survives only with no equal rival on either side, and a contested
    pair merges nothing — a missed id is recoverable, a wrong one mints
    an ``authored`` link onto the wrong person node. Crossref stays the
    source of truth for names/order — this never reorders or drops a
    Crossref entry, and an OpenAlex authorship with no unambiguous match
    is simply not merged.
    """
    if not crossref_authors or not authorships:
        return
    scores = [
        [_author_name_score(entry, a) for a in authorships]
        for entry in crossref_authors
    ]
    pairs: list[tuple[dict[str, Any], dict[str, str]]] = []
    for i, entry in enumerate(crossref_authors):
        row = scores[i]
        best = max(row)
        if best == 0 or row.count(best) != 1:
            continue
        j = row.index(best)
        col = [scores[k][j] for k in range(len(crossref_authors))]
        if max(col) != best or col.count(best) != 1:
            continue
        pairs.append((entry, authorships[j]))
    for entry, a in pairs:
        oa_id = str(a.get("openalex_author_id") or "").strip()
        if oa_id and not entry.get("openalex_author_id"):
            entry["openalex_author_id"] = oa_id
        orcid = str(a.get("orcid") or "").strip()
        if orcid and not entry.get("orcid"):
            entry["orcid"] = orcid


def _mint_and_link_orcid_authors(
    store: Store, paper_ref_id: int, authors: list[dict[str, Any]]
) -> int:
    """Mint a minimal ``kind='orcid'`` stub ref (if missing) per author
    carrying a fresh ``orcid`` id, and link it to the paper via the
    ``authored`` relation (src=author, dst=paper — ADR 0039), mirroring
    ``handlers/orcid.py::_link_authored``. See the module docstring for
    why this doesn't call the full ORCID Public API resolve.

    Explicitly get-or-create on both the mint (look up by slug before
    insert) and the link (``add_link`` is unique-constrained on
    ``(src, dst, relation)`` and no-ops on conflict) — a retry after a
    mid-way failure elsewhere in :func:`enrich_paper` (the
    ``authors_resolved_at`` stamp isn't written until every side effect
    succeeds) re-runs this safely: never a duplicate stub, never a
    duplicate edge.
    """
    linked = 0
    for a in authors:
        raw_orcid = a.get("orcid")
        if not isinstance(raw_orcid, str) or not raw_orcid.strip():
            continue
        try:
            orcid_id = orcid_api.normalize_orcid_id(raw_orcid)
        except Exception:
            continue  # malformed ORCID off Crossref — skip, don't fail the paper
        slug = orcid_api.slug_for(orcid_id)
        existing = store.get_ref(kind="orcid", id=slug)  # get-or-create: get
        if existing is None:
            display = author_display(a) or slug
            ref = store.insert_ref(  # get-or-create: create
                kind="orcid",
                slug=slug,
                title=display,
                provider="crossref",
                meta={"orcid_id": orcid_id},
            )
            orcid_ref_id = ref.id
        else:
            orcid_ref_id = existing.id
        if orcid_ref_id == paper_ref_id:
            continue
        # add_link is itself idempotent (ON CONFLICT DO UPDATE on the
        # unique (src, dst, relation) tuple) — a re-run never double-links.
        store.add_link(
            src_ref_id=orcid_ref_id,
            dst_ref_id=paper_ref_id,
            relation="authored",
            set_by="system",
            meta={"set_by": "paper-meta-enrich"},
        )
        linked += 1
    return linked


def _rebuild_cards(store: Store, ref_id: int, *, title: str | None = None) -> None:
    """Rewrite ``ref_id``'s derived search cards after an author/abstract
    change (mirrors ``workers/openalex_enrich.py::_rebuild_cards``).

    ``title`` overrides the stored ``refs.title``: ``enrich_paper``
    defers its title write to the final stamp transaction (module
    docstring "Ordering"), so the column still shows the old value when
    this runs — the pending string is passed in instead.
    """
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return
    meta = ref.meta or {}
    abstract = meta.get("abstract", "")
    abstract = abstract if isinstance(abstract, str) else ""
    authors_display = author_names(ref.authors)
    kw_raw = meta.get("keywords", [])
    keywords = list(kw_raw) if isinstance(kw_raw, list) else []
    with store.tx() as conn:
        rewrite_cards(
            conn,
            ref_id,
            title=title if title is not None else (ref.title or ""),
            author_names=authors_display,
            abstract=abstract,
            keywords=keywords,
        )
        if abstract:
            ensure_abstract_card(conn, ref_id, set_by="system", abstract=abstract)


def enrich_paper(
    store: Store,
    ref_id: int,
    *,
    doi: str | None,
    mailto: str = "",
    email: str = "",
    now: datetime | None = None,
    crossref_fn: CrossrefRawFn | None = None,
    openalex_fn: OpenAlexFn | None = None,
    link_orcid: bool = True,
) -> EnrichOutcome | None:
    """Re-resolve one paper's metadata; returns ``None`` if the ref is gone.

    ``doi`` — the ref's stored DOI (or ``None``/blank for a DOI-less
    paper). One Crossref fetch when a DOI is present; OpenAlex is fetched
    *in addition* only when Crossref resolved (it contributes identifiers
    only, not a metadata fallback). A DOI that 404s/errors at Crossref
    degrades to the same heuristic path as a DOI-less paper — this
    pass's job is to converge the row, not retry forever; the
    ``authors_resolved_at`` stamp (always written, and for a placeholder
    the ``title_recovery_v`` pin beside it) keeps it from being
    re-selected next pass either way.

    ``crossref_fn``/``openalex_fn`` default to ``None``, resolved to this
    module's ``_fetch_crossref_message`` / ``fetch_openalex_work`` *at
    call time* (not as a bound default value) so a test can
    ``monkeypatch`` those module-level names directly, mirroring
    ``workers/openalex_enrich.py``'s pattern.
    """
    fetch_crossref = crossref_fn or _fetch_crossref_message
    fetch_openalex = openalex_fn or fetch_openalex_work

    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return None
    now = now or datetime.now(UTC)
    verified = ref.human_verified_at is not None
    # Captured once at read time: the Crossref fill, the front-matter leg
    # and the recovery pin all gate on the title *as visited*, not on
    # whatever this call is about to write.
    was_placeholder = is_placeholder_title(ref.title)
    current_meta = ref.meta or {}
    outcome = EnrichOutcome(ref_id=ref_id)

    # RESOLVED_AT_KEY is deliberately NOT seeded here — it's written last,
    # in its own small update after every side effect below has landed
    # (see the module docstring's "Ordering" note).
    meta_patch: dict[str, Any] = {}
    extra_ids: list[tuple[str, str, str]] = []
    retraction: tuple[RetractionStatus, str | None, str | None] | None = None
    new_title: str | None = None
    new_year: int | None = None
    crossref_authors: list[dict[str, Any]] | None = None
    crossref_orcid_authors: list[dict[str, Any]] = []

    msg: dict[str, Any] | None = None
    doi = (doi or "").strip() or None
    if doi:
        # The default ``fetch_message`` takes a shared crossref limiter token
        # per attempt; a refusal (or a 429 that outlasted the retries) raises
        # RateLimited before any write, so the ref stays unstamped and the
        # caller defers the batch.
        try:
            msg = fetch_crossref(doi, mailto)
        except RateLimited:
            raise
        except Exception:
            log.exception(
                "paper_meta_enrich: crossref fetch failed for #%d (%s)", ref_id, doi
            )
            msg = None

    if doi and msg is not None:
        norm = _crossref_normalize(msg, doi)
        orcid_map = _orcid_lookup(msg)
        authors_raw = list(norm.get("authors") or [])
        for a in authors_raw:
            if isinstance(a, dict):
                key = (
                    (a.get("family") or "").strip().lower(),
                    (a.get("given") or "").strip().lower(),
                )
                orc = orcid_map.get(key)
                if orc:
                    a["orcid"] = orc
        candidate = normalize_authors(authors_raw)
        if candidate:
            crossref_authors = candidate

        # Title/year: fill-blanks-only, like the meta fields below. A
        # DOI-only acquire mints its stub with ``PLACEHOLDER_TITLE`` and a
        # NULL year (``Store.acquire_paper_stub``) and nothing downstream
        # ever replaced them — Crossref's answer was normalized, its
        # journal/issn/abstract kept, and its title dropped on the floor.
        # ``is_placeholder_title`` is what makes this safe to run over the
        # whole corpus: a real title (or a human's correction) is never a
        # placeholder, so it is never clobbered. The year rides along under
        # the same guard because a placeholder-titled stub's year is NULL
        # for the same reason — but is filled independently, since a
        # properly-titled ref can still be missing its year.
        if norm.get("title") and was_placeholder:
            new_title = str(norm["title"]).strip() or None
            outcome.title_filled = new_title
        elif not verified:
            # ALL-CAPS title (gr353794): the registry's casing wins; keep
            # the old string in meta.title_raw. Human-verified rows frozen.
            recased = registry_recase(ref.title, norm.get("title"))
            if recased is not None:
                new_title = recased
                outcome.title_recased = recased
                meta_patch["title_raw"] = ref.title
        if norm.get("year") and ref.year is None:
            new_year = int(norm["year"])
            outcome.year_filled = new_year

        if norm.get("entry_type") and not current_meta.get("entry_type"):
            meta_patch["entry_type"] = norm["entry_type"]
            outcome.entry_type = norm["entry_type"]
        crossref_journal = str(norm.get("journal") or "").strip()
        existing_journal = str(current_meta.get("journal") or "").strip()
        if crossref_journal and not existing_journal:
            meta_patch["journal"] = crossref_journal
        elif crossref_journal and crossref_journal != existing_journal and not verified:
            # gr606459: Crossref's ``container-title`` is authoritative for
            # a DOI-registered work — an S2 ``venue`` minted onto the stub
            # ("Applications of Surface Science" for a paper in "Applied
            # Surface Science") loses. The displaced string is kept as a
            # journal alias; a human-verified ref keeps its journal.
            meta_patch["journal"] = crossref_journal
            meta_patch["journal_aliases"] = _journal_aliases_with(
                current_meta, displaced=existing_journal, canonical=crossref_journal
            )
            outcome.journal_corrected = crossref_journal
        if norm.get("issn") and not current_meta.get("issn"):
            meta_patch["issn"] = norm["issn"]
        if norm.get("abstract") and not current_meta.get("abstract"):
            meta_patch["abstract"] = norm["abstract"]
        # Bibliographic locators for the exporters' bib entries (the keys
        # ``export/latex.py::build_bib`` reads): Crossref ``issue`` lands as
        # ``meta.number``. Fill-blanks-only, like journal/issn above.
        for meta_key, norm_key in (
            ("volume", "volume"),
            ("number", "issue"),
            ("pages", "pages"),
        ):
            if norm.get(norm_key) and not current_meta.get(meta_key):
                meta_patch[meta_key] = norm[norm_key]

        retraction = _extract_retraction(msg)
        if retraction is not None:
            outcome.retraction_status = retraction[0]

        try:
            work = fetch_openalex(doi, email=email)
        except RateLimited:
            # Shared openalex limiter refused: leave the ref unstamped so the
            # next pass retries it, instead of stamping it resolved without
            # its OpenAlex ids.
            raise
        except Exception:
            work = None
        if work:
            extra_ids = _openalex_extra_ids(work)
            if crossref_authors is not None:
                _merge_openalex_author_ids(
                    crossref_authors, _openalex_authorships(work)
                )

        if crossref_authors is not None:
            crossref_orcid_authors = [a for a in crossref_authors if a.get("orcid")]

    # gr658887: the placeholder Crossref couldn't fill (a miss, a fetch
    # error, a DOI-less ref, or a registry message with no usable title)
    # still has the answer in the paper's own front matter — the same
    # title-head + multi-name-byline signature the junk-title cascade's
    # last leg persists. ``repair_junk_title_papers`` skips DOI'd
    # placeholders exactly because this pass owns them, so without the
    # leg here that class sits blank forever. The derived string is
    # already ``_is_title_like``-gated and declines spliced bodies; like
    # the Crossref fill it only ever replaces a blank/sentinel.
    fm_title: str | None = None
    if new_title is None and was_placeholder:
        from precis.ingest.paper_hygiene import _front_matter_title

        fm_title = _front_matter_title(store, ref)
        if fm_title:
            new_title = fm_title
            outcome.title_filled = fm_title

    new_authors: list[dict[str, Any]] | None = None
    orcid_authors: list[dict[str, Any]] = []
    if verified:
        outcome.skipped_authors = True
    elif crossref_authors is not None:
        new_authors = crossref_authors
        outcome.authors_source = "crossref"
        orcid_authors = crossref_orcid_authors
    else:
        heuristic = normalize_authors(ref.authors)
        if heuristic:
            new_authors = heuristic
            outcome.authors_source = "heuristic"

    if outcome.authors_source:
        meta_patch[SOURCE_KEY] = outcome.authors_source

    # paper_authors.source (precis.utils.authors module docstring):
    # the Crossref replacement is tier 'crossref'; the comma-split
    # heuristic over the existing byline is no better than what a PDF
    # scrape would have given, so it lands as 'pdf'.
    authors_table_source = {"crossref": "crossref", "heuristic": "pdf"}.get(
        outcome.authors_source or ""
    )

    with store.tx() as conn:
        # ``title`` is deliberately absent — a placeholder's recovered
        # title commits with the final stamp below, not here (the module
        # docstring's "Ordering" note): on a stamped placeholder the
        # still-sentinel title is ``_claim_batch``'s claim key, so
        # writing it here would wedge the row if a later side effect
        # raises before the pin lands.
        store.update_paper_fields(
            ref_id,
            year=new_year,
            authors=new_authors,
            meta_patch=meta_patch,
            source="paper-meta-enrich",
            authors_source=authors_table_source,
            conn=conn,
        )
        if extra_ids:
            outcome.extra_identifiers = store.insert_ref_identifiers(
                ref_id, extra_ids, conn=conn
            )
        if retraction is not None:
            status, reason, url = retraction
            store.set_retraction_status(
                ref_id, status=status, reason=reason, url=url, conn=conn
            )
        elif msg is not None:
            # Crossref answered and carried no ``update-to`` notice: that is
            # a real "checked, clean" reading, so record it. Without this the
            # column only ever fills for *flagged* papers, "clean" is
            # indistinguishable from "never looked", and the TTL gate in
            # ``ingest/provenance.py`` re-fetches every paper forever.
            #
            # Timestamp only — never ``set_retraction_status(status=None)``,
            # which would clear a Retraction-Watch-only flag that Crossref
            # has no knowledge of. A DOI-less ref, or one whose fetch raised,
            # is deliberately left unstamped: we did not check it.
            store.touch_retraction_checked(ref_id, conn=conn)

    if new_authors or new_year is not None:
        # The write above just made the byline/year real — a junk-mint
        # cite_key (anon*/user*/the*) goes with it (gr523888: the sweep
        # kept re-keying papers enrichment had already repaired because
        # nothing re-minted here). Post-commit, so the rekey's own
        # transaction and PDF move see the write; the displaced key
        # stays resolvable as a cite_key_alias redirect. Same
        # REKEY_DEADLINE_S bound as apply_resolution — a wedged corpus
        # mount must not stall the pass.
        from precis.store.paper_rekey import REKEY_DEADLINE_S, remint_junk_cite_key
        from precis.utils.deadline import DeadlineExceeded, call_with_deadline

        try:
            outcome.cite_key_reminted = call_with_deadline(
                lambda: remint_junk_cite_key(store, ref_id, source="paper-meta-enrich"),
                deadline_s=REKEY_DEADLINE_S,
                label="remint_junk_cite_key",
            )
        except DeadlineExceeded:
            # The abandoned thread may still finish the rekey; the
            # junk-key sweep catches whatever it leaves behind.
            log.warning(
                "enrich_paper: auto-remint of ref %d exceeded %.0fs",
                ref_id,
                REKEY_DEADLINE_S,
            )

    if link_orcid and orcid_authors:
        outcome.orcid_links = _mint_and_link_orcid_authors(store, ref_id, orcid_authors)

    if new_authors or new_title or meta_patch.get("abstract"):
        _rebuild_cards(store, ref_id, title=new_title)
        outcome.cards_rebuilt = True

    # Stamp the idempotency marker LAST — see the module docstring's
    # "Ordering" note. If anything above raised, execution never reaches
    # here, the ref stays unstamped (and a placeholder keeps its sentinel
    # title + low pin, so the recovery leg still claims it), and the
    # next pass retries it (safe: every side effect above is itself
    # idempotent/get-or-create).
    stamp: dict[str, Any] = {RESOLVED_AT_KEY: now.isoformat()}
    if was_placeholder:
        # gr658887: pin the recovery version alongside. A placeholder
        # stamped before the front-matter leg existed carries no pin, so
        # ``_claim_batch``'s recovery leg re-selects it exactly once;
        # after this write a still-blank dead end is pinned and out.
        stamp[TITLE_RECOVERY_KEY] = _TITLE_RECOVERY_VERSION
    with store.tx() as conn:
        # The title write joins the stamp atomically (Ordering note): a
        # failure anywhere earlier left the placeholder in place, so a
        # stamped-placeholder row stayed claimable; a failure here rolls
        # the whole bundle back the same way.
        store.update_paper_fields(
            ref_id,
            title=new_title,
            meta_patch=stamp,
            source="paper-meta-enrich",
            conn=conn,
        )
        if fm_title:
            # Breadcrumb like ``title_backfill_queued``: a locally-derived
            # (not registry) title write is exactly the surprising change
            # ``view='log'`` should explain. In this tx so the event never
            # claims a title the row doesn't carry.
            store.append_event(
                ref_id,
                source="paper-meta-enrich",
                event="title_front_matter",
                payload={"title": fm_title[:200]},
                conn=conn,
            )

    return outcome


__all__ = [
    "RESOLVED_AT_KEY",
    "SOURCE_KEY",
    "TITLE_RECOVERY_KEY",
    "EnrichOutcome",
    "enrich_paper",
]
