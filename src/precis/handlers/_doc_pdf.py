"""Stored PDF metadata and signed downloads shared by document handlers.

Presence describes the stored PDF attachment, as in the paper download view;
handlers do not probe local files because the web reader may serve another
corpus mount. Tokens, expiry and byte/page metadata use one implementation
for papers, datasheets and presentations.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import Any

from precis import file_links
from precis import secrets as vault
from precis.errors import BadInput
from precis.response import Response
from precis.store import Ref, Store
from precis.utils import handle_registry


def _pdf_size_and_pages(store: Store, sha: str) -> tuple[str, str]:
    """Read ingest-time byte size and physical page count from the PDF row."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT size_bytes, page_count FROM pdfs WHERE pdf_sha256 = %s",
            (sha,),
        ).fetchone()
    size = f"{int(row[0])} B" if row else "unknown"
    pages = str(int(row[1])) if row else "unknown"
    return size, pages


def pdf_overview_line(store: Store, ref: Ref) -> str:
    """Report whether a PDF is attached, with its stored size and page count."""
    if not ref.pdf_sha256:
        return "pdf: missing"
    size, pages = _pdf_size_and_pages(store, ref.pdf_sha256)
    return f"pdf: present · pages: {pages} · size: {size}"


def render_pdf_link(
    store: Store, ref: Ref, args: dict[str, Any] | None = None
) -> Response:
    """Mint a reusable, short-lived link; PDF bytes never ride an MCP result."""
    handle = handle_registry.format_handle(ref.kind, ref.id)
    sha = ref.pdf_sha256
    if not sha:
        next_hint = (
            f"put(kind='paper', id='{handle}', args={{'pdf_url': 'https://…/x.pdf'}})"
            if ref.kind == "paper"
            else f"get(kind='skill', id='precis-{ref.kind}-help') for PDF ingest"
        )
        raise BadInput(f"{handle} holds no PDF — nothing to link", next=next_hint)
    ttl_raw = (args or {}).get("ttl_s", file_links.DEFAULT_TTL_S)
    try:
        ttl_s = int(ttl_raw)
    except (TypeError, ValueError, OverflowError):
        raise BadInput(
            f"args['ttl_s'] must be seconds, got {ttl_raw!r}",
            next=f"get(kind='{ref.kind}', id='{handle}', view='pdf_link')",
        ) from None
    token = file_links.mint("get", sha, ref.id, ttl_s, store=store)
    exp = file_links.parse(token).exp
    path = f"/files/pdf/{sha}?t={token}"
    base = (vault.get_secret(file_links.WEB_BASE_URL_SECRET, store=store) or "").rstrip(
        "/"
    )
    url = f"{base}{path}" if base else path
    size, pages = _pdf_size_and_pages(store, sha)
    expires = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    if not base:
        lines.append(
            "PRECIS_WEB_BASE_URL is not set — the link below is "
            "path-only; prepend the precis_web origin to fetch it "
            "(the operator sets it with `precis secret set "
            "PRECIS_WEB_BASE_URL` or on /secrets)."
        )
    stem = ref.slug or handle
    lines += [
        f"# {handle} — PDF download link",
        f"url: {url}",
        f"expires: {expires} (reusable until then)",
        f"sha256: {sha}",
        f"bytes: {size} · pages: {pages}",
        "",
        f"curl -fo {shlex.quote(stem + '.pdf')} '{url}'",
    ]
    return Response(body="\n".join(lines))
