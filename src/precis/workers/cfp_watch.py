"""Weekly re-check of ``WATCH:*``-tagged ``cfp`` refs (gr462088, Reto
2026-10-11: re-check weekly or when ``/whatneedsdoing`` runs).

A conference call is a web page that quietly moves its deadline. For each
live ``cfp`` ref carrying a ``WATCH:<interval>`` tag whose ``meta.source_url``
is set, this pass re-fetches the page via :func:`precis.utils.safe_fetch.
safe_get`, hashes the visible text, pulls out any dates on
deadline-flavoured lines, and compares both against ``meta.cfp_watch``
(the last snapshot). The first sighting only records a baseline. A change
patches the new snapshot into meta (keeping a short ``history``) and raises
a ``cfp_watch`` alert so it shows in the attention queue.

Bounded: at most :data:`MAX_FETCHES_PER_RUN` fetches per run, oldest
check first, and a ref checked within :data:`MIN_RECHECK_S` is skipped so
repeated ``/whatneedsdoing`` runs don't hammer the organiser's site.
"""

from __future__ import annotations

import hashlib
import html as _html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.alerts import raise_alert
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

META_KEY = "cfp_watch"
ALERT_SOURCE = "cfp_watch"
MAX_FETCHES_PER_RUN = 25
MIN_RECHECK_S = 20 * 3600
_HISTORY_KEEP = 5
_INTERVALS = ("hourly", "daily", "weekly", "monthly")

_TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    "|january|february|march|april|june|july|august|september|october|november|december"
)
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\w*\.?,?\s+\d{{4}}\b"
    rf"|\b(?:{_MONTHS})\w*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",
    re.I,
)
_DEADLINE_LINE_RE = re.compile(
    r"deadline|due|submission|submit|closes?|opens?|abstract", re.I
)

Fetch = Callable[[str], str]


def _default_fetch(url: str) -> str:
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=30.0) as client:
        resp = safe_get(client, url)
    resp.raise_for_status()
    return resp.text


def page_text(raw: str) -> str:
    """Visible text of ``raw`` (tags/scripts dropped, whitespace folded)."""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", raw))).strip()


def snapshot(raw: str) -> dict[str, Any]:
    """``{hash, dates}`` for a fetched page: sha256 of the visible text and
    the sorted dates found on deadline/opens/submission lines."""
    text = page_text(raw)
    lines = _TAG_RE.sub("\n", _html.unescape(raw)).splitlines()
    dates: set[str] = set()
    for line in lines:
        line = _WS_RE.sub(" ", line)
        if _DEADLINE_LINE_RE.search(line):
            dates.update(m.group(0).lower() for m in _DATE_RE.finditer(line))
    return {
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "dates": sorted(dates),
    }


def _watched_cfps(store: Store) -> list[Any]:
    seen: dict[int, Any] = {}
    for interval in _INTERVALS:
        for ref in store.list_refs(kind="cfp", tags=[f"WATCH:{interval}"], limit=500):
            seen.setdefault(ref.id, ref)
    return list(seen.values())


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def run_cfp_watch_pass(
    store: Store,
    *,
    fetch: Fetch | None = None,
    max_fetches: int = MAX_FETCHES_PER_RUN,
    min_recheck_s: int = MIN_RECHECK_S,
    now: datetime | None = None,
) -> BatchResult:
    """One re-check run. ``claimed`` = refs fetched, ``ok`` = fetched without
    error (changed or not), ``failed`` = fetch/compare errors."""
    do_fetch = fetch or _default_fetch
    now = now or datetime.now(UTC)
    candidates: list[tuple[datetime, Any, str]] = []
    for ref in _watched_cfps(store):
        meta = ref.meta or {}
        url = meta.get("source_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            log.info("cfp_watch: cf%d tagged WATCH but has no meta.source_url", ref.id)
            continue
        prev = meta.get(META_KEY) or {}
        checked = _parse_ts(prev.get("checked_at")) if prev else None
        if checked and (now - checked).total_seconds() < min_recheck_s:
            continue
        candidates.append((checked or datetime.min.replace(tzinfo=UTC), ref, url))
    candidates.sort(key=lambda c: c[0])

    claimed = ok = failed = 0
    for _, ref, url in candidates[:max_fetches]:
        claimed += 1
        try:
            snap = snapshot(do_fetch(url))
            prev = (ref.meta or {}).get(META_KEY) or {}
            record: dict[str, Any] = {
                "hash": snap["hash"],
                "dates": snap["dates"],
                "checked_at": now.isoformat(),
                "history": prev.get("history", []),
            }
            changed = bool(prev) and (
                prev.get("hash") != snap["hash"] or prev.get("dates") != snap["dates"]
            )
            if changed:
                record["changed_at"] = now.isoformat()
                record["history"] = [
                    {
                        "at": now.isoformat(),
                        "old_dates": prev.get("dates", []),
                        "new_dates": snap["dates"],
                    },
                    *record["history"],
                ][:_HISTORY_KEEP]
            elif prev.get("changed_at"):
                record["changed_at"] = prev["changed_at"]
            store.update_ref(ref.id, meta_patch={META_KEY: record})
            if changed:
                dates_note = (
                    f"deadline dates {prev.get('dates', [])} -> {snap['dates']}"
                    if prev.get("dates") != snap["dates"]
                    else "page text changed (no date change detected)"
                )
                raise_alert(
                    store,
                    source=ALERT_SOURCE,
                    fingerprint=f"changed:{ref.id}",
                    title=f"[cfp_watch] cf{ref.id} call page changed",
                    detail=f"{ref.title or ref.id}: {dates_note}\n{url}\n\n"
                    "Re-read the call, update the proposal plan/todos, then "
                    "resolve this alert.",
                    subject_ref_id=ref.id,
                )
            ok += 1
        except Exception:
            log.exception("cfp_watch: re-check failed for cf%d (%s)", ref.id, url)
            failed += 1
    return BatchResult(handler="cfp_watch", claimed=claimed, ok=ok, failed=failed)


__all__ = ["META_KEY", "run_cfp_watch_pass", "snapshot"]
