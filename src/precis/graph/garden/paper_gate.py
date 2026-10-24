"""The identity gate a machine paper merge must clear (gr512125).

Two papers with the same normalized title and first author are not
necessarily one work: a supplementary file, a preprint and its journal
version, and a corrigendum all share that shape. Between 2026-10-23
17:47Z and 10-24 05:00Z the consumer applied 21 ``paper_identity``
merges whose only stored evidence was title, author and year
(``doi='missing'``, ``same_pdf=false`` on every one) — the reviewing
model approved on "identical title ... same first author". This gate
runs before the model is asked and again inside the apply transaction,
so a model verdict never stands in for identity evidence.

**Refused** (the proposal closes ``rejected`` with the reason; a
rejected key stays closed while the target is unchanged, so only an
unambiguous shape refuses):

- ``target_gone`` — an endpoint no longer exists.
- ``supplement`` — either side is not a main text (``refs.pdf_role``),
  carries an SI edge (:mod:`precis.store.si_links`), has a component DOI
  (``.s001`` — :data:`precis.ingest.si_discovery._COMPONENT_SUFFIX_RE`),
  or a title opening "Supporting/Supplementary Information".
- ``corrigendum`` — either title opens "Corrigendum/Erratum/Correction/
  Retraction/Addendum", or a ``corrects``/``retracts`` edge joins the
  pair.
- ``version_pair`` — one side carries a preprint id the other lacks (an
  ``arxiv`` identifier or a preprint-server DOI,
  :data:`precis.ingest.add._PREPRINT_DOI_PREFIXES`) and the other side
  is a published record (a journal DOI or a non-preprint
  ``meta.journal``). The preprint side's own journal is ignored: prod
  review 8504 is an arXiv copy carrying the NeurIPS journal copied from
  its published version. Only a shared DOI exempts the pair.

**Held for a human** (the proposal stays pending, never applied by a
machine; :data:`HOLD_REASONS`):

- ``version_unclear`` — a preprint id on one side and nothing on the
  other to say which version it is, or a version-pair shape on a
  byte-identical PDF (contradictory metadata). The same PDF lifts the
  first case.
- ``venue_differs`` — both sides name a journal and :func:`same_venue`
  says they differ, with no shared DOI or PDF. Abbreviations
  ("Phys. Rev. B" / "Physical Review B") compare equal.
- ``title_author_only`` — no positive evidence below.

**Positive evidence** (one is required):

- ``same_doi`` — a DOI on both sides (``ref_identifiers`` keys a DOI to
  one ref, so this fires only on a case/format variant);
- ``same_pdf`` — the same ``refs.pdf_sha256``;
- ``text_overlap`` — the Jaccard similarity of the two body texts'
  word 8-gram shingle sets is at least :data:`TEXT_OVERLAP_MIN`, with at
  least :data:`TEXT_OVERLAP_MIN_SHINGLES` shingles on each side.

**Why 0.8.** Measured with this code's own shingling (first 200 body
paragraphs per side) on the 11 of those 21 prod merges with body text on
both sides (2026-10-24): two PDFs of the same text score 0.94-0.995
(SimMOF two copies 0.995, the 1976 Annals paper's three copies 0.94);
the word2vec arXiv/NeurIPS pair 0.73; a one-page journal stub against a
full text 0.01-0.09; the 2007 arXiv / 2010 Annals pair of one result
0.14. 0.8 sits between the version pair (0.73) and every same-text pair
(≥0.94). The version-pair check does not rely on it: a near-identical
preprint is still a distinct record. The shingle floor
(200, about two paragraphs) keeps a near-empty side from scoring 1.0 on
a shared abstract. Chunk ``content_sha`` is not populated for paper
chunks, so the shingles are built from the chunk text here; only the
first :data:`TEXT_OVERLAP_MAX_CHUNKS` body paragraphs are read per side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

#: Shingle-set Jaccard at or above which two papers' body texts count as
#: one text (see the module docstring for the measurement).
TEXT_OVERLAP_MIN = 0.8
#: Shingles each side needs before an overlap score counts at all.
TEXT_OVERLAP_MIN_SHINGLES = 200
#: Words per shingle.
SHINGLE_WORDS = 8
#: Body paragraphs read per side.
TEXT_OVERLAP_MAX_CHUNKS = 200

#: The refusal reasons, in check order — the proposal closes ``rejected``.
SHAPE_REASONS: dict[str, str] = {
    "target_gone": "an endpoint no longer exists",
    "supplement": "one side is a supplementary file, not the main text",
    "corrigendum": "one side is a corrigendum/erratum/retraction notice",
    "version_pair": "a preprint id on one side, a published record on the other",
}
#: Reason when no positive identity evidence exists.
NO_EVIDENCE_REASON = "title_author_only"
#: The hold reasons — ambiguous, so the proposal stays pending for a
#: human instead of closing (a ``rejected`` key stays closed while the
#: target is unchanged, so a wrong refusal would bury a real duplicate).
HOLD_REASONS: dict[str, str] = {
    NO_EVIDENCE_REASON: "no shared DOI, PDF or body text — title/author only",
    "version_unclear": "a preprint id on one side, nothing on the other",
    "venue_differs": "the two journal names differ",
}

_SI_TITLE_RE = re.compile(
    r"^\s*(?:electronic\s+)?(?:supporting|supplementary|supplemental)\s+"
    r"(?:information|materials?|data|text|methods|figures?|notes?)\b",
    re.IGNORECASE,
)
_SI_DOI_RE = re.compile(r"/suppl\b|\.s\d+$", re.IGNORECASE)
_CORRIGENDUM_TITLE_RE = re.compile(
    r"^\s*(?:corrigendum|corrigenda|erratum|errata|correction|retraction|"
    r"retracted|addendum|expression\s+of\s+concern|publisher\s+correction|"
    r"author\s+correction)\b",
    re.IGNORECASE,
)
_CORRECTION_RELATIONS = (
    "corrects",
    "corrected-by",
    "retracts",
    "retracted-by",
)
_VENUE_STOPWORDS = frozenset({"the", "of", "and", "for", "in", "on", "a"})
_PREPRINT_VENUE_RE = re.compile(
    r"arxiv|biorxiv|medrxiv|chemrxiv|research\s*square|preprints\.org|ssrn",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Side:
    ref_id: int
    title: str
    pdf_role: str
    pdf_sha: str | None
    journal: str
    dois: tuple[str, ...]
    arxiv: tuple[str, ...]
    is_si: bool


@dataclass(slots=True)
class PaperIdentity:
    """What the gate found for one pair. ``refused`` is a
    :data:`SHAPE_REASONS` key, ``hold`` a :data:`HOLD_REASONS` key;
    ``positive`` lists the evidence names that hold. ``passes`` is the
    apply decision."""

    refused: str | None = None
    hold: str | None = None
    why: str = ""
    positive: list[str] = field(default_factory=list)
    text_overlap: float | None = None
    shingles: list[int] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return self.refused is None and self.hold is None and bool(self.positive)

    @property
    def reason(self) -> str | None:
        """The close (:data:`SHAPE_REASONS`) or hold
        (:data:`HOLD_REASONS`) reason, ``None`` when the pair passes."""
        if self.refused is not None:
            return self.refused
        if self.hold is not None:
            return self.hold
        return None if self.positive else NO_EVIDENCE_REASON

    def as_meta(self) -> dict[str, Any]:
        return {
            "refused": self.refused,
            "hold": self.hold,
            "why": self.why,
            "positive": list(self.positive),
            "text_overlap": self.text_overlap,
            "shingles": list(self.shingles),
            "text_overlap_min": TEXT_OVERLAP_MIN,
        }


def _norm_journal(j: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", j.lower()).split())


def _sides(conn: Any, a: int, b: int) -> dict[int, _Side]:
    rows = conn.execute(
        "SELECT r.ref_id, r.title, COALESCE(r.pdf_role, 'main'), r.pdf_sha256, "
        "  COALESCE(r.meta->>'journal', ''), "
        "  ARRAY(SELECT lower(i.id_value) FROM ref_identifiers i "
        "        WHERE i.ref_id = r.ref_id AND i.id_kind = 'doi'), "
        "  ARRAY(SELECT lower(i.id_value) FROM ref_identifiers i "
        "        WHERE i.ref_id = r.ref_id AND i.id_kind = 'arxiv'), "
        "  EXISTS (SELECT 1 FROM links l WHERE l.src_ref_id = r.ref_id "
        "          AND l.relation = %(si)s AND l.meta->>'role' = 'supplement') "
        "FROM refs r WHERE r.ref_id = ANY(%(ids)s)",
        {"ids": [a, b], "si": _si_relation()},
    ).fetchall()
    return {
        int(r[0]): _Side(
            ref_id=int(r[0]),
            title=str(r[1] or ""),
            pdf_role=str(r[2]),
            pdf_sha=None if r[3] is None else str(r[3]),
            journal=str(r[4] or ""),
            dois=tuple(str(d) for d in r[5] or ()),
            arxiv=tuple(str(x) for x in r[6] or ()),
            is_si=bool(r[7]),
        )
        for r in rows
    }


def _si_relation() -> str:
    from precis.store.si_links import SI_RELATION

    return SI_RELATION


def _preprint_doi(doi: str) -> bool:
    from precis.ingest.add import _PREPRINT_DOI_PREFIXES

    return doi.startswith(_PREPRINT_DOI_PREFIXES)


def _supplement(s: _Side) -> str | None:
    from precis.ingest.si_discovery import _COMPONENT_SUFFIX_RE

    if s.pdf_role != "main":
        return f"ref {s.ref_id} has pdf_role={s.pdf_role}"
    if s.is_si:
        return f"ref {s.ref_id} carries a supplement edge"
    for doi in s.dois:
        if _COMPONENT_SUFFIX_RE.search(doi) or _SI_DOI_RE.search(doi):
            return f"ref {s.ref_id} has a component DOI {doi}"
    if _SI_TITLE_RE.search(s.title):
        return f"ref {s.ref_id}'s title names a supplement"
    return None


def _preprint_ids(s: _Side) -> set[str]:
    """The side's preprint identifiers: arXiv ids and preprint-server
    DOIs. ``meta.journal`` is not one — on prod an arXiv copy carries
    the journal copied from its published version (review 8504)."""
    return {f"arxiv:{a}" for a in s.arxiv} | {d for d in s.dois if _preprint_doi(d)}


def _published(s: _Side) -> bool:
    if any(not _preprint_doi(d) for d in s.dois):
        return True
    return bool(s.journal) and not _PREPRINT_VENUE_RE.search(s.journal)


def _venue_tokens(j: str) -> list[str]:
    return [t for t in _norm_journal(j).split() if t not in _VENUE_STOPWORDS]


def same_venue(a: str, b: str) -> bool:
    """Whether two ``meta.journal`` strings name one venue, allowing the
    usual abbreviation: after dropping stopwords, the token lists have
    equal length and each token of one is a prefix of the other's
    ("Phys. Rev. B" = "Physical Review B", "J. Chem. Phys." = "The
    Journal of Chemical Physics")."""
    ta, tb = _venue_tokens(a), _venue_tokens(b)
    if len(ta) != len(tb):
        return False
    return all(x.startswith(y) or y.startswith(x) for x, y in zip(ta, tb, strict=True))


def _version_signal(x: _Side, y: _Side) -> tuple[str, str] | None:
    """``(verdict, why)`` for a preprint id on one side only —
    ``'refuse'`` when the other side is a published record, ``'hold'``
    when it carries nothing to say what it is."""
    for p, q in ((x, y), (y, x)):
        ids = _preprint_ids(p) - _preprint_ids(q)
        if not ids:
            continue
        if _published(q):
            return (
                "refuse",
                f"ref {p.ref_id} carries preprint id {sorted(ids)[0]} and ref "
                f"{q.ref_id} is a published record "
                f"({q.journal or ', '.join(q.dois)})",
            )
        return (
            "hold",
            f"ref {p.ref_id} carries preprint id {sorted(ids)[0]}; ref "
            f"{q.ref_id} carries no id or venue to say which version it is",
        )
    return None


def _shingles(texts: list[str]) -> set[int]:
    words = re.sub(r"[^a-z0-9]+", " ", " ".join(texts).lower()).split()
    n = SHINGLE_WORDS
    return {hash(tuple(words[i : i + n])) for i in range(len(words) - n + 1)}


def _body_texts(conn: Any, ref_id: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "  AND chunk_kind = 'paragraph' AND retired_at IS NULL "
            "ORDER BY ord LIMIT %s",
            (ref_id, TEXT_OVERLAP_MAX_CHUNKS),
        ).fetchall()
    ]


