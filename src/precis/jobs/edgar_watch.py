"""Run due EDGAR searches and project notable filings into the briefing.

The downloaded-byte ledger survives partial passes. A watch advances its
cadence only when its search and all new hits have completed. News uses
the filing accession and findings use the finding handler's deterministic
identity, so overlapping watches and retries converge on the same refs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote

from precis.dispatch import Hub
from precis.handlers import _edgar_watch_db as watch_db
from precis.handlers._edgar_client import ARCHIVE_BASE, EdgarError
from precis.handlers._edgar_diff import apply_diff_tags, compute_diff
from precis.handlers._edgar_ingest import ingest_filing
from precis.handlers._edgar_notable import is_notable
from precis.handlers._edgar_parse import EdgarHit, parse_fts_response
from precis.handlers.finding import FindingHandler
from precis.handlers.news import article_blocks
from precis.store import Tag
from precis.store.advisory import try_xact_advisory_lock
from precis.store.revision_context import revision_context

if TYPE_CHECKING:
    from pathlib import Path

    from precis.handlers._edgar_client import EdgarClientProto
    from precis.store import Ref, Store

log = logging.getLogger(__name__)

DEFAULT_FAIR_USE_LIMIT_GB = 3.0
_GIB = 1024**3
_FTS_RESULT_WINDOW = 10_000


@dataclass
class WatchResult:
    name: str
    new_accessions: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    results: list[WatchResult] = field(default_factory=list)
    paused: bool = False
    busy: bool = False
    rolling_bytes: int = 0


def fair_use_limit_gb(override: float | None) -> float:
    value = (
        override
        if override is not None
        else float(
            os.getenv("PRECIS_EDGAR_FAIR_USE_LIMIT_GB", str(DEFAULT_FAIR_USE_LIMIT_GB))
        )
    )
    if not math.isfinite(value) or value <= 0:
        raise ValueError("EDGAR fair-use limit must be a positive finite number")
    return value


def _rolling_bytes(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT coalesce(sum(bytes), 0)
            FROM edgar_watch_downloads
            WHERE downloaded_at >= clock_timestamp() - interval '7 days'
            """
        ).fetchone()
    assert row is not None
    return int(row[0])


@dataclass
class _Budget:
    store: Store
    summary: RunSummary
    limit: float
    dry_run: bool

    def account(self, size: int) -> None:
        if not self.dry_run:
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT INTO edgar_watch_downloads (bytes) VALUES (%s)", (size,)
                )
        self.summary.rolling_bytes += size

    def exhausted(self) -> bool:
        if self.summary.rolling_bytes < self.limit * _GIB:
            return False
        if not self.summary.paused:
            self.summary.paused = True
            log.warning(
                "EDGAR watches paused: rolling seven-day usage %.2f GiB reached %.2f GiB",
                self.summary.rolling_bytes / _GIB,
                self.limit,
            )
        return True


def _search(
    watch: watch_db.EdgarWatch, client: EdgarClientProto, budget: _Budget
) -> dict[str, EdgarHit]:
    found: dict[str, EdgarHit] = {}
    seen = set(watch.last_seen_accessions)
    offset = 0
    previous_ids: list[str] | None = None
    while offset < _FTS_RESULT_WINDOW and not budget.exhausted():
        response = client.search(watch.params, from_=offset)
        budget.account(response.bytes_out)
        envelope = json.loads(response.json)
        rows = envelope["hits"]["hits"]
        if not isinstance(rows, list):
            raise EdgarError("EDGAR FTS returned an invalid hit list")
        ids = [str(row.get("_id")) for row in rows if isinstance(row, dict)]
        if ids and ids == previous_ids:
            raise EdgarError("EDGAR FTS repeated a page")
        previous_ids = ids
        hits, total = parse_fts_response(response.json)
        for hit in hits:
            if hit.accession not in seen:
                found.setdefault(hit.accession, hit)
        offset += len(rows)
        if not rows or offset >= total:
            if total <= _FTS_RESULT_WINDOW:
                return found
            break
    if not budget.exhausted():
        raise EdgarError("EDGAR FTS exceeded its result window; narrow the watch")
    return found


