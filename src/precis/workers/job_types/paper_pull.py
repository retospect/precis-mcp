"""``paper_pull`` job_type — fetch an agent-supplied PDF URL into a paper stub.

The enqueued half of ``put(kind='paper', id=<stub>, args={'pdf_url': ...})``
(gr478881): the one MCP door for a *known-legal* PDF the OA cascade cannot
find (author / institution hosts). Steps:

1. **Re-check the target** (the put checked too; the state may have moved):
   a live ``paper`` with no body chunks and ``pdf_sha256 IS NULL`` — the only
   shape the ingest fold promotes in place
   (``precis.ingest.add._valid_fold_stub``).
2. **Fetch** through ``safe_stream`` (an agent-supplied URL is external
   input), capped at 50 MB, ``%PDF`` magic required — the same bounded
   download ``datasheet_pull`` uses.
3. **Gate** the download (:func:`check_pdf`): the final post-redirect host
   must not be a shadow library, the PDF's first pages must name this stub
   (its DOI, or most of its title words), and an article must not be a
   1-2 page publisher preview. Nothing is written when a check fails.
4. **Drop** the PDF into the watch inbox (``PRECIS_WATCH_INBOX``, the
   directory ``precis watch`` scans and the OA fetcher writes) with a fetch
   sidecar (``ref_id`` = the stub, ``source='agent_url'``, ``url``), sidecar
   first and PDF last through ``.staging/`` so a watcher never sees the PDF
   without it. The existing ingest fold attaches the body.

The outcome is recorded on the paper's ``meta.paper_pull``; a deterministic
failure is a recorded state, not a raised error. Runs under ``job_inproc``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from precis.ingest.fetch_sidecar import sidecar_path, write_sidecar
from precis.store._body_predicate import has_body_sql
from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import datasheet_pull as _dl

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "paper_pull"
#: ``source`` written to the fetch sidecar (provenance of the drop).
SIDECAR_SOURCE = "agent_url"

#: Shadow-library hosts refused outright (put time, and again on the final
#: post-redirect host). A host matches when any non-TLD dot-label *starts
#: with* one of these prefixes, case-insensitively - mirrors come and go
#: under new TLDs (sci-hub.se, sci-hub.ru, libgen.is, annas-archive.org, ...),
#: so the label is the stable part. ``gen.lib.rus.ec`` (a Library Genesis
#: mirror) is the one that needs a host suffix instead.
BLOCKED_HOST_LABELS = (
    "sci-hub",
    "scihub",
    "libgen",
    "libgenesis",
    "library-genesis",
    "zlibrary",
    "z-library",
    "z-lib",
    "1lib",
    "singlelib",
    "annas-archive",
    "booksc",
    "b-ok",
    "pdfdrive",
    "oceanofpdf",
    "ocean-of-pdf",
)
BLOCKED_HOST_SUFFIXES = ("lib.rus.ec",)

#: ``entry_type`` values for long works, where a 1-2 page file is not the
#: article-preview signature.
_NON_ARTICLE_TYPES = frozenset(
    {
        "book",
        "inbook",
        "incollection",
        "thesis",
        "phdthesis",
        "mastersthesis",
        "dissertation",
        "techreport",
        "report",
        "monograph",
        "manual",
    }
)
_PREVIEW_MARKERS = (
    "this is a preview",
    "preview of",
    "access through your institution",
)
#: Pages of text the identity check reads (cheap; no Marker).
_IDENTITY_PAGES = 3
#: Fraction of the stub title's significant words that must occur.
_TITLE_OVERLAP = 0.8
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "via",
        "using",
        "use",
        "its",
        "their",
        "this",
        "that",
    ]
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {"type": "integer", "minimum": 1},
        "url": {"type": "string", "minLength": 8},
    },
    "required": ["ref_id", "url"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Fetch an agent-supplied PDF URL into the watch inbox, with a fetch "
    "sidecar naming the paper stub it belongs to."
)


def idem_key(ref_id: int, url: str) -> str:
    return f"{JOB_TYPE}:{ref_id}:{url}"


def target_problem(store: Store, ref_id: int) -> str | None:
    """Why ``ref_id`` cannot take a pulled PDF, else ``None``. Shared by the
    put (immediate refusal) and the job (state may have moved)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT r.kind, {has_body_sql('r')} FROM refs r "
            "WHERE r.ref_id = %s AND r.retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        return "is not a live ref"
    kind, has_body = row
    if kind != "paper":
        return f"is a {kind}, not a paper"
    if has_body:
        return "already has a body (chunks); pdf_url only feeds body-less stubs"
    # A bodiless ref that already holds a PDF (e.g. a 1-page preview) is a
    # valid sidecar fold target since 5d95cb2fc (gr465474).
    return None


def blocked_host(url: str) -> str | None:
    """The shadow-library host ``url`` points at, else ``None``."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    labels = host.split(".")
    for label in labels[:-1]:
        if label.startswith(BLOCKED_HOST_LABELS):
            return host
    if any(host == suf or host.endswith("." + suf) for suf in BLOCKED_HOST_SUFFIXES):
        return host
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _sig_words(title: str) -> list[str]:
    return [w for w in _words(title) if len(w) > 2 and w not in _STOPWORDS]


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 8), "")
    return line[:120]


def _stub_facts(store: Store, ref_id: int) -> tuple[str, str | None, str]:
    """``(title, doi, entry_type)`` of the stub; empty string / ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT coalesce(title, ''), coalesce(meta->>'entry_type', '') "
            "FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        doi = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'doi' ORDER BY id_value LIMIT 1",
            (ref_id,),
        ).fetchone()
    title, etype = (row[0], row[1]) if row else ("", "")
    return str(title), (str(doi[0]) if doi else None), str(etype)


