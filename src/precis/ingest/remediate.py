"""Re-derive metadata for papers that ingested with junk / empty metadata.

Companion to the ingest root-cause fix (the garbage-title / garbage-author
filters in :mod:`precis.ingest.pdf_sidecar` plus the body-title -> S2
verify-gated rescue in :mod:`precis.ingest.lookup`). Those stop *new* bad
rows from being minted; this repairs the ones already in the corpus.

A **suspect** paper is a local paper whose stored title is empty / a known
junk default ("No Job Name") **or** whose author list is empty — the
symptom of the old lookup cascade falling through to the junk embedded
``/Info`` dictionary. For each suspect we relocate the on-disk PDF and
re-run :func:`extract_metadata_from_sources` (which now includes the
rescue), then either:

* **FIX** — a usable title was recovered: rewrite the ``refs`` columns +
  ``meta.abstract``, rename the ``cite_key`` (the paper's slug) and move
  the PDF to the matching ``<corpus>/<letter>/<cite_key>.pdf`` path,
  refresh DOI / arXiv / S2 aliases, and rewrite the
  ``card_title`` / ``card_authors`` / ``card_combined`` chunks — dropping
  their embeddings + keywords so the embed / chunk_keywords workers
  re-derive them.
* **TRIAGE** — still no usable title (no DOI, S2 miss): tag the ref
  ``needs-triage`` for the manual paste-title -> S2 flow.

Everything is idempotent and gated behind ``dry_run`` so a planning pass
can be reviewed before any production write. The PDF is *copied* to its
new path before the DB transaction commits and the old copy removed only
after; a mid-run failure therefore never leaves the DB pointing at a path
with no file.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.identity import make_cite_key
from precis.ingest.pdf_metadata import extract_metadata_from_sources
from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
from precis.store import Store
from precis.store.types import Tag
from precis.utils.authors import to_name_dicts

log = logging.getLogger(__name__)

#: Open tag applied to papers automation could not recover. The future
#: web triage flow (paste title -> S2) reads this set.
TRIAGE_TAG = Tag.open("needs-triage")

#: Card chunk kinds whose text is derived from the bibliographic
#: metadata, so they must be rewritten when the metadata is repaired.
_CARD_KINDS = ("card_title", "card_authors", "card_abstract", "card_combined")


def _corpus_pdf_dest(cite_key: str, corpus_dir: Path, *, suffix: str = ".pdf") -> Path:
    """Canonical on-disk path for ``cite_key``: ``<root>/<letter>/<key><suffix>``.

    Mirrors ``precis.cli.watch._corpus_pdf_dest`` (and the web's
    ``_pdf_candidates``) — the letter shard is the lower-case first
    char of the cite_key, or ``_`` when it isn't ASCII-alphanumeric.
    Inlined here to keep the ingest package off the watchdog dep chain.
    """
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    return corpus_dir / letter / f"{cite_key}{suffix}"


@dataclass
class Outcome:
    """Result of remediating one suspect paper."""

    ref_id: int
    action: str  # "fixed" | "triaged" | "no_pdf" | "no_change"
    old_cite_key: str
    new_cite_key: str = ""
    new_title: str = ""
    detail: str = ""

    def line(self) -> str:
        """One-line human summary for the CLI report."""
        if self.action == "fixed":
            rename = (
                f" {self.old_cite_key} -> {self.new_cite_key}"
                if self.new_cite_key != self.old_cite_key
                else " (slug unchanged)"
            )
            return f"FIXED   #{self.ref_id}{rename}: {self.new_title[:70]!r}"
        if self.action == "triaged":
            return f"TRIAGE  #{self.ref_id} ({self.old_cite_key}): {self.detail}"
        if self.action == "no_pdf":
            return f"NO_PDF  #{self.ref_id} ({self.old_cite_key}): {self.detail}"
        return f"NOCHG   #{self.ref_id} ({self.old_cite_key}): {self.detail}"


# ---------------------------------------------------------------------------
# Suspect selection
# ---------------------------------------------------------------------------

# A title is "junk" if blank or one of the generator-baked defaults that
# is_garbage_title / is_pii recognise. We keep the SQL coarse (catch the
# two literal values that exist in prod plus blank/NULL) and re-confirm in
# Python with the full predicate so the set can't drift from the filters.
_SUSPECT_SQL = """
    SELECT r.ref_id,
           COALESCE(ck.id_value, '') AS cite_key,
           r.title,
           r.authors
      FROM refs r
      LEFT JOIN ref_identifiers ck
        ON ck.ref_id = r.ref_id AND ck.id_kind = 'cite_key'
     WHERE r.kind = 'paper'
       AND r.provider = 'local'
       AND r.deleted_at IS NULL
       AND (
            r.title = ''
         OR r.title IS NULL
         OR r.title = 'No Job Name'
         OR r.authors = '[]'::jsonb
         OR r.authors IS NULL
       )
     ORDER BY r.ref_id
