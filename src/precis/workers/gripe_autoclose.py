"""gripe_autoclose — close a landed gripe when its prod check runs
(gr519676).

The last rung of the gripe-fix loop (``docs/backlog/
gripe-fix-loop-on-the-gripe.md`` § "Auto-close on deploy"): the fixer
records the fix's sha in ``meta.fix.landed`` and a **check line** in
``meta.fix.check`` at land time; when the sha is deployed, this pass
runs the check read-only and either closes the gripe or closes it and
refiles a linked triage gripe — Reto's 2026-10-26 ruling. It complements
``scripts/gripe-validate``: that script closes ``STATUS:verify`` gripes
on "the sha reached prod" alone; this tick closes ``meta.fix`` gripes on
"the fix *works* in prod" — and only gripes that opted in by carrying a
check line.

Since td570413 the check line's home is the gripe's ``fixes``-linked
**code todo**: ``tag(kind='todo', id=T, meta={'fix': {'check': …}})``
(written and parse-checked like the gripe-side edit) plus the
``landed: <sha>`` comment land-notify already posts — the todo's
``STATUS:done`` and its newest ``landed:`` line are the land record.
The legacy gripe-side ``meta.fix.landed`` + ``meta.fix.check`` path
keeps working; when both carry a line the todo's wins. ``scripts/
gripe-validate`` skips either shape, so a check-lined gripe is this
pass's to close, never the sha-in-prod flip's.

The check line is a small closed grammar, no LLM anywhere:

- ``read <kind> <id> contains "<substring>"`` — the ref's raw title +
  chunk text carries the substring. This is NOT the rendered
  ``get(kind=..., id=...)`` output: rendered views add the card chrome,
  link annotations and tag lists a raw read never sees — write the
  needle against title/body text. A read of the gripe's OWN id is
  refused (a gripe always contains whatever the fixer wrote — the check
  would be tautological).
- ``read <kind> <id> count "<substring>" <cmp> <n>`` — occurrence count.
- ``count <kind> [with <pred> [and <pred>…]] <cmp> <n>`` — a read-only
  SQL count over live refs; ``pred`` is ``tag=<tag>`` or
  ``meta.<dotted.path>=<literal>``; ``<cmp>`` is ``= != > < >= <=``.
  Parse refuses: a kind the ``kinds`` table doesn't know (a typo'd kind
  counts zero refs and reads as "the fix worked"); a comparison that is
  true (``>= 0``, ``> -1``) or false (``= -1``, ``< 0``) for EVERY
  count — it can never go the other way; and, unless a ``tag=`` /
  ``meta.`` predicate anchors it, a comparison an EMPTY result
  satisfies (``= 0``, ``< n``, ``<= n``, ``!= n≠0``) — vacuous on a
  mistyped kind.

One pass (the hourly ``gripe_autoclose`` scheduler cadence; the lease is
the fleet-singleton throttle, same as ``citer_watch``):

1. **Candidates**: live gripes with ``meta.fix.landed`` AND a non-empty
   ``meta.fix.check``, minus terminal STATUS (``done``/``wontfix``) and
   the ``hold:no-autoclose`` never-close mark (gr547015) — both honoured
   only while their ``ref_tags`` row is unexpired, same as
   ``build_tag_filter`` — oldest first, capped at :data:`MAX_PER_PASS`.
2. **Deployed?** the sha must be an ancestor of the CURRENT recorded
   prod head — ``git merge-base --is-ancestor <sha> <head>`` in the
   ``PRECIS_THREADS_DIR`` checkout (or this package's repo root), the
   ``gripe_triage._is_ancestor`` shape. Git is truth both ways: a
   rolled-back fix still listed in the record's ``landed`` reads NOT
   deployed; a sha aged out of ``landed`` that is still an ancestor of
   head reads deployed. When no checkout can answer (clone-less worker,
   objects missing) the record itself is the fallback —
   :func:`precis.plans.gates.sha_deployed`'s ``head``/``landed``
   membership, same as ``sha-in-prod``.
   The head must also have *stood*: a record whose ``deployed_at`` is
   younger than :data:`MIN_DEPLOY_AGE_S`
   (``PRECIS_AUTOCLOSE_MIN_DEPLOY_AGE_S``) defers the whole pass — a
   check run mid-rollout flaps on partial deploys. Not yet →
   ``pending``, retried next fire.
3. **Run the check** under a 5 s ``SET LOCAL statement_timeout`` —
   a cancelled count is ``skipped``, never a verdict. Pass → clear any
   ``meta.fix.check_failed_at`` marker (a pass resets the failure
   clock), then ``resolved: <sha> (<rN|prod head9>) — prod check
   passed: <line>`` + ``STATUS:done`` through the gripe handler, so the
   ``resolved:`` guard and close cascade run exactly as on a manual
   close. Fail → the FIRST failure only records
   ``meta.fix.check_failed_at`` (skipped, retried next fire); a failure
   on a pass at least :data:`FAIL_RECHECK_S` later is confirmed — the
   gripe closes FIRST (``resolved: … prod check FAILED …``), then a
   NEW gripe is filed back to ``STATUS:triaged``, linked
   ``derived-from`` the old one, first line naming the sha and the
   observed check output. Close-first means a refused close files
   nothing; before filing, a live gripe already linked ``derived-from``
   this one is reused instead — a chain never piles up duplicates. At
   :data:`MAX_CHAIN` consecutive failures on one root the refiled gripe
   carries ``hold:no-autoclose`` — the cap hands the chain to a human
   instead of looping forever.

A malformed check line (hand-edited meta — ``edit(mode='fix')`` refuses
them) is logged and skipped, never closed on. Idempotent: a closed
gripe leaves the candidate set, so a re-run is a no-op.

**Ships dark** — the ``citer_watch`` shape: the cadence fires and the
lease advances, but the pass no-ops until a ``service_config`` row
lights ``gripe_autoclose`` (``prio > 0``) —
``precis service prio '*' gripe_autoclose 1``. A one-off manual run is
``precis worker --only gripe_autoclose --once`` (``enabled=True``, the
operator escape hatch). Every read is local SQL; the only writes are
the gripe close, the refiled gripe, and the failure marker.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection

    from precis.store.store import Store

from precis.errors import PrecisError
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Tag
from precis.utils.env import env_int

log = logging.getLogger(__name__)

#: The ``service_config`` service name an operator lights (``prio > 0``)
#: to arm the pass — the prod flag; the cadence itself is inert while
#: dark.
SERVICE_NAME = "gripe_autoclose"

#: Grips evaluated per fire — a release carries a handful of landed
#: fixes; the cap bounds one fire's writes, the rest wait an hour.
MAX_PER_PASS = 25

#: Consecutive auto-refiles on one root before the chain hands off: the
#: refiled gripe of the Nth failure carries ``hold:no-autoclose``.
MAX_CHAIN = 3

#: The never-close mark (gr547015) — restated from
#: ``precis.cli.gripe.HOLD_NO_AUTOCLOSE_TAG`` (a script-facing module;
#: the literal is the contract either side reads). ``Tag.parse`` stores
#: it as an OPEN-namespace tag (lowercase prefix → open), so the
#: candidate exclusion matches ``(namespace, value) = ('OPEN', <tag>)``.
HOLD_TAG = "hold:no-autoclose"

#: The refile link's relation: new gripe ``derived-from`` the closed one.
_REFILE_REL = "derived-from"

#: The recorded prod head must have been deployed at least this long
#: before checks evaluate against it — a mid-rollout read is partial and
#: flaps. Env-tunable via ``PRECIS_AUTOCLOSE_MIN_DEPLOY_AGE_S``.
MIN_DEPLOY_AGE_S = 30 * 60
_MIN_DEPLOY_AGE_ENV = "PRECIS_AUTOCLOSE_MIN_DEPLOY_AGE_S"

#: A FAILED check must fail on two passes at least this far apart before
#: the gripe closes + refiles — a transient prod blip clears itself
#: instead of bouncing the gripe back to triage (module docstring §3).
FAIL_RECHECK_S = 3600

#: ``meta.fix`` key carrying the first failure's UTC-Z timestamp.
_FAILED_AT_KEY = "check_failed_at"

#: ``SET LOCAL statement_timeout`` on every check query — a runaway
#: count cancels to ``skipped`` instead of stalling the pass.
CHECK_TIMEOUT_S = 5

#: Cap on one git read — the ``gripe_triage._GIT_TIMEOUT`` value.
_GIT_TIMEOUT = 15

#: This package's repo root — the ``gripe_triage._REPO_ROOT``
#: resolution; a real checkout in dev/CI, a venv's site-packages (not a
#: repo) in a clone-less install, where ancestry simply falls back to
#: the record.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``gripe_triage._THREADS_ENV`` — the operator's checkout hint.
_THREADS_ENV = "PRECIS_THREADS_DIR"

_CMPS: tuple[str, ...] = (">=", "<=", "!=", "=", ">", "<")
_TERMINAL_STATUS = ("done", "wontfix")
_META_PATH_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")

#: The todo's land record (td570413): land-notify's ``landed: <sha9> —
#: <subject>`` comment (``scripts/lib/land_notify.py``); the newest one
#: names the sha the deploy check runs against.
_LANDED_RE = re.compile(r"^\s*landed:\s*([0-9a-f]{7,40})\b")


@dataclass
class _Candidate:
    """One check-lined gripe the SQL fetch surfaced — evaluable or not.

    ``fix`` is the resolved ``{"landed", "check", "check_failed_at"}``
    bag; ``owner_id`` is the ref it lives on — the gripe itself on the
    legacy path, the ``fixes``-linked code todo on the new one (the
    failure marker writes there too). ``landed_raw`` is the landed
    text to judge (``meta.fix.landed`` vs the NEWEST ``landed:``
    comment); ``todo_done`` the todo's live ``STATUS:done``. ``sha`` is
    the sha ``_evaluable`` proved — set only on a candidate this pass
    will actually evaluate.
    """

    gid: int
    title: str
    fix: dict[str, Any]
    owner_id: int
    landed_what: str
    landed_raw: str
    todo_done: bool = True
    sha: str = ""


class CheckError(ValueError):
    """A check line that does not parse — carries the fix-hint text."""


@dataclass(frozen=True)
class ReadCheck:
    """``read <kind> <id> contains|count "<needle>" [<cmp> <n>]``."""

    kind: str
    ref_id: int
    needle: str
    cmp: str | None = None
    n: int | None = None


@dataclass(frozen=True)
class CountCheck:
    """``count <kind> [with <pred>…] <cmp> <n>``."""

    kind: str
    tags: tuple[str, ...]
    meta_eq: tuple[tuple[str, str], ...]
    cmp: str
    n: int


Check = ReadCheck | CountCheck


def _check_example() -> str:
    return (
        'check is one line: read <kind> <id> contains "<substring>" · '
        'read <kind> <id> count "<substring>" <cmp> <n> · '
        "count <kind> [with tag=<tag>|meta.<path>=<value> [and …]] <cmp> <n> "
        "(<cmp> is = != > < >= <=)"
    )


def _parse_cmp(tok: str) -> str:
    if tok not in _CMPS:
        raise CheckError(f"comparison {tok!r} is not one of {_CMPS}")
    return tok


def _parse_n(tok: str) -> int:
    try:
        return int(tok)
    except ValueError:
        raise CheckError(f"expected an integer count, got {tok!r}") from None


def _parse_pred(tok: str) -> tuple[str, tuple[str, str] | str]:
    """One ``count`` predicate → ``('tag', value)`` or
    ``('meta', (path, literal))``."""
    if tok.startswith("tag="):
        value = tok[4:].strip()
        if not value:
            raise CheckError("tag= predicate needs a value")
        Tag.parse_strict(value)  # BadInput propagates as a parse error
        return "tag", value
    if tok.startswith("meta."):
        path, sep, literal = tok[len("meta.") :].partition("=")
        if not sep:
            raise CheckError(f"meta predicate {tok!r} needs meta.<path>=<value>")
        keys = path.split(".")
        if not all(k and set(k) <= _META_PATH_RE_CHARS for k in keys):
            raise CheckError(
                f"meta path {path!r}: lowercase letters, digits, '_' segments only"
            )
        return "meta", (path, literal)
    raise CheckError(
        f"predicate {tok!r} is not tag=<tag> or meta.<path>=<value>; "
        + _check_example()
    )


def _check_cmp_viable(cmp_: str, n: int) -> None:
    """Reject a comparison no non-negative count can move: always-true
    (``>= 0``, ``> -1``, ``!= <negative>`` — it can never fail) and
    always-false (``= <negative>``, ``< 0``, ``<= <negative>`` — it can
    never pass). A verdict either direction can't possibly be evidence."""
    if (cmp_ == ">=" and n <= 0) or (cmp_ in (">", "!=") and n < 0):
        raise CheckError(
            f"comparison `{cmp_} {n}` is true for every count — "
            "the check can never fail"
        )
    if (cmp_ in ("=", "<=") and n < 0) or (cmp_ == "<" and n <= 0):
        raise CheckError(
            f"comparison `{cmp_} {n}` is false for every count — "
            "the check can never pass"
        )


