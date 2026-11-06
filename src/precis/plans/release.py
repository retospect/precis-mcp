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
  :func:`release_of` with their own ``todo_id``. The ``release-fix`` loop's
  re-cut is a second ``release_cut`` (td543855): the same call overwrites
  ``meta.release`` whole, rewrites the still-open gate / loop / ask to the
  new range and r<N>, and reopens a ``tests-green`` gate an earlier cut's
  range had opened -- that verdict does not cover the new cut.
- **CI verdict and prod head writers**, store-side twins of
  ``scripts/release``'s ``record_ci_verdict`` / ``record_prod_head`` (the
  script cannot import this package; the row shapes are the same, so the
  gates in :mod:`precis.plans.gates` read either writer's rows).
- **The shared round record** (:func:`record_round` / :func:`read_round`),
  store-side twins of ``scripts/round``'s publish: ``release.round`` holds
  the active release's row (``{n, branch, base, state, owner, cut_at,
  deploy?}``) so a second clone and the release loop read the same
  lifecycle state the coordinator's ``round.json`` tracks.
  :func:`update_round` is the release job types' write path (gr575362):
  ``release_cut`` creates the row (stamping just ``pass`` on a same-n
  ``cut`` row the script published first), ``release_deploy`` merges it
  forward to ``deployed``, and ``release_poll``'s dropped-pass abandon
  takes the same merge — a rank-ordered move that never lowers a state,
  lets a job-side terminal write take a rank-0 row only, never touches
  a higher ``n`` and never overwrites an unreadable row.
  :func:`pinned_deploy_sha` resolves the sha the runtime must serve (the
  round row's ``deploy.sha`` for this release, else the prod head);
  :func:`record_confirm` merges the runtime confirmation into the row
  (stamping ``state_at``) and
  ``meta.release.confirmed``, deferring to the script's own publish where
  it wrote first (td573432). ``release_cut`` also reads the row before
  cutting: :func:`round_confirmed` is the proof prod moved past the
  clone's own stale local record (``confirmed`` at its number or later —
  only ``confirmed`` means the release actually rolled out), and
  :func:`live_deploy_job` is the store-wide live ``release_deploy``
  check that blocks the local supersede.
- **The approval check** (:func:`approval`): the deploy runs only when the
  approval ask that blocks it carries an ``answer:`` comment naming
  ``deploy r<N>`` for the release the plan cut. The ``answer`` gate opens on
  *any* answer (``hold`` included), so this check is what keeps a deploy
  Reto's call (authority.md: release/deploy are Reto-only). The answer is
  Reto's own comment, or the tick's under his standing approval
  (me573231): with ``release.auto_deploy`` on, :func:`auto_answer_approval`
  posts the ``deploy r<N>`` answer itself once CI is recorded green on the
  cut's exact sha and ``release.round`` names an open round on that head
  sha — fail closed on any doubt, an existing answer never overwritten.
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
from precis.principal import worker_comment_meta
from precis.store.types import ChunkInsert, Tag
from precis.utils import handle_registry

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
        if node_id == root:
            continue
        if status == "done" and _tests_green(meta.get("gate")):
            if meta["gate"] != new_gate:
                # Opened on an earlier cut's range: that verdict says nothing
                # about this cut, so the gate closes again over the new one.
                store.stamp_ref_meta(node_id, {"gate": new_gate, "gate_result": None})
                store.add_tag(
                    node_id,
                    Tag.closed("STATUS", "open"),
                    set_by="system",
                    replace_prefix=True,
                )
                notes.append(f"td{node_id}: gate reopened -> {rn}")
            continue
        if status in _CLOSED:
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


# ── the standing approval (release.auto_deploy, td573809) ─────────

#: The DB-only setting arming the auto-answer (``precis.settings.REGISTRY``).
_AUTO_DEPLOY_KEY = "release.auto_deploy"

#: ``release.round`` states that let the tick answer for the deploy — the
#: open-release lifecycle (``scripts/lib/round_state.py::OPEN_STATES``,
#: ``scripts/round._OPEN_STATES``; a test pins them equal). A partial
#: rollout (``deploying``/``deployed``) and every terminal state refuse.
_ROUND_OPEN_STATES = frozenset({"cut", "gating", "fixing", "green"})


