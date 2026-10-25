"""OpenAlex snapshot works loader — stream parts, stage held edges.

Slice 1 of ``docs/backlog/openalex-snapshot-cites.md``: a resumable,
idempotent pass over the public ``s3://openalex`` works dump
(``data/jsonl/works/updated_date=YYYY-MM-DD/part_NNNN.gz``) that keeps only
the citation edges touching a held paper and stages them in
``openalex_cite_edges`` (migration 0240).

- **Anonymous HTTPS, one file at a time.** The bucket answers unauthenticated
  GETs and ``list-type=2`` listings, so no AWS account is involved. Each part
  is streamed through :func:`gzip.GzipFile` — a file never lands in memory or
  on disk whole (max ~1 GB gz in flight).
- **Held matching, in order: OpenAlex id, then DOI.** The held set is
  ``ref_identifiers`` rows with ``id_kind IN ('openalex','doi')`` loaded once
  per run (``openalex`` values are the short ``W…`` form, ``doi`` values
  lowercase — migration 0049's trigger enforces it). An edge survives when
  the citing work OR the cited work resolves.
- **Per-file transaction + checkpoint.** All of one file's edge inserts and
  its ``openalex_snapshot_files`` ``done`` row commit together, so a kill
  mid-file rolls the file back wholesale and a re-run re-reads it from byte
  zero — no partial state, and ``ON CONFLICT DO NOTHING`` covers the
  cross-partition duplicate case. A ``done`` row whose ``content_length``
  still matches the listing skips the file; a ``failed`` row never skips.

Out of slice 1: cites-link minting (slice 2, via the gf-citeslink writer),
the monthly partition-diff refresh and fleet wiring (slice 3). The spec's
``resource_slots`` hold for the whole run lands with the job wiring — the
slot vocabulary has no network token today (``gpu``/``podman``/``tts``/
``embedder`` only); slice 1 paces S3 through the ``openalex-snapshot``
outbound policy's auto-seeded ``host:`` limiter row instead.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from precis.identity import normalize_doi
from precis.utils.http import (
    FATAL_STATUSES,
    POLICIES,
    RETRY_STATUSES,
    with_backoff,
)
from precis.utils.safe_fetch import safe_get, safe_stream

if TYPE_CHECKING:
    import httpx

    from precis.store import Store

log = logging.getLogger(__name__)

#: Public bucket endpoint — anonymous GETs and listings, no signing.
BUCKET_URL = "https://openalex.s3.amazonaws.com"
WORKS_PREFIX = "data/jsonl/works"
MANIFEST_KEY = "data/jsonl/manifest.json"

#: ``POLICIES`` service name; the S3 bucket is not the OpenAlex API, so it
#: deliberately does NOT draw on the shared ``openalex`` limiter row.
SERVICE = "openalex-snapshot"

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

#: Kept edges per ``executemany`` inside the per-file transaction.
_EDGE_BATCH = 5_000


@dataclass(frozen=True)
class PartFile:
    """One ``part_NNNN.gz`` object from the bucket listing."""

    s3_key: str
    size: int


@dataclass(frozen=True)
class FileOutcome:
    """What happened to one part file in a run."""

    s3_key: str
    status: str  # 'done' | 'skipped' | 'failed'
    records: int = 0
    edges: int = 0
    error: str | None = None


@dataclass
class LoadSummary:
    """Aggregate counters for one :func:`run_load` pass."""

    outcomes: list[FileOutcome] = field(default_factory=list)
    held_openalex: int = 0
    held_doi: int = 0

    @property
    def files_done(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "done")

    @property
    def files_skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def files_failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")

    @property
    def records_seen(self) -> int:
        return sum(o.records for o in self.outcomes)

    @property
    def edges_kept(self) -> int:
        return sum(o.edges for o in self.outcomes)


# ── Held-paper index ─────────────────────────────────────────────────────

_HELD_SQL = (
    "SELECT id_kind, id_value, ref_id FROM ref_identifiers "
    "WHERE id_kind IN ('openalex', 'doi')"
)


@dataclass
class HeldIndex:
    """The held-corpus lookup: OpenAlex ``w…`` id → ref, DOI → ref.

    Keys are the ``ref_identifiers`` canonical form — lowercase (an
    OpenAlex id stores as ``w…`` while the API records carry ``W…``;
    gr514198). Both sides are lowered at index-build AND probe time, so a
    legacy mixed-case row still matches.
    """

    by_oa: dict[str, int]
    by_doi: dict[str, int]

    def ref_for(self, oa_id: str, doi: str | None) -> int | None:
        """Resolve a work to a held ``ref_id``: OpenAlex id first, then DOI."""
        ref = self.by_oa.get(oa_id.lower())
        if ref is None and doi:
            ref = self.by_doi.get(doi.lower())
        return ref


def load_held_index(store: Store) -> HeldIndex:
    """Read the held ``openalex``/``doi`` maps once for a whole run."""
    with store.pool.connection() as conn:
        rows = conn.execute(_HELD_SQL).fetchall()
    by_oa: dict[str, int] = {}
    by_doi: dict[str, int] = {}
    for kind, value, ref_id in rows:
        (by_oa if kind == "openalex" else by_doi)[str(value).lower()] = int(ref_id)
    return HeldIndex(by_oa, by_doi)


# ── Record → edge extraction (pure) ──────────────────────────────────────


def short_oa_id(url: Any) -> str:
    """``https://openalex.org/W123`` → ``W123``; anything else → ``""``."""
    if not url:
        return ""
    short = str(url).rstrip("/").rsplit("/", 1)[-1]
    return short if short.startswith("W") else ""