def _check_kind(kind: str, kinds: Collection[str] | None) -> None:
    """Reject a kind the ``kinds`` table never heard of — a typo'd kind
    counts zero refs and a zero count is a verdict the fixer never
    meant. ``kinds=None`` (the table unread) skips the check."""
    if kinds is not None and kind not in kinds:
        raise CheckError(
            f"unknown kind {kind!r} — known: {sorted(kinds)}; " + _check_example()
        )


def parse_check(text: str, *, kinds: Collection[str] | None = None) -> Check:
    """Parse one ``meta.fix.check`` line; raise :class:`CheckError` on any
    deviation — the closed grammar is the whole safety story.

    ``kinds`` is the live ``kinds.slug`` set when the caller can read it
    (the pass does; the write-time ``edit(mode='fix')`` parse leaves it
    ``None`` and stays grammar-only) — an unknown kind is a parse
    refusal, not a zero count.
    """
    try:
        toks = shlex.split(text or "")
    except ValueError as e:
        raise CheckError(f"check line does not tokenize: {e}") from None
    if not toks:
        raise CheckError("empty check line; " + _check_example())
    if toks[0] == "read":
        if len(toks) < 5 or toks[3] not in ("contains", "count"):
            raise CheckError(
                'read check is `read <kind> <id> contains "<substring>"` '
                'or `read <kind> <id> count "<substring>" <cmp> <n>`'
            )
        _check_kind(toks[1], kinds)
        try:
            ref_id = int(toks[2])
        except ValueError:
            raise CheckError(f"read id {toks[2]!r} is not a numeric ref id") from None
        if toks[3] == "contains":
            needle = " ".join(toks[4:])
            if not needle:
                raise CheckError("contains needs a substring")
            return ReadCheck(kind=toks[1], ref_id=ref_id, needle=needle)
        # read … count "<needle>" <cmp> <n>
        if len(toks) < 7:
            raise CheckError(
                'read count is `read <kind> <id> count "<substring>" <cmp> <n>`'
            )
        cmp_ = _parse_cmp(toks[-2])
        n = _parse_n(toks[-1])
        _check_cmp_viable(cmp_, n)
        needle = " ".join(toks[4:-2])
        if not needle:
            raise CheckError("count needs a substring")
        return ReadCheck(kind=toks[1], ref_id=ref_id, needle=needle, cmp=cmp_, n=n)
    if toks[0] == "count":
        if len(toks) < 4:
            raise CheckError(
                "count check is `count <kind> [with <pred> [and <pred>…]] <cmp> <n>`"
            )
        _check_kind(toks[1], kinds)
        cmp_ = _parse_cmp(toks[-2])
        n = _parse_n(toks[-1])
        _check_cmp_viable(cmp_, n)
        middle = toks[2:-2]
        tags: list[str] = []
        meta_eq: list[tuple[str, str]] = []
        if middle:
            if middle[0] != "with" or len(middle) < 2:
                raise CheckError(
                    f"count predicates start `with`, got {' '.join(middle)!r}; "
                    + _check_example()
                )
            preds = middle[1:]
            if len(preds) % 2 == 0 or preds[1::2] != ["and"] * (len(preds) // 2):
                raise CheckError(
                    "count predicates join with `and`: `with <pred> [and <pred>…]`"
                )
            for tok in preds[::2]:
                _, payload = _parse_pred(tok)
                if isinstance(payload, str):
                    tags.append(payload)
                else:
                    meta_eq.append(payload)
        # A predicate-less count an EMPTY result satisfies reads as a
        # pass on the wrong kind — `count gripe = 0` is "no open gripes"
        # only because nothing anchors it. With a tag=/meta. predicate
        # the zero is the point and stays legal.
        if not tags and not meta_eq and _cmp_ok(0, cmp_, n):
            raise CheckError(
                f"`{cmp_} {n}` is satisfied by an empty count — an "
                "unanchored count passes vacuously on the wrong kind; add "
                "`with tag=<tag>` or `meta.<path>=<value>` to anchor it"
            )
        return CountCheck(
            kind=toks[1], tags=tuple(tags), meta_eq=tuple(meta_eq), cmp=cmp_, n=n
        )
    raise CheckError(
        f"check line starts `read` or `count`, got {toks[0]!r}; " + _check_example()
    )


def _cmp_ok(got: int, cmp_: str, want: int) -> bool:
    return {
        ">=": got >= want,
        "<=": got <= want,
        ">": got > want,
        "<": got < want,
        "!=": got != want,
        "=": got == want,
    }[cmp_]


def _read_check(store: Store, check: ReadCheck) -> tuple[bool, str]:
    ref = store.get_ref(kind=check.kind, id=check.ref_id)
    if ref is None:
        return False, f"{check.kind} id={check.ref_id} not found"
    texts = [ref.title or ""]
    texts.extend(
        c.text for c in store.chunks.list_chunks_for_ref(check.ref_id) if c.ord >= 0
    )
    body = "\n".join(texts)
    if check.cmp is None:
        ok = check.needle in body
        return ok, f"substring {'present' if ok else 'ABSENT'}"
    n = body.count(check.needle)
    ok = _cmp_ok(n, check.cmp, check.n or 0)
    return ok, f"occurrences={n} (want {check.cmp} {check.n})"


def _count_check(store: Store, check: CountCheck) -> tuple[bool, str]:
    clauses = ["r.retired_at IS NULL", "r.kind = %s"]
    params: list[Any] = [check.kind]
    if check.tags:
        frag, params_ = build_tag_filter(list(check.tags), ref_alias="r")
        clauses.append(frag)
        params.extend(params_)
    for path, literal in check.meta_eq:
        clauses.append("r.meta #>> %s = %s")
        params.append("{" + path.replace(".", ",") + "}")
        params.append(literal)
    sql = "SELECT count(*) FROM refs r WHERE " + " AND ".join(clauses)
    with store.pool.connection() as conn, conn.transaction():
        # ``set_config(..., true)`` is ``SET LOCAL`` — the cap dies with
        # this transaction. A cancelled count raises QueryCanceled; the
        # pass reads any check error as ``skipped``, never a verdict.
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{CHECK_TIMEOUT_S}s",),
        )
        row = conn.execute(sql, params).fetchone()
    got = int(row[0]) if row else 0
    ok = _cmp_ok(got, check.cmp, check.n)
    return ok, f"count={got} (want {check.cmp} {check.n})"


