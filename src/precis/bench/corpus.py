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
corpus. The ``quick`` / ``full`` sets use train + dev only (1,169 papers).

The frozen held-out set (``heldout``, :data:`HELDOUT_N` papers) comes from the
v0.3 *test* split, a second archive (:data:`TEST_ARCHIVE_URL`, pinned by
:data:`TEST_ARCHIVE_SHA256`). It is ranked by the same tier rule and
deterministic key as ``quick`` / ``full`` but over the test split alone, so no
heldout id can appear in ``quick`` / ``full`` (different splits). Tuning and
headline tables must not share a set: tune on ``quick`` / ``full``, score the
headline on ``heldout``. Nobody runs the bench on ``heldout`` while tuning.

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

Re-ingest variants (the ``chunk-*`` / ``no-links`` / ``distractor-N`` arms)
--------------------------------------------------------------------------
Chunking is index-time, so a chunking arm is a re-ingest of the set into its
own throwaway database — the same ``qasper:<id>`` ref cannot exist twice in
one DB (``existing_ref`` is the idempotency key). :class:`Variant` names each
arm's world: which ``chunker`` cut it, whether it writes links, and how much
non-gold padding it carries. :func:`variant_dsn` derives the world's DSN
(``<base dbname>__<set>_<corpus sha256 prefix>__<arm with dashes as _>``,
still ``precis_dev*``-shaped — the corpus identity is in the name, so a
``quick`` run never lands in a ``full`` world or a re-pinned manifest's),
:func:`ensure_database` creates and migrates it, and
:func:`provision_variant` runs the ingest. Each chunk meta still carries
``doc_char_start`` / ``doc_char_end``, so
:func:`precis.bench.gold.spans_overlap_chunk` scores every variant unchanged.

The chunkers (:func:`_pieces`):

``sentence-pysbd``   today's default: ``split_text`` at
                     ``DEFAULT_CHUNK_SIZE`` — pysbd sentence boundaries and
                     the 150-char overlap (spec arm ``chunk-sentence-pysbd``).
``fixed-<N>``        fixed ``N``-token windows — ``4N`` chars at the runner's
                     chars/4 estimator — cut per unit, no sentence awareness
                     and no overlap (``chunk-fixed-512``).
``section``          structure-aware: one chunk per source unit (a QASPER
                     paragraph is the unit; ``enforce_hard_max`` still caps a
                     pathological unit), ``chunk-section``.

``no-links`` ingests without mesh/citation edges — today that is the only
ingest anyway (edges are a paid LLM pass), so provisioning deletes whatever
``links`` rows landed and the run's coverage marks the arm vacuous until an
edges pass exists. ``distractor-<N>`` pads the set with N× extra non-gold
body paragraphs taken from train-split papers outside every manifest set
(:func:`select_distractors`), so recall and tokens are measured under corpus
growth.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tarfile
from collections.abc import Iterable, Mapping, Sequence
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

#: v0.3 test split (+ evaluator) archive, the source of the ``heldout`` set.
TEST_ARCHIVE_URL = (
    "https://qasper-dataset.s3.us-west-2.amazonaws.com/"
    "qasper-test-and-evaluator-v0.3.tgz"
)
TEST_ARCHIVE_NAME = "qasper-test-and-evaluator-v0.3.tgz"
TEST_ARCHIVE_SHA256 = "72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72"
TEST_MEMBERS: dict[str, str] = {"qasper-test-v0.3.json": "test"}

SELECTION_SEED = "precis-bench-qasper-v1"
MIN_QUESTIONS = 5
QUICK_N = 30
FULL_N = 200
HELDOUT_N = 100
#: The test split has only 89 papers with MIN_QUESTIONS questions; the last 11
#: of heldout come from papers with at least this many (tiers 2 / 3).
HELDOUT_MIN_QUESTIONS = 4
HELDOUT_SET = "heldout"
SET_SIZES: dict[str, int] = {"quick": QUICK_N, "full": FULL_N}
#: Every set the manifest carries; ``heldout`` is for ingest/tasks only (the
#: ablation runner keeps :data:`SET_SIZES`).
ALL_SETS: dict[str, int] = {**SET_SIZES, HELDOUT_SET: HELDOUT_N}

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
    name: str = ARCHIVE_NAME,
) -> Path:
    """Return the cached archive, downloading it (SSRF-guarded) when absent.

    A cached file whose hash disagrees with ``sha256`` is discarded and
    re-fetched once; a fresh download that disagrees raises. ``sha256=None``
    skips verification (only the manifest builder bootstrapping a new pin).
    """
    from precis.utils.http import call_with_backoff

    root = cache if cache is not None else cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name
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


