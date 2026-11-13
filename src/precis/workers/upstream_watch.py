"""upstream_watch — daily watch of the upstream issues and PRs we filed
(gr543879).

We file issues on projects we depend on (the first batch:
speedyk-005/yasbd-lib #409-#415, on which td543849 waits) and nothing
told us when they moved — a maintainer's question, a wontfix, or a fix
shipped in a release surfaced only through GitHub notifications or
someone re-reading the waiting todo.

One pass (fired from the daily ``upstream_watch`` scheduler cadence —
the lease is the fleet-singleton throttle, same as ``citer_watch`` /
``health_digest``):

1. **Find**: ``GET /search/issues?q=author:<login> -user:<login>
   updated:>=<last poll>`` — auto-registration. Everything our GitHub
   account (``upstream_watch.author`` setting, default ``retospect``)
   authored on repos it does not own and that moved since the last
   check; ``-user:<login>`` is the own-repo exclusion, re-checked
   client-side. Issues and PRs both come back (a ``pull_request`` key
   marks the latter). No hand-maintained list — filing the issue IS the
   registration.
2. **Classify**: each moved item is diffed against its stored snapshot
   (``upstream_watch:seen`` app_state map: state + comment count) and
   its issue timeline is pulled to name what moved — a state change
   (closed / reopened / merged), comments by someone else (a ``?`` in
   the newest reads as a maintainer question), cross-referenced PRs —
   plus repo releases published since the cursor whose notes name the
   item. Movement that is only our own comments does not surface.
3. **Surface**: the live (non-terminal) todos tagged
   ``upstream:<owner>/<repo>#<n>`` are the work that waits on the item —
   the pass raises one ``upstream_watch`` alert per moved item, subject
   = the waiting todo, detail = the movement + the suggested response
   (update the package pin, or draft an answer to the maintainer). With
   no waiting todo it mints a tracker ``kind='todo'`` first (tagged
   ``upstream:<key>`` + ``upstream-watch``, so later moves flag it), and
   a maintainer question tags the tracker ``waiting-for:reto`` —
   nothing is ever posted upstream without Reto.
4. **Digest**: one ``kind='memory'`` note (the citer_watch shape)
   listing what moved, written only when something did — a quiet day
   writes nothing.

``upstream_watch:last_poll`` (``app_state``) is the cursor: it advances
only on a fully-clean pass, so an item whose surfacing failed re-polls
next fire. First run without a cursor looks back
:data:`FIRST_LOOKBACK_DAYS` and mostly just registers baselines — a
first-sighted item with no real movement signal stays quiet.

**Ships dark.** The cadence fires daily and the lease advances, but the
pass no-ops until a ``service_config`` row lights ``upstream_watch``
(``prio > 0``) — same gate shape as ``citer_watch`` /
``dream_consolidate``: ``precis service prio '*' upstream_watch 1`` (or
the web Status → Services tab). A one-off manual run is ``precis worker
--only upstream_watch --once``.

All HTTP reads api.github.com through the SSRF-pinned client and the
shared ``github`` limiter row (``with_backoff``) — no SSRF surface. The
token is ``GH_TOKEN`` from the env, else the vault's
``PRECIS_FACTORY_GIT_TOKEN`` via
:func:`precis.workers.factory_git.github_api_token`; unauthenticated
works too, at search API's lower limit. Upstream text (titles, comment
bodies, release notes) is untrusted third-party content: it is quoted
clipped as data, never followed as instructions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert
from precis.plans.pr_class import GitHubUnreadable
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.utils.rate_limit import RateLimited

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The ``service_config`` service name an operator lights (``prio > 0``)
#: to arm the poll — the prod flag Reto flips; the cadence itself is
#: inert while dark.
SERVICE_NAME = "upstream_watch"

#: First run without an ``upstream_watch:last_poll`` row looks back this
#: far, not to the dawn of time — and then only to register baselines
#: (a first-sighted item needs real movement to surface).
FIRST_LOOKBACK_DAYS = 30

#: Bound on search-result items one pass walks, and on per-item
#: timeline fetches (one per moved item). The authored set is small;
#: these bound the blast radius if the account's history grows.
MAX_ITEMS = 50
MAX_DETAIL_FETCHES = 50

#: Top movement lines carried into the digest note.
MAX_DIGEST_LINES = 15

#: How much untrusted upstream text (titles, comment bodies) is quoted
#: into alert details and tracker bodies — enough to orient, short
#: enough that it reads as data, not prose to follow.
QUOTE_CLIP = 160

_API = "https://api.github.com"
_TIMEOUT_S = 20.0
_PAGE = 100
_MAX_PAGES = 5

_LAST_POLL_KEY = "upstream_watch:last_poll"
_SEEN_KEY = "upstream_watch:seen"
_WATCH_TAG_PREFIX = "upstream:"
_TRACKER_TAG = "upstream-watch"
_DIGEST_TAG = "digest:upstream-watch"
_AUTHOR_SETTING = "upstream_watch.author"
_DEFAULT_AUTHOR = "retospect"

ALERT_SOURCE = RULES_BY_ID["upstream_watch/moved:{item}"].rule


@dataclass
class _Movement:
    """What one moved upstream item should say, in our words."""

    reasons: list[str] = field(default_factory=list)
    suggestion: str = ""
    #: A non-author comment looks like a question → the waiting work
    #: sits on Reto's voice (``waiting-for:reto`` on a minted tracker).
    question: bool = False
    #: The thread was locked upstream (a bot auto-lock like yasbd-lib
    #: #293, or a maintainer) — replying there is no longer possible.
    locked: bool = False


class GitHubClient:
    """api.github.com reads for the pass — the injectable seam.

    The default instance talks REST through the SSRF-pinned client
    (:func:`precis.utils.safe_fetch.safe_get`, fixed host) inside the
    shared ``github`` limiter policy (:func:`with_backoff`); tests
    substitute a fake with the same three methods and recorded fixtures.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send(self, url: str, params: dict[str, Any]) -> Any:
        from precis.utils.http import http_client
        from precis.utils.safe_fetch import safe_get

        with http_client(timeout=_TIMEOUT_S, headers=self._headers()) as client:
            return safe_get(client, url, params=params)

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        from precis.utils.http import with_backoff

        resp = with_backoff(lambda: self._send(url, params), service="github", url=url)
        if resp.status_code != 200:
            raise GitHubUnreadable(f"GET {url}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubUnreadable(f"GET {url}: not JSON") from exc

    def _pages(self, url: str, params: dict[str, Any] | None = None) -> list[dict]:
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self._get_json(
                url, {**(params or {}), "per_page": _PAGE, "page": page}
            )
            if isinstance(data, dict):
                data = data.get("items") or []
            if not isinstance(data, list):
                raise GitHubUnreadable(f"GET {url}: expected a list")
            out += [d for d in data if isinstance(d, dict)]
            if len(data) < _PAGE:
                break
        return out

    def search_moved(self, author: str, since: str) -> list[dict[str, Any]]:
        """``GET /search/issues`` for items the account authored on repos
        it does not own, touched since ``since`` (``-user:`` is the
        own-repo exclusion). Issues and PRs both."""
        q = f"author:{author} -user:{author} updated:>={since}"
        return self._pages(
            f"{_API}/search/issues",
            {"q": q, "sort": "updated", "order": "asc"},
        )

    def timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        """``GET /repos/<repo>/issues/<n>/timeline`` — comments,
        cross-referenced PRs, and state events for one item."""
        return self._pages(f"{_API}/repos/{repo}/issues/{int(number)}/timeline")

    def releases(self, repo: str) -> list[dict[str, Any]]:
        """``GET /repos/<repo>/releases`` — one page is plenty for "did a
        release since the cursor name this item"."""
        return self._pages(f"{_API}/repos/{repo}/releases", {"per_page": 10})


def _default_github() -> GitHubClient:
    """The real client: ``GH_TOKEN`` from the env, else the factory's
    vault token (``PRECIS_FACTORY_GIT_TOKEN``); neither → unauthenticated
    (the search API's low unauthed ceiling still serves a daily pass)."""
    from precis.workers.factory_git import github_api_token

    return GitHubClient(token=github_api_token())


def upstream_watch_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``upstream_watch``
    anywhere. Never raises: a missing table or a connection blip reads
    as dark — an unreadable gate must stop the pass, not arm it. Mirrors
    ``citer_watch_enabled``'s gate shape."""

    def _q(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (SERVICE_NAME,),
        ).fetchone()
        return row is not None

    try:
        if conn is not None:
            return _q(conn)
        with store.pool.connection() as c:
            return _q(c)
    except Exception:
        log.warning("upstream_watch: service_config lookup failed", exc_info=True)
        return False


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _repo_of(item: dict[str, Any]) -> str:
    """``owner/repo`` from a search item's ``repository_url``."""
    return str(item.get("repository_url") or "").rsplit("/repos/", 1)[-1]


def _clip(text: Any, n: int = QUOTE_CLIP) -> str:
    """Untrusted upstream text as quoted data: whitespace-folded, clipped."""
    out = " ".join(str(text or "").split())
    return out if len(out) <= n else out[: n - 1].rstrip() + "…"


def _event_ts(event: dict[str, Any]) -> datetime | None:
    """Newest timestamp an event carries; ``None`` when it carries none
    (and such events are kept — fail toward reporting movement)."""
    stamps = [
        ts
        for k in ("created_at", "updated_at", "submitted_at", "timestamp")
        if (ts := _parse_ts(event.get(k))) is not None
    ]
    return max(stamps) if stamps else None


def _classify(
    item: dict[str, Any],
    prev: dict[str, Any] | None,
    events: list[dict[str, Any]] | None,
    releases: list[dict[str, Any]] | None,
    since_dt: datetime,
    author: str,
) -> _Movement:
    """Reason lines + suggested response for one moved item.

    ``prev`` is the stored snapshot (None on first sighting — the state/
    comments diff then can't run, and state alone is not movement).
    ``events=None`` means the timeline fetch failed: comment detection
    degrades to the raw count delta, which cannot tell our own comments
    apart and so errs toward surfacing."""
    mv = _Movement()
    state = str(item.get("state") or "")
    comments = int(item.get("comments") or 0)
    pull = item.get("pull_request") or {}

    if prev is not None and state and state != str(prev.get("state") or ""):
        if pull and pull.get("merged_at"):
            mv.reasons.append("merged upstream")
        else:
            mv.reasons.append(f"state {prev.get('state')}→{state}")

    # An event counts as new when it postdates the item's own stored
    # snapshot (``prev.updated_at`` — what the last poll saw), falling
    # back to the poll cursor for first-sighted items.
    cutoff = (_parse_ts(prev.get("updated_at")) if prev else None) or since_dt
    linked_prs: list[str] = []
    if events is not None:
        for ev in events:
            ts = _event_ts(ev)
            if ts is not None and ts <= cutoff:
                continue
            kind = str(ev.get("event") or "")
            actor = str((ev.get("actor") or {}).get("login") or "")
            if kind == "commented" and actor and actor.lower() != author.lower():
                body = str(ev.get("body") or "")
                if "?" in body:
                    mv.question = True
                    mv.reasons.append(
                        f"comment by {actor} (looks like a question): “{_clip(body)}”"
                    )
                else:
                    mv.reasons.append(f"comment by {actor}: “{_clip(body)}”")
            elif kind == "cross-referenced":
                src_issue = ((ev.get("source") or {}).get("issue")) or {}
                if src_issue.get("pull_request"):
                    url = str(src_issue.get("html_url") or "")
                    if url and url not in linked_prs:
                        linked_prs.append(url)
                        mv.reasons.append(f"linked PR {url}")
            elif (
                kind in ("closed", "reopened", "merged", "locked")
                and actor
                and actor.lower() != author.lower()
            ):
                mv.reasons.append(f"{kind} by {actor}")
                if kind == "locked":
                    mv.locked = True
    elif prev is not None:
        delta = comments - int(prev.get("comments") or 0)
        if delta > 0:
            mv.reasons.append(f"{delta} new comment(s) — commenter unknown")

    release_hit = ""
    for rel in releases or []:
        published = _parse_ts(rel.get("published_at"))
        if published is None or published < since_dt:
            continue
        blob = f"{rel.get('name') or ''}\n{rel.get('body') or ''}"
        number = item.get("number")
        if (number and f"#{number}" in blob) or str(item.get("html_url") or "") in blob:
            release_hit = str(rel.get("tag_name") or rel.get("name") or "")
            mv.reasons.append(f"release {release_hit} mentions it")
            break

    if mv.locked:
        mv.suggestion = (
            "the thread is locked — replying there is no longer possible; "
            "if the matter still stands, consider a follow-up issue"
        )
    elif release_hit:
        mv.suggestion = (
            f"release {release_hit} mentions it — update the package pin, "
            "verify the fix, then close the waiting work"
        )
    elif pull.get("merged_at") or state == "closed":
        mv.suggestion = (
            "closed upstream — check whether a release carries the fix; "
            "update the package pin, or record wontfix on the waiting todo"
        )
    elif mv.question:
        mv.suggestion = (
            "the maintainer asked a question — draft an answer; nothing "
            "posts upstream without Reto"
        )
    elif linked_prs:
        mv.suggestion = "watch the linked PR — if it merges, update the package pin"
    elif mv.reasons:
        mv.suggestion = "read the thread; decide whether it needs a reply"
    return mv


def _watch_tag(key: str) -> str:
    """The open tag linking a waiting todo to one upstream item."""
    return f"{_WATCH_TAG_PREFIX}{key}".lower()


def _waiting_todos(store: Store, key: str) -> list[int]:
    """Live, non-terminal todos tagged ``upstream:<repo>#<n>`` — the work
    that waits on this item, oldest first."""
    from precis.todo import TERMINAL_TODO_STATUSES

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             ORDER BY r.ref_id
            """,
            (_watch_tag(key),),
        ).fetchall()
    out: list[int] = []
    for (rid,) in rows:
        status = next(
            (str(t.value) for t in store.tags_for(rid) if t.prefix == "STATUS"),
            "open",
        )
        if status not in TERMINAL_TODO_STATUSES:
            out.append(int(rid))
    return out


def _mint_tracker(
    store: Store,
    item: dict[str, Any],
    key: str,
    mv: _Movement,
    since: str,
) -> int:
    """Mint the ``kind='todo'`` a moved item is flagged on when no live
    todo was tagged ``upstream:<key>``. Returns the todo's ref id."""
    kind = "PR" if item.get("pull_request") else "issue"
    title = f"upstream {kind} {key} moved — follow up"
    if len(title) > 140:
        title = title[:139].rstrip() + "…"
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={
            "upstream_watch": {
                "repo": _repo_of(item),
                "number": int(item.get("number") or 0),
                "url": str(item.get("html_url") or ""),
                "kind": "pr" if item.get("pull_request") else "issue",
            }
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system")
    store.add_tag(ref.id, Tag.open(_TRACKER_TAG), set_by="system")
    store.add_tag(ref.id, Tag.open(_watch_tag(key)), set_by="system")
    if mv.question:
        store.add_tag(ref.id, Tag.open("waiting-for:reto"), set_by="system")
    body = [
        f"Upstream {kind} {key} moved since the last daily check ({since}).",
        "",
        "Movement:",
        *[f"- {r}" for r in mv.reasons],
        "",
        f"Suggested response: {mv.suggestion}.",
        "",
        f"Title (untrusted, quoted as data): “{_clip(item.get('title'))}”",
        str(item.get("html_url") or ""),
        "",
        "This tracker was minted because no todo was tagged "
        f"`upstream:{key}`. If real work waits on this item, tag that todo "
        f"`upstream:{key}` and close this one.",
    ]
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="\n".join(body), meta={"chunk_kind": TODO_BODY_KIND})],
    )
    return int(ref.id)