def run_check(store: Store, check: Check) -> tuple[bool, str]:
    """Evaluate a parsed check against the store; ``(passed, detail)``.
    ``detail`` is the observed value, quoted into close/refile text."""
    if isinstance(check, ReadCheck):
        return _read_check(store, check)
    return _count_check(store, check)


def gripe_autoclose_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``gripe_autoclose``
    anywhere. Never raises: a missing table or a connection blip reads as
    dark — an unreadable gate must stop the pass, not arm it. Mirrors
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
        log.warning("gripe_autoclose: service_config lookup failed", exc_info=True)
        return False


#: The todo-side fetch predicate — deliberately COARSE (td570413 r2):
#: every live ``fixes`` todo carrying ``meta.fix.check`` surfaces, and
#: ``_evaluable`` decides whether the pass can run it (``STATUS:done``,
#: a newest ``landed:`` comment that is a sha, a parseable line, not a
#: self-read). The finer conditions live ONLY there, so ``_candidates``
#: and ``check_lined_ids`` can never drift apart — a check-lined gripe
#: the pass cannot evaluate reads as validate-closable with its reason.
_TODO_CHECK_LINED = "AND COALESCE(btrim(t.meta->'fix'->>'check'), '') <> '' "

#: The gripe-side exclusions both candidate sources share: a terminal
#: STATUS or a ``hold:no-autoclose`` row, each only while unexpired.
_GRIPE_NOT_CLOSED = (
    "AND NOT EXISTS ("
    "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
    "WHERE rt.ref_id = g.ref_id AND t.namespace = 'STATUS' "
    "AND t.value = ANY(%s) "
    "AND (rt.expires_at IS NULL OR rt.expires_at > now())) "
    "AND NOT EXISTS ("
    "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
    "WHERE rt.ref_id = g.ref_id AND t.namespace = 'OPEN' "
    "AND t.value = %s "
    "AND (rt.expires_at IS NULL OR rt.expires_at > now()))"
)