def load_archive(
    path: Path, members: Mapping[str, str] = MEMBERS
) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{qasper_id: (split, paper_dict)}`` read from the tgz in memory.

    Only the names in ``members`` (default :data:`MEMBERS`) are read, never
    extracted to disk.
    """
    papers: dict[str, tuple[str, dict[str, Any]]] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            split = members.get(member.name)
            if split is None or not member.isfile():
                continue
            fh = tar.extractfile(member)
            assert fh is not None
            data = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
            for qid, paper in data.items():
                papers[qid] = (split, paper)
    if not papers:
        raise BenchCorpusError(f"{path}: none of {sorted(members)} found")
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


def paper_tier(
    paper: Mapping[str, Any], *, min_questions: int = MIN_QUESTIONS
) -> int | None:
    """Selection preference, lower is better; ``None`` = unusable.

    Unusable: fewer than :data:`MIN_QUESTIONS` questions, no title or
    abstract, or no body paragraph. Tier 0 additionally has at least one
    abstractive (free-form) and one extractive answer among its questions;
    tier 1 is everything else usable. Train + dev hold 142 tier-0 papers, so
    the 200-paper ``full`` set takes 58 tier-1 papers after them.
    """
    qas = paper.get("qas") or []
    if len(qas) < min_questions or not paper.get("abstract") or not paper.get("title"):
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
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    n: int,
    *,
    relax_to: int | None = None,
) -> list[str]:
    """The first ``n`` usable ids by (:func:`paper_tier`, :func:`rank_key`,
    id). A pure function of the ids and paper contents, independent of dict
    order; the ``n``-prefix of a longer selection is the shorter selection.

    ``relax_to`` (the test split only: it holds 89 papers with
    :data:`MIN_QUESTIONS` questions, fewer than :data:`HELDOUT_N`) admits
    papers with at least ``relax_to`` questions as tiers 2 / 3 (the same
    abstractive + extractive split), ranked after every tier 0 / 1 paper."""
    ranked: list[tuple[int, str, str]] = []
    for qid, (_, p) in papers.items():
        tier = paper_tier(p)
        if tier is None and relax_to is not None:
            low = paper_tier(p, min_questions=relax_to)
            tier = None if low is None else low + 2
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
    heldout_papers: Mapping[str, tuple[str, Mapping[str, Any]]] | None = None,
    heldout_archive_sha256: str = TEST_ARCHIVE_SHA256,
    heldout_archive_bytes: int | None = None,
    heldout_n: int = HELDOUT_N,
) -> dict[str, Any]:
    """The committed manifest, a deterministic function of ``papers`` (and,
    for the ``heldout`` set, of ``heldout_papers``, the test split).

    ``quick_n`` / ``full_n`` / ``heldout_n`` exist for tests; the committed
    file uses the module defaults."""
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
    manifest: dict[str, Any] = {
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
    if heldout_papers is not None:
        clash = sorted(set(heldout_papers) & set(papers))
        if clash:
            raise BenchCorpusError(
                f"heldout ids also in train/dev: {', '.join(clash[:5])}"
            )
        held_ids = select_papers(
            heldout_papers, heldout_n, relax_to=HELDOUT_MIN_QUESTIONS
        )

        def held_record(qid: str) -> dict[str, Any]:
            split, paper = heldout_papers[qid]
            tier = paper_tier(paper)
            if tier is None:
                tier = 2 + (paper_tier(paper, min_questions=HELDOUT_MIN_QUESTIONS) or 0)
            return {
                "qasper_id": qid,
                "split": split,
                "title": " ".join(str(paper["title"]).split()),
                "n_questions": len(paper["qas"]),
                "tier": tier,
                "sha256": paper_sha256(paper),
                "licence": DATASET_LICENCE,
                "hf_revision": HF_REVISION,
            }

        held = [held_record(q) for q in held_ids]
        test_archive: dict[str, Any] = {
            "url": TEST_ARCHIVE_URL,
            "name": TEST_ARCHIVE_NAME,
            "sha256": heldout_archive_sha256,
            "members": sorted(TEST_MEMBERS),
        }
        if heldout_archive_bytes is not None:
            test_archive["bytes"] = heldout_archive_bytes
        manifest["dataset"]["heldout_archive"] = test_archive
        manifest["selection"]["heldout"] = {
            "pool": "test",
            "pool_size": len(heldout_papers),
            "tier_sizes": {
                str(t): sum(1 for r in held if r["tier"] == t) for t in (0, 1, 2, 3)
            },
            "tiers": "tiers 0/1 as above (>= 5 questions); the test split has "
            f"only 89 such papers, so tiers 2/3 are the same split over papers "
            f"with >= {HELDOUT_MIN_QUESTIONS} questions, ranked after them",
            "note": "same rank as quick/full, over the test split; "
            "frozen, never tuned on",
        }
        manifest["corpus_sha256"][HELDOUT_SET] = corpus_sha256(held)
        manifest[HELDOUT_SET] = held
    return manifest


def load_set_papers(
    man: Mapping[str, Any], set_name: str, cache: Path | None = None
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Fetch (cached, pinned) and read the archive ``set_name`` is drawn from."""
    if set_name == HELDOUT_SET:
        pin = man["dataset"]["heldout_archive"]
        path = fetch_archive(
            cache, url=pin["url"], sha256=pin["sha256"], name=pin["name"]
        )
        return load_archive(path, TEST_MEMBERS)
    return load_archive(
        fetch_archive(cache, sha256=man["dataset"]["archive"]["sha256"])
    )


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

