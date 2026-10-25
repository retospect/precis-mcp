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
- **Stream first, then one short write tx per file.** A part's kept edges
  collect in memory while it streams (thousands of tuples against 400k
  records — well under the RSS bound): a busy node's pull of a ~1 GB gz
  can outlast prod ``agent_rw``'s
  ``idle_in_transaction_session_timeout = 300s``, so no transaction is
  open while the stream runs. One short transaction then writes the
  batched inserts and the ``openalex_snapshot_files`` ``done`` row
  together, so a kill mid-file rolls the file back wholesale and a re-run
  re-reads it from byte zero — no partial state, and ``ON CONFLICT DO
  NOTHING`` covers the cross-partition duplicate case. A ``done`` row
  whose ``content_length`` still matches the listing skips the file; a
  ``failed`` row never skips.

Slice 2 (``mint_staged_cites``) turns every staged row with BOTH ref ids
into a paper-level ``cites`` link through the shared writer
(:func:`precis.workers.bib_parse.ensure_bib_cites_links`, the gf-citeslink
path — no second ``links`` insert) and stamps ``minted_at`` in the same
commit, so a re-run's ``minted_at IS NULL`` scan finds nothing. Each call
first re-resolves NULL ref ids off ``ref_identifiers`` (a paper held after
its edges staged still mints — the insert's ``ON CONFLICT DO NOTHING``
never upgrades a NULL), and retired refs never resolve or mint.

Slice 3 (``refresh_snapshot`` / ``run_snapshot_refresh_pass``) is the
standing refresh: a ``--only openalex_snapshot`` worker lane throttled to
~30 days via ``app_state`` (``openalex_snapshot:last_run``) that, when the
bucket manifest's release ``date`` differs from
``openalex_snapshot:last_release``, re-plans the whole ``works/`` prefix —
any part file whose ``(s3_key, content_length)`` is checkpointed ``done``
under ANY snapshot is skipped, so only newer/moved/changed files stream —
then applies the cumulative ``deleted_ids.csv.gz`` deletion log (src side
only; minted links retire through the normal link path — its object is
itself checkpointed by ``content_length`` so a retry never re-streams the
~234 MB file) and mints the new staged edges. ``last_release`` advances
only on a clean, unlimited run — a failed file leaves it set so the next
tick re-plans (done checkpoints confine the retry to the failed files),
and a tick that *raises* backs off ~6 h via
``openalex_snapshot:last_error`` instead of re-pulling the listing every
idle interval. The spec's
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
from datetime import UTC, datetime
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