def _candidate_rows(
    store: Store, ids: Collection[int] | None = None
) -> list[_Candidate]:
    """Every check-lined candidate row — evaluable or not (td570413
    r2: evaluability is ``_evaluable``'s call alone, so every shape it
    can refuse must surface here). Possibly several rows per gripe:
    each live ``fixes`` todo carrying ``meta.fix.check`` (with its live
    ``STATUS:done`` flag and its NEWEST ``landed:`` comment's text),
    oldest first; then the legacy gripe-side ``meta.fix`` line last.
    :func:`_picked` resolves which row owns the gripe.

    Terminal STATUS and ``hold:no-autoclose`` are excluded on the
    gripe, each only while its ``ref_tags`` row is unexpired (the
    ``build_tag_filter`` expiry rule). ``hold:no-autoclose`` parses as
    an OPEN-namespace tag (:data:`HOLD_TAG`), so the match is
    ``(namespace, value)``, not a bare value. ``ids`` scopes the fetch
    (:func:`check_lined_ids`).
    """
    scope = "AND g.ref_id = ANY(%s) " if ids is not None else ""
    params: list[Any] = ([list(ids)] if ids is not None else []) + [
        list(_TERMINAL_STATUS),
        HOLD_TAG,
    ]
    with store.pool.connection() as conn:
        legacy = conn.execute(
            "SELECT g.ref_id, g.title, g.meta->'fix' AS fix "
            "FROM refs g "
            "WHERE g.kind = 'gripe' AND g.retired_at IS NULL "
            + scope
            + "AND COALESCE(btrim(g.meta->'fix'->>'check'), '') <> '' "
            + _GRIPE_NOT_CLOSED
            + " ORDER BY g.ref_id",
            params,
        ).fetchall()
        todos = conn.execute(
            "SELECT g.ref_id, g.title, t.ref_id, t.meta->'fix' AS fix, "
            "EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t2 ON t2.tag_id = rt.tag_id "
            "WHERE rt.ref_id = t.ref_id AND t2.namespace = 'STATUS' "
            "AND t2.value = 'done' "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now())) AS done, "
            "(SELECT c.text FROM chunks c WHERE c.ref_id = t.ref_id "
            " AND c.chunk_kind = 'todo_comment' AND c.retired_at IS NULL "
            " AND c.text LIKE 'landed:%%' ORDER BY c.ord DESC LIMIT 1) "
            "FROM refs g "
            "JOIN links l ON l.dst_ref_id = g.ref_id AND l.relation = 'fixes' "
            "JOIN refs t ON t.ref_id = l.src_ref_id "
            "WHERE g.kind = 'gripe' AND g.retired_at IS NULL "
            + scope
            + "AND t.kind = 'todo' AND t.retired_at IS NULL "
            + _TODO_CHECK_LINED
            + _GRIPE_NOT_CLOSED
            + " ORDER BY g.ref_id, t.ref_id",
            params,
        ).fetchall()
    todo_rows: dict[int, list[_Candidate]] = {}
    for gid, title, tid, fix, done, landed_line in todos:
        todo_rows.setdefault(int(gid), []).append(
            _Candidate(
                gid=int(gid),
                title=str(title or ""),
                fix=dict(fix or {}),
                owner_id=int(tid),
                landed_what=f"td{int(tid)} landed: comment",
                landed_raw=str(landed_line or ""),
                todo_done=bool(done),
            )
        )
    legacy_rows: dict[int, _Candidate] = {
        int(gid): _Candidate(
            gid=int(gid),
            title=str(title or ""),
            fix=dict(fix or {}),
            owner_id=int(gid),
            landed_what="meta.fix.landed",
            landed_raw=str((fix or {}).get("landed") or ""),
        )
        for gid, title, fix in legacy
    }
    # Per gripe: todos oldest-first, the legacy gripe-side line last —
    # the order _picked walks (first EVALUABLE wins; the legacy line is
    # the fallback when no todo is evaluable).
    out: list[_Candidate] = []
    for gid in sorted(set(todo_rows) | set(legacy_rows)):
        out.extend(todo_rows.get(gid, []))
        if gid in legacy_rows:
            out.append(legacy_rows[gid])
    return out