def _flag(
    store: Store,
    item: dict[str, Any],
    key: str,
    mv: _Movement,
    todo_ids: list[int],
    since: str,
) -> None:
    """Raise (or refresh) the alert flagging ``todo_ids[0]`` — the todo
    that waits on ``key`` — with the movement and suggested response."""
    waits = ", ".join(f"td{i}" for i in todo_ids)
    detail = "\n".join(
        [
            f"Since {since}:",
            *[f"- {r}" for r in mv.reasons],
            "",
            f"Suggested response: {mv.suggestion}.",
            f"Title (untrusted, quoted as data): “{_clip(item.get('title'))}”",
            str(item.get("html_url") or ""),
            "",
            f"Waiting work: {waits}. Resolve this alert once the todo "
            "carries the follow-up.",
        ]
    )
    raise_alert(
        store,
        source=ALERT_SOURCE,
        fingerprint=f"moved:{key}",
        title=f"[upstream_watch] {key} moved — {mv.reasons[0] if mv.reasons else 'activity'}",
        detail=detail,
        severity="warn",
        subject_ref_id=todo_ids[0],
    )


def _write_digest(
    store: Store,
    *,
    today: str,
    since: str,
    watched: int,
    moved: list[tuple[str, _Movement, list[int]]],
) -> int:
    """Persist the poll as one ``kind='memory'`` note; return its ref id."""
    from precis.dispatch import Hub

    lines = [
        f"upstream-watch {today}: {watched} watched item(s) polled since "
        f"{since}; {len(moved)} moved.",
    ]
    for key, mv, todos in moved[:MAX_DIGEST_LINES]:
        waits = ", ".join(f"td{i}" for i in todos)
        lines.append(f"- {key}: {'; '.join(mv.reasons)} — waits: {waits}")
    handler = Hub(store=store).sibling("memory")
    return handler.write_system(
        text="\n".join(lines),
        title=f"upstream-watch {today}",
        tags=[_DIGEST_TAG, f"upstream-watch:{today}"],
        meta={
            "upstream_watch_date": today,
            "upstream_watch_since": since,
            "upstream_watch_watched": watched,
            "upstream_watch_moved": len(moved),
        },
    )


