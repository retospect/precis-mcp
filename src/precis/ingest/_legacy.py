"""Bundle ingest — `.acatome` files → v2 store.

`.acatome` bundles are gzipped JSON with a stable schema:

    { "header":    {...},
      "blocks":    [ {"text": ..., "embeddings": {...}, ...}, ... ],
      "enrichment_meta": {...} }

This module is sync, in-process, and uses the active embedder when a
block has no pre-computed vector (or when its vector dim doesn't match
the active embedding model).

The actual ingest entry point is ``Store.ingest_bundle(path, embedder)``;
this module provides the parsing + transformation logic so the store
method stays small and the ingest pipeline is independently testable.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ftfy

from precis.embedder import Embedder
from precis.errors import BadInput, Upstream
from precis.store.types import Density
from precis.utils.slug import mint_slug

log = logging.getLogger(__name__)


# Defense-in-depth mojibake repair config for bundle ingest. **MUST**
# match the chemistry-safe config in
# ``acatome_extract.marker._FTFY_CONFIG`` (see that module for the
# rationale on every switch — the short version: NEVER turn on
# ``unescape_html``, ``uncurl_quotes``, ``fix_character_width``, or
# NFKC normalization on a scientific corpus). Duplicated here rather
# than imported because:
#
#   1. ``acatome-extract`` is an optional dep on precis-mcp (installs
#      via the ``[paper]`` extra). Importing from it at module load
#      would crash plain installs that just want the MCP server with
#      no PDF-extraction pipeline.
#   2. The config is small enough that drift is easy to spot in code
#      review, and the per-switch rationale lives in the upstream
#      module's docstring — we cite it here.
#
# If you change either side of this duplication, change both.
_FTFY_CONFIG = ftfy.TextFixerConfig(
    fix_encoding=True,
    fix_c1_controls=True,
    fix_surrogates=True,
    decode_inconsistent_utf8=True,
    replace_lossy_sequences=True,
    restore_byte_a0=True,
    fix_line_breaks=True,
    remove_terminal_escapes=True,
    fix_latin_ligatures=False,
    fix_character_width=False,
    uncurl_quotes=False,
    unescape_html=False,
    normalization="NFC",
    remove_control_chars=False,
    explain=False,
)


def _sanitize(text: str) -> str:
    """Run the chemistry-safe ftfy config on a single string.

    Idempotent — calling on already-clean text is a no-op (and ftfy
    fast-paths "looks fine" inputs anyway, so the cost is negligible
    on bundles produced by recent ``acatome-extract`` versions).
    """
    if not text:
        return text
    return ftfy.fix_text(text, config=_FTFY_CONFIG)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of a single `Store.ingest_bundle()` call."""

    ref_id: int
    slug: str
    block_count: int
    inserted: bool
    """False if the paper was already present (idempotent skip)."""
    embedding_dim: int


# ---------------------------------------------------------------------------
# Bundle parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedBundle:
    """Normalized view of a bundle's relevant fields, ready to ingest."""

    title: str
    authors: list[Any]
    year: int | None
    doi: str | None
    arxiv_id: str | None
    journal: str | None
    abstract: str | None
    bundle_slug: str | None  # may exist; we mint our own from authors+year+title
    pdf_hash: str | None
    s2_id: str | None
    """Semantic Scholar `paperId`. Surfaced as a top-level field
    (alongside `doi` / `arxiv_id` / `pdf_hash`) so the ingest path can
    feed it straight into `ref_identifiers` without re-reading
    `raw_meta`. Populated for bundles whose lookup cascade went through
    the S2 path (most arXiv-only papers + title-search fallbacks);
    `None` when the bundle came purely from CrossRef DOI metadata."""
    external_ids: dict[str, str]
    """Full Semantic Scholar `externalIds` cluster captured at extract
    time — DOI / ArXiv / PubMed / PubMedCentralID / MAG / DBLP /
    CorpusId / OpenAlex. Keys are S2's verbatim casing; values are the
    raw identifier strings. Empty dict for bundles produced by older
    `acatome-extract` versions that didn't propagate this field. The
    ingest path translates these into normalised
    ``ref_identifiers`` rows, complementing the four primary keys
    (DOI / arxiv_id / s2_id / pdf_hash) above with whatever extra
    schemes S2 happened to know about."""
    provider: str
    """Mapped from `header.source` via `_map_provider()`."""
    blocks: list[ParsedBlock]
    raw_meta: dict[str, Any]
    """Full header so we can stash everything in refs.meta verbatim."""


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    text: str
    embedding: list[float] | None
    density: Density | None