#: The default chunker — the one a plain ``scripts/bench ingest`` writes.
DEFAULT_CHUNKER = "sentence-pysbd"
SECTION_CHUNKER = "section"
#: ``fixed-<N>`` names a fixed ``N``-token window; the runner's token
#: estimator counts 4 characters per token, so a window is ``4N`` chars.
CHARS_PER_TOKEN = 4
_FIXED_RE = re.compile(r"^fixed-(\d+)$")


def _check_chunker(chunker: str) -> None:
    if chunker in (DEFAULT_CHUNKER, SECTION_CHUNKER) or _FIXED_RE.match(chunker):
        return
    raise BenchCorpusError(
        f"unknown chunker {chunker!r}; use {DEFAULT_CHUNKER!r}, "
        f"{SECTION_CHUNKER!r} or 'fixed-<tokens>'"
    )


def fixed_windows(text: str, width: int) -> list[str]:
    """Contiguous ``width``-char slices of ``text`` — the ``fixed-<N>``
    chunker, with no boundary awareness and no overlap."""
    return [text[i : i + width] for i in range(0, len(text), width)]


def _pieces(unit: Unit, chunker: str = DEFAULT_CHUNKER) -> list[str]:
    """The chunks one unit is cut into under ``chunker``.

    ``sentence-pysbd`` is the same split policy as markup ingest: prose via
    ``split_text``, structural units (title, captions) whole and only
    hard-capped. ``fixed-<N>`` slices every unit into fixed windows — windows
    never cross a unit, so ``para_id`` / ``para_char_*`` / ``doc_char_*``
    stay exact. ``section`` emits each unit whole (hard-capped).
    """
    fixed = _FIXED_RE.match(chunker)
    if fixed is not None:
        return fixed_windows(unit.text, int(fixed.group(1)) * CHARS_PER_TOKEN)
    if unit.src in ("title", "float") or chunker == SECTION_CHUNKER:
        return enforce_hard_max([unit.text])
    if chunker == DEFAULT_CHUNKER:
        return enforce_hard_max(split_text(unit.text, DEFAULT_CHUNK_SIZE))
    raise BenchCorpusError(
        f"unknown chunker {chunker!r}; use {DEFAULT_CHUNKER!r}, "
        f"{SECTION_CHUNKER!r} or 'fixed-<tokens>'"
    )


