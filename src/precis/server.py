"""FastMCP server — unified tools for papers and documents.

4 tools: search(), get(), put(), move()
Dispatch: id contains .docx/.tex/.md → file handler, else → paper handler.
"""

from __future__ import annotations

import os.path
import re

from mcp.server.fastmcp import FastMCP

from precis import tools
from precis.uri import _SEP_CHARS, SEP

mcp = FastMCP("precis")

# File extensions that trigger the file: scheme
_FILE_EXTENSIONS = {".docx", ".tex", ".md", ".markdown", ".rst", ".txt"}

# Max chars for multi-ID results before paginating
_MULTI_ID_BUDGET = 6000

# DOI pattern: 10.NNNN/suffix (registrant code is 4+ digits)
_DOI_RE = re.compile(r"^10\.\d{4,}/.")


def _split_sep(text: str) -> list[str]:
    """Split text at the first selector separator (› or ~)."""
    import re as _re

    return _re.split(r"[" + _re.escape(_SEP_CHARS) + r"]", text, maxsplit=1)


def _to_uri(id: str) -> str:
    """Convert a user-facing id to an internal URI.

    Resolution order:
    1. Known scheme prefix (doi:, arxiv:, usc:, irs:, ie:) → keep as-is
    2. File extension (.docx, .tex, etc.) → file: scheme
    3. Bare DOI pattern (10.NNNN/...) → doi: scheme
    4. Otherwise → paper: scheme (slug lookup)
    """
    if not id:
        return "paper:"
    # Known scheme prefixes — keep their scheme intact
    for scheme in ("doi:", "arxiv:", "usc:", "irs:", "ie:", "todo:"):
        if id.startswith(scheme):
            return id  # already a valid URI
    # Strip accidental scheme prefixes the LLM might copy
    for prefix in ("slug:", "s2:", "ref:"):
        if id.startswith(prefix):
            id = id[len(prefix) :]
            break
    # Auto-detect bare DOI (10.NNNN/...)
    bare = _split_sep(id)[0]
    if _DOI_RE.match(bare):
        return f"doi:{id}"
    # Split at SEP to check base path for extension
    base = bare.split("/")[0]
    _, ext = os.path.splitext(base)
    if ext.lower() in _FILE_EXTENSIONS:
        return f"file:{id}"
    return f"paper:{id}"


# ── Tools ────────────────────────────────────────────────────────────


@mcp.tool()
def search(
    query: str = "",
    top_k: int = 5,
    scope: str = "",
) -> str:
    """Semantic search over stored papers.

    query: natural language search query (REQUIRED)
    top_k: number of results (default 5)
    scope: slug or filename to restrict search (omit to search ALL papers)

    Examples:
      search(query='CO2 capture metal-organic frameworks')
      search(query='selectivity', scope='wang2020state')
      search(query='methods', scope='planning.docx')

    Without scope, searches across the entire paper library.
    Returns ranked results with snippets.
    Use get(id='wang2020state›N') to read full chunk text.
    """
    if not query.strip():
        return "ERROR: query is required. Example: search(query='CO2 capture MOF')"
    uri = _to_uri(scope) if scope else "paper:"
    return tools.read(uri=uri, query=query, page=1, top_k=top_k)


