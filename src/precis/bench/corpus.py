"""QASPER fixture corpus: pinned fetch, deterministic selection, ingest-local.

Source and pin
--------------
QASPER (Dasigi et al. 2021, AI2) is on Hugging Face as ``allenai/qasper``,
licence CC BY 4.0. At the pinned revision :data:`HF_REVISION` the HF repo
holds only the loader script ``qasper.py``; the script's ``_URL_TRAIN_DEV``
is the archive :data:`ARCHIVE_URL` (v0.3 train + dev JSON). The data
therefore comes from that URL, fetched through
:func:`precis.utils.safe_fetch.safe_stream`, and is pinned by the archive's
sha256 (:data:`ARCHIVE_SHA256`) plus a per-paper sha256 in the manifest — a
changed byte anywhere fails the ingest loudly instead of drifting the
corpus. Only train + dev are used (1,169 papers); the held-out test split
stays unseen.

Manifest (``bench/corpus/manifest.json``)
-----------------------------------------
:func:`build_manifest` is a pure function of the archive: usable papers are
ranked by :func:`paper_tier` (0 = >= :data:`MIN_QUESTIONS` questions with both
an abstractive and an extractive answer, 1 = >= :data:`MIN_QUESTIONS`
questions only) and then by ``sha256(f"{SELECTION_SEED}:{qasper_id}")``;
``quick`` is the first :data:`QUICK_N`, ``full`` the first :data:`FULL_N`
(``quick`` is a prefix of ``full``). Per paper it records the qasper id,
split, title, the sha256 of the canonical paper JSON (:func:`paper_sha256`),
the licence and the HF revision. ``corpus_sha256`` per set hashes the
ordered ``id sha256`` lines. No prod text anywhere.

Ingest
------
:func:`ingest_set` mints one ``kind='paper'`` ref per paper through the
ingest primitives (:func:`precis.ingest.db_writer.write_paper`, the card
builder and :func:`precis.ingest.text_chunker.split_text` that markup ingest
uses). Identity is ``paper_id = "qasper:<id>"`` — deliberately not an arXiv
or DOI identifier, so a seeded dev DB holding the real paper never collides
with the fixture. Idempotent: a paper whose ``refs.meta.qasper.id`` is
already present is skipped (body chunks are append-only; a changed paper
hash is reported as ``stale``, never rewritten). No embedder is called here:
embeddings and the derived passes belong to the worker (``fill_embeddings``).

Chunk meta (the contract gold mapping reads)
--------------------------------------------
Body chunks carry ``meta["qasper"]`` with the id of the *source unit* the
chunk was cut from and character offsets, so gold stored as spans survives
any re-chunking:

``para_id``     ``"title"``, ``"abstract"``, ``"s<S>p<P>"`` (section index S,
                paragraph index P in the raw QASPER ``full_text``, empty
                paragraphs keep their index) or ``"f<N>"`` (N-th entry of
                ``figures_and_tables``; its caption is the unit text, the
                string QASPER evidence quotes after ``FLOAT SELECTED:``).
``src``         ``title`` | ``abstract`` | ``paragraph`` | ``float``.
``para_char_start`` / ``para_char_end``
                half-open offsets of the chunk text inside the unit text
                (``None`` if the chunk text could not be located verbatim).
``doc_char_start`` / ``doc_char_end``
                the same span in :func:`canonical_text` (the units joined by
                a blank line, in :func:`canonical_units` order).
``section_idx`` / ``para_idx``  integers, ``None`` outside the body.

Chunks overlap by the chunker's overlap, so one gold span can touch two
chunks; recall is span overlap with a rendered chunk. The title and an
abstract short enough to be one chunk are not repeated as body chunks: their
text is the ``card_title`` / ``card_abstract`` card (``write_paper`` drops a
duplicate text, so the loader skips it first and keeps body ``ord`` gapless).
A longer abstract is split into ``abstract`` body chunks.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.identity import make_content_hash
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite, write_paper
from precis.ingest.pipeline import _build_cards, _title_cite_key
from precis.ingest.text_chunker import (
    CHUNKER_VERSION,
    DEFAULT_CHUNK_SIZE,
    enforce_hard_max,
    split_text,
)
from precis.utils.numerics import extract_numerics_wide

log = logging.getLogger(__name__)

HF_REPO = "allenai/qasper"
#: HF ``main`` of the repo at pin time (holds the v0.3 loader script).
HF_REVISION = "fdc9d8214fbab5dd782958601db4d678e6934a54"
DATASET_LICENCE = "CC-BY-4.0"
ARCHIVE_URL = (
    "https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz"
)
ARCHIVE_NAME = "qasper-train-dev-v0.3.tgz"
ARCHIVE_SHA256 = "a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a"
#: Hard cap on the download (the archive is ~11 MB).
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
#: Archive members read — nothing else is ever extracted.
MEMBERS: dict[str, str] = {
    "qasper-train-v0.3.json": "train",
    "qasper-dev-v0.3.json": "dev",
}

SELECTION_SEED = "precis-bench-qasper-v1"
MIN_QUESTIONS = 5
QUICK_N = 30
FULL_N = 200
SET_SIZES: dict[str, int] = {"quick": QUICK_N, "full": FULL_N}

MANIFEST_SCHEMA = 1
#: Refuse any database whose name is not a throwaway one (prod is never one).
_SAFE_DBNAME_RE = re.compile(r"^precis_(dev|test|bench)")


class BenchCorpusError(RuntimeError):
    """A pin, manifest or target-database check failed."""


# ---------------------------------------------------------------------------
# Paths, cache, fetch
# ---------------------------------------------------------------------------


def default_manifest_path() -> Path:
    """``bench/corpus/manifest.json`` of the source checkout.

    ``PRECIS_BENCH_MANIFEST`` overrides it (an installed wheel has no repo).
    """
    env = os.environ.get("PRECIS_BENCH_MANIFEST")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "bench" / "corpus" / "manifest.json"


def cache_dir() -> Path:
    """``PRECIS_BENCH_CACHE`` or the platformdirs user cache for the bench."""
    env = os.environ.get("PRECIS_BENCH_CACHE")
    if env:
        return Path(env)
    from platformdirs import user_cache_dir

    return Path(user_cache_dir("precis-bench"))


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download_once(url: str, part: Path, sha256: str | None) -> None:
    """One SSRF-guarded download attempt into ``part``, size-capped and hashed."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_stream

    size = 0
    with http_client(timeout=120.0) as client:
        with safe_stream(client, "GET", url) as resp:
            resp.raise_for_status()
            with part.open("wb") as out:
                for block in resp.iter_bytes(chunk_size=1 << 16):
                    size += len(block)
                    if size > MAX_ARCHIVE_BYTES:
                        raise BenchCorpusError(
                            f"{url} exceeds the {MAX_ARCHIVE_BYTES}-byte cap"
                        )
                    out.write(block)
    got = _file_sha256(part)
    if sha256 is not None and got != sha256:
        raise BenchCorpusError(
            f"{url} sha256 {got} != pinned {sha256}; the upstream file changed"
        )