def work_edge(record: dict[str, Any]) -> tuple[str, str | None, list[str]]:
    """Pull ``(src_oa_id, src_doi, [dst_oa_id, …])`` out of one work record.

    ``src_doi`` is normalised (``https://doi.org/…`` stripped, lowercase) to
    match the ``ref_identifiers`` storage form; ``None`` when the record
    carries no DOI.
    """
    ids = record.get("ids")
    src = short_oa_id(record.get("id")) or (
        short_oa_id(ids.get("openalex")) if isinstance(ids, dict) else ""
    )
    doi_raw = record.get("doi")
    if not doi_raw and isinstance(ids, dict):
        doi_raw = ids.get("doi")
    doi = normalize_doi(doi_raw) if doi_raw else None
    dsts = [
        short for r in record.get("referenced_works") or [] if (short := short_oa_id(r))
    ]
    return src, doi, dsts


def iter_kept_edges(
    record: dict[str, Any], held: HeldIndex
) -> Iterator[tuple[str, str, int | None, int | None, str | None]]:
    """Yield ``(src_oa, dst_oa, src_ref, dst_ref, src_doi)`` staging tuples.

    An edge survives when the citing work OR the cited work is held —
    dst matching is OpenAlex id only (``referenced_works`` carries no DOIs).
    """
    src_oa, src_doi, dsts = work_edge(record)
    if not src_oa:
        return
    src_ref = held.ref_for(src_oa, src_doi)
    for dst_oa in dsts:
        dst_ref = held.by_oa.get(dst_oa.lower())
        if src_ref is None and dst_ref is None:
            continue
        yield (src_oa, dst_oa, src_ref, dst_ref, src_doi)


# ── Streaming decode ─────────────────────────────────────────────────────


class _ChunkReader(io.RawIOBase):
    """``read()``-capable adapter over a byte-chunk iterator.

    Lets :class:`gzip.GzipFile` decompress a streamed HTTP body without
    ever holding the whole object.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._it = iter(chunks)
        self._buf = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while not self._buf:
            try:
                self._buf += next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        del self._buf[:n]
        return n


def _iter_gz_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """Decompress gzip'd JSONL from a byte-chunk stream, one record per line."""
    raw = io.BufferedReader(_ChunkReader(chunks), buffer_size=1 << 20)
    gz = gzip.GzipFile(fileobj=raw)
    yield from io.TextIOWrapper(gz, encoding="utf-8")


@contextmanager
def _open_stream(client: httpx.Client, url: str) -> Iterator[Any]:
    """Backoff-guarded streaming GET; yields the final ``httpx.Response``.

    The politeness core retries *opening* the stream (429/5xx back off,
    ``Retry-After`` parks the host row); each retry gets a fresh
    :func:`safe_stream` context, and a refused attempt's response is closed
    inside the thunk so the connection never pools. A mid-stream failure
    is not retried here — it fails the file and the checkpoint leaves it
    for the next run.
    """
    keep: list[Any] = []

    def once() -> Any:
        cm = safe_stream(client, "GET", url)
        resp = cm.__enter__()
        if resp.status_code in RETRY_STATUSES or resp.status_code in FATAL_STATUSES:
            cm.__exit__(None, None, None)
        else:
            keep.append(cm)
        return resp

    resp = with_backoff(once, service=SERVICE, policy=POLICIES[SERVICE], url=url)
    try:
        yield resp
    finally:
        for cm in keep:
            cm.__exit__(None, None, None)