def _evaluable(
    cand: _Candidate, *, kinds: Collection[str] | None
) -> tuple[str | None, str | None]:
    """``(sha, None)`` when this candidate's check line can run, else
    ``(None, why)`` — the ONE judge of evaluability, shared by
    :func:`_candidates` (which drops the row) and :func:`check_lined_ids`
    (which reports the reason), so a check-lined gripe the pass cannot
    evaluate is never skipped by gripe-validate (td570413 r2's ruling:
    such a gripe behaves as check-less and closes on the sha).

    Judges, in order:

    - the **landed sha** — ``_clean_sha`` on the gripe-side
      ``meta.fix.landed``; on the todo path the todo must be
      ``STATUS:done`` and the NEWEST ``landed:`` comment must match
      :data:`_LANDED_RE` (``landed: pending`` after a real sha is no
      record);
    - the **check line** — ``parse_check`` must accept it;
    - **own-ref reads** — ``read <kind> <id>`` of the gripe itself or
      of the todo the line lives on is tautological.
    """
    sha: str | None
    if cand.owner_id == cand.gid:
        from precis.plans.gates import _clean_sha

        sha, bad = _clean_sha(cand.landed_raw, cand.landed_what)
        if bad is not None:
            return None, bad.why
    else:
        if not cand.todo_done:
            return None, f"fixes todo td{cand.owner_id} is not STATUS:done"
        if not cand.landed_raw:
            return None, f"fixes todo td{cand.owner_id} has no landed: comment"
        m = _LANDED_RE.match(cand.landed_raw)
        if m is None:
            return None, (
                f"newest landed: comment on td{cand.owner_id} is not "
                "'landed: <hex-sha>'"
            )
        sha = m.group(1)
    check_line = str(cand.fix.get("check") or "")
    try:
        check = parse_check(check_line, kinds=kinds)
    except (CheckError, PrecisError) as e:
        return None, f"check does not parse ({e})"
    if isinstance(check, ReadCheck) and check.ref_id in (cand.gid, cand.owner_id):
        # A check's read of the ref the line lives on can never be
        # evidence: the gripe always carries whatever the fixer wrote,
        # and the code todo always carries its own landed: comment.
        return None, "check reads its own ref"
    return sha, None