def chunk_units(
    units: Iterable[Unit],
    *,
    seen_texts: Iterable[str] = (),
    chunker: str = DEFAULT_CHUNKER,
) -> list[ChunkToWrite]:
    """Body chunks (``ord`` from 0) with source ids and offsets in meta.

    ``write_paper`` drops a chunk whose text equals an earlier chunk's
    (first seen wins, cards included), which would leave holes in ``ord``.
    A piece already in ``seen_texts`` or already emitted is therefore skipped
    here, before ``ord`` is assigned: the title and a short abstract live on
    as ``card_title`` / ``card_abstract`` rather than as duplicate body
    chunks, and the body ``ord`` run stays contiguous.
    """
    _check_chunker(chunker)
    seen = set(seen_texts)
    chunks: list[ChunkToWrite] = []
    for unit in units:
        cursor = 0
        for piece in _pieces(unit, chunker):
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
                        "bench_chunker": chunker,
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
    chunker: str = DEFAULT_CHUNKER,
    distractor: bool = False,
) -> PaperToWrite:
    """Assemble the :class:`PaperToWrite` for one QASPER paper.

    ``distractor`` marks a non-gold padding paper (``distractor-<N>`` arms):
    it keeps its ``qasper:<id>`` identity (it *is* a QASPER paper) and is
    flagged ``meta.qasper.bench_distractor`` so a run can name it.
    """
    title = " ".join(str(paper["title"]).split())
    abstract = str(paper.get("abstract") or "")
    units = canonical_units(paper)
    cards = _build_cards(title=title, authors=[], abstract=abstract, keywords=[])
    body = chunk_units(units, seen_texts=(c.text for c in cards), chunker=chunker)
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
    if distractor:
        meta["qasper"]["bench_distractor"] = True
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
    chunker: str = DEFAULT_CHUNKER,
) -> IngestReport:
    """Mint the refs for ``records`` (manifest rows) from ``papers``.

    One transaction per paper, so an interrupted run resumes where it stopped.
    Every paper is hash-checked against its manifest row before any write.
    """
    _check_chunker(chunker)
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
                paper_to_write(qid, split, paper, sha256=got, chunker=chunker),
                conn=conn,
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
    chunker: str = DEFAULT_CHUNKER,
) -> IngestReport:
    """Fetch (cached, pinned) and ingest the ``quick``, ``full`` or ``heldout`` set."""
    if set_name not in ALL_SETS:
        raise BenchCorpusError(
            f"unknown set {set_name!r}; use one of {sorted(ALL_SETS)}"
        )
    man = manifest if manifest is not None else load_manifest()
    records = man[set_name]
    papers = load_set_papers(man, set_name, cache)
    expected = man["corpus_sha256"][set_name]
    if corpus_sha256(records) != expected:
        raise BenchCorpusError(
            f"manifest {set_name!r} does not match its corpus_sha256"
        )
    return ingest_papers(
        conn,
        records,
        papers,
        set_name=set_name,
        corpus_hash=expected,
        chunker=chunker,
    )