def fetch_archive(
    cache: Path | None = None,
    *,
    url: str = ARCHIVE_URL,
    sha256: str | None = ARCHIVE_SHA256,
) -> Path:
    """Return the cached archive, downloading it (SSRF-guarded) when absent.

    A cached file whose hash disagrees with ``sha256`` is discarded and
    re-fetched once; a fresh download that disagrees raises. ``sha256=None``
    skips verification (only the manifest builder bootstrapping a new pin).
    """
    from precis.utils.http import call_with_backoff

    root = cache if cache is not None else cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / ARCHIVE_NAME
    if dest.is_file():
        if sha256 is None or _file_sha256(dest) == sha256:
            return dest
        log.warning("bench cache %s fails its pin; re-fetching", dest)
        dest.unlink()
    part = dest.with_suffix(".part")
    try:
        # One ``host:<netloc>`` limiter token per attempt, full-jitter backoff
        # on transport/429/5xx only (invariants: outbound calls are polite).
        call_with_backoff(
            lambda: _download_once(url, part, sha256),
            service="bench-corpus",
            url=url,
        )
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def load_archive(path: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{qasper_id: (split, paper_dict)}`` read from the tgz in memory.

    Only the two names in :data:`MEMBERS` are read (never extracted to disk).
    """
    papers: dict[str, tuple[str, dict[str, Any]]] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            split = MEMBERS.get(member.name)
            if split is None or not member.isfile():
                continue
            fh = tar.extractfile(member)
            assert fh is not None
            data = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
            for qid, paper in data.items():
                papers[qid] = (split, paper)
    if not papers:
        raise BenchCorpusError(f"{path}: none of {sorted(MEMBERS)} found")
    return papers


# ---------------------------------------------------------------------------
# Hashes and selection
# ---------------------------------------------------------------------------


def paper_sha256(paper: Mapping[str, Any]) -> str:
    """sha256 of the paper record as canonical JSON: sorted keys, compact
    separators, ``ensure_ascii=False``, UTF-8, exactly the upstream fields."""
    blob = json.dumps(
        paper, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def paper_tier(paper: Mapping[str, Any]) -> int | None:
    """Selection preference, lower is better; ``None`` = unusable.

    Unusable: fewer than :data:`MIN_QUESTIONS` questions, no title or
    abstract, or no body paragraph. Tier 0 additionally has at least one
    abstractive (free-form) and one extractive answer among its questions;
    tier 1 is everything else usable. Train + dev hold 142 tier-0 papers, so
    the 200-paper ``full`` set takes 58 tier-1 papers after them.
    """
    qas = paper.get("qas") or []
    if len(qas) < MIN_QUESTIONS or not paper.get("abstract") or not paper.get("title"):
        return None
    if not any(s.get("paragraphs") for s in paper.get("full_text") or []):
        return None
    abstractive = extractive = False
    for qa in qas:
        for ans in qa.get("answers") or []:
            a = ans.get("answer") or {}
            abstractive = abstractive or bool((a.get("free_form_answer") or "").strip())
            extractive = extractive or bool(a.get("extractive_spans"))
    return 0 if abstractive and extractive else 1


def rank_key(qasper_id: str) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}:{qasper_id}".encode()).hexdigest()


def select_papers(
    papers: Mapping[str, tuple[str, Mapping[str, Any]]], n: int
) -> list[str]:
    """The first ``n`` usable ids by (:func:`paper_tier`, :func:`rank_key`,
    id). A pure function of the ids and paper contents, independent of dict
    order; the ``n``-prefix of a longer selection is the shorter selection."""
    ranked: list[tuple[int, str, str]] = []
    for qid, (_, p) in papers.items():
        tier = paper_tier(p)
        if tier is not None:
            ranked.append((tier, rank_key(qid), qid))
    ranked.sort()
    if len(ranked) < n:
        raise BenchCorpusError(f"only {len(ranked)} usable papers, need {n}")
    return [qid for _, _, qid in ranked[:n]]


def corpus_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    """Hash of the ordered ``<qasper_id> <paper sha256>`` lines."""
    body = "".join(f"{r['qasper_id']} {r['sha256']}\n" for r in records)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_manifest(
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    archive_sha256: str = ARCHIVE_SHA256,
    archive_bytes: int | None = None,
    quick_n: int = QUICK_N,
    full_n: int = FULL_N,
) -> dict[str, Any]:
    """The committed manifest, a deterministic function of ``papers``.

    ``quick_n`` / ``full_n`` exist for tests; the committed file uses the
    module defaults."""
    ids = select_papers(papers, full_n)

    def record(qid: str) -> dict[str, Any]:
        split, paper = papers[qid]
        return {
            "qasper_id": qid,
            "split": split,
            "title": " ".join(str(paper["title"]).split()),
            "n_questions": len(paper["qas"]),
            "tier": paper_tier(paper),
            "sha256": paper_sha256(paper),
            "licence": DATASET_LICENCE,
            "hf_revision": HF_REVISION,
        }

    full = [record(q) for q in ids]
    quick = full[:quick_n]
    archive: dict[str, Any] = {
        "url": ARCHIVE_URL,
        "name": ARCHIVE_NAME,
        "sha256": archive_sha256,
        "members": sorted(MEMBERS),
    }
    if archive_bytes is not None:
        archive["bytes"] = archive_bytes
    return {
        "schema": MANIFEST_SCHEMA,
        "dataset": {
            "name": "QASPER",
            "hf_repo": HF_REPO,
            "hf_revision": HF_REVISION,
            "version": "v0.3",
            "licence": DATASET_LICENCE,
            "licence_url": "https://creativecommons.org/licenses/by/4.0/",
            "homepage": "https://allenai.org/data/qasper",
            "citation": "Dasigi et al. 2021, A Dataset of Information-Seeking "
            "Questions and Answers Anchored in Research Papers (NAACL)",
            "archive": archive,
        },
        "selection": {
            "seed": SELECTION_SEED,
            "rank": "(tier, sha256(seed + ':' + qasper_id)) ascending",
            "tiers": f"0 = >= {MIN_QUESTIONS} questions with >= 1 abstractive "
            f"and >= 1 extractive answer; 1 = >= {MIN_QUESTIONS} questions only",
            "pool": "train + dev",
            "pool_size": len(papers),
            "tier0_size": sum(1 for _, p in papers.values() if paper_tier(p) == 0),
            "tier1_size": sum(1 for _, p in papers.values() if paper_tier(p) == 1),
            "sha256_of": "paper JSON, sorted keys, compact separators, "
            "ensure_ascii=False, UTF-8",
        },
        "corpus_sha256": {
            "quick": corpus_sha256(quick),
            "full": corpus_sha256(full),
        },
        "quick": quick,
        "full": full,
    }


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    p = path or default_manifest_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise BenchCorpusError(f"{p}: unsupported manifest schema {data.get('schema')}")
    return data


def manifest_text(manifest: Mapping[str, Any]) -> str:
    """Stable on-disk rendering (trailing newline, non-ASCII kept)."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Canonical text units
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """One source unit of a paper (title, abstract, paragraph, float caption)."""

    para_id: str
    src: str  # title | abstract | paragraph | float
    text: str  # verbatim from QASPER, never stripped (title: whitespace-collapsed)
    section_path: tuple[str, ...]
    section_idx: int | None
    para_idx: int | None
    doc_start: int  # offset of ``text`` inside :func:`canonical_text`

    @property
    def doc_end(self) -> int:
        return self.doc_start + len(self.text)


_UNIT_SEP = "\n\n"


def canonical_units(paper: Mapping[str, Any]) -> list[Unit]:
    """Non-blank units in reading order: title, abstract, every section
    paragraph, then figure/table captions. ``doc_start`` is exact:
    ``canonical_text(paper)[u.doc_start:u.doc_end] == u.text``."""
    raw: list[tuple[str, str, str, tuple[str, ...], int | None, int | None]] = [
        # whitespace-collapsed, as the ref title and card_title are
        (
            "title",
            "title",
            " ".join(str(paper.get("title") or "").split()),
            (),
            None,
            None,
        ),
        (
            "abstract",
            "abstract",
            str(paper.get("abstract") or ""),
            ("Abstract",),
            None,
            None,
        ),
    ]
    for s_idx, sec in enumerate(paper.get("full_text") or []):
        name = str(sec.get("section_name") or "").strip()
        path = tuple(p.strip() for p in name.split(":::") if p.strip())
        for p_idx, para in enumerate(sec.get("paragraphs") or []):
            raw.append(
                (f"s{s_idx}p{p_idx}", "paragraph", str(para), path, s_idx, p_idx)
            )
    for f_idx, fig in enumerate(paper.get("figures_and_tables") or []):
        raw.append(
            (f"f{f_idx}", "float", str(fig.get("caption") or ""), (), None, None)
        )

    units: list[Unit] = []
    pos = 0
    for para_id, src, text, path, sec_idx, par_idx in raw:
        if not text.strip():
            continue
        units.append(Unit(para_id, src, text, path, sec_idx, par_idx, pos))
        pos += len(text) + len(_UNIT_SEP)
    return units


def canonical_text(paper: Mapping[str, Any]) -> str:
    return _UNIT_SEP.join(u.text for u in canonical_units(paper))


# ---------------------------------------------------------------------------
# Chunks and ref
# ---------------------------------------------------------------------------

_KIND_BY_SRC = {
    "title": "heading",
    "abstract": "paragraph",
    "paragraph": "paragraph",
    "float": "caption",
}


def _pieces(unit: Unit) -> list[str]:
    """Same split policy as markup ingest: prose via ``split_text``, structural
    units (title, captions) whole and only hard-capped."""
    if unit.src in ("title", "float"):
        return enforce_hard_max([unit.text])
    return enforce_hard_max(split_text(unit.text, DEFAULT_CHUNK_SIZE))


def chunk_units(
    units: Iterable[Unit], *, seen_texts: Iterable[str] = ()
) -> list[ChunkToWrite]:
    """Body chunks (``ord`` from 0) with source ids and offsets in meta.

    ``write_paper`` drops a chunk whose text equals an earlier chunk's
    (first seen wins, cards included), which would leave holes in ``ord``.
    A piece already in ``seen_texts`` or already emitted is therefore skipped
    here, before ``ord`` is assigned: the title and a short abstract live on
    as ``card_title`` / ``card_abstract`` rather than as duplicate body
    chunks, and the body ``ord`` run stays contiguous.
    """
    seen = set(seen_texts)
    chunks: list[ChunkToWrite] = []
    for unit in units:
        cursor = 0
        for piece in _pieces(unit):
            start = unit.text.find(piece, cursor)
            if start < 0:
                start = unit.text.find(piece)
            if start >= 0:
                cursor = start + 1
            if piece in seen:
                continue
            seen.add(piece)
            if start >= 0:
                p_start: int | None = start
                p_end: int | None = start + len(piece)
                d_start: int | None = unit.doc_start + start
                d_end: int | None = unit.doc_start + start + len(piece)
            else:
                p_start = p_end = d_start = d_end = None
            chunks.append(
                ChunkToWrite(
                    ord=len(chunks),
                    chunk_kind=_KIND_BY_SRC[unit.src],
                    text=piece,
                    section_path=list(unit.section_path),
                    numerics=extract_numerics_wide(piece),
                    meta={
                        "qasper": {
                            "para_id": unit.para_id,
                            "src": unit.src,
                            "section_idx": unit.section_idx,
                            "para_idx": unit.para_idx,
                            "para_char_start": p_start,
                            "para_char_end": p_end,
                            "doc_char_start": d_start,
                            "doc_char_end": d_end,
                        },
                        "chunker_version": CHUNKER_VERSION,
                    },
                )
            )
    return chunks


def paper_to_write(
    qasper_id: str,
    split: str,
    paper: Mapping[str, Any],
    *,
    sha256: str | None = None,
) -> PaperToWrite:
    """Assemble the :class:`PaperToWrite` for one QASPER paper."""
    title = " ".join(str(paper["title"]).split())
    abstract = str(paper.get("abstract") or "")
    units = canonical_units(paper)
    cards = _build_cards(title=title, authors=[], abstract=abstract, keywords=[])
    body = chunk_units(units, seen_texts=(c.text for c in cards))
    meta: dict[str, Any] = {
        "source_format": "qasper",
        "abstract": abstract,
        "qasper": {
            "id": qasper_id,
            "split": split,
            "sha256": sha256 or paper_sha256(paper),
            "hf_repo": HF_REPO,
            "hf_revision": HF_REVISION,
            "licence": DATASET_LICENCE,
            "n_questions": len(paper.get("qas") or []),
        },
    }
    return PaperToWrite(
        title=title,
        authors=[],
        year=None,
        kind="paper",
        provider="local",
        set_by="system",
        paper_id=f"qasper:{qasper_id}",
        cite_key_prefix=_title_cite_key(title, None),
        content_hash=make_content_hash(_UNIT_SEP.join(u.text for u in units)),
        meta=meta,
        chunks=cards + body,
    )


# ---------------------------------------------------------------------------
# ingest-local
# ---------------------------------------------------------------------------


def assert_throwaway_dsn(dsn: str) -> None:
    """Refuse a database that is not a dev/test/bench one (never prod)."""
    from psycopg.conninfo import conninfo_to_dict

    name = str(conninfo_to_dict(dsn).get("dbname") or "")
    if not _SAFE_DBNAME_RE.match(name):
        raise BenchCorpusError(
            f"refusing to ingest into database {name!r}: the bench writes only to "
            "a throwaway database named precis_dev*/precis_test*/precis_bench* "
            "(scripts/dev db-up creates precis_dev)"
        )


@dataclass
class IngestReport:
    set_name: str
    corpus_sha256: str
    inserted: int = 0
    skipped: int = 0
    stale: int = 0
    chunks: int = 0

    def line(self) -> str:
        return (
            f"set={self.set_name} corpus_sha256={self.corpus_sha256} "
            f"inserted={self.inserted} skipped={self.skipped} stale={self.stale} "
            f"chunks_written={self.chunks}"
        )


def existing_ref(conn: Any, qasper_id: str) -> tuple[int, str | None] | None:
    """``(ref_id, stored paper sha256)`` for a live ref minted from this
    QASPER id, else ``None`` — the idempotency key."""
    row = conn.execute(
        "SELECT ref_id, meta->'qasper'->>'sha256' FROM refs "
        "WHERE kind = 'paper' AND retired_at IS NULL "
        "AND meta->'qasper'->>'id' = %s ORDER BY ref_id LIMIT 1",
        (qasper_id,),
    ).fetchone()
    return None if row is None else (int(row[0]), row[1])


def ingest_papers(
    conn: Any,
    records: list[Mapping[str, Any]],
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    set_name: str = "custom",
    corpus_hash: str | None = None,
) -> IngestReport:
    """Mint the refs for ``records`` (manifest rows) from ``papers``.

    One transaction per paper, so an interrupted run resumes where it stopped.
    Every paper is hash-checked against its manifest row before any write.
    """
    report = IngestReport(set_name, corpus_hash or corpus_sha256(records))
    for rec in records:
        qid = rec["qasper_id"]
        if qid not in papers:
            raise BenchCorpusError(f"{qid} is in the manifest but not in the archive")
        split, paper = papers[qid]
        got = paper_sha256(paper)
        if got != rec["sha256"]:
            raise BenchCorpusError(
                f"{qid}: paper sha256 {got} != manifest {rec['sha256']}"
            )
        have = existing_ref(conn, qid)
        if have is not None:
            if have[1] != got:
                report.stale += 1
                log.warning(
                    "%s: ref %s was minted from a different paper hash", qid, have[0]
                )
            report.skipped += 1
            continue
        with conn.transaction():
            result = write_paper(
                paper_to_write(qid, split, paper, sha256=got), conn=conn
            )
        report.inserted += 1
        report.chunks += result.chunks_written
    return report


def ingest_set(
    conn: Any,
    set_name: str,
    *,
    manifest: Mapping[str, Any] | None = None,
    cache: Path | None = None,
) -> IngestReport:
    """Fetch (cached, pinned) and ingest the ``quick`` or ``full`` set."""
    if set_name not in SET_SIZES:
        raise BenchCorpusError(
            f"unknown set {set_name!r}; use one of {sorted(SET_SIZES)}"
        )
    man = manifest if manifest is not None else load_manifest()
    records = man[set_name]
    archive = fetch_archive(cache, sha256=man["dataset"]["archive"]["sha256"])
    papers = load_archive(archive)
    expected = man["corpus_sha256"][set_name]
    if corpus_sha256(records) != expected:
        raise BenchCorpusError(
            f"manifest {set_name!r} does not match its corpus_sha256"
        )
    return ingest_papers(conn, records, papers, set_name=set_name, corpus_hash=expected)