#: A retired ref never resolves (same ``retired_at IS NULL`` gate the
#: link writer applies to targets at mint time): a paper merged away
#: before the load must not stage or mint edges under its old id.
_HELD_SQL = (
    "SELECT ri.id_kind, ri.id_value, ri.ref_id FROM ref_identifiers ri "
    "JOIN refs r ON r.ref_id = ri.ref_id AND r.retired_at IS NULL "
    "WHERE ri.id_kind IN ('openalex', 'doi')"
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


def _list_objects(client: httpx.Client, prefix: str) -> Iterator[PartFile]:
    """Yield every ``.gz`` object under ``prefix`` (paginated listing).

    Anonymous ``list-type=2`` paginated listing (1,000 keys/page); each
    entry's ``Size`` doubles as the checkpoint's ``content_length``.
    """
    token: str | None = None
    while True:
        params: dict[str, str] = {"list-type": "2", "prefix": prefix}
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


def list_partition_files(
    client: httpx.Client, partition: str, *, prefix: str = WORKS_PREFIX
) -> Iterator[PartFile]:
    """Yield every ``part_NNNN.gz`` under ``updated_date=<partition>/``."""
    yield from _list_objects(client, f"{prefix}/updated_date={partition}/")


def _is_works_part(s3_key: str) -> bool:
    """``updated_date=YYYY-MM-DD/part_NNNN.gz`` — a loadable works part."""
    return "/updated_date=" in s3_key and s3_key.rsplit("/", 1)[-1].startswith("part_")


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

    The stream runs with NO transaction open — kept edges collect in a
    list first (a busy node's pull of a ~1 GB gz can outlast prod
    ``agent_rw``'s ``idle_in_transaction_session_timeout = 300s``, which
    would kill the session mid-file on every retry), then one short
    transaction writes the batched inserts plus the ``done`` checkpoint.
    A mid-stream failure leaves no partial rows; a second (short)
    transaction records ``failed`` with the partial counts. ``dry_run``
    counts edges and writes nothing.
    """
    records = 0
    kept: list[tuple[str, str, int | None, int | None, str | None, str]] = []

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

    if dry_run:
        edges = 0
        for _edge in gen_edges():
            edges += 1
        return FileOutcome(part.s3_key, "done", records, edges)

    try:
        for edge in gen_edges():
            kept.append((*edge, snapshot_date))
        with store.pool.connection() as conn, conn.transaction():
            cur = conn.cursor()
            for i in range(0, len(kept), _EDGE_BATCH):
                cur.executemany(_INSERT_EDGE, kept[i : i + _EDGE_BATCH])
            conn.execute(
                _CHECKPOINT,
                (
                    snapshot_date,
                    part.s3_key,
                    part.size,
                    records,
                    len(kept),
                    "done",
                    None,
                ),
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
                    len(kept),
                    "failed",
                    f"{type(exc).__name__}: {exc}"[:2000],
                ),
            )
        return FileOutcome(part.s3_key, "failed", records, len(kept), str(exc))
    return FileOutcome(part.s3_key, "done", records, len(kept))


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


# ── Link minting (slice 2) ───────────────────────────────────────────────

#: ``links.meta.source`` for edges this minter writes — distinct from
#: ``bib_parse``'s ``_LINK_SOURCE`` and ``oa_refs``' ``OA_LINK_SOURCE`` so
#: provenance stays attributable (and inbound_chase's source-filtered claim
#: query never picks these up, same reasoning as ``bib_parse._LINK_SOURCE``).
_MINT_SOURCE = "openalex_snapshot"

#: ``meta`` key carrying the staging edge's ``src>dst`` W-id pair —
#: ``ensure_bib_cites_links``' provenance slot (``bib_entry_id`` there).
_MINT_META_KEY = "openalex_edge"

#: Staging rows consumed per write transaction — bounds one commit's size,
#: same reasoning as ``backfill.bib_cites._DEFAULT_BATCH_REFS``.
_MINT_BATCH = 2_000

#: The unminted scan — both endpoints held, not yet consumed. The 0240
#: ``openalex_cite_edges_unminted`` partial index covers exactly this
#: shape. The ``refs`` join reports the citing ref's liveness: a ref
#: retired between staging and minting still stamps (the scan converges)
#: but writes no link — ``ensure_bib_cites_links`` applies the same gate
#: to the TARGET side.
_UNMINTED = (
    "SELECT e.src_ref_id, e.dst_ref_id, e.src_oa_id, e.dst_oa_id, "
    "       (r.retired_at IS NULL) AS src_live "
    "FROM openalex_cite_edges e "
    "JOIN refs r ON r.ref_id = e.src_ref_id "
    "WHERE e.src_ref_id IS NOT NULL AND e.dst_ref_id IS NOT NULL "
    "  AND e.minted_at IS NULL "
    "ORDER BY e.src_ref_id, e.dst_ref_id "
    "LIMIT %s"
)

_STAMP_MINTED = (
    "UPDATE openalex_cite_edges SET minted_at = now() "
    "WHERE src_oa_id = %s AND dst_oa_id = %s"
)

#: Live held ``openalex`` identifiers per re-resolve round — the resolve
#: drives from the SMALL side (held ids, keyset-paged through the
#: ``ref_identifiers`` PK ``(id_kind, id_value)``) so every pass probes
#: every held paper. Paging the staging side's NULL keys instead can
#: stall forever on a front window of ids that will never be held (R1).
_RESOLVE_BATCH = 5_000

_HELD_OA_BATCH = (
    "SELECT ri.ref_id, ri.id_value, upper(ri.id_value) AS oa "
    "FROM ref_identifiers ri "
    "JOIN refs r ON r.ref_id = ri.ref_id AND r.retired_at IS NULL "
    "WHERE ri.id_kind = 'openalex' AND ri.id_value > %s "
    "ORDER BY ri.id_value LIMIT %s"
)

#: Bare-column equality against the upper()'d stored id: staged
#: ``*_oa_id`` is ``W…`` and ``id_value`` stores canonical lowercase
#: ``w…``, so ``upper(ri.id_value)`` is the exact staged form and the
#: staging column stays index-usable — the PK's ``src_oa_id`` lead on the
#: citing side, ``openalex_cite_edges_dst_oa`` on the cited side.
_RESOLVE_SRC_OA = (
    "UPDATE openalex_cite_edges e SET src_ref_id = ids.ref_id "
    "FROM unnest(%s::bigint[], %s::text[]) AS ids(ref_id, oa) "
    "WHERE e.src_oa_id = ids.oa AND e.src_ref_id IS NULL"
)
_RESOLVE_DST_OA = (
    "UPDATE openalex_cite_edges e SET dst_ref_id = ids.ref_id "
    "FROM unnest(%s::bigint[], %s::text[]) AS ids(ref_id, oa) "
    "WHERE e.dst_oa_id = ids.oa AND e.dst_ref_id IS NULL"
)

#: DOI side, run ONCE per call (``HeldIndex.ref_for``'s precedence is
#: preserved: the OpenAlex pass fills first, DOI only what remains).
#: There is no ``src_doi`` index and RULING says no migration for it —
#: one hash-join statement over the NULL-src set, seconds on a monthly
#: few-million-row join. ``src_doi`` is ``normalize_doi``-canonical at
#: staging, the same lowercase form ``id_kind='doi'`` stores.
_RESOLVE_SRC_DOI = (
    "UPDATE openalex_cite_edges e SET src_ref_id = ri.ref_id "
    "FROM ref_identifiers ri "
    "JOIN refs r ON r.ref_id = ri.ref_id AND r.retired_at IS NULL "
    "WHERE e.src_ref_id IS NULL AND e.src_doi IS NOT NULL "
    "  AND ri.id_kind = 'doi' AND ri.id_value = e.src_doi"
)


@dataclass
class MintSummary:
    """Aggregate counters for one :func:`mint_staged_cites` call."""

    rows: int = 0  # staging rows consumed (minted_at stamped)
    links: int = 0  # add_link calls made (ensures, not necessarily inserts)
    papers: int = 0  # distinct citing refs processed
    resolved: int = 0  # NULL ref ids filled off live refs before minting


def resolve_staged_ref_ids(store: Store, *, limit: int | None = None) -> int:
    """Fill NULL ``src_ref_id``/``dst_ref_id`` on staged rows off live refs.

    The ``ON CONFLICT DO NOTHING`` insert never upgrades a NULL ref id, so
    a paper held AFTER its edges staged would leave them unmintable —
    every mint pass re-resolves first. The walk pages the live held
    ``openalex`` identifiers (``_RESOLVE_BATCH`` per transaction) and
    probes the staging table by index per id, so the whole held corpus is
    covered every call; a citing side that still has no OpenAlex match
    then gets one hash-join pass on DOI. ``limit`` caps staging rows
    filled per call — an early stop re-walks the identifiers next call.
    Returns the staging rows updated.
    """
    filled = 0
    after = ""
    while True:
        with store.pool.connection() as conn:
            ids = conn.execute(_HELD_OA_BATCH, (after, _RESOLVE_BATCH)).fetchall()
        if not ids:
            break
        after = str(ids[-1][1])
        ref_ids = [int(r[0]) for r in ids]
        oas = [str(r[2]) for r in ids]
        with store.pool.connection() as conn, conn.transaction():
            filled += conn.execute(_RESOLVE_SRC_OA, (ref_ids, oas)).rowcount
            filled += conn.execute(_RESOLVE_DST_OA, (ref_ids, oas)).rowcount
        if len(ids) < _RESOLVE_BATCH or (limit is not None and filled >= limit):
            break
    with store.pool.connection() as conn, conn.transaction():
        filled += conn.execute(_RESOLVE_SRC_DOI).rowcount
    return filled


def mint_staged_cites(store: Store, *, limit: int | None = None) -> MintSummary:
    """Mint paper-level ``cites`` links from staged both-held edges.

    First runs :func:`resolve_staged_ref_ids` so papers held AFTER their
    edges staged still mint — the insert's ``ON CONFLICT DO NOTHING``
    never upgrades a NULL ref id. Then selects up to ``limit`` unminted
    staging rows (``src_ref_id`` AND ``dst_ref_id`` set, ``minted_at IS
    NULL``), groups them by citing ref and ensures each paper's edges
    through :func:`precis.workers.bib_parse.ensure_bib_cites_links` — the
    gf-citeslink writer, idempotent on the links unique tuple — in one
    transaction per ``_MINT_BATCH`` rows, stamping ``minted_at`` on every
    selected row in the same commit.

    ``minted_at`` marks *consumption*, not necessarily a written link: a
    self-cite (same held ref under two OpenAlex ids), a citing ref retired
    since staging, or a retired target is skipped by the writer and
    stamped anyway, so the unminted scan converges instead of re-reading
    dead rows every run. ``limit`` bounds rows per call; a run stopped
    early re-selects the unstamped remainder next invocation.
    """
    from precis.workers.bib_parse import ensure_bib_cites_links

    summary = MintSummary(resolved=resolve_staged_ref_ids(store))
    while limit is None or summary.rows < limit:
        want = _MINT_BATCH if limit is None else min(_MINT_BATCH, limit - summary.rows)
        with store.pool.connection() as conn:
            rows = conn.execute(_UNMINTED, (want,)).fetchall()
            conn.commit()
        if not rows:
            break
        by_src: dict[int, list[tuple[str, int, str]]] = {}
        live_src: set[int] = set()
        for src_ref, dst_ref, src_oa, dst_oa, src_live in rows:
            by_src.setdefault(int(src_ref), []).append(
                (str(src_oa), int(dst_ref), str(dst_oa))
            )
            if src_live:
                live_src.add(int(src_ref))
        summary.papers += len(by_src)
        with store.pool.connection() as conn, conn.transaction():
            for src_ref in sorted(by_src):
                if src_ref in live_src:
                    pairs = [
                        (f"{src_oa}>{dst_oa}", dst_ref)
                        for src_oa, dst_ref, dst_oa in by_src[src_ref]
                    ]
                    summary.links += ensure_bib_cites_links(
                        store,
                        src_ref,
                        pairs,
                        conn=conn,
                        source=_MINT_SOURCE,
                        meta_key=_MINT_META_KEY,
                    )
                conn.cursor().executemany(
                    _STAMP_MINTED,
                    [(src_oa, dst_oa) for src_oa, _r, dst_oa in by_src[src_ref]],
                )
        summary.rows += len(rows)
    return summary


# ── Refresh: new releases, changed folders, deletions (slice 3) ─────────

#: The cumulative works deletion log — one ``work_id,deleted_date`` CSV
#: (gzipped) per release; IDs are never reused.
DELETED_KEY = f"{WORKS_PREFIX}/deleted_ids.csv.gz"

#: ``app_state`` keys: the release the staging tree was last refreshed to
#: (the manifest ``date``) and the last time the timer pass evaluated a
#: refresh (the ``_throttle.due`` marker — even an unchanged manifest
#: stamps it, so the lane stays a monthly tick, not a per-cycle fetch).
_LAST_RELEASE_KEY = "openalex_snapshot:last_release"
_LAST_RUN_KEY = "openalex_snapshot:last_run"

#: Env var + default (~30 days) for the pass cadence — see
#: :func:`precis.workers._throttle.due`. Public data ships quarterly; a
#: monthly tick is a cheap manifest fetch in two runs out of three.
_REFRESH_ENV_VAR = "PRECIS_OPENALEX_SNAPSHOT_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 24.0 * 30

#: Done checkpoints matched across EVERY snapshot — a file already read
#: unchanged under an older release never re-streams.
_DONE_ANY_SNAPSHOT = (
    "SELECT s3_key, content_length FROM openalex_snapshot_files WHERE status = 'done'"
)

#: One ``DELETE … WHERE src_oa_id = ANY`` per batch of parsed ids —
#: spec: drop staging rows whose *citing* work was deleted (minted links
#: retire through the normal link path, not deleted here).
_DELETE_SRC_BATCH = "DELETE FROM openalex_cite_edges WHERE src_oa_id = ANY(%s)"
_DELETE_BATCH = 5_000

#: ``app_state`` keys + backoff for a tick that RAISES or finishes with
#: failed files: ``last_error`` holds the retry for
#: ``_ERROR_BACKOFF_HOURS`` instead of re-pulling the works listing and
#: the ~234 MB deletion log (or the failed ~1 GB parts) every idle
#: interval while a deterministic fault repeats. ``failed_ticks`` counts
#: consecutive failed-file ticks; at ``_FAILED_TICK_GIVE_UP`` the lane
#: stamps ``last_run`` and parks the files until the next cadence —
#: a ~1 GB part that fails the same way every attempt costs ~23 GB/day
#: otherwise (R2).
_LAST_ERROR_KEY = "openalex_snapshot:last_error"
_FAILED_TICKS_KEY = "openalex_snapshot:failed_ticks"
_RETRY_ENV_VAR = "PRECIS_OPENALEX_SNAPSHOT_RETRY_HOURS"
_ERROR_BACKOFF_HOURS = 6.0
_FAILED_TICK_GIVE_UP = 4


@dataclass
class RefreshSummary:
    """Aggregate counters for one :func:`refresh_snapshot` call."""

    snapshot_date: str = ""
    planned: int = 0  # part files selected for streaming
    planned_bytes: int = 0  # gzip bytes the plan would transfer
    load: LoadSummary = field(default_factory=LoadSummary)
    deleted: int = 0  # staging rows dropped by the deletion log
    minted: MintSummary = field(default_factory=MintSummary)


@dataclass(frozen=True)
class DeletionSummary:
    """What one ``deleted_ids.csv.gz`` apply did."""

    ids: int = 0  # work_id lines parsed
    deleted: int = 0  # staging rows removed


def _done_keys(store: Store) -> set[tuple[str, int]]:
    """``(s3_key, content_length)`` pairs checkpointed ``done`` — every snapshot."""
    with store.pool.connection() as conn:
        return {
            (str(r[0]), int(r[1])) for r in conn.execute(_DONE_ANY_SNAPSHOT).fetchall()
        }


def plan_refresh(store: Store, files: Iterable[PartFile]) -> list[PartFile]:
    """Select the listed part files that still need reading.

    A file is skipped when a ``done`` checkpoint with the same
    ``(s3_key, content_length)`` exists under ANY ``snapshot_date`` — that
    is what makes a whole-tree listing cheap: unchanged files keep their
    key and size across releases, so the plan reduces to the folders the
    new release moved or rewrote (plus files added under older folders).
    """
    done = _done_keys(store)
    return [p for p in files if (p.s3_key, p.size) not in done]


def apply_deletion_log(store: Store, lines: Iterable[str]) -> DeletionSummary:
    """Drop staging rows whose *citing* work the release deleted.

    ``lines`` is the decoded ``deleted_ids.csv.gz`` content (the same
    ``_iter_gz_lines`` decode a part file gets — :func:`stream_part_lines`
    in production). Only column 0 (``work_id``) is read, through
    :func:`short_oa_id` so both the URL and the short form resolve; the
    header line resolves to ``""`` and is skipped. One transaction, batched
    ``ANY`` deletes; returns the ids parsed and rows removed.
    """
    ids = 0
    deleted = 0
    with store.pool.connection() as conn, conn.transaction():
        batch: list[str] = []
        for line in lines:
            oa = short_oa_id(line.split(",", 1)[0].strip())
            if not oa:
                continue
            ids += 1
            batch.append(oa)
            if len(batch) >= _DELETE_BATCH:
                deleted += conn.execute(_DELETE_SRC_BATCH, (batch,)).rowcount
                batch.clear()
        if batch:
            deleted += conn.execute(_DELETE_SRC_BATCH, (batch,)).rowcount
    return DeletionSummary(ids, deleted)


def refresh_snapshot(
    store: Store,
    *,
    fetch_date: Callable[[], str],
    list_files: Callable[[], Iterable[PartFile]],
    lines_for: Callable[[PartFile], Iterable[str]],
    deletion_lines: Callable[[], Iterable[str]],
    dry_run: bool = False,
    limit_files: int | None = None,
) -> RefreshSummary | None:
    """One release-diff refresh: plan, stream the new files, delete, mint.

    ``fetch_date()`` returns the bucket manifest's release ``date``; when it
    equals ``app_state[openalex_snapshot:last_release]`` the call is a no-op
    returning ``None`` (the manifest fetch is the whole cost — the whole
    point of the monthly timer). ``list_files()`` yields the whole
    ``works/`` listing — every ``.gz`` object, e.g.
    ``_list_objects(client, f"{WORKS_PREFIX}/")``: the ``part_`` files plan
    through :func:`plan_refresh` and the ``deleted_ids.csv.gz`` object's
    size doubles as its checkpoint (the log is cumulative, so a ``done``
    row with a matching ``content_length`` under ANY snapshot means
    nothing new was deleted — the retry path never re-streams it).
    The selected files load under the new ``snapshot_date``, the
    deletion log drops deleted citer rows, the new both-held edges mint,
    and ``last_release`` advances — but only on a clean, unlimited run:
    a failed file leaves it set, so the next evaluation re-plans and the
    done checkpoints confine the retry to the failed files.

    ``dry_run`` plans and reports ``planned``/``planned_bytes`` but
    streams and writes nothing. ``limit_files`` bounds files read — the
    first dev pass's escape hatch.
    """
    snap = fetch_date()
    if snap == store.get_setting(_LAST_RELEASE_KEY):
        return None
    listed = list(list_files())
    del_obj = next((p for p in listed if p.s3_key == DELETED_KEY), None)
    plan = plan_refresh(store, (p for p in listed if _is_works_part(p.s3_key)))
    summary = RefreshSummary(
        snapshot_date=snap,
        planned=len(plan),
        planned_bytes=sum(p.size for p in plan),
    )
    if dry_run:
        return summary
    summary.load = run_load(
        store,
        plan,
        snapshot_date=snap,
        held=load_held_index(store),
        lines_for=lines_for,
        limit_files=limit_files,
    )
    if del_obj is not None and (DELETED_KEY, del_obj.size) in _done_keys(store):
        pass  # unchanged cumulative log — nothing new was deleted
    else:
        delsum = apply_deletion_log(store, deletion_lines())
        summary.deleted = delsum.deleted
        if del_obj is not None:
            with store.pool.connection() as conn, conn.transaction():
                conn.execute(
                    _CHECKPOINT,
                    (
                        snap,
                        DELETED_KEY,
                        del_obj.size,
                        delsum.ids,
                        delsum.deleted,
                        "done",
                        None,
                    ),
                )
    summary.minted = mint_staged_cites(store)
    if summary.load.files_failed == 0 and limit_files is None:
        store.set_setting(_LAST_RELEASE_KEY, snap)
    return summary


def run_refresh(
    store: Store, *, dry_run: bool = False, limit_files: int | None = None
) -> RefreshSummary | None:
    """The CLI/worker refresh path against the live bucket.

    Builds the pinning-transport client and wires the production seams of
    :func:`refresh_snapshot` — the whole ``works/`` listing, part streams,
    and the ``deleted_ids.csv.gz`` stream.
    """
    with _bucket_client() as client:
        return refresh_snapshot(
            store,
            fetch_date=lambda: fetch_snapshot_date(client),
            list_files=lambda: _list_objects(client, f"{WORKS_PREFIX}/"),
            lines_for=lambda part: stream_part_lines(client, part.s3_key),
            deletion_lines=lambda: stream_part_lines(client, DELETED_KEY),
            dry_run=dry_run,
            limit_files=limit_files,
        )


def run_snapshot_refresh_pass(
    store: Store,
    *,
    refresh: Callable[[], RefreshSummary | None] | None = None,
) -> dict[str, Any]:
    """The ``--only openalex_snapshot`` lane tick — ~30-day cadence.

    ``_throttle.due`` gates the pass on ``openalex_snapshot:last_run``; due
    means "evaluate a refresh now" (fetch the manifest — itself the cheap
    part — and let :func:`refresh_snapshot` decide whether the release
    actually moved). The stamp is written when the tick completes CLEAN —
    an unchanged manifest or a failure-free run. A run with failed files
    instead stamps ``openalex_snapshot:last_error`` (~6 h backoff, same as
    a raise — a deterministically-failed ~1 GB part must not re-stream
    hourly) and counts ``openalex_snapshot:failed_ticks``; after
    ``_FAILED_TICK_GIVE_UP`` consecutive failed ticks it stamps
    ``last_run`` and logs the failed keys, parking the files until the
    next cadence. A clean tick clears both markers. ``refresh`` is the
    test seam — production builds the networked call.
    """
    from precis.workers import _throttle

    if not _throttle.due(
        store, _LAST_RUN_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS
    ) or not _throttle.due(
        store, _LAST_ERROR_KEY, _RETRY_ENV_VAR, _ERROR_BACKOFF_HOURS
    ):
        return {"claimed": 0, "ok": 0, "failed": 0}
    if refresh is None:

        def refresh() -> RefreshSummary | None:
            return run_refresh(store)

    try:
        summary = refresh()
    except Exception:
        store.set_setting(_LAST_ERROR_KEY, datetime.now(UTC).isoformat())
        raise
    now = datetime.now(UTC).isoformat()
    store.set_setting(_LAST_ERROR_KEY, "")
    if summary is None:
        store.set_setting(_LAST_RUN_KEY, now)
        store.set_setting(_FAILED_TICKS_KEY, "0")
        return {"claimed": 0, "ok": 0, "failed": 0, "release": "unchanged"}
    if summary.load.files_failed == 0:
        store.set_setting(_LAST_RUN_KEY, now)
        store.set_setting(_FAILED_TICKS_KEY, "0")
    else:
        ticks = int(store.get_setting(_FAILED_TICKS_KEY) or "0") + 1
        if ticks < _FAILED_TICK_GIVE_UP:
            store.set_setting(_FAILED_TICKS_KEY, str(ticks))
            store.set_setting(_LAST_ERROR_KEY, now)
        else:
            keys = ", ".join(
                o.s3_key for o in summary.load.outcomes if o.status == "failed"
            )
            log.warning(
                "openalex-snapshot: %d consecutive ticks with failed files — "
                "parking until the next cadence; failed: %s",
                ticks,
                keys,
            )
            store.set_setting(_FAILED_TICKS_KEY, "0")
            store.set_setting(_LAST_RUN_KEY, now)
    return {
        "claimed": summary.planned,
        "ok": summary.load.files_done,
        "failed": summary.load.files_failed,
        "release": summary.snapshot_date,
        "records": summary.load.records_seen,
        "edges": summary.load.edges_kept,
        "deleted": summary.deleted,
        "mint_rows": summary.minted.rows,
        "mint_links": summary.minted.links,
        "resolved": summary.minted.resolved,
    }


__all__ = [
    "BUCKET_URL",
    "DELETED_KEY",
    "MANIFEST_KEY",
    "SERVICE",
    "WORKS_PREFIX",
    "DeletionSummary",
    "FileOutcome",
    "HeldIndex",
    "LoadSummary",
    "MintSummary",
    "PartFile",
    "RefreshSummary",
    "apply_deletion_log",
    "fetch_snapshot_date",
    "iter_kept_edges",
    "list_partition_files",
    "load_held_index",
    "mint_staged_cites",
    "plan_refresh",
    "process_file",
    "refresh_snapshot",
    "resolve_staged_ref_ids",
    "run_load",
    "run_partition",
    "run_refresh",
    "run_snapshot_refresh_pass",
    "short_oa_id",
    "stream_part_lines",
    "work_edge",
]