# ---------------------------------------------------------------------------
# Re-ingest variants: the chunk-*/no-links/distractor-N arms (td606579)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One re-ingest arm's world — how its database differs from the base
    ingest. ``arm`` is the runner-side arm name.

    ``chunker``     the chunker ``ingest_papers`` uses (``sentence-pysbd``,
                    ``section``, ``fixed-<tokens>``).
    ``links``       ``False`` for ``no-links``: provisioning deletes every
                    ``links`` row (a no-op today — edges are a paid pass the
                    free fill never runs — and real the day one exists).
    ``distractor``  the corpus-growth multiple: ``N`` pads the set with N×
                    extra non-gold body paragraphs from train-split papers
                    outside every manifest set.
    """

    arm: str
    chunker: str = DEFAULT_CHUNKER
    links: bool = True
    distractor: int = 0


#: The named re-ingest arms. ``chunk-sentence-pysbd`` re-ingests with the
#: default chunker: identical chunks, an independent world — the reference
#: column of the chunking comparison.
VARIANTS: tuple[Variant, ...] = (
    Variant("chunk-sentence-pysbd"),
    Variant("chunk-fixed-512", chunker="fixed-512"),
    Variant("chunk-section", chunker=SECTION_CHUNKER),
    Variant("no-links", links=False),
)
_VARIANT_BY_ARM: dict[str, Variant] = {v.arm: v for v in VARIANTS}
_DISTRACTOR_RE = re.compile(r"^distractor-(\d+)$")


def variant_for_arm(arm: str) -> Variant | None:
    """The :class:`Variant` an arm names, else ``None``.

    Besides :data:`VARIANTS`, ``distractor-<N>`` for any ``N >= 1`` is a
    valid (open-ended) arm: the fixture padded with N× extra non-gold
    paragraphs.
    """
    found = _VARIANT_BY_ARM.get(arm)
    if found is not None:
        return found
    m = _DISTRACTOR_RE.match(arm)
    if m is not None and int(m.group(1)) >= 1:
        return Variant(arm, distractor=int(m.group(1)))
    return None


def variant_dbname(dsn: str) -> str:
    """The ``dbname`` a (variant) DSN points at — what the results header
    records instead of the full DSN."""
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(dsn).get("dbname") or "")


def variant_dsn(dsn: str, arm: str, set_name: str, manifest: Mapping[str, Any]) -> str:
    """The DSN of ``arm``'s own throwaway database for one corpus, derived
    from the base ``dsn``: same server, database
    ``<base dbname>__<set>_<corpus sha256 prefix>__<arm with dashes as _>``
    (``precis_dev``, ``quick`` -> ``precis_dev__quick_<sha>__no_links``).

    The set and its ``corpus_sha256`` are part of the name — the world's
    identity is the corpus's: a ``quick`` run never reuses a ``full``
    world (nor a re-pinned manifest's), and a ``distractor-<N>`` world is
    never padded for another set's paragraph base.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert_throwaway_dsn(dsn)
    if variant_for_arm(arm) is None:
        raise BenchCorpusError(
            f"{arm!r} is not a re-ingest arm; known: "
            f"{', '.join(sorted(_VARIANT_BY_ARM))} and distractor-<N>"
        )
    if not re.fullmatch(r"[a-z0-9_]+", set_name):
        raise BenchCorpusError(
            f"set name {set_name!r} is not database-name safe ([a-z0-9_]+)"
        )
    try:
        corpus_id = str(manifest["corpus_sha256"][set_name])
    except KeyError:
        raise BenchCorpusError(
            f"manifest has no corpus_sha256 for set {set_name!r}"
        ) from None
    name = str(conninfo_to_dict(dsn).get("dbname") or "")
    vname = f"{name}__{set_name}_{corpus_id[:12]}__{arm.replace('-', '_')}"
    if not _SAFE_DBNAME_RE.match(vname):
        raise BenchCorpusError(
            f"variant database name {vname!r} is not throwaway-shaped"
        )
    return make_conninfo(dsn, dbname=vname)


def ensure_database(dsn: str) -> None:
    """Create the DSN's database when it does not exist and apply the
    packaged migrations (the ``scripts/dev db-up`` path for a variant DB)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert_throwaway_dsn(dsn)
    dbname = str(conninfo_to_dict(dsn).get("dbname") or "")
    # template1 exists in every cluster and is connectable; ``postgres`` is
    # only a convention.
    admin = make_conninfo(dsn, dbname="template1")
    with psycopg.connect(admin, autocommit=True) as conn:
        hit = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if hit is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    from precis.store.migrate import packaged_migrator

    packaged_migrator(dsn).apply_all()


# ---------------------------------------------------------------------------
# distractor-N padding
# ---------------------------------------------------------------------------


def _body_paragraphs(paper: Mapping[str, Any]) -> int:
    return sum(1 for u in canonical_units(paper) if u.src == "paragraph")


def select_distractors(
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    exclude: set[str],
    times: int,
    base_paragraphs: int,
) -> list[str]:
    """Deterministic pick of ``times`` × ``base_paragraphs`` non-gold body
    paragraphs: whole train-split papers not in ``exclude`` (every manifest
    set's ids), in the manifest's own rank order. Whole papers, not sampled
    paragraphs, so the padding reads as documents rather than confetti."""
    target = times * base_paragraphs
    candidates = sorted(
        (
            qid
            for qid, (split, p) in papers.items()
            if split == "train" and qid not in exclude and _body_paragraphs(p) > 0
        ),
        key=rank_key,
    )
    picked: list[str] = []
    got = 0
    for qid in candidates:
        if got >= target:
            break
        picked.append(qid)
        got += _body_paragraphs(papers[qid][1])
    if target > 0 and got < target:
        raise BenchCorpusError(
            f"only {got} distractor paragraphs outside the manifest sets, "
            f"need {target} (distractor-{times})"
        )
    return picked


def _distractor_pick(
    records: Sequence[Mapping[str, Any]],
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    times: int,
) -> tuple[list[str], int]:
    """``(the deterministic distractor id list, the set's body-paragraph
    base it was sized from)`` — the padding a ``distractor-<N>`` world must
    hold *in full* before it counts as provisioned."""
    excluded = {str(r["qasper_id"]) for name in SET_SIZES for r in manifest[name]}
    base = sum(_body_paragraphs(papers[str(r["qasper_id"])][1]) for r in records)
    ids = select_distractors(
        papers, exclude=excluded, times=times, base_paragraphs=base
    )
    return ids, base


def ingest_distractors(
    conn: Any,
    records: Sequence[Mapping[str, Any]],
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    times: int,
    chunker: str = DEFAULT_CHUNKER,
) -> dict[str, int]:
    """Pad the DB with ``times`` × the set's body-paragraph count of non-gold
    papers (:func:`select_distractors`), idempotent per paper. ``records`` is
    the ingested set's manifest rows; ``manifest`` supplies the excluded id
    pool (every set)."""
    ids, base = _distractor_pick(records, papers, manifest, times=times)
    inserted = 0
    paragraphs = 0
    for qid in ids:
        split, paper = papers[qid]
        paragraphs += _body_paragraphs(paper)
        if existing_ref(conn, qid) is not None:
            continue
        with conn.transaction():
            write_paper(
                paper_to_write(qid, split, paper, chunker=chunker, distractor=True),
                conn=conn,
            )
        inserted += 1
    return {
        "base_paragraphs": base,
        "wanted_paragraphs": times * base,
        "papers": len(ids),
        "inserted": inserted,
        "paragraphs": paragraphs,
    }


def provision_variant(
    dsn: str,
    variant: Variant,
    *,
    set_name: str,
    manifest: Mapping[str, Any] | None = None,
    cache: Path | None = None,
    papers: Mapping[str, tuple[str, Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Bring ``arm``'s database up: create + migrate when missing, ingest the
    set under the variant's chunker, pad its distractors, drop its edges for
    ``no-links``. Idempotent — a provisioned DB is left alone, a partial one
    resumes. "Provisioned" includes the padding: a ``distractor-<N>`` world
    is ready only when *every* paper of the deterministic distractor pick is
    present — one flagged ref is not proof (an interrupted ingest left a
    partial pick that used to report ``already`` and silently run
    under-padded) — so distractor arms do read the archive to check."""
    import psycopg

    ensure_database(dsn)
    man = manifest if manifest is not None else load_manifest()
    records = man[set_name]
    qids = [str(r["qasper_id"]) for r in records]
    report: dict[str, Any] = {
        "arm": variant.arm,
        "set": set_name,
        "chunker": variant.chunker,
        "links": variant.links,
        "distractor": variant.distractor,
        "already": False,
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'paper' "
            "AND retired_at IS NULL AND meta->'qasper'->>'id' = ANY(%s)",
            (qids,),
        ).fetchone()
        have = 0 if row is None else int(row[0])
        ready = have == len(qids)
        if ready and variant.distractor:
            if papers is None:
                papers = load_set_papers(man, set_name, cache)
            want, _base = _distractor_pick(
                records, papers, man, times=variant.distractor
            )
            row = conn.execute(
                "SELECT count(DISTINCT meta->'qasper'->>'id') FROM refs "
                "WHERE kind = 'paper' AND retired_at IS NULL "
                "AND meta->'qasper'->>'id' = ANY(%s)",
                (want,),
            ).fetchone()
            ready = row is not None and int(row[0]) == len(want)
        if ready:
            report["already"] = True
        else:
            if papers is None:
                papers = load_set_papers(man, set_name, cache)
            report["ingest"] = ingest_papers(
                conn,
                records,
                papers,
                set_name=set_name,
                corpus_hash=man["corpus_sha256"][set_name],
                chunker=variant.chunker,
            ).line()
            if variant.distractor:
                report["distractors"] = ingest_distractors(
                    conn,
                    records,
                    papers,
                    man,
                    times=variant.distractor,
                    chunker=variant.chunker,
                )
        if not variant.links:
            conn.execute("DELETE FROM links")
    return report