def _picked(
    rows: list[_Candidate], *, kinds: Collection[str] | None
) -> list[tuple[_Candidate, str | None]]:
    """One ``(candidate, None)`` per gripe whose first-evaluable line
    ``_evaluable`` accepts — the walk order is each ``fixes`` todo
    oldest-first, then the legacy gripe-side line (td570413 r3: an
    EVALUABLE newer todo beats an inevaluable older one; when no todo
    is evaluable, an evaluable legacy ``meta.fix`` line stands). When
    nothing is evaluable, ``(first todo row, first refusal reason)``
    — the gripe is validate's to close on the sha, and ``why`` is the
    reason to name."""
    groups: dict[int, list[_Candidate]] = {}
    order: list[int] = []
    for cand in rows:
        if cand.gid not in groups:
            order.append(cand.gid)
            groups[cand.gid] = []
        groups[cand.gid].append(cand)
    out: list[tuple[_Candidate, str | None]] = []
    for gid in order:
        cands = groups[gid]
        why: str | None = None
        for cand in cands:
            sha, w = _evaluable(cand, kinds=kinds)
            if sha is not None:
                cand.sha = sha
                out.append((cand, None))
                break
            if why is None:
                why = w
        else:
            out.append((cands[0], why or "not evaluable"))
    return out


def _candidates(store: Store, *, limit: int) -> list[_Candidate]:
    """The check-lined gripes this pass can actually evaluate, oldest
    first, capped at ``limit``. A row ``_evaluable`` refuses is dropped
    here — not skipped in the loop — so gripe-validate can close that
    gripe on the sha (td570413 r2)."""
    kinds = _kind_slugs(store)
    out: list[_Candidate] = []
    for cand, why in _picked(_candidate_rows(store), kinds=kinds):
        if why is not None:
            log.warning(
                "gripe_autoclose: gr%d check not evaluable — %s; "
                "left for gripe-validate",
                cand.gid,
                why,
            )
            continue
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def check_lined_ids(store: Store, ids: Collection[int]) -> dict[int, str | None]:
    """The check-lined gripes among ``ids``: ``{gid: None}`` when the
    line is evaluable this pass (autoclose owns the close), ``{gid:
    why}`` when a check line exists but ``_evaluable`` refuses it —
    that gripe is validate's to close on the sha, and ``why`` is the
    reason to name (td570413 r2). Empty while the pass is dark —
    the whole mechanism is inert until service_config lights
    :data:`SERVICE_NAME` (td570413 r3)."""
    if not ids or not gripe_autoclose_enabled(store):
        return {}
    kinds = _kind_slugs(store)
    out: dict[int, str | None] = {}
    for cand, why in _picked(_candidate_rows(store, ids=set(ids)), kinds=kinds):
        out[cand.gid] = why
    return out


def _kind_slugs(store: Store) -> set[str] | None:
    """The ``kinds`` table's slug set for :func:`parse_check`'s kind
    gate; ``None`` when unreadable (grammar-only parse — a bogus kind
    then fails on its own query instead of being caught early)."""
    try:
        with store.pool.connection() as conn:
            return {
                str(r[0]) for r in conn.execute("SELECT slug FROM kinds").fetchall()
            }
    except Exception:
        log.warning("gripe_autoclose: kinds table unread", exc_info=True)
        return None


def _chain_depth(store: Store, ref_id: int, *, cap: int) -> int:
    """Ancestors via outbound ``derived-from`` edges to gripe refs —
    "consecutive failures on the same root". Bounded by ``cap``."""
    depth = 0
    seen = {ref_id}
    cur = ref_id
    with store.pool.connection() as conn:
        while depth < cap:
            row = conn.execute(
                "SELECT l.dst_ref_id FROM links l JOIN refs r "
                "ON r.ref_id = l.dst_ref_id "
                "WHERE l.src_ref_id = %s AND l.relation = %s "
                "AND r.kind = 'gripe' AND r.retired_at IS NULL "
                "ORDER BY l.dst_ref_id LIMIT 1",
                (cur, _REFILE_REL),
            ).fetchone()
            if row is None or int(row[0]) in seen:
                break
            seen.add(int(row[0]))
            cur = int(row[0])
            depth += 1
    return depth