def read_bundle(path: Path) -> dict[str, Any]:
    """Read a gzipped JSON bundle. Raises Upstream on parse failure."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        raise Upstream(
            f"could not read bundle {path}: {exc}",
            next="re-extract the source PDF with `acatome-extract` and retry",
        ) from exc


def parse_bundle(
    data: dict[str, Any],
    *,
    embedding_dim: int,
) -> ParsedBundle:
    """Project a raw bundle into a `ParsedBundle`.

    `embedding_dim` is the active model's vector size; bundle blocks
    carrying a different dim are dropped (set to None) so the active
    embedder can re-embed them.
    """
    header = data.get("header") or {}
    blocks_raw = data.get("blocks") or []
    if not isinstance(header, dict) or not isinstance(blocks_raw, list):
        raise BadInput(
            "bundle missing required `header` / `blocks` fields",
            next="bundle should match acatome-extract's v1 schema",
        )

    # Mojibake repair on display-facing fields. Block text is the
    # high-volume offender — old bundles can carry "Î±-helix" forward
    # into search and embeddings — but title / abstract / journal are
    # rendered to humans in list and overview views, so they need the
    # same treatment. Authors come from CrossRef / S2 metadata which is
    # already UTF-8 in our corpus, so we skip the per-name walk for
    # speed; if a future ingest source changes that, extend
    # ``_normalize_author_list`` to call ``_sanitize`` per entry.
    title = _sanitize(str(header.get("title") or "")).strip()
    if not title:
        raise BadInput(
            "bundle has empty title - refusing to ingest",
            next="rerun acatome-extract with --rescue or fix the source PDF",
        )

    blocks: list[ParsedBlock] = []
    for raw in blocks_raw:
        text = _sanitize(str(raw.get("text") or "")).strip()
        if not text:
            continue
        emb = _pick_embedding(raw, expected_dim=embedding_dim)
        density = _normalize_density(raw.get("density"))
        if density is None:
            density = classify_density(text)
        blocks.append(ParsedBlock(text=text, embedding=emb, density=density))

    raw_external = header.get("external_ids") or {}
    external_ids: dict[str, str] = {}
    if isinstance(raw_external, dict):
        for k, v in raw_external.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                external_ids[k] = v.strip()

    return ParsedBundle(
        title=title,
        authors=_normalize_author_list(header.get("authors")),
        year=_normalize_year(header.get("year")),
        doi=_or_none(header.get("doi")),
        arxiv_id=_or_none(header.get("arxiv_id")),
        journal=_sanitize(_or_none(header.get("journal")) or "") or None,
        abstract=_sanitize(_or_none(header.get("abstract")) or "") or None,
        bundle_slug=_or_none(header.get("slug")),
        pdf_hash=_or_none(header.get("pdf_hash")),
        s2_id=_or_none(header.get("s2_id")),
        external_ids=external_ids,
        provider=_map_provider(header.get("source")),
        blocks=blocks,
        raw_meta=header,
    )


# ---------------------------------------------------------------------------
# Provider mapping (header.source -> refs.provider FK)
# ---------------------------------------------------------------------------

# Translation table per `docs/paper_ingest.md`: bundle `header.source`
# values are mapped onto the closed `providers.slug` vocabulary in
# migration 0001.  Unknown values fall through to ``"manual"`` so the
# FK never fires at ingest time.
_PROVIDER_MAP: dict[str, str] = {
    "embedded": "manual",
    "manual": "manual",
    "local": "local",
    "crossref": "crossref",
    "arxiv": "arxiv",
    "s2": "s2",
    "semantic_scholar": "s2",
    "semantic-scholar": "s2",
    "unpaywall": "unpaywall",
}


def _map_provider(source: Any) -> str:
    """Map a bundle's `header.source` onto a `providers.slug` value.

    Unknown / missing → `"manual"` so the FK in the schema is always
    satisfied. The original value lives in `refs.meta` regardless.
    """
    if not isinstance(source, str):
        return "manual"
    key = source.strip().lower()
    return _PROVIDER_MAP.get(key, "manual")


# ---------------------------------------------------------------------------
# Density classification (cheap heuristic)
# ---------------------------------------------------------------------------


def classify_density(text: str) -> Density:
    """Three-bucket classifier: sparse / medium / dense.

    Heuristic only — refine empirically later. Schema doesn't constrain
    the algorithm, so handlers can re-run it via a sweep job.
    """
    if not text:
        return "sparse"
    n_tokens = max(len(text.split()), 1)
    n_digits = sum(c.isdigit() for c in text)
    nl_density = text.count("\n") / n_tokens
    if n_tokens < 20 or nl_density > 0.15:
        return "sparse"
    if n_digits / n_tokens > 0.10:
        return "dense"
    return "medium"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_embedding(
    block: dict[str, Any],
    *,
    expected_dim: int,
) -> list[float] | None:
    """Return a block's embedding if present and dim-compatible.

    Bundles store embeddings under ``block["embeddings"][profile]`` —
    we accept any single profile that matches `expected_dim`. If
    nothing matches, return None and let the embedder rebuild it.
    """
    embs = block.get("embeddings")
    if isinstance(embs, dict):
        for vec in embs.values():
            if not isinstance(vec, list):
                continue
            if len(vec) != expected_dim:
                continue
            try:
                return [float(x) for x in vec]
            except (TypeError, ValueError):
                continue
    # Some bundles inline the embedding directly.
    direct = block.get("embedding")
    if isinstance(direct, list) and len(direct) == expected_dim:
        try:
            return [float(x) for x in direct]
        except (TypeError, ValueError):
            return None
    return None


def _normalize_density(raw: Any) -> Density | None:
    if raw in ("sparse", "medium", "dense"):
        return raw
    return None


def _or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _normalize_year(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return int(s)
    return None


def _normalize_author_list(raw: Any) -> list[Any]:
    """Keep raw shape if list/dict; coerce string forms into list of dicts."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        return [{"name": a.strip()} for a in raw.split(";") if a.strip()]
    return []


