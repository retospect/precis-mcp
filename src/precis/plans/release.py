"""Release-pass plumbing for the dark release ops (td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven".
The ``release-pass`` template (:func:`precis.plans.templates.mint_release_pass`)
mints cut -> poll -> gate ``tests-green`` -> approval ask -> deploy; the three
mechanical ops (``release_cut``, ``ci_verdict_poll``, ``release_deploy``) share
what lives here:

- **The cut's facts flow through the plan root.** ``release_cut`` parses
  ``RELEASE_CUT: r<N> <sha>`` / ``RELEASE_RANGE: <prev>..<sha>`` from
  ``scripts/release --cut-only`` and calls :func:`record_cut`, which stamps
  ``meta.release = {rN, sha, range, repo, host, checkout, cut_at}`` on the plan root and
  rewrites the plan's open ``tests-green`` gates and loop ``until`` specs
  from the :data:`PENDING_RANGE` placeholder to the real range, and the
  approval ask's options to ``['deploy r<N>', 'hold']``. The later ops read
  :func:`release_of` with their own ``todo_id``.
- **CI verdict and prod head writers**, store-side twins of
  ``scripts/release``'s ``record_ci_verdict`` / ``record_prod_head`` (the
  script cannot import this package; the row shapes are the same, so the
  gates in :mod:`precis.plans.gates` read either writer's rows).
- **The approval check** (:func:`approval`): the deploy runs only when the
  approval ask that blocks it carries an ``answer:`` comment naming
  ``deploy r<N>`` for the release the plan cut. The ``answer`` gate opens on
  *any* answer (``hold`` included), so this check is what keeps a deploy
  Reto's call (authority.md: release/deploy are Reto-only).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.plans.gates import (
    CI_VERDICTS_KEY,
    PROD_HEAD_KEY,
    read_ci_verdicts,
    read_prod_head,
)

if TYPE_CHECKING:
    from precis.store import Store

#: ``tests-green`` range of a release-pass gate minted before its cut (a
#: valid sha shape that matches no commit: the gate stays closed).
PENDING_RANGE = "0000000"
#: The approval ask's options; ``r<N>`` is filled in at the cut.
APPROVE_OPTION = "deploy {rn}"
HOLD_OPTION = "hold"
#: Abbreviation length of recorded shas (``scripts/release.LANDED_SHA_LEN``).
LANDED_SHA_LEN = 12
#: Caps, the same as ``scripts/release``'s.
MAX_CI_VERDICTS = 500
MAX_LANDED = 2000

#: The todo's comment chunk kind (``handlers.todo._COMMENT_KIND``; restated).
_COMMENT_KIND = "todo_comment"
_CLOSED = frozenset({"done", "won't-do", "cancelled"})

CUT_RE = re.compile(r"^RELEASE_CUT: (r\d+) ([0-9a-f]{7,40})\s*$", re.MULTILINE)
RANGE_RE = re.compile(r"^RELEASE_RANGE: (\S+)\s*$", re.MULTILINE)
DEPLOYED_RE = re.compile(
    r"^RELEASE_DEPLOYED: (r\d+) ([0-9a-f]{7,40}) ([0-9a-f]{7,40}|-)\s*$",
    re.MULTILINE,
)
_RN_RE = re.compile(r"^r\d+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REMOTE_RE = re.compile(
    r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_from_remote(url: str) -> str | None:
    """``owner/repo`` from a GitHub remote url (ssh or https), else ``None``."""
    m = _REMOTE_RE.search(url.strip())
    return m.group(1) if m else None


def valid_repo(repo: object) -> str | None:
    """``repo`` when it is an ``owner/repo`` slug, else ``None``."""
    if isinstance(repo, str) and _REPO_RE.fullmatch(repo.strip()):
        return repo.strip()
    return None


# ── parse the script's result lines ────────────────────────────────


def parse_cut(text: str) -> tuple[str, str, str] | None:
    """``(rN, sha, range)`` from ``scripts/release --cut-only`` output; the
    range defaults to the sha when the script printed none."""
    m = CUT_RE.search(text)
    if m is None:
        return None
    rn, sha = m.group(1), m.group(2)
    r = RANGE_RE.search(text)
    return rn, sha, r.group(1) if r else sha


def parse_deployed(text: str) -> tuple[str, str, str] | None:
    """``(rN, head, prev)`` from ``--deploy-only`` output (``prev`` is ''
    when prod had no head)."""
    m = DEPLOYED_RE.search(text)
    if m is None:
        return None
    prev = m.group(3)
    return m.group(1), m.group(2), "" if prev == "-" else prev


# ── plan reads and writes ──────────────────────────────────────────


def plan_root(store: Store, todo_id: int) -> int:
    """The id in ``todo_id``'s ``plan:<id>`` tag; a todo without one is its
    own root."""
    for tag in store.tags_for(todo_id):
        value = str(tag.value)
        if value.startswith("plan:") and value[5:].isdigit():
            return int(value[5:])
    return todo_id


def release_of(store: Store, todo_id: int) -> dict[str, Any] | None:
    """The plan root's ``meta.release`` for a node of the plan, or ``None``
    before the cut has recorded one."""
    ref = store.get_ref(kind="todo", id=plan_root(store, todo_id))
    rel = (ref.meta or {}).get("release") if ref is not None else None
    if not isinstance(rel, dict) or not _RN_RE.match(str(rel.get("rN") or "")):
        return None
    return rel


def pinned_host(store: Store, todo_id: int) -> str | None:
    """The node the plan's cut ran on (``meta.release.host``), else ``None``."""
    rel = release_of(store, todo_id)
    host = str((rel or {}).get("host") or "").strip()
    return host or None


