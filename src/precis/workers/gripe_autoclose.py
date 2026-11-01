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

The check line is a small closed grammar, no LLM anywhere:

- ``read <kind> <id> contains "<substring>"`` — the MCP-read shape: the
  ref's title + chunk text carries the substring.
- ``read <kind> <id> count "<substring>" <cmp> <n>`` — occurrence count.
- ``count <kind> [with <pred> [and <pred>…]] <cmp> <n>`` — a read-only
  SQL count over live refs; ``pred`` is ``tag=<tag>`` or
  ``meta.<dotted.path>=<literal>``; ``<cmp>`` is ``= != > < >= <=``.

One pass (the hourly ``gripe_autoclose`` scheduler cadence; the lease is
the fleet-singleton throttle, same as ``citer_watch``):

1. **Candidates**: live gripes with ``meta.fix.landed`` AND a non-empty
   ``meta.fix.check``, minus terminal STATUS (``done``/``wontfix``) and
   the ``hold:no-autoclose`` never-close mark (gr547015), oldest first,
   capped at :data:`MAX_PER_PASS`.
2. **Deployed?** the sha must be in the recorded prod set —
   :func:`precis.plans.gates.sha_deployed` reads the same
   ``release.prod_head`` record ``sha-in-prod`` does (the server has no
   git; the record's ``head`` + accumulated ``landed`` shas *are*
   "deployed"). Not yet → ``pending``, retried next fire.
3. **Run the check.** Pass → ``resolved: <sha> (<rN|prod head9>) — prod
   check passed: <line>`` + ``STATUS:done`` through the gripe handler,
   so the ``resolved:`` guard and close cascade run exactly as on a
   manual close. Fail → the gripe still closes (``resolved: … prod
   check FAILED … → refiled as gr<new>``) and a NEW gripe is filed back
   to ``STATUS:triaged``, linked ``derived-from`` the old one, first
   line naming the sha and the observed check output. At
   :data:`MAX_CHAIN` consecutive failures on one root (the derived-from
   ancestry) the refiled gripe carries ``hold:no-autoclose`` — the cap
   hands the chain to a human instead of looping forever.

A malformed check line (hand-edited meta — ``edit(mode='fix')`` refuses
them) is logged and skipped, never closed on. Idempotent: a closed
gripe leaves the candidate set, so a re-run is a no-op.

**Ships dark** — the ``citer_watch`` shape: the cadence fires and the
lease advances, but the pass no-ops until a ``service_config`` row
lights ``gripe_autoclose`` (``prio > 0``) —
``precis service prio '*' gripe_autoclose 1``. A one-off manual run is
``precis worker --only gripe_autoclose --once`` (``enabled=True``, the
operator escape hatch). Every read is local SQL; the only writes are
the gripe close + the refiled gripe.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.errors import PrecisError
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store.store import Store

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
#: the literal is the contract either side reads).
HOLD_TAG = "hold:no-autoclose"

#: The refile link's relation: new gripe ``derived-from`` the closed one.
_REFILE_REL = "derived-from"

_CMPS: tuple[str, ...] = (">=", "<=", "!=", "=", ">", "<")
_TERMINAL_STATUS = ("done", "wontfix")
_META_PATH_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


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


def parse_check(text: str) -> Check:
    """Parse one ``meta.fix.check`` line; raise :class:`CheckError` on any
    deviation — the closed grammar is the whole safety story."""
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
        needle = " ".join(toks[4:-2])
        if not needle:
            raise CheckError("count needs a substring")
        return ReadCheck(kind=toks[1], ref_id=ref_id, needle=needle, cmp=cmp_, n=n)
    if toks[0] == "count":
        if len(toks) < 4:
            raise CheckError(
                "count check is `count <kind> [with <pred> [and <pred>…]] <cmp> <n>`"
            )
        cmp_ = _parse_cmp(toks[-2])
        n = _parse_n(toks[-1])
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
                kind, payload = _parse_pred(tok)
                if kind == "tag":
                    tags.append(str(payload))
                else:
                    meta_eq.append(payload)  # type: ignore[arg-type]
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
    with store.pool.connection() as conn:
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