def stream_part_lines(client: httpx.Client, s3_key: str) -> Iterator[str]:
    """Yield decoded JSONL lines of one part object straight off the wire."""
    with _open_stream(client, f"{BUCKET_URL}/{s3_key}") as resp:
        resp.raise_for_status()
        yield from _iter_gz_lines(resp.iter_raw())


# ── Bucket listing + manifest ────────────────────────────────────────────


def list_partition_files(
    client: httpx.Client, partition: str, *, prefix: str = WORKS_PREFIX
) -> Iterator[PartFile]:
    """Yield every ``part_NNNN.gz`` under ``updated_date=<partition>/``.

    Anonymous ``list-type=2`` paginated listing (1,000 keys/page); each
    entry's ``Size`` doubles as the checkpoint's ``content_length``.
    """
    token: str | None = None
    while True:
        params: dict[str, str] = {
            "list-type": "2",
            "prefix": f"{prefix}/updated_date={partition}/",
        }
        if token:
            params["continuation-token"] = token
        url = f"{BUCKET_URL}/"

        def once(u: str = url, p: dict[str, str] = params) -> Any:
            return safe_get(client, u, params=p)

        resp = with_backoff(
            once,
            service=SERVICE,
            policy=POLICIES[SERVICE],
            url=url,
        )
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        for c in root.findall(f"{_S3_NS}Contents"):
            key = c.findtext(f"{_S3_NS}Key")
            if not key or not key.endswith(".gz"):
                continue
            yield PartFile(s3_key=key, size=int(c.findtext(f"{_S3_NS}Size") or 0))
        token = root.findtext(f"{_S3_NS}NextContinuationToken")
        if root.findtext(f"{_S3_NS}IsTruncated") != "true" or not token:
            return


def fetch_snapshot_date(client: httpx.Client) -> str:
    """The release date stamped in ``data/jsonl/manifest.json`` (``YYYY-MM-DD``)."""
    url = f"{BUCKET_URL}/{MANIFEST_KEY}"
    resp = with_backoff(
        lambda: safe_get(client, url),
        service=SERVICE,
        policy=POLICIES[SERVICE],
        url=url,
    )
    resp.raise_for_status()
    data = resp.json()
    date = data.get("date") if isinstance(data, dict) else None
    if not date:
        raise ValueError("manifest.json carries no 'date' field")
    return str(date)


def _bucket_client() -> Any:
    """The pinning-transport client for bucket calls (private factory).

    Read timeout is per socket read, not per file: 120 s covers a stalled
    S3 send while a healthy stream of a ~1 GB part never trips it. The
    connect bound keeps a dead edge from holding the file loop.
    """
    import httpx

    from precis.utils.http import http_client, polite_user_agent

    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=60.0)
    return http_client(timeout=timeout, user_agent=polite_user_agent())


def run_partition(
    store: Store,
    *,
    partition: str,
    snapshot_date: str | None = None,
    dry_run: bool = False,
    limit_files: int | None = None,
) -> LoadSummary:
    """The CLI's whole network path: list, load held ids, stream each part.

    ``snapshot_date`` defaults to the release ``date`` in the bucket
    manifest. Returns the per-file :class:`LoadSummary`; a file that fails
    mid-stream is recorded ``failed`` and the walk continues.
    """
    with _bucket_client() as client:
        snap = snapshot_date or fetch_snapshot_date(client)
        held = load_held_index(store)
        return run_load(
            store,
            list_partition_files(client, partition),
            snapshot_date=snap,
            held=held,
            lines_for=lambda part: stream_part_lines(client, part.s3_key),
            dry_run=dry_run,
            limit_files=limit_files,
        )


# ── Per-file processing ──────────────────────────────────────────────────

_INSERT_EDGE = (
    "INSERT INTO openalex_cite_edges "
    "(src_oa_id, dst_oa_id, src_ref_id, dst_ref_id, src_doi, snapshot_date) "
    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
)

_CHECKPOINT = (
    "INSERT INTO openalex_snapshot_files "
    "(snapshot_date, s3_key, content_length, records_seen, edges_kept, "
    " status, error, done_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (snapshot_date, s3_key) DO UPDATE SET "
    "  content_length = EXCLUDED.content_length, "
    "  records_seen = EXCLUDED.records_seen, "
    "  edges_kept = EXCLUDED.edges_kept, "
    "  status = EXCLUDED.status, "
    "  error = EXCLUDED.error, "
    "  done_at = now()"
)