def _derived_refile(store: Store, ref_id: int) -> int | None:
    """A live gripe already linked ``derived-from`` THIS one — the refile
    target to reuse instead of filing a duplicate (a close refused after
    an earlier filing would otherwise mint a new triage gripe every
    pass)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT l.src_ref_id FROM links l JOIN refs r "
            "ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND r.kind = 'gripe' AND r.retired_at IS NULL "
            "ORDER BY l.src_ref_id LIMIT 1",
            (ref_id, _REFILE_REL),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _release_label(rec: dict[str, Any]) -> str:
    """``r<N>`` the deploy ran under, else ``prod <head9>``."""
    rn = str(rec.get("release") or "").strip()
    if rn:
        return rn
    head = str(rec.get("head") or "")
    return f"prod {head[:9]}" if head else "prod"


def _parse_ts(raw: object) -> datetime | None:
    """An ISO-8601 timestamp with Z/offset; ``None`` for anything else —
    a missing or naive timestamp can't prove an age."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """One git read in ``repo``; ``None`` on any failure — the
    ``gripe_triage._git`` shape: a missing repo or a bad ref degrades
    the verdict, never kills the pass."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_dir(repo: Path | None) -> Path | None:
    """The checkout ancestry runs in — an explicit ``repo`` when given,
    else ``gripe_triage``'s resolution (``PRECIS_THREADS_DIR``'s
    checkout, else this package's repo root). ``None`` when the answer
    isn't a directory."""
    if repo is not None:
        return repo if repo.is_dir() else None
    env = os.environ.get(_THREADS_ENV)
    td = Path(env) if env else _REPO_ROOT / "docs" / "backlog" / "threads"
    root = td if td.is_dir() else _REPO_ROOT
    return root if root.is_dir() else None


def _ancestor_of(repo: Path, sha: str, head: str) -> bool | None:
    """``git merge-base --is-ancestor <sha> <head>`` — the verdict, or
    ``None`` when git can't say (no repo, a missing object, an error
    exit that isn't the 0/1 verdict pair)."""
    for rev in (sha, head):
        cp = _git(repo, "cat-file", "-e", f"{rev}^{{commit}}")
        if cp is None or cp.returncode != 0:
            return None
    cp = _git(repo, "merge-base", "--is-ancestor", sha, head)
    if cp is None or cp.returncode not in (0, 1):
        return None
    return cp.returncode == 0


