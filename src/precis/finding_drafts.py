"""Promote URL-backed draft findings only after a matching paper body exists.

A saved verifier verdict is evidence to retain, not permission to skip citation
chase. Promotion therefore enters ``STATUS:tracing`` and leaves unrelated
metadata and links intact. The finding lock makes duplicate import retries
idempotent; quote matching and frontier replacement share that transaction.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from precis.identity import normalize_doi
from precis.store.types import Tag
from precis.utils.quote_match import _match_quote, _norm
from precis.utils.timeutil import now_iso
from precis.utils.url import canonical_url

if TYPE_CHECKING:
    from precis.store.store import Store


_SOURCE_FIELDS = ("url", "source_url", "pdf_url", "markup_source_url", "oa_url")


def _doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = normalize_doi(value)
    return (
        normalized
        if normalized and re.fullmatch(r"10\.[^/\s]+/\S+", normalized)
        else None
    )


def _url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        if urlsplit(value.strip()).scheme.lower() not in {"http", "https"}:
            return None
        return canonical_url(value)
    except ValueError:
        return None


def promote_drafts_for_source(store: Store, source_url: Any) -> int:
    """Promote staged drafts whose source matches an already-imported paper.

    Called at staged-finding creation: the paper may have been imported
    before the draft existed. Candidate papers are found by DOI
    identifier or an exact match on the canonical source URL in the
    paper's ``meta`` fields.
    """
    url = _url(source_url)
    doi = _doi(source_url)
    if url is None and doi is None:
        return 0
    paper_ids: set[int] = set()
    with store.pool.connection() as conn:
        if doi is not None:
            paper_ids.update(
                int(r[0])
                for r in conn.execute(
                    "SELECT ri.ref_id FROM ref_identifiers ri "
                    "JOIN refs r ON r.ref_id = ri.ref_id "
                    "WHERE ri.id_kind = 'doi' AND ri.id_value = %s "
                    "AND r.kind = 'paper' AND r.retired_at IS NULL",
                    (doi,),
                ).fetchall()
            )
        if url is not None:
            # Stored meta URLs are raw — a canonical draft URL never
            # equals ``meta->>'url'`` verbatim when the stored form
            # carries e.g. case or tracking params. Canonicalization is
            # Python-side, so the SQL only narrows to papers mentioning
            # the canonical host (a case-insensitive substring every
            # stored URL that canonicalizes to ``url`` must contain)
            # and ``_url`` decides — the same canonical-to-canonical
            # compare ``promote_drafts_for_paper`` runs.
            host = (urlsplit(url).hostname or "").lower()
            if host:
                select = ", ".join(f"meta->>'{k}'" for k in _SOURCE_FIELDS)
                where = " OR ".join(f"meta->>'{k}' ILIKE %s" for k in _SOURCE_FIELDS)
                rows = conn.execute(
                    f"SELECT ref_id, {select} FROM refs "
                    "WHERE kind = 'paper' AND retired_at IS NULL "
                    f"AND ({where})",
                    tuple(f"%{host}%" for _ in _SOURCE_FIELDS),
                ).fetchall()
                for row in rows:
                    if any(_url(value) == url for value in row[1:]):
                        paper_ids.add(int(row[0]))
    promoted = 0
    for paper_id in sorted(paper_ids):
        promoted += promote_drafts_for_paper(store, paper_id)
    return promoted


def promote_drafts_for_paper(store: Store, paper_id: int) -> int:
    """Match draft source URL/DOI and quote, then seed ordinary citation chase.

    No body, no matching source, or no distinctive quote leaves the staged
    finding untouched. The source contract is ``meta.draft_source`` with
    ``url``, ``quote``, and optionally the initial ``web_ref_id`` frontier.
    """
    promoted = 0
    with store.tx() as conn:
        paper = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND kind = 'paper' "
            "AND retired_at IS NULL FOR SHARE",
            (paper_id,),
        ).fetchone()
        if paper is None:
            return 0
        paper_meta = paper[0] or {}
        source_values = [paper_meta.get(key) for key in _SOURCE_FIELDS]
        source_values.extend(
            row[0]
            for row in conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (paper_id,),
            ).fetchall()
        )
        urls = {url for value in source_values if (url := _url(value))}
        dois = {doi for value in source_values if (doi := _doi(value))}
        if not urls and not dois:
            return 0
        candidates = conn.execute(
            "SELECT r.ref_id, r.meta FROM refs r "
            "WHERE r.kind = 'finding' AND r.retired_at IS NULL "
            "AND r.meta ? 'draft_source' AND EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
            "AND t.value = 'staged' "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now())) "
            "ORDER BY r.ref_id"
        ).fetchall()
        # Check the source URL/DOI against the paper BEFORE locking —
        # canonicalization happens in Python, so an unlocked pass narrows
        # the FOR UPDATE set to drafts that could actually match.
        matching = []
        for finding_id, meta in candidates:
            source = (meta or {}).get("draft_source")
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if _url(url) in urls or _doi(url) in dois:
                matching.append(int(finding_id))
        cache: dict[int, list] = {}
        for finding_id in matching:
            finding = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s "
                "AND kind = 'finding' AND retired_at IS NULL FOR UPDATE",
                (finding_id,),
            ).fetchone()
            if finding is None:
                continue
            # A competing promotion may have completed while the row lock waited.
            status = conn.execute(
                "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
                "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' "
                "AND t.value = 'staged' "
                "AND (rt.expires_at IS NULL OR rt.expires_at > now())",
                (finding_id,),
            ).fetchone()
            if status is None:
                continue
            meta = finding[0] or {}
            source = meta.get("draft_source")
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if _url(url) not in urls and _doi(url) not in dois:
                continue
            quote = source.get("quote")
            reason: str | None
            if not isinstance(quote, str) or not quote.strip():
                reason = "no_quote"
            elif len(_norm(quote).strip()[:50]) < 15:
                reason = "quote_too_short"
            else:
                reason = None
            chunk_id = (
                _match_quote(store, paper_id, quote, cache, conn=conn)
                if reason is None and isinstance(quote, str)
                else None
            )
            if chunk_id is None:
                # The source matched but re-anchoring failed — leave a
                # note on the staged finding so the miss is diagnosable
                # instead of a silent skip.
                store.update_ref(
                    finding_id,
                    meta_patch={
                        "staged_reanchor": {
                            "paper_id": paper_id,
                            "reason": reason or "no_quote_match",
                            "at": now_iso(),
                        }
                    },
                    conn=conn,
                )
                continue
            chunk = conn.execute(
                "SELECT ord FROM chunks WHERE chunk_id = %s AND ref_id = %s "
                "AND ord >= 0 AND retired_at IS NULL FOR SHARE",
                (chunk_id, paper_id),
            ).fetchone()
            if chunk is None:
                # The matched chunk failed the re-read (retired or
                # moved off this paper between match and lock) — same
                # diagnosable note as the no-match path above.
                store.update_ref(
                    finding_id,
                    meta_patch={
                        "staged_reanchor": {
                            "paper_id": paper_id,
                            "reason": "chunk_missing",
                            "at": now_iso(),
                        }
                    },
                    conn=conn,
                )
                continue
            pos = int(chunk[0])
            store.add_link(
                src_ref_id=finding_id,
                dst_ref_id=paper_id,
                dst_pos=pos,
                relation="derived-from",
                conn=conn,
            )
            web_ref_id = source.get("web_ref_id")
            if isinstance(web_ref_id, int) and not isinstance(web_ref_id, bool):
                store.remove_link(
                    src_ref_id=finding_id,
                    dst_ref_id=web_ref_id,
                    relation="derived-from",
                    conn=conn,
                )
            store.update_ref(
                finding_id,
                meta_patch={
                    "chain": [{"ref_id": paper_id, "chunk_id": chunk_id, "ord": pos}]
                },
                conn=conn,
            )
            store.add_tag(
                finding_id,
                Tag.closed("STATUS", "tracing"),
                replace_prefix=True,
                set_by="system",
                conn=conn,
            )
            promoted += 1
    return promoted