def _mint_news(store: Store, ref: Ref, accession: str) -> None:
    form = str(ref.meta.get("form") or "")
    filed = str(ref.meta.get("filed_date") or "")
    if not filed or not is_notable(form, ref.meta.get("items") or []):
        return
    slug = f"edgar-{accession}"
    with store.atomic() as conn:
        news = store.get_ref(kind="news", id=slug, conn=conn)
        if news is None:
            cik = str(int(ref.meta["cik"]))
            path = quote(str(ref.meta["primary_doc"]), safe="")
            url = f"{ARCHIVE_BASE}/{cik}/{accession.replace('-', '')}/{path}"
            news, _ = store.put_cache_entry(
                kind="news",
                slug=slug,
                title=f"{ref.title} — filed {filed}",
                body_blocks=article_blocks(
                    f"{ref.title} was filed on {filed}.", embedder=None
                ),
                provider="news",
                request_hash=hashlib.sha256(slug.encode()).hexdigest(),
                ttl_seconds=None,
                ref_meta={"source": "edgar", "url": url, "edgar_ref_id": ref.id},
            )
        for tag in (
            "category:news",
            "source:edgar",
            f"form:{form}",
            f"published:{filed}",
        ):
            store.add_tag(news.id, Tag.parse(tag), set_by="system", conn=conn)
        store.add_link(
            src_ref_id=news.id,
            dst_ref_id=ref.id,
            relation="derived-from",
            set_by="system",
            conn=conn,
        )


def _mint_findings(store: Store, handler: FindingHandler, ref: Ref) -> None:
    diff = compute_diff(store, ref)
    if diff is None:
        return
    apply_diff_tags(store, ref, diff)
    for delta in diff.deltas:
        section = " ".join(delta.section_path) or delta.canonical_id
        title = f"{ref.meta['company']} {diff.form}: {section} was {delta.status}"
        body = (
            f"Section {section} was {delta.status} in SEC filing {diff.current_slug} "
            f"compared with {diff.prior_slug} "
            f"({len(delta.added_paras)} added, {len(delta.removed_paras)} removed)."
        )
        with revision_context(trust_origin="generated", trust_actor_type="worker"):
            handler.put(
                title=title,
                body=body,
                scope={"section": delta.canonical_id, "prior": diff.prior_slug},
                meta={"edgar_section_delta": True},
                cited_in=f"edgar:{diff.current_slug}",
                link=f"edgar:{diff.prior_slug}",
                rel="derived-from",
                dedup=False,
            )


def run_one_pass(
    *,
    store: Store,
    client: EdgarClientProto,
    raw_root: Path,
    only_name: str | None = None,
    dry_run: bool = False,
    limit_gb: float | None = None,
) -> RunSummary:
    """Run one pass. Serialize writers across CLI and scheduled invocations."""
    summary = RunSummary()
    limit = fair_use_limit_gb(limit_gb)
    if dry_run:
        _run(store, client, raw_root, only_name, _Budget(store, summary, limit, True))
        return summary

    if store.dsn is None:
        raise ValueError("EDGAR watch passes require a Store connected with a DSN")
    with try_xact_advisory_lock(
        store.dsn, 0x656467617277, application_name="precis edgar watch pass"
    ) as held:
        if not held:
            summary.busy = True
            return summary
        _run(store, client, raw_root, only_name, _Budget(store, summary, limit, False))
    return summary


def _run(
    store: Store,
    client: EdgarClientProto,
    raw_root: Path,
    only_name: str | None,
    budget: _Budget,
) -> None:
    budget.summary.rolling_bytes = _rolling_bytes(store)
    if not budget.dry_run:
        with store.tx() as conn:
            conn.execute(
                "DELETE FROM edgar_watch_downloads "
                "WHERE downloaded_at < clock_timestamp() - interval '7 days'"
            )
    # Global fair-use pre-check: even when nothing is due, an exhausted
    # budget must pause the pass (and the CLI must report it).
    if budget.exhausted():
        return
    watches = (
        [watch_db.get_by_name(store, only_name)]
        if only_name
        else watch_db.list_due(store)
    )
    handler = FindingHandler(hub=Hub(store=store))
    for watch in watches:
        if budget.exhausted():
            break
        result = WatchResult(name=watch.name)
        budget.summary.results.append(result)
        try:
            hits = _search(watch, client, budget)
        except (EdgarError, ValueError, KeyError, TypeError) as exc:
            result.errors.append(str(exc))
            log.warning("EDGAR search for %s failed: %s", watch.name, exc)
            continue
        ordered = sorted(
            hits.values(), key=lambda hit: (hit.filed_date or "", hit.accession)
        )
        result.new_accessions = [hit.accession for hit in ordered]
        if budget.dry_run:
            continue
        for hit in ordered:
            if budget.exhausted():
                break
            try:
                filing = ingest_filing(
                    hit.accession,
                    store=store,
                    client=client,
                    embedder=None,
                    raw_root=raw_root,
                    filer_cik=hit.filer_cik,
                    account_download=budget.account,
                )
                ref = store.get_ref(kind="edgar", id=filing.ref_id)
                assert ref is not None
                _mint_news(store, ref, filing.slug)
                _mint_findings(store, handler, ref)
            except Exception as exc:
                log.exception("EDGAR watch %s failed on %s", watch.name, hit.accession)
                result.errors.append(f"{hit.accession}: {exc}")
                continue
            result.completed.append(hit.accession)
        finished = not budget.exhausted() and len(result.completed) == len(ordered)
        watch_db.record_pass(store, watch.id, result.completed, finished=finished)