def check_pdf(
    path: Path, *, title: str, doi: str | None, entry_type: str
) -> dict[str, Any]:
    """Identity + preview gate on a downloaded PDF.

    Returns extra record fields (``identity``: ``doi`` / ``title`` /
    ``unchecked``). Raises ``_Failed`` (``unreadable_pdf``,
    ``preview_suspected``, ``identity_mismatch``) when the file must not be
    dropped.
    """
    import fitz  # PyMuPDF

    try:
        with fitz.open(str(path)) as doc:
            n_pages = doc.page_count
            texts = [doc[i].get_text() for i in range(min(_IDENTITY_PAGES, n_pages))]
    except Exception as exc:  # fitz raises its own FileDataError family
        raise _dl._Failed("unreadable_pdf", f"{type(exc).__name__}: {exc}") from exc
    text = "\n".join(texts)
    first = texts[0] if texts else ""

    if n_pages <= 2 and entry_type.strip().lower() not in _NON_ARTICLE_TYPES:
        raise _dl._Failed("preview_suspected", f"{n_pages} page(s) for an article")
    low_first = first.lower()
    for marker in _PREVIEW_MARKERS:
        if marker in low_first:
            raise _dl._Failed("preview_suspected", f"page 1 says {marker!r}")

    out: dict[str, Any] = {"pages": n_pages}
    sig = _sig_words(title)
    if doi and re.sub(r"\s+", "", doi.lower()) in re.sub(r"\s+", "", text.lower()):
        out["identity"] = "doi"
    elif len(sig) >= 2:
        have = set(_words(text))
        overlap = sum(w in have for w in sig) / len(sig)
        if overlap < _TITLE_OVERLAP:
            raise _dl._Failed(
                "identity_mismatch",
                f"title overlap {overlap:.2f} < {_TITLE_OVERLAP}; "
                f"page 1: {_first_line(first)!r}",
            )
        out["identity"] = "title"
    elif doi:
        raise _dl._Failed(
            "identity_mismatch",
            f"DOI {doi} not in first pages; page 1: {_first_line(first)!r}",
        )
    else:
        out["identity"] = "unchecked"
        out["identity_unchecked"] = True
    return out


def _inbox() -> Path:
    inbox = os.environ.get("PRECIS_WATCH_INBOX", "").strip()
    if not inbox:
        raise _dl._Failed(
            "no_inbox", "PRECIS_WATCH_INBOX is unset on this worker; nowhere to drop"
        )
    return Path(inbox)


def _cite_key(store: Store, ref_id: int) -> str:
    ref = store.get_ref(kind="paper", id=ref_id)
    return str(getattr(ref, "slug", "") or "") if ref is not None else ""


def pull(store: Store, ref_id: int, url: str) -> dict[str, Any]:
    """Run one pull and record the outcome on the paper; returns the record."""
    from precis.workers.fetch_oa import _sanitise

    try:
        problem = target_problem(store, ref_id)
        if problem is not None:
            raise _dl._Failed("bad_target", f"ref {ref_id} {problem}")
        if (bad := blocked_host(url)) is not None:
            raise _dl._Failed("blocked_host", bad)
        inbox = _inbox()
        cite_key = _cite_key(store, ref_id)
        stem = _sanitise(cite_key) if cite_key else f"ref_{ref_id}"
        target = inbox / f"{stem}.pdf"
        if target.exists():
            raise _dl._Failed("already_in_inbox", target.name)
        staging = inbox / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / target.name
        try:
            final_url, sha = _dl._download(url, staged)
            if (bad := blocked_host(final_url)) is not None:
                raise _dl._Failed("blocked_host", f"redirected to {bad}")
            title, doi, etype = _stub_facts(store, ref_id)
            checks = check_pdf(staged, title=title, doi=doi, entry_type=etype)
            write_sidecar(
                staged,
                ref_id=ref_id,
                identifiers={"cite_key": cite_key},
                source=SIDECAR_SOURCE,
                url=final_url,
            )
            # Sidecar first, PDF last: the watcher keys off the ``.pdf``.
            os.replace(sidecar_path(staged), sidecar_path(target))
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        record: dict[str, Any] = {
            "status": "dropped",
            "url": final_url,
            "sha256": sha,
            "file": target.name,
            "at": now_iso(),
            **checks,
        }
    except _dl._Failed as exc:
        record = {"status": "failed", "reason": exc.reason, "url": url, "at": now_iso()}
        if exc.detail:
            record["detail"] = exc.detail[:300]
    store.update_ref(ref_id, meta_patch={"paper_pull": record})
    return record


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = ctx.meta.get("params") or {}
    ref_id = int(params["ref_id"])
    record = pull(ctx.store, ref_id, str(params["url"]))
    ctx.append_chunk(
        "job_summary",
        f"paper_pull ref {ref_id}: {record['status']}"
        + (f" ({record['reason']})" if record.get("reason") else ""),
    )
    ctx.set_meta(
        ref_id=ref_id, pull_status=record["status"], pull_reason=record.get("reason")
    )


SPEC = JobTypeSpec(
    name=JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=None,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "SPEC",
    "blocked_host",
    "check_pdf",
    "idem_key",
    "load",
    "pull",
    "target_problem",
]