_DONE = (
    "SELECT 1 FROM openalex_snapshot_files "
    "WHERE snapshot_date = %s AND s3_key = %s "
    "  AND status = 'done' AND content_length = %s"
)


def process_file(
    store: Store,
    part: PartFile,
    *,
    snapshot_date: str,
    held: HeldIndex,
    lines: Iterable[str],
    dry_run: bool = False,
) -> FileOutcome:
    """Consume one part file's JSONL lines; stage kept edges + checkpoint.

    One transaction per file: a mid-stream failure rolls the whole file
    back, then a second (short) transaction records ``failed`` with the
    partial counts. ``dry_run`` counts edges and writes nothing.
    """
    records = 0

    def gen_edges() -> Iterator[tuple[str, str, int | None, int | None, str | None]]:
        nonlocal records
        for line in lines:
            if not line.strip():
                continue
            records += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning(
                    "openalex-snapshot: malformed JSONL record %d skipped", records
                )
                continue
            if isinstance(record, dict):
                yield from iter_kept_edges(record, held)

    edges = 0
    if dry_run:
        for _edge in gen_edges():
            edges += 1
        return FileOutcome(part.s3_key, "done", records, edges)

    try:
        with store.pool.connection() as conn, conn.transaction():
            batch: list[tuple] = []
            for edge in gen_edges():
                batch.append((*edge, snapshot_date))
                if len(batch) >= _EDGE_BATCH:
                    conn.cursor().executemany(_INSERT_EDGE, batch)
                    edges += len(batch)
                    batch.clear()
            if batch:
                conn.cursor().executemany(_INSERT_EDGE, batch)
                edges += len(batch)
            conn.execute(
                _CHECKPOINT,
                (snapshot_date, part.s3_key, part.size, records, edges, "done", None),
            )
    except Exception as exc:
        # The file's transaction rolled back — record the failure in a
        # fresh, short one so the next run knows this file is unfinished.
        with store.pool.connection() as conn, conn.transaction():
            conn.execute(
                _CHECKPOINT,
                (
                    snapshot_date,
                    part.s3_key,
                    part.size,
                    records,
                    edges,
                    "failed",
                    f"{type(exc).__name__}: {exc}"[:2000],
                ),
            )
        return FileOutcome(part.s3_key, "failed", records, edges, str(exc))
    return FileOutcome(part.s3_key, "done", records, edges)


def run_load(
    store: Store,
    files: Iterable[PartFile],
    *,
    snapshot_date: str,
    held: HeldIndex,
    lines_for: Callable[[PartFile], Iterable[str]],
    dry_run: bool = False,
    limit_files: int | None = None,
) -> LoadSummary:
    """Walk ``files``, checkpoint-skipping, and process each unfinished one.

    ``lines_for(part)`` supplies the file's decoded JSONL line iterator
    (``stream_part_lines`` in production, a fixture in tests). ``limit_files``
    bounds how many files are *read* this run — skipped ``done`` files don't
    count. Dry-run ignores the checkpoint state entirely: it counts every
    requested file and writes nothing.
    """
    summary = LoadSummary(held_openalex=len(held.by_oa), held_doi=len(held.by_doi))
    read = 0
    for part in files:
        if limit_files is not None and read >= limit_files:
            break
        if not dry_run:
            with store.pool.connection() as conn:
                done = conn.execute(
                    _DONE, (snapshot_date, part.s3_key, part.size)
                ).fetchone()
            if done:
                summary.outcomes.append(FileOutcome(part.s3_key, "skipped"))
                continue
        read += 1
        outcome = process_file(
            store,
            part,
            snapshot_date=snapshot_date,
            held=held,
            lines=lines_for(part),
            dry_run=dry_run,
        )
        if outcome.status == "failed":
            log.warning(
                "openalex-snapshot: %s failed after %d records: %s",
                part.s3_key,
                outcome.records,
                outcome.error,
            )
        summary.outcomes.append(outcome)
    return summary


__all__ = [
    "BUCKET_URL",
    "MANIFEST_KEY",
    "SERVICE",
    "WORKS_PREFIX",
    "FileOutcome",
    "HeldIndex",
    "LoadSummary",
    "PartFile",
    "fetch_snapshot_date",
    "iter_kept_edges",
    "list_partition_files",
    "load_held_index",
    "process_file",
    "run_load",
    "run_partition",
    "short_oa_id",
    "stream_part_lines",
    "work_edge",
]