def author_strings(authors: list[Any]) -> list[str]:
    """Flatten the loose authors shape into a list of name strings."""
    out: list[str] = []
    for item in authors:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Slug minting helper that talks to the store
# ---------------------------------------------------------------------------


def mint_paper_slug(
    parsed: ParsedBundle,
    slug_taken: Callable[[str], bool],
) -> str:
    """Mint a slug from the parsed bundle, deduplicating against `slug_taken`.

    Uses the bundle's own slug as the base when present and unused —
    otherwise computes a fresh one from authors+year+title. This keeps
    slugs stable across re-extracts of the same PDF without carrying
    over a typo in someone's manual override.
    """
    if parsed.bundle_slug and not slug_taken(parsed.bundle_slug):
        return parsed.bundle_slug
    return mint_slug(
        authors=author_strings(parsed.authors),
        year=parsed.year,
        title=parsed.title,
        existing=slug_taken,
    )


# ---------------------------------------------------------------------------
# Embedding fill
# ---------------------------------------------------------------------------


def fill_embeddings(
    blocks: Iterable[ParsedBlock],
    *,
    embedder: Embedder,
) -> list[ParsedBlock]:
    """Re-embed blocks that lack a vector. Returns a new list.

    Skips blocks that already have a vector matching the embedder's dim.
    """
    items = list(blocks)
    todo_indices: list[int] = []
    todo_texts: list[str] = []
    for i, b in enumerate(items):
        if b.embedding is not None and len(b.embedding) == embedder.dim:
            continue
        todo_indices.append(i)
        todo_texts.append(b.text)

    if not todo_indices:
        return items

    log.info("embedding %d blocks", len(todo_indices))
    new_vecs = embedder.embed(todo_texts)
    rebuilt = list(items)
    for i, vec in zip(todo_indices, new_vecs):
        b = rebuilt[i]
        rebuilt[i] = ParsedBlock(text=b.text, embedding=vec, density=b.density)
    return rebuilt


__all__ = [
    "IngestResult",
    "ParsedBlock",
    "ParsedBundle",
    "author_strings",
    "classify_density",
    "fill_embeddings",
    "mint_paper_slug",
    "parse_bundle",
    "read_bundle",
]