@mcp.tool()
def get(
    id: str = "",
    grep: str = "",
    depth: int = 0,
) -> str:
    """Read content by identifier. What you get depends on the id.

    id: identifier — dispatches by file extension vs paper slug
    grep: filter nodes — plain text, /regex/, or /regex/i
    depth: heading depth. 0=all, 1=H1, 2=H1+H2, 4=headings only

    Papers:
      get(id='wang2020state')              — overview (title, abstract, hints)
      get(id='wang2020state/toc')          — chunk index
      get(id='wang2020state/abstract')     — abstract text
      get(id='wang2020state/summary')      — enrichment summary
      get(id='wang2020state›38')           — chunk 38 full text
      get(id='wang2020state›38..42')       — chunks 38–42
      get(id='wang2020state›38/summary')   — chunk summary
      get(id='wang2020state/cite/bib')     — BibTeX citation
      get(id='wang2020state/fig')          — list figures
      get(id='wang2020state/fig/3')        — figure 3 overview + caption
      get(id='wang2020state/fig/3/legend') — caption text only
      get(id='wang2020state/fig/3/image')  — encoded image data
      get(id='wang2020state/fig/3/image/export') — save to ./figures/
      get(id='doi:10.1021/jacs.2c01234')   — lookup by DOI
      get(id='10.1021/jacs.2c01234')       — bare DOI (auto-detected)
      get(id='arxiv:2301.12345')           — lookup by arXiv ID
      get(grep='MOF')                  — filter paper list by keyword
      get(grep='ingested:today')       — papers added today
      get(grep='ingested:this-week')   — papers added this week
      get(grep='year:2020-2024')       — published 2020–2024
      get(grep='tag:review')           — filter by tag

    Documents:
      get(id='doc.docx')               — table of contents
      get(id='doc.docx›PLXDX')        — paragraph by slug
      get(id='doc.docx›S2.1')         — section scope
      get(id='doc.docx›PLXDX,ABCDE')  — multiple nodes
      get(id='doc.docx', grep='methods') — grep document
      get(id='doc.docx', depth=2)      — outline only
    """
    if not id and not grep:
        return (
            "ERROR: id or grep is required. Do not call get() with empty parameters.\n"
            "  get(id='wang2020state')      — paper overview\n"
            f"  get(id='wang2020state{SEP}5')    — read chunk 5\n"
            "  get(id='wang2020state/toc')  — table of contents\n"
            f"  get(id='slug1{SEP}4,slug2{SEP}9')   — multiple chunks at once\n"
            "  get(id='report.docx')        — document toc\n"
            "  get(grep='MOF')              — filter paper list"
        )
    # Comma-separated multi-ID: dispatch each, paginate if over budget
    ids = [s.strip() for s in id.split(",") if s.strip()] if id else []
    if len(ids) > 1:
        parts: list[str] = []
        total = 0
        for i, single_id in enumerate(ids):
            uri = _to_uri(single_id)
            result = tools.read(uri=uri, query=grep, depth=depth)
            total += len(result)
            parts.append(result)
            # Check budget after adding (always include at least 1 result)
            if total > _MULTI_ID_BUDGET and i < len(ids) - 1:
                remaining = ids[i + 1 :]
                parts.append(
                    f"\n[{i + 1} of {len(ids)} IDs shown. "
                    f"Remaining: get(id='{','.join(remaining)}')]"
                )
                break
        return "\n---\n".join(parts)
    uri = _to_uri(id) if id else "paper:"
    return tools.read(uri=uri, query=grep, depth=depth)


@mcp.tool()
def put(
    id: str,
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
    note: str = "",
    link: str = "",
) -> str:
    """Write, annotate, or delete content.

    id: target identifier (file›slug for docs, paper slug for notes)
    text: content to write.
    mode: append / replace / after / before / delete / comment / note
    tracked: DOCX track-changes (default true). LaTeX: ignored.
    note: annotation text — creates a note on the target ref or block.
    link: link spec as 'target_slug:relation' — creates a typed link.

    Headings: start line with # markers. Never number them.
      # Document Title    (Title style — one per document)
      ## Section           (Heading 1)
      ### Subsection       (Heading 2)
      #### Sub-subsection  (Heading 3, max depth)

    NEW content → mode='append' (creates file if needed):
      put(id='report.docx', text='## Methods', mode='append')
      put(id='report.docx', text='First paragraph.', mode='append')

    EDIT existing content → mode='replace' (requires ›SLUG in id):
      put(id='report.docx›PLXDX', text='Revised.', mode='replace')
      put(id='report.docx›PLXDX', text='New para.', mode='after')
      put(id='report.docx›PLXDX', mode='delete')
      put(id='report.docx›PLXDX', text='Fix this.', mode='comment')

    Citations (DOCX):
      Cite: [@slug] in text — slug is the paper name, NEVER include ›chunk.
      ✓ [@piscopo2020strategies]  ✗ [piscopo2020strategies›54]  ✗ [piscopo2020strategies]
      Define: put(id='report.docx', text='[@slug]: Author, Title, 2024.', mode='append')
      Undefined [@slug] references are flagged after each write.

    Notes (on any ref or block):
      put(id='wang2020state', note='Key finding about MOFs')
      put(id='wang2020state›38', note='Important result here')

    Links (between refs or blocks):
      put(id='wang2020state', link='jones2023surface:cites')
      put(id='wang2020state›38', link='jones2023surface:discusses')
      put(id='wang2020state', link='jones2023surface')  — defaults to 'references'

    Paper notes (legacy, still works):
      put(id='wang2020state', text='Key finding', mode='note')

    Multiple paragraphs separated by newlines are auto-split.
    """
    uri = _to_uri(id)
    return tools.put(
        uri=uri, text=text, mode=mode, tracked=tracked, note=note, link=link
    )


@mcp.tool()
def move(
    id: str,
    after: str,
) -> str:
    """Reorder nodes within a document.

    id: doc.docx›SLUG or doc.docx›SLUG1,SLUG2 to move
    after: doc.docx›SLUG — moved nodes placed after this node

    Slugs don't change. Paths are recomputed.
    """
    uri = _to_uri(id)
    # Extract the 'after' slug from id format (strip file part if present)
    after_sel = _split_sep(after)[-1] if any(c in after for c in _SEP_CHARS) else after
    return tools.put(uri=uri, text=after_sel, mode="move")


def main():
    """Run the MCP server."""
    mcp.run()