def _write_check_failed_at(store: Store, ref_id: int, stamp: datetime | None) -> None:
    """Set or clear ``meta.fix.check_failed_at`` on the ref the check
    bag lives on — the gripe on the legacy path, its code todo on the
    new one (td570413). ``meta_patch`` is a top-level ``meta ||`` merge,
    so the whole ``fix`` dict is rewritten each time — never a bare
    ``{"fix": {"check_failed_at": …}}``, which would drop
    ``landed``/``check``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'fix' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    fix = dict(row[0]) if row is not None and isinstance(row[0], dict) else {}
    if stamp is None:
        fix.pop(_FAILED_AT_KEY, None)
    else:
        fix[_FAILED_AT_KEY] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    store.update_ref(ref_id, meta_patch={"fix": fix})


def _comment(store: Store, ref_id: int, text: str) -> None:
    """A follow-up comment on the (already closed) gripe — best-effort:
    the close is durable, the pointer is for the audit trail. Starts
    ``resolved:`` so the newest-comment close guard still sees the
    resolved shape."""
    from precis.cli.gripe import comment_gripe

    if not comment_gripe(store, ref_id, text):
        log.debug("gripe_autoclose: pointer comment refused for gr%d", ref_id)


def _refile(
    store: Store,
    *,
    old_id: int,
    old_title: str,
    sha: str,
    check_line: str,
    detail: str,
    held: bool,
) -> int | None:
    """File the failure back to triage, linked ``derived-from`` the closed
    gripe; returns the new gripe's ref id (``None`` when the filing
    refused)."""
    from precis.cli.gripe import file_gripe

    first = f"fix at {sha} (gr{old_id}) failed its prod check: {check_line}"
    if held:
        first += f" — auto-close chain cap ({MAX_CHAIN}) reached, a human takes it"
    body = (
        f"{first}\n\n"
        f"- check output: {detail}\n"
        f"- derived-from gr{old_id} (closed with this failure): {old_title}\n"
        f"- the fix's sha is live in prod; the prod check line it carried "
        f"evaluated false"
    )
    tags = ["STATUS:triaged"]
    if held:
        tags.append(HOLD_TAG)
    return file_gripe(store, body, tags=tags, link=f"gripe:{old_id}", rel=_REFILE_REL)


def run_gripe_autoclose_pass(
    store: Store,
    *,
    enabled: bool | None = None,
    limit: int = MAX_PER_PASS,
    now: datetime | None = None,
    repo: Path | None = None,
) -> dict[str, int]:
    """One auto-close pass. Returns ``claimed`` (gripes whose check ran),
    ``ok`` (closed on pass), ``failed`` (closed on a confirmed check
    failure — the refiled gripe carries it back to triage), ``pending``
    (landed sha not deployed yet, or the prod head still inside its
    settle window), ``skipped`` (unparseable check line, a self-read, a
    first failure inside the re-check window, a check error, or a close
    that raised — all retried next fire).

    ``enabled=None`` resolves the ``service_config`` gate (dark until a
    row lights it); ``--only`` passes ``True``. ``now`` is the pass's
    clock (defaults to wall UTC); ``repo`` is the checkout ancestry runs
    in (defaults to the ``PRECIS_THREADS_DIR``/package-root resolution).
    """
    from precis.cli.gripe import resolve_gripe
    from precis.plans.gates import read_prod_head, sha_deployed

    if enabled is None:
        enabled = gripe_autoclose_enabled(store)
    if not enabled:
        log.info(
            "gripe_autoclose: dark — no live service_config row lights %s; no-op",
            SERVICE_NAME,
        )
        return {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}

    when = now if now is not None else datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    rec = read_prod_head(store) or {}
    rel = _release_label(rec) if rec else "prod"
    head_sha = str(rec.get("head") or "").strip().lower() if rec else ""

    cands = _candidates(store, limit=limit)
    out = {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}

    if rec:
        # The head must have stood: a check run against a minutes-old
        # deploy races the rollout and flaps. An unreadable deployed_at
        # can't prove the age either — wait rather than evaluate early.
        deployed_at = _parse_ts(rec.get("deployed_at"))
        min_age = env_int(_MIN_DEPLOY_AGE_ENV, MIN_DEPLOY_AGE_S, lo=0)
        if deployed_at is None or when - deployed_at < timedelta(seconds=min_age):
            log.info(
                "gripe_autoclose: prod head %s — waiting for the %ds settle "
                "window (%d candidates pending)",
                head_sha[:9] or "?",
                min_age,
                len(cands),
            )
            out["pending"] += len(cands)
            return out

    repo_dir = _repo_dir(repo)
    kinds = _kind_slugs(store)

    for cand in cands:
        gid, title, fix = cand.gid, cand.title, cand.fix
        # ``cand.sha`` is the clean landed sha ``_evaluable`` proved —
        # sha shape, parse and the own-ref refusal are all judged there.
        sha = cand.sha
        check_line = str(fix.get("check") or "")
        failed_at = _parse_ts(fix.get(_FAILED_AT_KEY))
        verdict = (
            _ancestor_of(repo_dir, sha, head_sha) if repo_dir and head_sha else None
        )
        if verdict is None:
            # Git can't say (clone-less host, objects missing) — the
            # record's head/landed membership is the degraded verdict.
            verdict = sha_deployed(store, sha).open
        if not verdict:
            out["pending"] += 1
            continue
        try:
            check = parse_check(check_line, kinds=kinds)
        except (CheckError, PrecisError) as e:
            # Defensive only: _evaluable parsed this same line at
            # selection, so a raise here cannot fire — but the pass
            # degrades, never dies, so it stays a skip not a crash.
            log.warning(
                "gripe_autoclose: gr%d check line does not parse (%s); skipped",
                gid,
                e,
            )
            out["skipped"] += 1
            continue
        try:
            passed, detail = run_check(store, check)
        except Exception:
            log.exception("gripe_autoclose: check evaluation failed for gr%d", gid)
            out["skipped"] += 1
            continue
        out["claimed"] += 1
        if passed:
            if failed_at is not None:
                _write_check_failed_at(store, cand.owner_id, None)
            if resolve_gripe(
                store,
                gid,
                f"resolved: {sha} ({rel}) — prod check passed: {check_line}",
            ):
                out["ok"] += 1
            else:
                log.warning("gripe_autoclose: close refused for gr%d", gid)
                out["skipped"] += 1
            continue
        # Failed. The first failure only records the marker — a second
        # failure at least FAIL_RECHECK_S later is the confirmed one.
        if failed_at is None:
            _write_check_failed_at(store, cand.owner_id, when)
            log.info(
                "gripe_autoclose: gr%d check failed (%s) — marker set; "
                "closes on a repeat failure >= %ds later",
                gid,
                detail,
                FAIL_RECHECK_S,
            )
            out["skipped"] += 1
            continue
        if when - failed_at < timedelta(seconds=FAIL_RECHECK_S):
            log.info(
                "gripe_autoclose: gr%d check failed again (%s) but inside "
                "the %ds re-check window — waiting",
                gid,
                detail,
                FAIL_RECHECK_S,
            )
            out["skipped"] += 1
            continue
        # Confirmed failure: close FIRST (a refused close files nothing
        # — that was the hourly-duplicate bug), then refile-or-reuse.
        existing = _derived_refile(store, gid)
        comment = (
            f"resolved: {sha} ({rel}) — prod check FAILED: {check_line} ({detail})"
        )
        comment += (
            f" → refiled as gr{existing}"
            if existing is not None
            else " — a triage refile follows"
        )
        if not resolve_gripe(store, gid, comment):
            log.warning("gripe_autoclose: close refused for gr%d", gid)
            out["skipped"] += 1
            continue
        new_id = existing
        if new_id is None:
            depth = _chain_depth(store, gid, cap=MAX_CHAIN)
            held = depth + 1 >= MAX_CHAIN
            new_id = _refile(
                store,
                old_id=gid,
                old_title=" ".join(title.split())[:120],
                sha=sha,
                check_line=check_line,
                detail=detail,
                held=held,
            )
            if new_id is None:
                log.warning("gripe_autoclose: gr%d closed but its refile refused", gid)
            else:
                _comment(store, gid, f"resolved: refiled as gr{new_id}")
        out["failed"] += 1
        log.info(
            "gripe_autoclose: gr%d check failed (%s) — closed, %s",
            gid,
            detail,
            (
                f"reused refile gr{new_id}"
                if existing is not None
                else (
                    f"refiled gr{new_id}{' (hold:no-autoclose)' if held else ''}"
                    if new_id is not None
                    else "refile refused"
                )
            ),
        )

    log.info(
        "gripe_autoclose: claimed=%d ok=%d failed=%d pending=%d skipped=%d",
        out["claimed"],
        out["ok"],
        out["failed"],
        out["pending"],
        out["skipped"],
    )
    return out


__all__ = [
    "CHECK_TIMEOUT_S",
    "FAIL_RECHECK_S",
    "HOLD_TAG",
    "MAX_CHAIN",
    "MAX_PER_PASS",
    "MIN_DEPLOY_AGE_S",
    "SERVICE_NAME",
    "CheckError",
    "CountCheck",
    "ReadCheck",
    "check_lined_ids",
    "gripe_autoclose_enabled",
    "parse_check",
    "run_check",
    "run_gripe_autoclose_pass",
]