"""


@dataclass
class _Suspect:
    ref_id: int
    cite_key: str
    title: str
    authors: list[Any] = field(default_factory=list)


def _title_is_junk(title: str | None) -> bool:
    """True if a stored title is unusable (blank / PII / generator junk)."""
    t = (title or "").strip()
    return not t or is_pii(t) or is_garbage_title(t)


def find_suspects(store: Store, *, limit: int | None = None) -> list[_Suspect]:
    """Return local papers with junk title or empty authors, oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(_SUSPECT_SQL).fetchall()
    suspects = [
        _Suspect(
            ref_id=int(r[0]),
            cite_key=r[1] or "",
            title=r[2] or "",
            authors=list(r[3] or []),
        )
        for r in rows
    ]
    return suspects[:limit] if limit else suspects


# ---------------------------------------------------------------------------
# Re-derivation + classification
# ---------------------------------------------------------------------------


def classify(title: str) -> str:
    """``"fix"`` when *title* is usable, else ``"triage"``."""
    return "triage" if _title_is_junk(title) else "fix"


def _combined_card_text(
    title: str, author_names: list[str], abstract: str, keywords: list[str]
) -> str:
    """Mirror :func:`precis.ingest.pipeline._build_cards`'s card_combined."""
    parts: list[str] = []
    if title:
        parts.append(title)
    if author_names:
        parts.append("; ".join(author_names))
    if abstract:
        parts.append(abstract)
    if keywords:
        parts.append("; ".join(keywords))
    return "\n\n".join(parts).strip() or "[no metadata]"


def _new_cite_key(
    conn: Any, author_dicts: list[dict[str, str]], year: int | None
) -> str:
    """Mint a collision-free cite_key for the recovered authors+year."""
    base = make_cite_key(author_dicts, year)
    # Probe the live index for the prefix's family (surname+yy) so the
    # canonical minter can pick the next free letter suffix.
    prefix = base.rstrip("abcdefghijklmnopqrstuvwxyz") or base
    rows = conn.execute(
        "SELECT id_value FROM ref_identifiers "
        "WHERE id_kind = 'cite_key' AND id_value LIKE %s",
        (prefix + "%",),
    ).fetchall()
    return make_cite_key(author_dicts, year, taken={r[0] for r in rows})


def _rewrite_cards(
    conn: Any,
    ref_id: int,
    *,
    title: str,
    author_names: list[str],
    abstract: str,
    keywords: list[str],
) -> int:
    """Rewrite the derived card chunks + drop their embeddings/keywords.

    Updates only the card rows that already exist (cards are derived
    search helpers — the ``refs`` columns are the source of truth). Drops
    the matching ``chunk_embeddings`` rows and nulls ``keywords`` so the
    embed / chunk_keywords workers re-claim them. Returns the number of
    chunk rows touched.
    """
    text_by_kind = {
        "card_title": title,
        "card_authors": "; ".join(author_names) if author_names else "",
        "card_abstract": abstract,
        "card_combined": _combined_card_text(title, author_names, abstract, keywords),
    }
    touched: list[int] = []
    for kind in _CARD_KINDS:
        text = text_by_kind[kind]
        if not text:
            continue
        rows = conn.execute(
            "UPDATE chunks SET text = %s, keywords = NULL, keywords_meta = NULL "
            "WHERE ref_id = %s AND chunk_kind = %s RETURNING chunk_id",
            (text, ref_id, kind),
        ).fetchall()
        touched.extend(int(r[0]) for r in rows)
    if touched:
        conn.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id = ANY(%s)", (touched,)
        )
    return len(touched)


def _locate_pdf(corpus_dirs: tuple[Path, ...], cite_key: str) -> Path | None:
    """First existing ``<root>/<letter>/<cite_key>.pdf`` across roots."""
    if not cite_key:
        return None
    for root in corpus_dirs:
        cand = _corpus_pdf_dest(cite_key, root)
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------------------
# Per-paper remediation
# ---------------------------------------------------------------------------


def remediate_one(
    store: Store,
    suspect: _Suspect,
    corpus_dirs: tuple[Path, ...],
    *,
    dry_run: bool,
    source: str = "remediate",
) -> Outcome:
    """Re-derive + repair one suspect paper. Pure read when ``dry_run``."""
    pdf = _locate_pdf(corpus_dirs, suspect.cite_key)
    if pdf is None:
        # No file to re-derive from — hand to manual triage.
        if not dry_run:
            store.add_tag(suspect.ref_id, TRIAGE_TAG, set_by="system")
        return Outcome(
            ref_id=suspect.ref_id,
            action="no_pdf",
            old_cite_key=suspect.cite_key,
            detail="PDF not found in any corpus root",
        )

    meta = extract_metadata_from_sources(pdf)
    if classify(meta.title) == "triage":
        if not dry_run:
            store.add_tag(suspect.ref_id, TRIAGE_TAG, set_by="system")
        return Outcome(
            ref_id=suspect.ref_id,
            action="triaged",
            old_cite_key=suspect.cite_key,
            detail="no usable title recovered (no DOI / S2 miss)",
        )

    author_dicts = to_name_dicts(meta.authors) if meta.authors else []
    author_names = [a["name"] for a in author_dicts if a.get("name")]

    if dry_run:
        # Compute the would-be cite_key for the report without writing.
        with store.pool.connection() as conn:
            new_key = _new_cite_key(conn, author_dicts, meta.year)
        return Outcome(
            ref_id=suspect.ref_id,
            action="fixed",
            old_cite_key=suspect.cite_key,
            new_cite_key=new_key,
            new_title=meta.title,
            detail="dry-run",
        )

    return _apply_fix(
        store,
        suspect,
        pdf,
        corpus_dirs,
        title=meta.title,
        year=meta.year,
        author_dicts=author_dicts,
        author_names=author_names,
        abstract=meta.abstract,
        keywords=meta.keywords,
        doi=meta.doi,
        source=source,
    )