def _round_raw(store: Store) -> str | None:
    """The ``app_settings`` row for ``release.round`` — raises on a
    database error (``settings._db_read`` swallows one as ``None``, which
    here would read as "no round" and could arm a deploy)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s",
            (ROUND_KEY,),
        ).fetchone()
    return None if row is None else str(row[0])


def _blocks_release_deploy(store: Store, blocks: object) -> bool:
    """``ask.blocks`` names a live todo whose ``meta.op`` is
    ``release_deploy`` — the auto-answer only ever speaks for that ask."""
    if not isinstance(blocks, str):
        return False
    parsed = handle_registry.parse(blocks)
    if parsed is None or parsed[0] != "todo" or parsed[1]:
        return False
    ref = store.get_ref(kind="todo", id=parsed[2])
    return ref is not None and (ref.meta or {}).get("op") == "release_deploy"


def _newest_sha_verdict(store: Store, sha12: str) -> dict[str, Any] | None:
    """The newest ``release.ci_verdicts`` row on ``sha12`` — the cut's
    exact sha only: no range equality and no prod-head fallback (the
    ``tests-green`` gate allows both; an unattended deploy needs the
    verdict recorded on the sha it will deploy)."""
    best: tuple[str, int] | None = None
    pick: dict[str, Any] | None = None
    for i, row in enumerate(read_ci_verdicts(store)):
        if str(row.get("sha") or "").lower()[:LANDED_SHA_LEN] != sha12:
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, pick = key, row
    return pick


def _open_round_record(
    store: Store, rn: str, sha12: str
) -> tuple[dict[str, Any] | None, str]:
    """``(row, "")`` when ``release.round`` names an open round on
    ``sha12``, else ``(None, <refusal>)``. Unlike ``release_poll`` — where
    absent means no round — an absent record refuses the deploy answer:
    at approval time a round must be open and name this release's head
    sha (me573231)."""
    try:
        raw = _round_raw(store)
    except Exception as exc:
        return None, f"{ROUND_KEY} unreadable ({type(exc).__name__})"
    if not raw or not raw.strip():
        return None, f"no open round: {ROUND_KEY} is absent"
    try:
        rec = json.loads(raw)
    except ValueError:
        return None, f"{ROUND_KEY} is unparsable"
    if not isinstance(rec, dict):
        return None, f"{ROUND_KEY} is not a record"
    state = rec.get("state")
    if not isinstance(state, str) or not state:
        return None, f"{ROUND_KEY} carries no state"
    want_n = int(rn[1:])
    if rec.get("n") != want_n:
        return None, f"{ROUND_KEY} is r{rec.get('n', '?')}, not {rn}"
    if state not in _ROUND_OPEN_STATES:
        return None, f"round r{want_n} is not open ({state})"
    base = str(rec.get("base") or "").strip().lower()
    if base[:LANDED_SHA_LEN] != sha12:
        return None, (
            f"round r{want_n} names {base[:LANDED_SHA_LEN] or 'no head sha'}, "
            f"not {sha12}"
        )
    return rec, ""


def _auto_answer_approval(
    store: Store, approval_id: int, *, now: datetime | None
) -> tuple[bool, str]:
    from precis import settings

    # The armed check is read fresh: the setting is the switch, a stale
    # cache must not answer a deploy after the owner turned it off.
    settings.invalidate(_AUTO_DEPLOY_KEY)
    if settings.get_bool(_AUTO_DEPLOY_KEY, store=store) is not True:
        return False, ""
    ref = store.get_ref(kind="todo", id=approval_id)
    ask = (ref.meta or {}).get("ask") if ref is not None else None
    rel = release_of(store, approval_id)
    rn = str(rel["rN"]) if rel is not None else ""
    if (
        not isinstance(ask, dict)
        or ask.get("kind") != "approval"
        or rel is None
        or ask.get("options") != approval_options(rn)
        or not _blocks_release_deploy(store, ask.get("blocks"))
    ):
        return False, ""
    if _newest_answer(store, approval_id) is not None:
        return False, ""
    nope = f"{_AUTO_DEPLOY_KEY} is on but td{approval_id} is not auto-answered"
    sha = str(rel.get("sha") or "").strip().lower()
    if len(sha) < LANDED_SHA_LEN:
        return False, f"{nope}: the cut's sha is unrecorded ({sha or 'empty'})"
    sha12 = sha[:LANDED_SHA_LEN]
    settings.invalidate(CI_VERDICTS_KEY)
    verdict = _newest_sha_verdict(store, sha12)
    if verdict is None or verdict["verdict"] != "green":
        return False, f"{nope}: no recorded green CI verdict for {sha12}"
    rec, why = _open_round_record(store, rn, sha12)
    if rec is None:
        return False, f"{nope}: {why}"
    # The comment is the only write and it comes last, after every check.
    run = str(verdict.get("run_id") or "") or "unrecorded"
    text = (
        f"answer: deploy {rn} (auto-answered under {_AUTO_DEPLOY_KEY}; "
        f"sha {sha12}; range {str(rel.get('range') or '')[:80]}; "
        f"CI green run {run} at {verdict.get('at') or 'unknown time'}; "
        f"round record r{rec.get('n')} {rec.get('state')}; "
        f"answered {_stamp(now or datetime.now(UTC))})"
    )
    existing = list(store.chunks.list_chunks_for_ref(approval_id))
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        approval_id,
        [
            ChunkInsert(
                ord=ord_,
                text=text,
                meta={
                    "chunk_kind": _COMMENT_KIND,
                    # worker-stamped — the G2 trust boundary (td575705).
                    **worker_comment_meta(),
                },
            )
        ],
    )
    store.remove_tag(approval_id, Tag.open("ask-user"))
    return True, f"auto-answered td{approval_id}: deploy {rn} at {sha12}"


def auto_answer_approval(
    store: Store, approval_id: int, *, now: datetime | None = None
) -> tuple[bool, str]:
    """``(answered, note)`` — the standing approval's answer to the
    release-pass approval ask ``approval_id``, or why it stays open.

    Armed only while ``release.auto_deploy`` resolves ``True`` (DB-only,
    owner-set); a quiet ``(False, "")`` covers the switch being off and
    every non-release approval ask. Answers only a real release approval
    (``meta.ask.kind == 'approval'``, options rewritten to the cut's
    r<N>, ``blocks`` a live ``release_deploy`` todo) that has no answer
    yet — a human ``answer:`` (``hold`` included) is never overwritten —
    and only once the newest recorded CI verdict on the cut's exact sha
    is green *and* ``release.round`` names an open round on that head
    sha. Every other case returns ``(False, "<the refusal>")`` for the
    tick's notes and posts nothing: fail closed. Never raises — a fault
    answers ``(False, "... error: <ClassName>")``."""
    try:
        return _auto_answer_approval(store, approval_id, now=now)
    except Exception as exc:
        return False, (
            f"{_AUTO_DEPLOY_KEY} auto-answer on td{approval_id} "
            f"error: {type(exc).__name__}"
        )


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


# ── the shared release.round record ───────────────────────────────

#: ``app_settings`` key ``scripts/round`` publishes the active release's
#: record to (same repeated-literal arrangement as ``PROD_HEAD_KEY``).
ROUND_KEY = "release.round"


def round_record(
    rel: dict[str, Any], *, n: int, owner: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """The ``release.round`` row for one ``round.json`` release record.

    ``deploy`` maps the rollout journal's ``sha`` / ``rollout_at`` /
    ``runtime_confirmed_at`` / ``runtime_evidence``; absent keys are
    omitted, never null, and ``deploy`` itself is omitted when the
    journal carries none of them (``scripts/round._round_record`` keeps
    the same literal). The row is a pure function of the record and
    owner: the settings row's own write time is the publish time, so an
    unchanged record dedupes against the last publish.
    """
    row: dict[str, Any] = {
        "n": n,
        "branch": rel["branch"],
        "base": rel["base"],
        "state": rel["state"],
        "owner": owner,
    }
    if rel.get("cut_at"):
        row["cut_at"] = rel["cut_at"]
    if rel.get("state_at"):
        row["state_at"] = rel["state_at"]
    dep = rel.get("deployment") or {}
    deploy = {
        dst: dep[src]
        for src, dst in (
            ("sha", "sha"),
            ("rollout_at", "rollout_at"),
            ("runtime_confirmed_at", "confirmed_at"),
            ("runtime_evidence", "evidence"),
        )
        if dep.get(src)
    }
    if deploy:
        row["deploy"] = deploy
    return row


def record_round(store: Store, row: dict[str, Any]) -> str:
    """Write ``row`` to ``release.round``; one status line."""
    from precis import settings

    settings.invalidate(ROUND_KEY)
    settings.set_setting(ROUND_KEY, json.dumps(row, separators=(",", ":")), store=store)
    return f"round record recorded: r{row.get('n', '?')} {row.get('state', '?')}"


def read_round(store: Store) -> dict[str, Any] | None:
    """The ``release.round`` row, or ``None`` when absent, unparsable or
    not a dict."""
    from precis import settings

    raw = settings.get_str(ROUND_KEY, store=store)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _rn_number(rn: object) -> int | None:
    """``7`` for ``"r7"``, else ``None``."""
    m = _RN_RE.match(str(rn or ""))
    return int(m.group(0)[1:]) if m else None


def pinned_deploy_sha(store: Store, rn: str) -> str | None:
    """The sha the deployed runtime must serve for ``rn`` (td573432).

    ``release.round`` wins when it names this release — ``scripts/round``
    records ``deploy.sha`` the moment the rollout lands, ahead of the
    ``release.prod_head`` write. The prod head is the fallback for a plan
    whose round record a host without the prod credential never
    published; ``None`` when neither names one.
    """
    row = read_round(store)
    n = _rn_number(rn)
    if row is not None and n is not None and row.get("n") == n:
        dep = row.get("deploy")
        if isinstance(dep, dict) and isinstance(dep.get("sha"), str):
            return dep["sha"]
    rec = read_prod_head(store)
    return str(rec["head"]) if rec is not None else None


#: Round ``state`` values ``record_confirm`` never moves back or over —
#: the release lifecycle's terminal states (``scripts/round`` §states).
_CONFIRM_TERMINAL = frozenset({"confirmed", "superseded", "abandoned"})

#: Lifecycle ``state`` order for a job-side :func:`update_round` write:
#: the open states rank 0, ``deploying`` 1, ``deployed`` 2, the terminal
#: states 3 — a job-side write never lowers the rank (gr575362).
_ROUND_STATE_RANK = {
    "cut": 0,
    "gating": 0,
    "fixing": 0,
    "green": 0,
    "deploying": 1,
    "deployed": 2,
    "confirmed": 3,
    "superseded": 3,
    "abandoned": 3,
}


def record_confirm(
    store: Store,
    todo_id: int,
    sha: str,
    evidence: str,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Record the runtime confirmation on ``release.round`` and the plan
    root (td573432); one note per write.

    Two writers can record the confirmation: ``scripts/round`` (it
    publishes ``deploy.confirmed_at`` and ``deploy.evidence`` from its
    journal at the end of the confirm verb, best-effort, only when its
    host has a prod credential) and this store-side call. The script's
    values win: only absent keys are filled, never an existing
    ``confirmed_at`` or ``evidence``. The row is skipped (with a note)
    when its ``n`` differs from the plan's release or its ``deploy.sha``
    differs from ``sha``; no row is ever written from nothing. ``state``
    moves to ``confirmed`` only when it is not already terminal. A row
    whose ``deploy`` carries no ``sha`` — a fallback deploy wrote none —
    takes ``sha`` from ``release.prod_head`` when the head names it, so
    the confirm does not skip forever (gr575362); a prod head on
    another sha keeps the skip. On the
    plan root ``meta.release.confirmed = {sha, at, evidence}`` is
    stamped, every other key kept, only while ``confirmed`` is absent or
    names a different sha — so the call is idempotent and a repeat call
    after the script's own publish returns ``[]``.
    """
    from precis import settings

    notes: list[str] = []
    when = _stamp(now or datetime.now(UTC))
    root = plan_root(store, todo_id)
    rel = release_of(store, todo_id)
    n = _rn_number((rel or {}).get("rN"))

    row = read_round(store)
    if row is None:
        notes.append("release.round: no row on record; nothing stamped")
    elif n is None or row.get("n") != n:
        notes.append(
            f"release.round: skipped — the row is r{row.get('n', '?')}, "
            f"this plan is {(rel or {}).get('rN') or 'r?'}"
        )
    else:
        dep = row.get("deploy")
        if not isinstance(dep, dict):
            dep = {}
        dep_sha = dep.get("sha")
        if dep_sha is None:
            # A fallback deploy (no RELEASE_DEPLOYED line) left the row
            # deployed with no deploy.sha: the prod head is the record of
            # what actually rolled out — when it names the confirmed sha,
            # fill it in and confirm.
            settings.invalidate(PROD_HEAD_KEY)
            head = read_prod_head(store)
            if (
                head is not None
                and str(head.get("head") or "").lower()[:LANDED_SHA_LEN]
                == sha.lower()[:LANDED_SHA_LEN]
            ):
                dep["sha"] = sha
                dep_sha = sha
        if dep_sha != sha:
            notes.append(
                f"release.round: skipped — deploy.sha "
                f"{str(dep_sha)[:12] if dep_sha else 'absent'} is not {sha[:12]}"
            )
        else:
            updates = {
                k: v
                for k, v in (("confirmed_at", when), ("evidence", evidence))
                if not dep.get(k)
            }
            if updates or str(row.get("state")) not in _CONFIRM_TERMINAL:
                new_row = dict(row)
                new_row["deploy"] = {**dep, **updates}
                if str(row.get("state")) not in _CONFIRM_TERMINAL:
                    new_row["state"] = "confirmed"
                    new_row["state_at"] = when
                settings.invalidate(ROUND_KEY)
                settings.set_setting(
                    ROUND_KEY,
                    json.dumps(new_row, separators=(",", ":")),
                    store=store,
                )
                notes.append(f"release.round: r{n} confirmed at {when}")

    if rel is not None:
        confirmed = rel.get("confirmed")
        if not (isinstance(confirmed, dict) and confirmed.get("sha") == sha):
            stamp = {"sha": sha, "at": when, "evidence": evidence}
            store.stamp_ref_meta(root, {"release": {**rel, "confirmed": stamp}})
            notes.append(f"td{root}: meta.release.confirmed = {sha[:12]}")
    return notes


