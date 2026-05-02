"""``precis jobs ingest-oracles`` — load oracle wisdom into the store.

V2 port of v1's ``precis-ingest-oracle``. Reads YAML files from
``src/precis/data/oracle/`` (or a user-supplied directory) and
ingests each one as an :class:`oracle` ref with one block per entry.

Schema (per YAML file, one tradition per file)::

    slug: iching            # → ref slug (kind='oracle' is implicit)
    title: I-Ching          # → ref title
    description: ...        # → ref meta.description
    tags: [i-ching, ...]    # → open tags ('built-in' is added)
    entries:
      - title: ...          # → block meta.section_path[0]
        body: |             # → block text body
          ...
        extra_section_path: [...]    # → appended to section_path
        original: ...       # → tail line `_original_: ...`
        pinyin: ...
        lang: ...
        source: ...
        trigrams: ...       # i-ching only
        binary: ...

The ``oracle`` corpus is created on demand so oracles stay visually
separate from papers/markdown (which live in ``default``).

Modes:

- ``--dry-run``: report what would be ingested; no DB writes.
- ``--overwrite``: replace existing refs (full re-ingest of all
  blocks). Default is to skip refs that already exist.
- ``--from <DIR>``: directory of YAML files; default is the bundled
  ``data/oracle/``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from precis.embedder import Embedder
from precis.store import Store
from precis.store.types import BlockInsert, Tag

log = logging.getLogger(__name__)


# Tag every ingested oracle with this open marker so operators can
# filter built-in traditions from custom ones via
# ``search(kind='oracle', tags=['built-in'])`` once a per-source
# tag is meaningful. ``oracle`` itself is the kind, not a tag, so
# we drop the redundant v1 tag.
_BUILTIN_TAG = "built-in"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def bundled_oracle_dir() -> Path | None:
    """Return the path to the bundled ``data/oracle/`` directory.

    Resolved through ``importlib.resources`` so the lookup works in
    both editable installs and built wheels. Returns ``None`` if
    the package shipped without the data directory (e.g. a sdist
    that excluded it).
    """
    try:
        files = resources.files("precis.data.oracle")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    # ``files`` is a Traversable; for a real on-disk package we can
    # cast it to a Path. For zipped wheels the caller should glob
    # via ``files.iterdir()`` instead — but precis ships as a
    # source-tree wheel today, so the on-disk path is the only
    # supported case.
    try:
        path = Path(str(files))
    except TypeError:
        return None
    if not path.is_dir():
        return None
    return path


# ---------------------------------------------------------------------------
# Body rendering — readable markdown with structured tail
# ---------------------------------------------------------------------------


_TAIL_KEYS: tuple[str, ...] = (
    "original",
    "pinyin",
    "source",
    "lang",
    "trigrams",
    "binary",
)


def render_chunk_body(entry: dict[str, Any]) -> str:
    """Compose the block text from an entry's body + structured tail.

    Body is taken verbatim (already markdown). Each of ``original /
    pinyin / source / lang / trigrams / binary`` adds a ``_key_:
    value`` tail line so the data is searchable in the ts-vector
    column and visible without consulting block.meta.
    """
    body = (entry.get("body") or "").strip()
    tail_lines: list[str] = []
    for key in _TAIL_KEYS:
        val = entry.get(key)
        if val is None or val == "":
            continue
        tail_lines.append(f"_{key}_: {val}")
    if tail_lines:
        return f"{body}\n\n" + "\n".join(tail_lines)
    return body


def section_path(entry: dict[str, Any]) -> list[str]:
    """Build the section_path list for a block.

    ``[title]`` plus any ``extra_section_path`` items, deduped while
    preserving order. Empty / sentinel ``"—"`` entries are dropped.
    """
    head = (entry.get("title") or "").strip()
    extras = list(entry.get("extra_section_path") or [])
    out = [head] if head else []
    for x in extras:
        s = str(x).strip()
        if s and s not in out and s != "—":
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_doc(yaml_path: Path, doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ValueError(f"{yaml_path}: top-level must be a mapping")
    for required in ("slug", "title", "entries"):
        if required not in doc:
            raise ValueError(f"{yaml_path}: missing required key {required!r}")
    if not isinstance(doc.get("entries") or [], list):
        raise ValueError(f"{yaml_path}: 'entries' must be a list")
    return doc


def ingest_paper(
    yaml_path: Path,
    *,
    store: Store,
    embedder: Embedder | None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Ingest one tradition's YAML into ``oracle:<slug>``.

    Returns stats: ``{created, replaced, chunks, skipped, errors}``.
    Writes are skipped when ``dry_run=True``.
    """
    if not yaml_path.exists():
        raise FileNotFoundError(yaml_path)

    with open(yaml_path) as f:
        doc = yaml.safe_load(f)
    doc = _validate_doc(yaml_path, doc)

    slug = str(doc["slug"]).strip()
    title = str(doc["title"]).strip()
    description = str(doc.get("description") or "")
    user_tags = [str(t).strip() for t in (doc.get("tags") or []) if str(t).strip()]
    entries: list[dict[str, Any]] = list(doc.get("entries") or [])

    # Always tag with the built-in marker plus the tradition-supplied
    # user tags. Open tags only — the oracle kind doesn't allow any
    # closed axes (see store/types.py::_KIND_ALLOWED_AXES).
    open_tags: list[str] = []
    if _BUILTIN_TAG not in user_tags:
        open_tags.append(_BUILTIN_TAG)
    open_tags.extend(t for t in user_tags if t != _BUILTIN_TAG)

    stats = {
        "created": 0,
        "replaced": 0,
        "chunks": 0,
        "skipped": 0,
        "errors": 0,
    }

    if dry_run:
        stats["created"] = 1
        stats["chunks"] = len(entries)
        return stats

    existing = store.get_ref(kind="oracle", id=slug)
    if existing is not None and not overwrite:
        stats["skipped"] = 1
        return stats

    # Compose blocks. Embedding is best-effort: when no embedder is
    # configured (e.g. unit tests without sentence-transformers), the
    # blocks land without vectors — semantic search across them is
    # then a no-op, but lexical search via tsvector still works.
    block_texts: list[str] = []
    block_metas: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        text = render_chunk_body(entry)
        block_texts.append(text)
        meta: dict[str, Any] = {"section_path": section_path(entry)}
        # Surface a few high-signal entry keys in block meta so
        # forensic queries don't have to re-parse the body. Skip
        # falsy values to keep meta lean.
        for key in _TAIL_KEYS:
            val = entry.get(key)
            if val:
                meta[key] = val
        block_metas.append(meta)

    embeddings: list[list[float] | None]
    if embedder is not None and block_texts:
        vecs = embedder.embed(block_texts)
        embeddings = list(vecs)
    else:
        embeddings = [None] * len(block_texts)

    # Block positions are **1-indexed** for oracles. For traditions
    # with an inherent numbering scheme (I-Ching: 64 hexagrams) this
    # makes ``iching~49`` resolve to "Hexagram 49" exactly, instead
    # of the off-by-one ``iching~48`` that 0-indexing produced.
    # Other traditions (stoic, zen, ...) have no inherent ordering,
    # so 1-indexing is harmless there — and uniform across the kind
    # is cheaper to remember than per-tradition exceptions.
    inserts = [
        BlockInsert(
            pos=i + 1,
            text=text,
            embedding=emb,
            token_count=len(text.split()),
            meta=meta,
        )
        for i, (text, emb, meta) in enumerate(zip(block_texts, embeddings, block_metas))
    ]

    ref_meta = {
        "tradition": slug,
        "description": description,
        "ingested_at": _now_iso(),
    }

    try:
        with store.tx() as conn:
            corpus_id = store.ensure_corpus("oracle", title="Oracle")
            if existing is not None:
                # Overwrite path: delete the existing ref outright (cascades
                # to blocks + tags) and re-create from scratch. Simpler
                # than a partial replace and keeps the ref-id discipline
                # honest (ingested_at moves forward, slug stable).
                conn.execute("DELETE FROM refs WHERE id = %s", (existing.id,))

            # provider stays NULL: oracle YAMLs aren't sourced from any
            # of the registered upstream providers (arxiv, crossref, …).
            # Origin is encoded via the ``built-in`` open tag instead.
            ref = store.insert_ref(
                corpus_id=corpus_id,
                kind="oracle",
                slug=slug,
                title=title,
                meta=ref_meta,
                conn=conn,
            )
            store.insert_blocks(ref.id, inserts, conn=conn)

            for tag_str in open_tags:
                store.add_tag(
                    ref.id,
                    Tag.open(tag_str),
                    set_by="system",
                    conn=conn,
                )

        if existing is not None:
            stats["replaced"] = 1
        else:
            stats["created"] = 1
        stats["chunks"] = len(inserts)
    except Exception as exc:
        log.error("ingest failed for oracle %r: %s", slug, exc)
        stats["errors"] += 1

    return stats


def ingest_directory(
    src_dir: Path,
    *,
    store: Store,
    embedder: Embedder | None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ingest every ``*.yaml`` / ``*.yml`` file under ``src_dir``.

    Returns aggregate stats with a per-file breakdown:

        {
          "files": int,
          "created": int, "replaced": int, "chunks": int,
          "skipped": int, "errors": int,
          "per_file": {<filename>: <stats dict>, …},
        }
    """
    yaml_files = sorted(src_dir.glob("*.yaml")) + sorted(src_dir.glob("*.yml"))
    if not yaml_files:
        raise FileNotFoundError(f"no YAML files in {src_dir}")

    aggregate: dict[str, Any] = {
        "files": 0,
        "created": 0,
        "replaced": 0,
        "chunks": 0,
        "skipped": 0,
        "errors": 0,
        "per_file": {},
    }
    for yp in yaml_files:
        stats = ingest_paper(
            yp,
            store=store,
            embedder=embedder,
            overwrite=overwrite,
            dry_run=dry_run,
        )
        aggregate["files"] += 1
        for k in ("created", "replaced", "chunks", "skipped", "errors"):
            aggregate[k] += stats[k]
        aggregate["per_file"][yp.name] = stats
    return aggregate