def _apply_fix(
    store: Store,
    suspect: _Suspect,
    pdf: Path,
    corpus_dirs: tuple[Path, ...],
    *,
    title: str,
    year: int | None,
    author_dicts: list[dict[str, str]],
    author_names: list[str],
    abstract: str,
    keywords: list[str],
    doi: str,
    source: str,
) -> Outcome:
    """Apply a FIX: DB writes in one tx, PDF copied first / old removed last."""
    staged_dest: Path | None = None
    new_key = suspect.cite_key
    try:
        with store.tx() as conn:
            new_key = _new_cite_key(conn, author_dicts, year)
            store.update_paper_fields(
                suspect.ref_id,
                title=title,
                year=year,
                authors=author_dicts or None,
                meta_patch={"abstract": abstract} if abstract else None,
                source=source,
                conn=conn,
            )
            # Refresh aliases recovered during re-derivation.
            if doi:
                store.set_ref_identifier(
                    suspect.ref_id, "doi", doi, source=source, conn=conn
                )
            # Stage the file copy before the cite_key flips so the DB never
            # commits a slug whose file isn't in place.
            if new_key != suspect.cite_key:
                staged_dest = _stage_pdf_copy(pdf, corpus_dirs, new_key)
                store.set_ref_identifier(
                    suspect.ref_id, "cite_key", new_key, source=source, conn=conn
                )
            _rewrite_cards(
                conn,
                suspect.ref_id,
                title=title,
                author_names=author_names,
                abstract=abstract,
                keywords=keywords,
            )
    except Exception:
        if staged_dest is not None and staged_dest.exists():
            staged_dest.unlink(missing_ok=True)  # roll back the staged copy
        raise

    # Committed: drop the now-stale old file.
    if (
        staged_dest is not None
        and pdf.exists()
        and pdf.resolve() != staged_dest.resolve()
    ):
        pdf.unlink(missing_ok=True)

    return Outcome(
        ref_id=suspect.ref_id,
        action="fixed",
        old_cite_key=suspect.cite_key,
        new_cite_key=new_key,
        new_title=title,
    )


def _stage_pdf_copy(pdf: Path, corpus_dirs: tuple[Path, ...], new_key: str) -> Path:
    """Copy *pdf* to the new cite_key's canonical path (same corpus root)."""
    # Keep the file in the root it currently lives in.
    root = next(
        (r for r in corpus_dirs if _is_within(pdf, r)),
        corpus_dirs[0],
    )
    dest = _corpus_pdf_dest(new_key, root, suffix=pdf.suffix.lower())
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != pdf.resolve():
        shutil.copy2(str(pdf), str(dest))
    return dest


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_remediation(
    store: Store,
    corpus_dirs: tuple[Path, ...],
    *,
    dry_run: bool = True,
    limit: int | None = None,
    skip_triaged: bool = True,
    source: str = "remediate",
) -> list[Outcome]:
    """Remediate every suspect paper; return one :class:`Outcome` each.

    ``skip_triaged`` skips papers already carrying the ``needs-triage``
    tag so a re-run only reattempts the recoverable tail. ``dry_run``
    (default) performs no writes — it reports the planned title + slug
    rename for each fixable paper.
    """
    suspects = find_suspects(store, limit=limit)
    outcomes: list[Outcome] = []
    for s in suspects:
        if skip_triaged and store.has_tag(s.ref_id, "OPEN", "needs-triage"):
            continue
        try:
            outcomes.append(
                remediate_one(store, s, corpus_dirs, dry_run=dry_run, source=source)
            )
        except Exception as exc:  # one bad paper must not abort the sweep
            log.exception("remediate: ref %d failed", s.ref_id)
            outcomes.append(
                Outcome(
                    ref_id=s.ref_id,
                    action="no_change",
                    old_cite_key=s.cite_key,
                    detail=f"error: {type(exc).__name__}: {exc}",
                )
            )
    return outcomes


__all__ = [
    "Outcome",
    "classify",
    "find_suspects",
    "remediate_one",
    "run_remediation",
]