def text_overlap(conn: Any, a: int, b: int) -> tuple[float | None, list[int]]:
    """``(jaccard, [n_a, n_b])`` over the two body texts' shingle sets;
    the score is ``None`` when either side is under
    :data:`TEXT_OVERLAP_MIN_SHINGLES`."""
    sa, sb = _shingles(_body_texts(conn, a)), _shingles(_body_texts(conn, b))
    sizes = [len(sa), len(sb)]
    if min(sizes) < TEXT_OVERLAP_MIN_SHINGLES:
        return None, sizes
    return round(len(sa & sb) / len(sa | sb), 4), sizes


def paper_identity(
    store: Store, loser: int, winner: int, *, conn: Any = None
) -> PaperIdentity:
    """Judge one paper pair (see the module docstring). ``conn`` lets
    the apply door re-run it inside its transaction."""
    if conn is None:
        with store.pool.connection() as c:
            return paper_identity(store, loser, winner, conn=c)
    sides = _sides(conn, loser, winner)
    out = PaperIdentity()
    if loser not in sides or winner not in sides:
        out.refused, out.why = "target_gone", "an endpoint no longer exists"
        return out
    x, y = sides[loser], sides[winner]
    for s in (x, y):
        why = _supplement(s)
        if why:
            out.refused, out.why = "supplement", why
            return out
    for s in (x, y):
        if _CORRIGENDUM_TITLE_RE.search(s.title):
            out.refused = "corrigendum"
            out.why = f"ref {s.ref_id}'s title names a correction notice"
            return out
    edge = conn.execute(
        "SELECT relation FROM links WHERE relation = ANY(%(rels)s) AND ("
        "  (src_ref_id = %(a)s AND dst_ref_id = %(b)s) OR "
        "  (src_ref_id = %(b)s AND dst_ref_id = %(a)s)) LIMIT 1",
        {"rels": list(_CORRECTION_RELATIONS), "a": loser, "b": winner},
    ).fetchone()
    if edge is not None:
        out.refused = "corrigendum"
        out.why = f"a {edge[0]!r} edge joins the pair"
        return out
    same_doi = bool(set(x.dois) & set(y.dois))
    same_pdf = bool(x.pdf_sha) and x.pdf_sha == y.pdf_sha
    if same_doi:
        out.positive.append("same_doi")
    if same_pdf:
        out.positive.append("same_pdf")
    if not same_doi:
        # A shared DOI is the one thing that outranks a preprint id; a
        # byte-identical PDF makes a contradictory record ambiguous
        # (held), never a refusal.
        signal = _version_signal(x, y)
        if signal is not None:
            verdict, why = signal
            if verdict == "refuse" and not same_pdf:
                out.refused, out.why = "version_pair", why
                return out
            if not same_pdf or verdict == "refuse":
                out.hold, out.why = "version_unclear", why
                return out
    if not (same_doi or same_pdf) and x.journal and y.journal:
        if not same_venue(x.journal, y.journal):
            out.hold = "venue_differs"
            out.why = f"the journals differ ({x.journal!r} vs {y.journal!r})"
            return out
    if not (same_doi or same_pdf):
        score, sizes = text_overlap(conn, loser, winner)
        out.text_overlap, out.shingles = score, sizes
        if score is not None and score >= TEXT_OVERLAP_MIN:
            out.positive.append("text_overlap")
        if not out.positive:
            out.why = (
                "no shared DOI or PDF and the body texts "
                + (
                    f"overlap {score:.2f} (floor {TEXT_OVERLAP_MIN})"
                    if score is not None
                    else f"are too short to compare ({sizes[0]}/{sizes[1]} shingles)"
                )
                + " — title/author agreement alone is not identity"
            )
    return out


__all__ = [
    "HOLD_REASONS",
    "NO_EVIDENCE_REASON",
    "SHAPE_REASONS",
    "SHINGLE_WORDS",
    "TEXT_OVERLAP_MAX_CHUNKS",
    "TEXT_OVERLAP_MIN",
    "TEXT_OVERLAP_MIN_SHINGLES",
    "PaperIdentity",
    "paper_identity",
    "same_venue",
    "text_overlap",
]