def pin_refusal(
    rel: dict[str, Any] | None, *, host: str | None, checkout: str | None = None
) -> str | None:
    """Why the deploy may not run on ``host`` / ``checkout``, or ``None``.

    ``cut`` and ``deploy`` share one checkout (the ``.release-owner``
    marker ``scripts/release`` leaves there makes it the release's owner),
    so a deploy off the recorded host or checkout is refused. A cut that
    recorded neither (older plans) pins nothing; ``checkout`` ``None``
    skips the checkout check (the tick that mints the job is not the
    worker that runs it)."""
    if not rel:
        return None
    want_host = str(rel.get("host") or "").strip()
    if want_host and host != want_host:
        return (
            f"the cut ran on {want_host!r}, not {host or 'an unpinned worker'!r}; "
            f"next: run the deploy on {want_host!r} (params.target_node={want_host!r})"
        )
    want_checkout = str(rel.get("checkout") or "").strip()
    if want_checkout and checkout is not None and checkout != want_checkout:
        return (
            f"the cut ran in checkout {want_checkout!r}, not {checkout!r}; "
            f"next: point PRECIS_SLICE_ROOT at {want_checkout!r} on "
            f"{want_host or 'the cut host'!r}"
        )
    return None


def _plan_nodes(store: Store, root: int) -> list[tuple[int, dict[str, Any], str]]:
    """``(id, meta, status)`` of every live todo tagged ``plan:<root>``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                              AND t.value = %s)
             ORDER BY r.ref_id
            """,
            (f"plan:{root}",),
        ).fetchall()
    return [(int(i), dict(m or {}), str(s or "open")) for i, m, s in rows]


def _tests_green(spec: object) -> bool:
    return isinstance(spec, dict) and spec.get("kind") == "tests-green"


def approval_options(rn: str) -> list[str]:
    return [APPROVE_OPTION.format(rn=rn), HOLD_OPTION]


