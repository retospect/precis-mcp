"""The release pass owns inherited CI reds until its fixes reach main."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis import settings
from precis.plans import release
from precis.todo import TERMINAL_TODO_STATUSES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from precis.store import Store

TESTS_KEY = "release_test_failures"
CARRIED_KEY = "release_tests_carried"
# The CI watcher also reports infra and collection failures. Only actual
# pytest test identities can be matched to the release digest.
TEST_GRIPE_SQL = """
    r.meta->'filed_by'->>'principal' = 'worker:ci_watch'
    AND r.meta->'ci'->>'wf' = 'nightly'
    AND starts_with(r.meta->'ci'->>'sig', 'ci:check.yml:tests/')
    AND strpos(r.meta->'ci'->>'sig', '::') > 0
"""


def note_failures(
    store: Store, todo_id: int, rn: str, ids: Iterable[str], *, complete: bool
) -> None:
    """Persist digest identities before the fixer briefs become runnable."""
    from precis.workers.job_types.ci_watch import _test_signature

    root = release.plan_root(store, todo_id)
    with store.atomic() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s FOR UPDATE", (root,)
        ).fetchone()
        if not row or (row[0].get("release") or {}).get("rN") != rn:
            return
        previous = row[0].get(TESTS_KEY) or {}
        if previous.get("rN") != rn:
            previous = {}
        signatures = set(previous.get("signatures", ()))
        signatures.update(_test_signature(node) for node in ids if "::" in node)
        store.stamp_ref_meta(
            root,
            {
                TESTS_KEY: {
                    "rN": rn,
                    "signatures": sorted(signatures),
                    # A later digest cannot recover IDs missed by an earlier one.
                    "complete": complete and bool(previous.get("complete", True)),
                }
            },
            conn=conn,
        )


def carried_receipt(
    store: Store, meta: dict[str, Any], rel: dict[str, Any], head: str
) -> dict[str, Any] | None:
    """Only an actual post-cut head with an unwaived green verdict proves carry."""
    failures = meta.get(TESTS_KEY) or {}
    cut = str(rel.get("cut_sha") or rel.get("sha") or "").lower()
    if (
        not cut
        or head.startswith(cut)
        or failures.get("rN") != rel["rN"]
        or not failures.get("signatures")
    ):
        return None
    # The CI poll may run in another worker process. This write is the
    # handoff to main intake, so it needs the latest persisted verdict.
    settings.invalidate(release.CI_VERDICTS_KEY)
    verdict = release._newest_sha_verdict(store, head[: release.LANDED_SHA_LEN])
    if not verdict or verdict.get("verdict") != "green" or verdict.get("waived"):
        return None
    at = verdict.get("at")
    if not isinstance(at, str):
        return None
    try:
        if datetime.fromisoformat(at).tzinfo is None:
            return None
    except ValueError:
        return None
    return {**failures, "head": head, "green_at": at}


@dataclass(frozen=True)
class Carried:
    rn: str
    head: str
    green_at: datetime

    @property
    def note(self) -> str:
        return f"carried by release/{self.rn} into main at {self.head[:9]}"


@dataclass
class Ownership:
    pending_all: bool
    pending: set[str]
    carried: dict[str, Carried]

    def holds(self, sig: str) -> bool:
        return self.pending_all or sig in self.pending


def ownership(store: Store) -> Ownership:
    """Read active reservations and durable receipts together, across restarts."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.meta->'release', r.meta->%s, r.meta->%s,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r JOIN ref_tags rp ON rp.ref_id = r.ref_id
              JOIN tags p ON p.tag_id = rp.tag_id
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND p.namespace = 'OPEN' AND p.value = 'release-pass'
               AND r.meta->'release'->>'rN' IS NOT NULL
             ORDER BY r.ref_id
            """,
            (TESTS_KEY, CARRIED_KEY),
        ).fetchall()
    state = Ownership(False, set(), {})
    for rel, tests, receipt, status in rows:
        if receipt and receipt["rN"] == rel["rN"]:
            fix = Carried(
                receipt["rN"],
                receipt["head"],
                datetime.fromisoformat(receipt["green_at"]),
            )
            state.carried.update(dict.fromkeys(receipt["signatures"], fix))
        if status in TERMINAL_TODO_STATUSES or (rel.get("merge_back") or {}).get(
            "state"
        ) in {"merged", "noop"}:
            continue
        if tests and tests["rN"] == rel["rN"]:
            state.pending.update(tests["signatures"])
            state.pending_all |= not tests.get("complete", False)
        else:
            # Until the cut is judged green or its red is digested, any
            # nightly test may be one the release has inherited.
            sha = str(rel.get("sha") or "")[: release.LANDED_SHA_LEN].lower()
            verdict = release._newest_sha_verdict(store, sha)
            if (
                not verdict
                or verdict.get("verdict") != "green"
                or verdict.get("waived")
            ):
                state.pending_all = True
    return state


def _gripes(
    store: Store, *, root: int | None = None
) -> list[tuple[int, str, datetime]]:
    """The latest report time prevents an old carry closing a fresh regression."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, r.meta->'ci'->>'sig', GREATEST(r.created_at,
                   (SELECT MAX(c.created_at) FROM chunks c WHERE c.ref_id = r.ref_id
                      AND c.chunk_kind = 'gripe_comment' AND c.text LIKE 'seen again:%%'))
              FROM refs r
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL AND ({TEST_GRIPE_SQL})
               AND (%s::bigint IS NULL OR EXISTS (
                    SELECT 1 FROM links l WHERE l.src_ref_id = %s
                      AND l.dst_ref_id = r.ref_id AND l.relation = 'related-to'
                      AND 1 = (SELECT count(*) FROM links other
                               JOIN refs source ON source.ref_id = other.dst_ref_id
                               WHERE other.src_ref_id = l.src_ref_id
                                 AND other.relation = 'related-to'
                                 AND source.kind IN ('gripe', 'todo')
                                 AND source.retired_at IS NULL)))
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                    WHERE rt.ref_id = r.ref_id AND t.namespace IN ('OPEN', 'FLAG')
                      AND t.value = 'hold:no-autoclose')
            """,
            (root, root),
        ).fetchall()
    return [(int(gid), sig, at) for gid, sig, at in rows]


def _matching_carry(state: Ownership, sig: str, at: datetime) -> Carried | None:
    fix = state.carried.get(sig)
    return fix if fix is not None and at <= fix.green_at else None


def plan_disposition(
    store: Store, root: int, state: Ownership | None = None
) -> tuple[bool, Carried | None]:
    """Whether the single-gripe plan waits, or can close as carried."""
    gripes = _gripes(store, root=root)
    if not gripes:
        return False, None
    _, sig, at = gripes[0]
    if state is None:
        state = ownership(store)
    return state.holds(sig), _matching_carry(state, sig, at)


def resolve_carried_gripes(store: Store, state: Ownership) -> None:
    from precis.workers.job_types.land_slice import _resolve_gripe

    if not state.carried:
        return
    for gid, sig, at in _gripes(store):
        fix = _matching_carry(state, sig, at)
        if fix is not None:
            _resolve_gripe(store, gid, fix.head, fix.note, status="auto_verify")
