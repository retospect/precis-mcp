"""Structured block extraction via Marker.

Marker v1.x returns MarkdownOutput with:
  .markdown  — full document as markdown string
  .images    — dict[str, PIL.Image] keyed by e.g. '_page_0_Picture_45.jpeg'
  .metadata  — dict with 'table_of_contents' and 'page_stats'

We parse the markdown into structured blocks, classify each, and attach
images from the .images dict.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import ftfy

from precis.identity import make_node_id
from precis.ingest.text_chunker import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TABLE_CHUNK_SIZE,
    enforce_hard_max,
    split_table,
    split_text,
)

log = logging.getLogger(__name__)

# Ligature normalization map
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}


_SPACED_OUT_RE = re.compile(r"(?<![A-Za-z])([A-Za-z](?:\s[A-Za-z]){3,})(?![A-Za-z])")


# ftfy config tuned for **chemistry / scientific** corpora. The default
# ftfy preset is aggressive: it would fold ′ (PRIME, U+2032) into a
# straight apostrophe, normalize ₂ (SUBSCRIPT TWO) into 2 under NFKC, and
# unescape HTML entities that scientific papers sometimes contain
# legitimately ("&lt;1 nm"). All of those are silent corruption hazards
# in this domain.
#
# Chemistry-safe rule of thumb: only repair things that are unambiguously
# *broken* (mis-decoded byte sequences, lossy U+FFFD, surrogate pairs,
# C1-control bytes from cp1252). Leave everything that's intentionally
# typed alone — Greek letters (α β γ Δ Σ μ Ω), arrows (→ ↔ ⇌), sub/super
# scripts (₂ ³⁺), units (°C Å μ ±), math operators (≤ ≥ ≠ ≈ ∫ ∑), and
# primes (′ ″). NFC composition is OK and matches our existing post-
# processing in :func:`_clean_text`; NFKC would corrupt sub/superscripts
# and is explicitly off.
_FTFY_CONFIG = ftfy.TextFixerConfig(
    # Core encoding repairs — what we actually need.
    fix_encoding=True,  # "Ã©" → "é", "â‚‚" → "₂"
    fix_c1_controls=True,  # cp1252-mojibake repair
    fix_surrogates=True,  # UTF-16 surrogate pairs
    decode_inconsistent_utf8=True,
    replace_lossy_sequences=True,  # "?" → original where guessable
    restore_byte_a0=True,
    # Whitespace / line-break hygiene — safe.
    fix_line_breaks=True,
    remove_terminal_escapes=True,
    # NB: ftfy 6.x no longer exposes a ``remove_bom`` switch; BOMs are
    # handled by the encoding-fix pass and (defensively) by our explicit
    # \ufeff strip in :func:`_clean_text` below.
    # Things that would corrupt scientific text — explicitly off.
    fix_latin_ligatures=False,  # we handle ﬁ→fi via _LIGATURES below
    fix_character_width=False,  # don't fold fullwidth/halfwidth
    uncurl_quotes=False,  # ′ ″ have semantic value (primes / minutes)
    unescape_html=False,  # "&lt;1 nm" is legitimate scientific text
    # Use NFC (composes á from a + ́); never NFKC (folds ₂ → 2).
    normalization="NFC",
    # We do our own control-char strip in _clean_text; ftfy's pass would
    # be redundant work.
    remove_control_chars=False,
    # Don't compute per-fix explanations — we apply this thousands of
    # times per paper and the explanation list eats ~1ms each.
    explain=False,
)


def _fix_spaced_out(match: re.Match) -> str:
    """Collapse 'M E T H O D S' → 'METHODS'."""
    return match.group(1).replace(" ", "")


def _clean_text(text: str) -> str:
    """Normalize PDF-extracted text.

    - **ftfy mojibake repair** with a chemistry-safe config (see
      :data:`_FTFY_CONFIG` for the rationale on each switch). Repairs
      ``"Ã©" → "é"``, ``"â‚‚" → "₂"``, etc. without touching intentional
      Unicode (Greek, arrows, sub/superscripts, primes, units).
    - NFC Unicode normalization (also performed by ftfy under our
      config; explicit pass kept here for defense in depth in case
      future ftfy upgrades flip the default).
    - Replace ligatures (\\ufb01 fi, \\ufb02 fl, etc.)
    - Fix spaced-out kerning artifacts ('M E T H O D S' → 'METHODS')
    - Strip control chars < 0x20 except \\n and \\t
    - Replace \\xa0 (non-breaking space), \\xad (soft hyphen), zero-width chars
    - Collapse multiple blank lines into one
    - Strip trailing whitespace per line
    """
    # ftfy first: it can introduce sequences (e.g. composing combining
    # marks) that subsequent ligature / control-char passes need to see
    # in their normalized form. Doing it first also means our cleanup
    # never has to reason about cp1252-mojibake — by the time we hit
    # the ligature map every character is in its true Unicode home.
    text = ftfy.fix_text(text, config=_FTFY_CONFIG)

    # NFC normalization — defensive duplicate of ftfy's NFC pass.
    text = unicodedata.normalize("NFC", text)

    # Ligatures
    for lig, repl in _LIGATURES.items():
        text = text.replace(lig, repl)

    # Spaced-out kerning artifacts: "M E T H O D S" → "METHODS"
    text = _SPACED_OUT_RE.sub(_fix_spaced_out, text)

    # Non-breaking space → space, soft hyphen → empty
    text = text.replace("\xa0", " ")
    text = text.replace("\xad", "")

    # Dehyphenate across line breaks: "under-\nstanding" → "understanding".
    # Heuristic: only join when the left fragment ends in a lowercase
    # ASCII letter AND the right fragment starts with a lowercase ASCII
    # letter. That preserves semantically-significant hyphens (e.g.
    # "Z-scheme\nphotocatalysis" — capital after the break — and
    # "Cu-MOF\nframework" — uppercase before) and never joins across
    # paragraph breaks (handled by the \s*\n\s* match consuming only
    # one newline). Without this fix Marker output of OCR'd PDFs
    # leaves broken tokens that corrupt the verifier's exact-quote
    # search; tracked by gap-3 in the 2026-05-31 ingest audit.
    text = re.sub(r"([a-z])-\s*\n\s*([a-z])", r"\1\2", text)

    # Zero-width chars
    text = text.replace("\ufeff", "")  # BOM / zero-width no-break space
    text = text.replace("\u200b", "")  # zero-width space
    text = text.replace("\u200c", "")  # zero-width non-joiner
    text = text.replace("\u200d", "")  # zero-width joiner

    # Strip control chars < 0x20 except \n (0x0a) and \t (0x09)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    # Collapse 3+ newlines → 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Strip trailing whitespace per line
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    return text.strip()


# Patterns for classifying markdown blocks
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LIST_RE = re.compile(r"^[-*]\s+")
_DISPLAY_EQ_RE = re.compile(r"^\$\$[\s\S]+\$\$$")
_INLINE_MATH_ONLY_RE = re.compile(r"^\$[^$]+\$$")
_TABLE_RE = re.compile(r"^\|")
_FIGURE_CAPTION_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?\s*\d+)\s*[:\.\-—–]\s*(.+)",
    re.IGNORECASE,
)

# Frontmatter headings that are journal boilerplate, not paper structure.
# Matched case-insensitively against heading text.
_JUNK_HEADING_RE = re.compile(
    r"^(?:"
    r"OPEN\s+ACCESS"
    r"|COPYRIGHT"
    r"|CITATION"
    r"|REVIEWED\s+BY\b"
    r"|EDITED\s+BY\b"
    r"|\*?CORRESPONDENCE\b"
    r"|RECEIVED\s+\d"
    r"|ACCEPTED\s+\d"
    r"|PUBLISHED\s+\d"
    r"|HANDLING\s+EDITOR\b"
    r"|ASSOCIATE\s+EDITOR\b"
    r"|TYPE\s"
    r")",
    re.IGNORECASE,
)

# Markdown link: [text](url)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _release_marker_caches() -> None:
    """Best-effort cleanup between PDFs in a long-running watcher.

    Runs :func:`gc.collect` to break ref cycles the Marker / surya /
    transformers stack tends to leave behind, plus ``empty_cache()``
    on whichever torch backend is active (CUDA on GPU hosts, MPS on
    Apple Silicon if torch is installed with MPS support, no-op on
    CPU-only deployments). All branches are import-and-feature
    guarded so the function is safe to call without torch installed.

    Cost: ~10 ms per call. Effect: cumulative resident-set growth
    across a long backfill is bounded rather than monotonic. Not a
    substitute for subprocess-per-batch isolation (Fix B), which is
    the structural fix; this is the cheap probe to layer on top.
    """
    import gc

    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        gc.collect()
        return

    if getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        torch.mps.empty_cache()  # type: ignore[attr-defined]

    gc.collect()


def extract_blocks_marker(pdf_path: Path, paper_id: str) -> list[dict[str, Any]]:
    """Extract structured blocks from a PDF using Marker.

    Falls back to fitz page-level extraction if Marker fails. Both
    paths feed into :func:`_merge_small_blocks` so the embedding-
    quality fix lands regardless of which extractor produced the
    blocks.
    """
    try:
        blocks = _marker_extract(pdf_path, paper_id)
    except Exception as exc:
        log.warning("Marker failed on %s (%s), using fitz fallback", pdf_path.name, exc)
        blocks = _fitz_fallback(pdf_path, paper_id)
    merged = _merge_small_blocks(blocks, paper_id=paper_id)
    # Best-effort cleanup after every ingest. The long-running watcher
    # accumulates tensor refs across consecutive PDFs (Surya layout
    # buffers, transformers cache) and eventually OOMs. Subprocess
    # isolation (Fix B / ADR 0015) is the structural fix; this is a
    # cheap probe layered on top. ~10 ms/PDF.
    _release_marker_caches()
    return merged


def _patch_text_config_ambiguity() -> None:
    """Monkey-patch transformers 4.48+ get_text_config() ambiguity.

    In transformers >= 4.48, PretrainedConfig.get_text_config() raises
    ValueError when a model config has both 'text_encoder' and 'decoder'
    sub-configs (as surya's models do). Fix: catch the ValueError and
    return text_encoder (or decoder as fallback).
    """
    try:
        from transformers import PretrainedConfig

        _orig = PretrainedConfig.get_text_config

        def _patched(self, **kwargs):
            try:
                return _orig(self, **kwargs)
            except ValueError:
                if hasattr(self, "text_encoder") and self.text_encoder is not None:
                    return self.text_encoder
                if hasattr(self, "decoder") and self.decoder is not None:
                    return self.decoder
                raise

        if PretrainedConfig.get_text_config is not _patched:
            PretrainedConfig.get_text_config = _patched
            log.debug("Patched PretrainedConfig.get_text_config (ambiguity fix)")
    except (ImportError, AttributeError):
        pass


def _patch_surya_config() -> None:
    """Monkey-patch surya SuryaOCRConfig to fix encoder KeyError.

    Bug: __init__ calls super().__init__(**kwargs) which empties kwargs,
    then tries kwargs.pop("encoder"). Fix: pop before super().__init__.
    """
    try:
        from surya.recognition.model.config import SuryaOCRConfig
        from transformers import PretrainedConfig

        _orig = SuryaOCRConfig.__init__

        def _patched_init(self, **kwargs):
            encoder_config = kwargs.pop("encoder", None)
            decoder_config = kwargs.pop("decoder", None)
            PretrainedConfig.__init__(self, **kwargs)
            self.encoder = encoder_config
            self.decoder = decoder_config
            self.is_encoder_decoder = True
            if isinstance(decoder_config, dict):
                self.decoder_start_token_id = decoder_config.get("bos_token_id")
                self.pad_token_id = decoder_config.get("pad_token_id")
                self.eos_token_id = decoder_config.get("eos_token_id")
            elif decoder_config is not None:
                self.decoder_start_token_id = getattr(
                    decoder_config, "bos_token_id", None
                )
                self.pad_token_id = getattr(decoder_config, "pad_token_id", None)
                self.eos_token_id = getattr(decoder_config, "eos_token_id", None)

        # Only patch if the bug exists (no default for encoder kwarg)
        import inspect

        sig = inspect.signature(_orig)
        if "encoder" not in sig.parameters:
            SuryaOCRConfig.__init__ = _patched_init
            log.debug("Patched SuryaOCRConfig.__init__ (encoder KeyError fix)")
    except ImportError:
        pass


def _marker_extract(pdf_path: Path, paper_id: str) -> list[dict[str, Any]]:
    """Run Marker and parse its MarkdownOutput into block schema."""
    import warnings

    warnings.filterwarnings("ignore", message=".*torch_dtype.*is deprecated")

    _patch_text_config_ambiguity()
    _patch_surya_config()
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    converter = PdfConverter(artifact_dict=create_model_dict())
    rendered = converter(str(pdf_path))

    # Marker's per-PDF output sometimes contains mojibake even after
    # marker's own internal ``ftfy`` passes (e.g. on PDFs whose
    # cmap claims one encoding while the embedded ToUnicode map
    # disagrees). Run our chemistry-safe cleanup over the entire
    # markdown blob *before* chunking so every downstream block,
    # caption, and section_path inherits the repaired text.
    # Previously this path skipped the cleanup that the fitz-fallback
    # path performed on every page; the asymmetry meant Marker-extracted
    # papers showed up in search with garbled δ-bonds while fitz-extracted
    # ones were clean. (See test_clean_text_chemistry_corpus.)
    md = _clean_text(rendered.markdown)
    images = rendered.images or {}
    metadata = rendered.metadata or {}

    # Build page boundary map from metadata
    toc = metadata.get("table_of_contents", [])
    page_stats = metadata.get("page_stats", [])
    total_pages = len(page_stats) if page_stats else 1

    # Split markdown into raw blocks (double newline separated)
    raw_chunks = re.split(r"\n{2,}", md)

    blocks: list[dict[str, Any]] = []
    current_section: list[str] = []
    block_counts: dict[int, int] = {}

    # Estimate page assignment: distribute chunks across pages
    page_assignments = _assign_pages(raw_chunks, total_pages, toc)

    for i, chunk in enumerate(raw_chunks):
        chunk = chunk.strip()
        if not chunk:
            continue

        page_num = page_assignments[i] if i < len(page_assignments) else 0
        block_type, text = _classify_chunk(chunk)

        if block_type == "section_header":
            # Strip markdown links from heading text
            text = _MD_LINK_RE.sub(r"\1", text).strip()
            current_section = [text]
            # Still emit the heading as a block
        elif block_type == "skip":
            continue

        # Type-aware chunking. After this branch, every entry in
        # ``sub_texts`` is structurally appropriate (paragraphs split
        # on prose boundaries, tables split on row groups with header
        # context preserved). The unconditional ``enforce_hard_max``
        # below then guarantees no chunk exceeds the embedder ceiling
        # regardless of type.
        if block_type in ("text", "list") and len(text) > DEFAULT_CHUNK_SIZE:
            sub_texts = split_text(text)
        elif block_type == "table" and len(text) > DEFAULT_TABLE_CHUNK_SIZE:
            sub_texts = split_table(text)
        else:
            sub_texts = [text]

        # Final safety net: force-split anything still oversized,
        # regardless of block type. Catches code dumps, equations,
        # and corrupted Marker-OCR'd "tables" that arrive as a single
        # newline-free string and so escape ``split_table``.
        sub_texts = enforce_hard_max(sub_texts)

        for sub_text in sub_texts:
            if page_num not in block_counts:
                block_counts[page_num] = 0
            idx = block_counts[page_num]
            block_counts[page_num] = idx + 1

            block: dict[str, Any] = {
                "node_id": make_node_id(paper_id, page_num, idx),
                "page": page_num,
                "type": block_type,
                "text": sub_text,
                "section_path": list(current_section),
                "bbox": None,
                "embeddings": {},
                "summaries": {},
            }

            # Attach images referenced in this chunk (first sub-block only)
            if sub_text is sub_texts[0]:
                img_refs = _IMAGE_RE.findall(chunk)
                for _alt, ref in img_refs:
                    img_key = _find_image_key(ref, images)
                    if img_key:
                        b64, mime = _encode_pil_image(images[img_key])
                        block["image_base64"] = b64
                        block["image_mime"] = mime
                        block["type"] = "figure"
                        break

            blocks.append(block)

    # Match figure captions
    blocks = _match_captions(blocks)

    # Mark frontmatter junk
    blocks = _mark_junk(blocks)

    return blocks


def _classify_chunk(chunk: str) -> tuple[str, str]:
    """Classify a markdown chunk and return (block_type, cleaned_text)."""
    first_line = chunk.split("\n")[0].strip()

    # Heading
    m = _HEADING_RE.match(first_line)
    if m:
        return "section_header", m.group(2).strip()

    # Image-only block
    if _IMAGE_RE.match(first_line) and len(chunk.split("\n")) <= 2:
        alt = _IMAGE_RE.match(first_line).group(1)
        return "figure", alt or ""

    # Table
    lines = chunk.strip().split("\n")
    if len(lines) >= 2 and all(_TABLE_RE.match(l.strip()) for l in lines):
        return "table", chunk

    # Display equation ($$...$$)
    stripped = chunk.strip()
    if _DISPLAY_EQ_RE.match(stripped):
        content = stripped.strip("$").strip()
        # Skip short formula fragments (e.g. "$$R^{3}$$")
        if len(content) < 40:
            return "skip", ""
        return "equation", content

    # Skip tiny inline-math-only fragments (e.g. "$R^{3}$")
    if _INLINE_MATH_ONLY_RE.match(stripped) and len(stripped) < 80:
        return "skip", ""

    # List block (all lines start with - or *)
    if all(_LIST_RE.match(l.strip()) for l in lines if l.strip()):
        return "list", chunk

    # Default text
    return "text", chunk


def _assign_pages(chunks: list[str], total_pages: int, toc: list[dict]) -> list[int]:
    """Assign each chunk to a page number.

    Uses TOC entries with page_ids as anchors. Between anchors,
    chunks are assigned to the most recent page.
    """
    if total_pages <= 1:
        return [0] * len(chunks)

    # Build anchor map: chunk_index → page_id from TOC title matches
    anchors: dict[int, int] = {}
    for entry in toc:
        title = (entry.get("title") or "").replace("\n", " ").strip()
        page_id = entry.get("page_id", 0)
        if not title:
            continue
        # Find chunk containing this title
        for i, chunk in enumerate(chunks):
            if title[:40] in chunk.replace("\n", " "):
                anchors[i] = page_id
                break

    # Assign pages: propagate from anchors
    assignments = [0] * len(chunks)
    current_page = 0
    for i in range(len(chunks)):
        if i in anchors:
            current_page = anchors[i]
        assignments[i] = min(current_page, total_pages - 1)

    return assignments


def _find_image_key(ref: str, images: dict) -> str | None:
    """Find matching image key from a markdown image reference."""
    # Direct match
    if ref in images:
        return ref
    # Try matching by filename portion
    ref_name = ref.rsplit("/", 1)[-1]
    for key in images:
        if ref_name in key or key in ref_name:
            return key
    return None


def _encode_pil_image(img: Any) -> tuple[str, str]:
    """Encode a PIL Image to base64 PNG."""
    buf = io.BytesIO()
    fmt = "PNG"
    mime = "image/png"
    # Use JPEG for larger images
    if hasattr(img, "size"):
        w, h = img.size
        if w * h > 500_000:
            fmt = "JPEG"
            mime = "image/jpeg"
            if img.mode == "RGBA":
                img = img.convert("RGB")
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, mime


def _match_captions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match figure blocks with captions from adjacent text blocks."""
    result = []
    skip_next = False

    for i, block in enumerate(blocks):
        if skip_next:
            skip_next = False
            continue

        if block.get("type") == "figure" and i + 1 < len(blocks):
            next_block = blocks[i + 1]
            next_text = next_block.get("text", "")
            if _FIGURE_CAPTION_RE.match(next_text.strip()):
                block["text"] = next_text.strip()
                skip_next = True

        result.append(block)

    return result


def _mark_junk(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Demote frontmatter boilerplate blocks to type 'junk'.

    A junk heading and all blocks that follow it (inheriting its
    section_path) are marked as junk.  Once a real section_header
    appears, junk mode ends and subsequent blocks are normal.
    """
    in_junk = False
    junk_section: list[str] | None = None

    for block in blocks:
        btype = block.get("type", "")
        text = block.get("text", "")

        if btype == "section_header":
            if _JUNK_HEADING_RE.match(text):
                in_junk = True
                junk_section = block.get("section_path")
                block["type"] = "junk"
            else:
                in_junk = False
                junk_section = None
        elif in_junk:
            # Followers of a junk heading inherit the junk section_path
            if block.get("section_path") == junk_section:
                block["type"] = "junk"

    return blocks


# Target combined length for the merge pass — set to ``DEFAULT_CHUNK_SIZE``
# so a merged chunk never re-triggers ``split_text``. The splitter and
# the merger meet at the same waterline: anything over this size gets
# split, anything under has the chance to absorb a neighbour, the steady
# state is "as close to ``DEFAULT_CHUNK_SIZE`` as the source allows".
_MERGE_TARGET_CHARS = DEFAULT_CHUNK_SIZE


def _merge_small_blocks(
    blocks: list[dict[str, Any]],
    *,
    paper_id: str,
) -> list[dict[str, Any]]:
    """Reduce tiny-block noise that degrades embedding quality.

    bge-m3 (and dense retrievers generally) embeds very short text
    near the centroid of the embedding space — exactly where short
    generic queries also land. Standalone ``section_header`` blocks
    ("Methods", "Discussion") and one-sentence ``text`` fragments
    therefore produce mid-score false positives across most queries
    rather than being harmless dead weight. Two passes here address
    the worst offenders:

    1. **Headers absorb forward.** Walk in order; accumulate
       consecutive ``section_header`` blocks; when the next non-header
       non-junk block appears, prepend the headers' text into its body
       (blank-line separated) and inherit the body block's type. The
       heading now embeds with semantic context instead of as a bare
       label; the resulting chunk's ``section_path`` stays the body
       block's value (which already names the heading).
    2. **Adjacent same-type small blocks merge.** Consecutive
       ``text``+``text`` or ``list``+``list`` blocks under identical
       ``section_path`` and ``page`` merge while combined length stays
       at or under :data:`_MERGE_TARGET_CHARS`. Tables, figures, and
       equations remain standalone — they either have useful
       standalone addressability or are content-rich enough that a
       short variant is still a meaningful retrieval anchor.

    Pass-through guarantees:

    - Junk blocks never merge with anything (would pollute kept
      content with frontmatter noise).
    - Pending headers immediately preceding a junk block flush as
      standalone — we don't fold meaningful headings into garbage.
    - After both passes, per-page block indices are renumbered and
      ``node_id`` is recomputed so the output list is self-consistent
      for downstream chunk INSERT. This is a one-time breaking change
      for ``node_id`` stability across the merge introduction; all
      papers need re-ingestion after this ships. Indices remain stable
      thereafter (the merge is deterministic on identical input).
    """
    if not blocks:
        return blocks

    # Pass 1: section_header absorption.
    pass1: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for block in blocks:
        btype = block.get("type", "")
        if btype == "section_header":
            pending.append(block)
            continue
        if btype == "junk" or not pending:
            # Flush any pending headers as standalone — folding into
            # junk content would propagate frontmatter pollution into
            # the heading's semantic representation.
            pass1.extend(pending)
            pending = []
            pass1.append(block)
            continue
        header_text = "\n\n".join(h.get("text", "") for h in pending)
        merged = dict(block)
        merged["text"] = f"{header_text}\n\n{block.get('text', '')}".strip()
        pending = []
        pass1.append(merged)
    # Trailing headers with no following body — rare in practice (a
    # final heading just before EOF) but keep them standalone rather
    # than dropping content silently.
    pass1.extend(pending)

    # Pass 2: adjacent same-type small block merge.
    mergeable_types = {"text", "list"}
    pass2: list[dict[str, Any]] = []
    for block in pass1:
        if not pass2:
            pass2.append(block)
            continue
        prev = pass2[-1]
        btype = block.get("type", "")
        combined_len = (
            len(prev.get("text", ""))
            + len(block.get("text", ""))
            + 2  # blank-line separator added on merge
        )
        if (
            btype in mergeable_types
            and prev.get("type") == btype
            and block.get("section_path") == prev.get("section_path")
            and block.get("page") == prev.get("page")
            and combined_len <= _MERGE_TARGET_CHARS
        ):
            prev["text"] = f"{prev.get('text', '')}\n\n{block.get('text', '')}"
        else:
            pass2.append(block)

    # Renumber per-page block indices and rewrite ``node_id`` so the
    # (page, idx, node_id) triple stays self-consistent. When no
    # merges actually fired this is a no-op — the counter walks the
    # same sequence the original emitter walked.
    per_page_idx: dict[int, int] = {}
    for block in pass2:
        page = block.get("page", 0) or 0
        idx = per_page_idx.get(page, 0)
        per_page_idx[page] = idx + 1
        block["node_id"] = make_node_id(paper_id, page, idx)

    return pass2


_HEADING_PATTERN = re.compile(r"^(\d+[\.\s]\s*\S|[A-Z][A-Z\s]{2,}$)", re.MULTILINE)


def _fitz_fallback(pdf_path: Path, paper_id: str) -> list[dict[str, Any]]:
    """Fallback: extract and chunk text via fitz + recursive splitter.

    1. Extract full page text via ``page.get_text()``
    2. Strip repeating headers/footers across pages
    3. Chunk each page's text with ``precis.ingest.text_chunker``
    4. Classify chunks (heading detection)
    """
    import fitz

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count

    # Collect raw page texts
    page_texts: list[tuple[int, str]] = []
    for page_num in range(total_pages):
        text = _clean_text(doc[page_num].get_text())
        if text:
            page_texts.append((page_num, text))
    doc.close()

    # Strip repeating headers/footers before chunking
    page_texts = _strip_running_lines(page_texts, total_pages)

    # Chunk each page and build blocks
    blocks: list[dict[str, Any]] = []
    current_section: list[str] = []

    for page_num, text in page_texts:
        # split_text honors chunk_size by default but keeps single
        # un-splittable words whole; enforce_hard_max catches that
        # edge case (e.g., a no-space OCR run).
        chunks = enforce_hard_max(split_text(text))
        for idx, chunk in enumerate(chunks):
            block_type = "text"
            if _is_likely_heading(chunk):
                block_type = "section_header"
                current_section = [chunk]

            blocks.append(
                {
                    "node_id": make_node_id(paper_id, page_num, idx),
                    "page": page_num,
                    "type": block_type,
                    "text": chunk,
                    "section_path": list(current_section),
                    "bbox": None,
                    "embeddings": {},
                    "summaries": {},
                }
            )

    log.info(
        "fitz fallback: %d pages → %d chunks",
        total_pages,
        len(blocks),
    )
    return blocks


def _strip_running_lines(
    page_texts: list[tuple[int, str]], total_pages: int
) -> list[tuple[int, str]]:
    """Remove lines that repeat verbatim on ≥40% of pages (headers/footers).

    Works on the first and last 3 lines of each page.
    """
    if total_pages < 3:
        return page_texts

    threshold = total_pages * 0.4
    line_pages: dict[str, set[int]] = {}

    for page_num, text in page_texts:
        lines = text.split("\n")
        candidates = lines[:3] + lines[-3:]
        for line in candidates:
            line = line.strip()
            if 3 < len(line) <= 120:
                line_pages.setdefault(line, set()).add(page_num)

    repeating = {ln for ln, pages in line_pages.items() if len(pages) >= threshold}
    if not repeating:
        return page_texts

    log.debug("Stripping %d repeating header/footer lines", len(repeating))
    result: list[tuple[int, str]] = []
    for page_num, text in page_texts:
        lines = [ln for ln in text.split("\n") if ln.strip() not in repeating]
        cleaned = "\n".join(lines).strip()
        if cleaned:
            result.append((page_num, cleaned))
    return result


def _is_likely_heading(text: str) -> bool:
    """Heuristic: short single-line text that looks like a section heading."""
    if "\n" in text or len(text) > 120 or len(text) < 3:
        return False
    # ALL CAPS (but not just an abbreviation)
    if text == text.upper() and len(text) > 5 and not text.endswith("."):
        return True
    # Numbered heading: "1. Introduction", "2 Methods", "3.1 Results"
    if re.match(r"^\d+[\.\d]*[\.\s]\s*[A-Z]", text) and not text.endswith("."):
        return True
    return False