def run_upstream_watch_pass(
    store: Store,
    *,
    enabled: bool | None = None,
    github: Any = None,
    now: datetime | None = None,
    author: str | None = None,
    max_items: int = MAX_ITEMS,
    max_detail: int = MAX_DETAIL_FETCHES,
) -> dict[str, int]:
    """One upstream poll. ``claimed`` = items the search returned,
    ``ok`` = moved items surfaced, ``failed`` = items whose surfacing
    raised (plus a failed search); ``quiet`` = items polled with no
    surfacing movement, ``digest`` = the digest ref id (0 = none).

    ``enabled=None`` resolves the ``service_config`` gate (dark until a
    row lights it); the ``--only`` path passes ``True`` (the operator
    escape hatch). ``github`` is the recorded-fixture seam — the default
    is :class:`GitHubClient` on the env/vault token, never raw HTTP."""
    from precis import settings as psettings

    now = now or datetime.now(UTC)
    today = now.date()
    if enabled is None:
        enabled = upstream_watch_enabled(store)
    if not enabled:
        log.info(
            "upstream_watch: dark — no live service_config row lights %s; no-op",
            SERVICE_NAME,
        )
        return {"claimed": 0, "ok": 0, "failed": 0, "quiet": 0, "digest": 0}

    author = author or (
        (psettings.get_str(_AUTHOR_SETTING, store=store) or "").strip()
        or _DEFAULT_AUTHOR
    )
    gh = github if github is not None else _default_github()

    last = store.get_setting(_LAST_POLL_KEY)
    since = last or (today - timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()
    since_dt = _parse_ts(since) or (now - timedelta(days=FIRST_LOOKBACK_DAYS))
    try:
        seen: dict[str, Any] = json.loads(store.get_setting(_SEEN_KEY) or "{}")
        if not isinstance(seen, dict):
            seen = {}
    except ValueError:
        seen = {}

    failed = 0
    try:
        items = gh.search_moved(author, since)
    except RateLimited:
        # Shared github limiter refused: the window stays parked — the
        # unpollled slice is re-claimed next fire because last_poll only
        # advances on a fully-clean run.
        log.info("upstream_watch: shared limiter refused the search; deferring")
        return {"claimed": 0, "ok": 0, "failed": 0, "quiet": 0, "digest": 0}
    except Exception:
        log.exception("upstream_watch: search failed")
        return {"claimed": 0, "ok": 0, "failed": 1, "quiet": 0, "digest": 0}

    watched = 0
    surfaced: list[tuple[str, _Movement, list[int]]] = []
    details = 0
    release_cache: dict[str, list[dict[str, Any]] | None] = {}
    for item in items:
        if watched >= max_items:
            break
        repo = _repo_of(item)
        number = int(item.get("number") or 0)
        if not repo or "/" not in repo or not number:
            continue
        if repo.split("/", 1)[0].lower() == author.lower():
            continue  # own repo — server-side -user: re-checked locally
        watched += 1
        key = f"{repo}#{number}"
        prev = seen.get(key)
        try:
            events: list[dict[str, Any]] | None = None
            if details < max_detail:
                details += 1
                try:
                    events = gh.timeline(repo, number)
                except RateLimited:
                    log.info(
                        "upstream_watch: limiter refused timeline for %s; "
                        "detail degrades",
                        key,
                    )
                except Exception:
                    log.info(
                        "upstream_watch: timeline failed for %s", key, exc_info=True
                    )
            if repo not in release_cache:
                try:
                    release_cache[repo] = gh.releases(repo)
                except Exception:
                    log.info(
                        "upstream_watch: releases failed for %s", repo, exc_info=True
                    )
                    release_cache[repo] = None
            mv = _classify(item, prev, events, release_cache[repo], since_dt, author)
            if not mv.reasons:
                seen[key] = {
                    "state": str(item.get("state") or ""),
                    "comments": int(item.get("comments") or 0),
                    "updated_at": str(item.get("updated_at") or ""),
                }
                continue  # quiet — registered, not surfaced
            todos = _waiting_todos(store, key)
            if not todos:
                todos = [_mint_tracker(store, item, key, mv, since)]
            _flag(store, item, key, mv, todos, since)
            surfaced.append((key, mv, todos))
            seen[key] = {
                "state": str(item.get("state") or ""),
                "comments": int(item.get("comments") or 0),
                "updated_at": str(item.get("updated_at") or ""),
            }
        except Exception:
            log.exception("upstream_watch: surfacing failed for %s", key)
            failed += 1

    store.set_setting(_SEEN_KEY, json.dumps(seen, sort_keys=True))

    digest = 0
    if surfaced:
        try:
            digest = _write_digest(
                store,
                today=today.isoformat(),
                since=since,
                watched=watched,
                moved=surfaced,
            )
        except Exception:
            log.exception("upstream_watch: digest write failed")

    if not failed:
        # The whole window polled clean — park the next fire at today.
        store.set_setting(_LAST_POLL_KEY, today.isoformat())

    log.info(
        "upstream_watch: watched=%d moved=%d quiet=%d failed=%d digest=%d since=%s",
        watched,
        len(surfaced),
        watched - len(surfaced) - failed,
        failed,
        digest,
        since,
    )
    return {
        "claimed": watched,
        "ok": len(surfaced),
        "failed": failed,
        "quiet": watched - len(surfaced) - failed,
        "digest": digest,
    }


__all__ = [
    "FIRST_LOOKBACK_DAYS",
    "MAX_DETAIL_FETCHES",
    "MAX_ITEMS",
    "SERVICE_NAME",
    "GitHubClient",
    "run_upstream_watch_pass",
    "upstream_watch_enabled",
]
