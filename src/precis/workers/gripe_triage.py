"""``gripe_triage`` — the scheduled triage pass over stale open gripes.

Batch F slice 4 (Reto approved 2026-10-18): a deterministic, no-LLM pass
fired by the daily ``gripe_triage`` scheduler cadence. It is dark by
default: the cadence fires everywhere, but the pass does nothing unless a
``service_config`` row lights ``gripe_triage`` (``prio > 0``) on some host
(:func:`gripe_triage_enabled`, the ``graph_garden`` gate shape).

**Picks** open, non-retired gripes older than :data:`MIN_AGE_DAYS` that
have no PRIO or no ``thread:<slug>`` tag, oldest first, at most
:data:`MAX_COMMENTS_PER_RUN` per fire. A gripe that already carries a
``triage:`` comment from the last :data:`REPEAT_DAYS` is skipped, so each
gripe gets at most one triage comment per three days.

**Posts** one ``triage:`` comment per pick, suggesting only what is
missing, each with its reason and the call that applies it:

- PRIO: an ``origin:*`` tag (system-filed) suggests 7, else 5.
- Thread: the first rule that matches, against the thread files'
  pointers and Status lines (:func:`load_threads`): (1) the thread file
  names the gripe's own ``gr<id>``; (2) it names a ref the gripe links to
  (``gr``/``td``/``qu``); (3) the gripe's title and body share at least
  :data:`MIN_SHARED_TERMS` terms with the thread's title + Status line
  (most shared wins, ties by slug). No match says so.

It never closes, retags or edits a gripe: the comment is a suggestion an
agent or Reto applies. Thread files are read from the clone this worker
runs from (``PRECIS_THREADS_DIR`` overrides); the comment names that
clone's sha, so a stale host clone is visible in the suggestion.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

SERVICE_NAME = "gripe_triage"
#: Open gripes younger than this are left alone.
MIN_AGE_DAYS = 3
#: A gripe gets at most one triage comment per this many days.
REPEAT_DAYS = 3
#: Comments posted per fire, max (oldest gripes first).
MAX_COMMENTS_PER_RUN = 10
#: Term-overlap floor for the weakest thread rule.
MIN_SHARED_TERMS = 2
#: Every triage comment starts with this; the repeat check keys on it.
COMMENT_PREFIX = "triage: "
#: Open-tag prefix that links a gripe to its owning thread.
THREAD_TAG_PREFIX = "thread:"
#: PRIO suggestions: system-filed (``origin:*``) vs human-filed.
AUTO_FILED_PRIO = 7
HUMAN_FILED_PRIO = 5

_THREADS_ENV = "PRECIS_THREADS_DIR"
_NOT_THREADS = frozenset({"README", "INDEX", "PRIORITIES"})
_KIND_PREFIX = {"gripe": "gr", "todo": "td", "quest": "qu"}
_POINTER_RE = re.compile(r"\b(gr|td|qu)(\d+)\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{3,}")
_STOPWORDS_TEXT = """
    about after again against also because been before being between both
    does done each ends every from have here into just last like long make
    more most much must next none once only other over same says should
    since some still such than that their them then there these they this
    those through today until upon very want were what when where which
    while will with without would your
"""
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def gripe_triage_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``gripe_triage`` anywhere.
    Never raises: a missing table or a connection blip reads as dark."""

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
        log.warning("gripe_triage: service_config lookup failed", exc_info=True)
        return False


# ── thread files ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Thread:
    """One ``docs/backlog/threads/<slug>.md``: the pointers it names and
    the terms of its title + Status line."""

    slug: str
    pointers: frozenset[str]
    terms: frozenset[str]


@dataclass(frozen=True, slots=True)
class ThreadIndex:
    threads: tuple[Thread, ...]
    #: Short sha of the clone the files were read from, or ``"unknown"``.
    sha: str


def _terms(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS)