def _candidates(store: Store, *, limit: int) -> list[tuple[int, str, str, str]]:
    """``(ref_id, title, landed sha, check line)`` for live gripe-loop
    gripes carrying a check line — terminal STATUS and
    ``hold:no-autoclose`` excluded."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   r.meta->'fix'->>'landed' AS landed,
                   r.meta->'fix'->>'check'  AS check
              FROM refs r
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND COALESCE(btrim(r.meta->'fix'->>'landed'), '') <> ''
               AND COALESCE(btrim(r.meta->'fix'->>'check'), '') <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      AND t.value = ANY(%s))
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.value = %s)
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (list(_TERMINAL_STATUS), HOLD_TAG, limit),
        ).fetchall()
    return [(int(r[0]), str(r[1] or ""), str(r[2]), str(r[3])) for r in rows]


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


def _release_label(rec: dict[str, Any]) -> str:
    """``r<N>`` the deploy ran under, else ``prod <head9>``."""
    rn = str(rec.get("release") or "").strip()
    if rn:
        return rn
    head = str(rec.get("head") or "")
    return f"prod {head[:9]}" if head else "prod"


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
) -> dict[str, int]:
    """One auto-close pass. Returns ``claimed`` (gripes whose check ran),
    ``ok`` (closed on pass), ``failed`` (closed on check failure — the
    refiled gripe carries it back to triage), ``pending`` (landed sha not
    yet in the recorded prod set), ``skipped`` (unparseable check line or
    a close that raised — retried next fire).

    ``enabled=None`` resolves the ``service_config`` gate (dark until a
    row lights it); ``--only`` passes ``True``.
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

    rec = read_prod_head(store) or {}
    rel = _release_label(rec) if rec else "prod"

    out = {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}
    for gid, title, landed, check_line in _candidates(store, limit=limit):
        verdict = sha_deployed(store, landed)
        if not verdict.open:
            out["pending"] += 1
            continue
        try:
            check = parse_check(check_line)
        except (CheckError, PrecisError) as e:
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
            if resolve_gripe(
                store,
                gid,
                f"resolved: {landed} ({rel}) — prod check passed: {check_line}",
            ):
                out["ok"] += 1
            else:
                log.warning("gripe_autoclose: close refused for gr%d", gid)
                out["skipped"] += 1
            continue
        depth = _chain_depth(store, gid, cap=MAX_CHAIN)
        held = depth + 1 >= MAX_CHAIN
        new_id = _refile(
            store,
            old_id=gid,
            old_title=" ".join(title.split())[:120],
            sha=landed,
            check_line=check_line,
            detail=detail,
            held=held,
        )
        if new_id is None:
            log.warning("gripe_autoclose: refile refused for gr%d; not closing", gid)
            out["skipped"] += 1
            continue
        if not resolve_gripe(
            store,
            gid,
            f"resolved: {landed} ({rel}) — prod check FAILED: {check_line} "
            f"({detail}) → refiled as gr{new_id}",
        ):
            log.warning("gripe_autoclose: close refused for gr%d", gid)
            out["skipped"] += 1
            continue
        out["failed"] += 1
        log.info(
            "gripe_autoclose: gr%d check failed (%s) — closed, refiled gr%d%s",
            gid,
            detail,
            new_id,
            " (hold:no-autoclose)" if held else "",
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
    "HOLD_TAG",
    "MAX_CHAIN",
    "MAX_PER_PASS",
    "SERVICE_NAME",
    "CheckError",
    "CountCheck",
    "ReadCheck",
    "gripe_autoclose_enabled",
    "parse_check",
    "run_check",
    "run_gripe_autoclose_pass",
]