def round_confirmed(row: dict[str, Any] | None, n: int) -> bool:
    """The shared ``release.round`` row proves prod moved past a clone's
    local ``r<n>`` record: ``state`` is ``confirmed`` — the only state
    that means the release actually rolled out and was runtime-confirmed
    (``superseded`` and ``abandoned`` prove nothing about prod) — at
    ``n`` or a later number (a coordinator may have released r<n+1> by
    hand too). A smaller number or any other state answers ``False``."""
    if not isinstance(row, dict):
        return False
    have = row.get("n")
    return bool(
        type(have) is int
        and type(n) is int
        and have >= n
        and row.get("state") == "confirmed"
    )


def live_deploy_job(store: Store) -> bool:
    """A non-terminal ``release_deploy`` job anywhere in the store — its
    own ``deployed`` write may still land, so a stale local record must
    not be superseded out from under it (td575911).
    ``release_poll._live_deploy_job``'s check without the plan join; the
    newest 50 such jobs are enough."""
    from precis.workers._mint import JOB_TERMINAL_STATUSES

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta ->> 'job_type' = 'release_deploy'
             ORDER BY j.ref_id DESC
             LIMIT 50
            """
        ).fetchall()
    for (jid,) in rows:
        status = next(
            (str(t.value) for t in store.tags_for(int(jid)) if t.prefix == "STATUS"),
            "open",
        )
        if status not in JOB_TERMINAL_STATUSES:
            return True
    return False


def update_round(
    store: Store,
    fields: dict[str, Any],
    *,
    create: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """Write the shared ``release.round`` row from a job type (gr575362);
    one note per call, written or skipped.

    ``fields`` carries the row literal — ``n`` (int) required, plus
    ``branch`` / ``base`` / ``owner`` / ``pass`` for a create and
    ``state`` / ``deploy`` for an advance. ``create=True`` is the cut's write: it
    fills ``state_at`` and ``cut_at`` with ``now`` when the fields do not
    carry them, writes ``fields`` whole when no row exists or the row is
    a lower ``n`` (a re-cut r<N+1> moves the row), and leaves a row for
    the same or a higher ``n`` alone — a same-``n`` row is the script's
    own publish, which wins; the one exception is a same-``n`` ``cut``
    row lacking ``pass``, where the field's ``pass`` is stamped in and
    nothing else changes, so ``release_poll``'s dropped-pass abandon can
    still fire on it. ``create=False`` is the deploy's merge — and the
    dropped-pass abandon's: same ``n`` only, ``state`` must be a forward
    move under :data:`_ROUND_STATE_RANK`, a job-side terminal target
    (``confirmed`` / ``superseded`` / ``abandoned``) takes a rank-0 row
    only so a concurrent ``deploying`` / ``deployed`` write survives,
    ``state_at`` is stamped, and ``deploy`` plus any other non-reserved
    keys merge with the recorded keys winning. An unreadable,
    unparsable or non-dict row is never overwritten. Nothing is written
    on a missing ``n``; a database error answers one note, never a
    raise."""
    when = _stamp(now or datetime.now(UTC))
    n = fields.get("n")
    if not isinstance(n, int) or isinstance(n, bool):
        return ["release.round: the update carries no n; nothing written"]
    try:
        raw = _round_raw(store)
    except Exception as exc:
        return [f"release.round: unreadable ({type(exc).__name__}); nothing written"]
    row: dict[str, Any] | None = None
    if raw and raw.strip():
        try:
            rec = json.loads(raw)
        except ValueError:
            return ["release.round: unparsable; nothing written"]
        if not isinstance(rec, dict):
            return ["release.round: not a record; nothing written"]
        row = rec

    def _create() -> str:
        new_row = dict(fields)
        if "state" in fields:
            new_row.setdefault("state_at", when)
            new_row.setdefault("cut_at", when)
        record_round(store, new_row)
        return f"release.round: wrote r{n} {new_row.get('state', '?')}"

    if row is None:
        if not create:
            return ["release.round: no row on record; nothing written"]
        return [_create()]
    have_n = row.get("n")
    if have_n != n:
        if create and type(have_n) is int and have_n < n:
            return [_create()]
        label = f"r{have_n}" if isinstance(have_n, int) else repr(have_n)
        return [f"release.round: skipped — the row is {label}, not r{n}"]
    if create:
        stamped = fields.get("pass")
        if row.get("state") == "cut" and "pass" not in row and stamped:
            record_round(store, {**row, "pass": stamped})
            return [f"release.round: r{n} already on record (cut); stamped {stamped}"]
        return [
            f"release.round: r{n} already on record "
            f"({row.get('state', '?')}); the script's publish wins"
        ]
    want = str(fields.get("state") or "")
    have = str(row.get("state") or "")
    want_rank = _ROUND_STATE_RANK.get(want)
    have_rank = _ROUND_STATE_RANK.get(have)
    if want_rank is None or have_rank is None or want_rank <= have_rank:
        return [
            f"release.round: r{n} is {have or '?'}; {want or '?'} is not a move forward"
        ]
    if want in _CONFIRM_TERMINAL and have_rank != 0:
        # The dropped-pass abandon goes through here too: a row that
        # reads as rolled out must survive it — a concurrent deployed
        # write always wins.
        return [
            f"release.round: r{n} is {have}; a job-side terminal write "
            f"takes a rank-0 row only"
        ]
    new_row = dict(row)
    new_row["state"] = want
    new_row["state_at"] = when
    for key, value in fields.items():
        if key not in {"n", "state", "state_at", "deploy"}:
            new_row.setdefault(key, value)
    dep = row.get("deploy")
    merged = dict(dep) if isinstance(dep, dict) else {}
    add = fields.get("deploy")
    if isinstance(add, dict):
        for k, v in add.items():
            merged.setdefault(k, v)
    if merged:
        new_row["deploy"] = merged
    record_round(store, new_row)
    return [f"release.round: r{n} {have} -> {want}"]


__all__ = [
    "APPROVE_OPTION",
    "HOLD_OPTION",
    "PENDING_RANGE",
    "ROUND_KEY",
    "approval",
    "approval_options",
    "auto_answer_approval",
    "ci_verdict_rows",
    "live_deploy_job",
    "parse_cut",
    "parse_deployed",
    "pin_refusal",
    "pinned_deploy_sha",
    "pinned_host",
    "plan_root",
    "prod_head_record",
    "read_round",
    "record_ci_verdict",
    "record_confirm",
    "record_cut",
    "record_prod_head",
    "record_round",
    "release_of",
    "repo_from_remote",
    "round_confirmed",
    "round_record",
    "update_round",
    "valid_repo",
]