def _threads_dir() -> Path:
    env = os.environ.get(_THREADS_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "docs" / "backlog" / "threads"


def _clone_sha(path: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        cp = None
    if cp is not None and cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    return (os.environ.get("PRECIS_GIT_SHA") or "unknown")[:9]


def load_threads(path: Path | None = None) -> ThreadIndex:
    """Index every thread file under ``path`` (default: this clone's
    ``docs/backlog/threads``). A missing directory is an empty index."""
    root = path if path is not None else _threads_dir()
    if not root.is_dir():
        return ThreadIndex(threads=(), sha="unknown")
    threads: list[Thread] = []
    for f in sorted(root.glob("*.md")):
        if f.stem in _NOT_THREADS:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        title = next((ln[2:] for ln in text.splitlines() if ln.startswith("# ")), "")
        status = next(
            (ln for ln in text.splitlines() if ln.startswith("**Status:**")), ""
        )
        pointers = frozenset(p + n for p, n in _POINTER_RE.findall(text))
        threads.append(
            Thread(
                slug=f.stem,
                pointers=pointers,
                terms=_terms(f"{f.stem.replace('-', ' ')} {title} {status}"),
            )
        )
    return ThreadIndex(threads=tuple(threads), sha=_clone_sha(root))


# ── candidates ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StaleGripe:
    """One open gripe the pass may comment on."""

    ref_id: int
    title: str
    body: str
    prio: int | None
    #: Lowercased OPEN-namespace tag values (``origin:…``, ``thread:…``).
    open_tags: frozenset[str]
    #: ``gr``/``td``/``qu`` pointers of refs linked to or from the gripe.
    linked: frozenset[str]

    @property
    def thread_tags(self) -> frozenset[str]:
        return frozenset(t for t in self.open_tags if t.startswith(THREAD_TAG_PREFIX))

    @property
    def needs_triage(self) -> bool:
        return self.prio is None or not self.thread_tags


def candidates(store: Store, now: datetime) -> list[StaleGripe]:
    """Open, non-retired gripes older than :data:`MIN_AGE_DAYS` with no
    PRIO or no ``thread:`` tag and no ``triage:`` comment in the last
    :data:`REPEAT_DAYS`, oldest first."""
    older_than = now - timedelta(days=MIN_AGE_DAYS)
    repeat_after = now - timedelta(days=REPEAT_DAYS)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, coalesce(r.title, ''), r.prio,
                   coalesce((SELECT c.text FROM chunks c
                              WHERE c.ref_id = r.ref_id
                                AND c.chunk_kind = 'gripe_body'
                              ORDER BY c.ord LIMIT 1), ''),
                   coalesce((SELECT array_agg(t.value) FROM ref_tags rt
                               JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = r.ref_id
                                AND t.namespace = 'OPEN'), '{}'),
                   coalesce((SELECT array_agg(o.kind || ':' || o.ref_id)
                               FROM links l
                               JOIN refs o ON o.ref_id = CASE
                                    WHEN l.src_ref_id = r.ref_id THEN l.dst_ref_id
                                    ELSE l.src_ref_id END
                              WHERE (l.src_ref_id = r.ref_id
                                     OR l.dst_ref_id = r.ref_id)
                                AND o.ref_id <> r.ref_id
                                AND o.kind IN ('gripe', 'todo', 'quest')), '{}')
              FROM refs r
              JOIN ref_tags st ON st.ref_id = r.ref_id
              JOIN tags s ON s.tag_id = st.tag_id
                         AND s.namespace = 'STATUS' AND s.value = 'open'
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND r.created_at < %s
               AND NOT EXISTS (
                   SELECT 1 FROM chunks c
                    WHERE c.ref_id = r.ref_id
                      AND c.chunk_kind = 'gripe_comment'
                      AND c.text LIKE %s
                      AND c.created_at >= %s)
             ORDER BY r.created_at, r.ref_id
            """,
            (older_than, COMMENT_PREFIX + "%", repeat_after),
        ).fetchall()
    out: list[StaleGripe] = []
    for ref_id, title, prio, body, open_tags, linked in rows:
        pointers: set[str] = set()
        for item in linked:
            kind, _, rid = str(item).partition(":")
            pointers.add(_KIND_PREFIX[kind] + rid)
        c = StaleGripe(
            ref_id=int(ref_id),
            title=str(title),
            body=str(body),
            prio=None if prio is None else int(prio),
            open_tags=frozenset(str(t).lower() for t in open_tags),
            linked=frozenset(pointers),
        )
        if c.needs_triage:
            out.append(c)
    return out


# ── suggestion ────────────────────────────────────────────────────────────


def _match_thread(c: StaleGripe, index: ThreadIndex) -> tuple[str, str] | None:
    """``(slug, reason)`` for the owning thread, or ``None``."""
    own = f"gr{c.ref_id}"
    for t in index.threads:
        if own in t.pointers:
            return t.slug, f"threads/{t.slug}.md names {own}"
    for t in index.threads:
        hit = sorted(c.linked & t.pointers)
        if hit:
            return t.slug, f"it links {hit[0]}, which threads/{t.slug}.md names"
    words = _terms(f"{c.title} {c.body}")
    best: tuple[int, str, list[str]] | None = None
    for t in index.threads:
        shared = sorted(words & t.terms)
        if len(shared) < MIN_SHARED_TERMS:
            continue
        if best is None or len(shared) > best[0]:
            best = (len(shared), t.slug, shared)
    if best is None:
        return None
    _, slug, shared = best
    return slug, f"shares {', '.join(shared[:4])} with the {slug} title/Status line"


def suggest(c: StaleGripe, index: ThreadIndex) -> str | None:
    """The triage comment for ``c``, or ``None`` when nothing is missing."""
    parts: list[str] = []
    if c.prio is None:
        origin = sorted(t for t in c.open_tags if t.startswith("origin:"))
        if origin:
            prio, why = AUTO_FILED_PRIO, f"system-filed, tagged {origin[0]}"
        else:
            prio, why = HUMAN_FILED_PRIO, "human-filed, no origin:* tag"
        parts.append(
            f"suggest PRIO {prio} ({why}): tag(kind='gripe', id={c.ref_id}, prio={prio})"
        )
    if not c.thread_tags:
        match = _match_thread(c, index)
        if match is None:
            parts.append(
                f"no owning thread matched among {len(index.threads)} thread files"
            )
        else:
            slug, why = match
            parts.append(
                f"suggest thread {slug} ({why}): tag(kind='gripe', "
                f"id={c.ref_id}, add=['{THREAD_TAG_PREFIX}{slug}'])"
            )
    if not parts:
        return None
    return (
        f"{COMMENT_PREFIX}{'; '.join(parts)}. "
        f"Thread files read at {index.sha}; this pass never closes or retags."
    )


def _post_comment(store: Store, gripe_id: int, text: str) -> None:
    from precis.store.types import ChunkInsert

    next_pos = len(store.chunks.list_chunks_for_ref(gripe_id))
    with store.tx() as conn:
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=next_pos, text=text, meta={"chunk_kind": "gripe_comment"}
                )
            ],
            conn=conn,
        )


# ── the pass ──────────────────────────────────────────────────────────────


def run_gripe_triage_pass(
    store: Store,
    *,
    now: datetime | None = None,
    threads: ThreadIndex | None = None,
    limit: int = MAX_COMMENTS_PER_RUN,
) -> BatchResult:
    """One triage fire. Dark (all zeros) unless :func:`gripe_triage_enabled`.
    ``claimed`` = candidates considered, ``ok`` = comments posted,
    ``failed`` = posts that raised (logged; the rest still run)."""
    if not gripe_triage_enabled(store):
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    when = now if now is not None else datetime.now(UTC)
    index = threads if threads is not None else load_threads()
    picked = candidates(store, when)[:limit]
    ok = failed = 0
    for c in picked:
        text = suggest(c, index)
        if text is None:
            continue
        try:
            _post_comment(store, c.ref_id, text)
        except Exception:
            log.exception("gripe_triage: comment failed for gripe id=%d", c.ref_id)
            failed += 1
            continue
        ok += 1
    log.info(
        "gripe_triage: %d candidate(s), %d comment(s), %d failed (threads at %s)",
        len(picked),
        ok,
        failed,
        index.sha,
    )
    return BatchResult(handler=SERVICE_NAME, claimed=len(picked), ok=ok, failed=failed)