def record_cut(
    store: Store,
    todo_id: int,
    rn: str,
    sha: str,
    rng: str,
    *,
    repo: str | None = None,
    host: str | None = None,
    checkout: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Stamp the cut on ``todo_id``'s plan (module docstring); returns one
    note per node written. ``host`` / ``checkout`` are the node and repo
    root the cut ran in; the deploy must run in the same pair
    (:func:`pin_refusal`)."""
    root = plan_root(store, todo_id)
    when = _stamp(now or datetime.now(UTC))
    rel: dict[str, Any] = {"rN": rn, "sha": sha, "range": rng, "cut_at": when}
    if valid_repo(repo):
        rel["repo"] = repo
    if host:
        rel["host"] = host
    if checkout:
        rel["checkout"] = checkout
    store.stamp_ref_meta(root, {"release": rel})
    notes = [f"td{root}: meta.release = {rn} {sha[:9]} ({rng})"]
    new_gate = {"kind": "tests-green", "args": {"range": rng}}
    for node_id, meta, status in _plan_nodes(store, root):
        if status in _CLOSED or node_id == root:
            continue
        updates: dict[str, Any] = {}
        if _tests_green(meta.get("gate")):
            updates["gate"] = new_gate
            updates["gate_result"] = None
        loop = meta.get("loop")
        if isinstance(loop, dict) and _tests_green(loop.get("until")):
            updates["loop"] = {**loop, "until": new_gate}
        ask = meta.get("ask")
        if isinstance(ask, dict) and ask.get("kind") == "approval":
            updates["ask"] = {
                **ask,
                "options": approval_options(rn),
                "context": f"release/{rn} at {sha[:9]} ({rng}); green CI gates this ask",
            }
        if updates:
            store.stamp_ref_meta(node_id, updates)
            notes.append(f"td{node_id}: {', '.join(sorted(updates))} -> {rn}")
    return notes


def _newest_answer(store: Store, todo_id: int) -> str | None:
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == _COMMENT_KIND and str(c.text or "").strip()
    ]
    answers = [
        c for c in comments if str(c.text).lstrip().lower().startswith("answer:")
    ]
    if not answers:
        return None
    newest = max(answers, key=lambda c: (c.ord, c.id))
    return str(newest.text).lstrip()[len("answer:") :].strip()


def _approval_asks(store: Store, todo_id: int) -> list[int]:
    """The ``blocked-by`` targets of ``todo_id`` whose ``meta.ask.kind`` is
    ``approval``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.ref_id FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = %s
               AND b.retired_at IS NULL AND b.meta -> 'ask' ->> 'kind' = 'approval'
             ORDER BY b.ref_id
            """,
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def approval(store: Store, todo_id: int) -> tuple[str | None, str]:
    """``(rN, why)``: ``rN`` is the release the deploy todo ``todo_id`` may
    deploy, or ``None`` with the reason it may not. Approved means: the
    plan recorded a cut, an approval ask blocks ``todo_id``, and every such
    ask's newest ``answer:`` comment names ``deploy <rN>`` of that cut."""
    rel = release_of(store, todo_id)
    if rel is None:
        return None, f"no cut recorded on td{plan_root(store, todo_id)} (meta.release)"
    rn = str(rel["rN"])
    asks = _approval_asks(store, todo_id)
    if not asks:
        return None, f"no approval ask blocks td{todo_id}; a deploy needs one"
    want = APPROVE_OPTION.format(rn=rn).lower()
    for ask_id in asks:
        answer = _newest_answer(store, ask_id)
        if answer is None:
            return None, f"approval td{ask_id} has no answer: comment yet"
        text = answer.lower()
        if want not in text or HOLD_OPTION in text.split():
            return None, (
                f"approval td{ask_id} answered {answer[:80]!r}, not {want!r}; "
                "nothing deploys"
            )
    return rn, f"approved: {want} (td{', td'.join(str(a) for a in asks)})"


# ── verdict and prod-head writers (store twins of scripts/release) ──


def ci_verdict_rows(
    sha: str,
    rng: str,
    verdict: str,
    run_id: str,
    previous: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """``previous`` plus ``{sha, range, verdict, run_id, at}``, deduped by
    sha keeping the newest, capped (``scripts/release.ci_verdict_rows``)."""
    row = {
        "sha": sha[:LANDED_SHA_LEN],
        "range": rng,
        "verdict": verdict,
        "run_id": str(run_id or ""),
        "at": _stamp(now),
    }
    kept = [r for r in previous if str(r.get("sha", ""))[:LANDED_SHA_LEN] != row["sha"]]
    return [*kept, row][-MAX_CI_VERDICTS:]


def record_ci_verdict(
    store: Store,
    sha: str,
    rng: str,
    verdict: str,
    run_id: str = "",
    *,
    now: datetime | None = None,
) -> str:
    """Append a verdict row to ``release.ci_verdicts``; one status line."""
    from precis import settings

    if not sha:
        return "ci verdict not recorded: no sha"
    if verdict not in ("green", "red"):
        return f"ci verdict not recorded: verdict {verdict!r} is not green|red"
    settings.invalidate(CI_VERDICTS_KEY)  # read-modify-write: skip the cache
    rows = ci_verdict_rows(
        sha, rng, verdict, run_id, read_ci_verdicts(store), now or datetime.now(UTC)
    )
    settings.set_setting(
        CI_VERDICTS_KEY, json.dumps(rows, separators=(",", ":")), store=store
    )
    return f"ci verdict recorded: {sha[:9]} {verdict}"


def prod_head_record(
    head: str,
    landed: list[str],
    previous: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """``{head, deployed_at, landed}`` (``scripts/release.prod_head_record``)."""
    merged: list[str] = []
    seen: set[str] = set()
    old = [str(previous["head"])[:LANDED_SHA_LEN]] if previous else []
    old += [str(s) for s in (previous or {}).get("landed") or []]
    for s in [*landed, *old]:
        key = s[:LANDED_SHA_LEN]
        if key and key not in seen:
            seen.add(key)
            merged.append(key)
    return {"head": head, "deployed_at": _stamp(now), "landed": merged[:MAX_LANDED]}


def record_prod_head(
    store: Store,
    head: str,
    landed: list[str],
    *,
    now: datetime | None = None,
) -> str:
    """Merge ``head`` + ``landed`` into ``release.prod_head``; one status line."""
    from precis import settings

    if not head:
        return "prod head not recorded: no head sha"
    settings.invalidate(PROD_HEAD_KEY)
    rec = prod_head_record(
        head, landed, read_prod_head(store), now or datetime.now(UTC)
    )
    settings.set_setting(
        PROD_HEAD_KEY, json.dumps(rec, separators=(",", ":")), store=store
    )
    return f"prod head recorded: {head[:9]} (+{len(landed)} landed)"


__all__ = [
    "APPROVE_OPTION",
    "HOLD_OPTION",
    "PENDING_RANGE",
    "approval",
    "approval_options",
    "ci_verdict_rows",
    "parse_cut",
    "parse_deployed",
    "pin_refusal",
    "pinned_host",
    "plan_root",
    "prod_head_record",
    "record_ci_verdict",
    "record_cut",
    "record_prod_head",
    "release_of",
    "repo_from_remote",
    "valid_repo",
]
