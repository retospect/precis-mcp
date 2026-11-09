"""The Health tab's release-pass panel (gr606988).

Nothing rendered the auto release pass — cut, CI gate, fix loop, deploy —
outside the todo tree, ``gh run list --branch release/r<N>`` and the
steward's ticks. Every shape read here is written by the dark release ops;
:mod:`precis.plans.release` is the reference for them:

- one row per ``release-pass``-tagged plan root — a re-cut keeps the pass
  but takes a new release number, so the ``release_cut`` jobs'
  ``meta.release_cut`` history renders as the renumbering
  ``r259 → r260`` on the one row, never two unrelated rows;
- the plan nodes' STATUS + ``blocked-by`` edges give the pipeline's
  current step — a node is *active* when open and every blocker closed;
- ``release.ci_verdicts`` gives the gate's GitHub run + verdict,
  ``release.round`` the shared lifecycle record, ``release.prod_head``
  what prod actually serves.

Read-only. SQL rides the caller's connection so the section can share the
status page's one-request connection (``_request_conn``); the
``app_settings`` rows go through the ``precis.plans`` readers so the row
shapes stay single-sourced.
"""

from __future__ import annotations

from typing import Any

from precis.plans import release as _release
from precis.plans.gates import _prefix_match, read_ci_verdicts, read_prod_head
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.utils import handle_registry
from precis_web.timefmt import abs_ts as _abs_ts
from precis_web.timefmt import ago as _ago

#: The OPEN tag marking a release-pass plan root (``mint_release_pass``).
_PASS_TAG = "release-pass"
_PLAN_PREFIX = "plan:"

#: How many finished passes the panel lists under the open one(s).
_CLOSED_LIMIT = 5

#: The release-pass pipeline (``mint_release_pass``) in dispatch order —
#: display order for the steps strip.
_STEP_ORDER = ("cut", "poll", "gate", "loop", "approval", "deploy", "confirm")

#: ``meta.op`` job_type → pipeline step. The other steps are typed by
#: their ``gate`` / ``loop`` / ``ask`` metas instead (never by title — a
#: renamed node must not break the panel).
_OP_ROLE = {
    "release_cut": "cut",
    "ci_verdict_poll": "poll",
    "release_deploy": "deploy",
    "release_confirm": "confirm",
}

_ROOTS_SQL = """
SELECT r.ref_id, r.meta, r.created_at, r.updated_at,
       (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status
  FROM refs r
 WHERE r.kind = 'todo' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                  AND t.value = %s)
 ORDER BY r.ref_id
"""

#: Every node of every listed pass, with the node's current STATUS and
#: OPEN-tag values (the ``plan:<root>`` membership tag, ``ask-user``).
_NODES_SQL = """
SELECT r.ref_id, r.parent_id, r.meta,
       (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status,
       (SELECT array_agg(t.value) FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN') AS open_tags
  FROM refs r
 WHERE r.kind = 'todo' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                  AND t.value = ANY(%s))
 ORDER BY r.ref_id
"""

#: The recorded cuts: every ``release_cut`` job that reached its stamp
#: (``meta.release_cut``), oldest first — the r<N> history one pass's
#: re-cuts write. Scoped by ``parent_id`` (the dispatcher's ``_mint_job``
#: parents a job to the plan node it serves — an indexed column, so the
#: read stays off the whole jobs table).
_CUTS_SQL = """
SELECT r.ref_id, r.parent_id AS node_id, r.meta -> 'release_cut' AS cut
  FROM refs r
 WHERE r.kind = 'job' AND r.retired_at IS NULL
   AND r.parent_id = ANY(%s)
   AND r.meta ? 'release_cut'
 ORDER BY r.ref_id
"""

#: ``blocked-by`` edges out of plan nodes, with the target's current
#: STATUS — a node is *active* when open and every target is terminal.
_LINKS_SQL = """
SELECT l.src_ref_id, l.dst_ref_id,
       (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = l.dst_ref_id AND t.namespace = 'STATUS' LIMIT 1)
       AS dst_status
  FROM links l
 WHERE l.relation = 'blocked-by' AND l.src_ref_id = ANY(%s)
   AND EXISTS (SELECT 1 FROM refs b WHERE b.ref_id = l.dst_ref_id
                AND b.retired_at IS NULL)
"""


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _role(meta: dict[str, Any]) -> str | None:
    """The pipeline step a plan node plays, off its meta — never its title."""
    op = meta.get("op")
    name = str(op.get("type") if isinstance(op, dict) else op or "").strip()
    if name in _OP_ROLE:
        return _OP_ROLE[name]
    if isinstance(meta.get("loop"), dict):
        return "loop"
    ask = meta.get("ask")
    if isinstance(ask, dict) and ask.get("kind") == "approval":
        return "approval"
    gate = meta.get("gate")
    if isinstance(gate, dict) and gate.get("kind") == "tests-green":
        return "gate"
    return None


def _verdict_for(
    verdicts: list[dict[str, Any]], sha: str, rng: str
) -> dict[str, Any] | None:
    """The newest recorded verdict covering the cut — the tests-green
    gate's own match rule (sha prefix or range equality, newest ``at``
    wins, ties to the later row; :func:`precis.plans.gates._newest_verdict`
    over a pre-fetched list)."""
    want = rng.strip().lower()
    best: tuple[str, int] | None = None
    pick: dict[str, Any] | None = None
    for i, row in enumerate(verdicts):
        hit = _prefix_match(sha, str(row.get("sha") or "").lower()) or (
            bool(want) and str(row.get("range") or "").strip().lower() == want
        )
        if not hit:
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, pick = key, row
    return pick


def _run_url(repo: str | None, run_id: str) -> str | None:
    """``https://github.com/<repo>/actions/runs/<run_id>`` — the gate run
    link the panel's verdict cell carries."""
    repo = _release.valid_repo(repo)
    run_id = str(run_id or "").strip()
    if not repo or not run_id.isdigit():
        return None
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def _current_step(
    active: set[str],
    *,
    loop: dict[str, Any],
    verdict: dict[str, Any] | None,
    ask_user: bool,
    ruling: int | None,
) -> str:
    """One-line "where is it now" from the active (open + unblocked)
    pipeline steps — checked deepest stage first."""
    if "confirm" in active:
        return "confirming the deployed runtime"
    if "deploy" in active:
        return "deploying the release"
    if "approval" in active:
        return "awaiting deploy approval" + (" · ask-user" if ask_user else "")
    if "loop" in active:
        count = int(loop.get("count") or 0)
        if count:
            return f"fix loop — iteration {count} of {loop.get('max', '?')}"
        if verdict is not None and verdict.get("verdict") == "red":
            return "CI red — next fix iteration pending"
        return "awaiting the CI verdict"
    if "gate" in active:
        if verdict is None:
            return "awaiting the CI verdict"
        return f"CI {verdict.get('verdict')}"
    if "poll" in active:
        return "recording the CI verdict"
    if "cut" in active:
        return "cutting the release"
    if ruling is not None:
        return f"awaiting a ruling — td{ruling}"
    return "waiting on the plan tick"


def _cut_list(
    jobs: list[dict[str, Any]], rel: dict[str, Any] | None
) -> list[dict[str, str]]:
    """The pass's cuts in order — ``meta.release_cut`` rows plus the root's
    ``meta.release`` (the authoritative current cut, appended when the job
    trail doesn't reach it — a cut recorded by hand). Consecutive repeats
    of one r<N> (a retried cut job) collapse."""
    cuts: list[dict[str, str]] = []
    for job in jobs:
        cut = job["cut"] if isinstance(job["cut"], dict) else {}
        rn = str(cut.get("rN") or "")
        if rn and (not cuts or cuts[-1]["rn"] != rn):
            cuts.append(
                {
                    "rn": rn,
                    "sha": str(cut.get("sha") or ""),
                    "range": str(cut.get("range") or ""),
                }
            )
    if rel is not None:
        rn = str(rel.get("rN") or "")
        cur = {
            "rn": rn,
            "sha": str(rel.get("sha") or ""),
            "range": str(rel.get("range") or ""),
        }
        if rn and (not cuts or cuts[-1]["rn"] != rn):
            cuts.append(cur)
        elif cuts and cuts[-1]["rn"] == rn:
            cuts[-1] = cur
    return cuts


def _query(conn: Any) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    """The panel's four reads: pass roots, their plan nodes, the recorded
    cuts and the blocked-by edges — all on the caller's connection."""
    roots = conn.execute(_ROOTS_SQL, (_PASS_TAG,)).fetchall()
    root_ids = [int(r[0]) for r in roots]
    if not root_ids:
        return roots, [], [], []
    node_rows = conn.execute(
        _NODES_SQL, ([f"{_PLAN_PREFIX}{i}" for i in root_ids],)
    ).fetchall()
    node_ids = [int(r[0]) for r in node_rows]
    job_rows = conn.execute(_CUTS_SQL, (node_ids,)).fetchall()
    link_rows = conn.execute(_LINKS_SQL, (node_ids,)).fetchall()
    return roots, node_rows, job_rows, link_rows


def release_passes(
    store: Any, conn: Any | None = None, *, closed_limit: int = _CLOSED_LIMIT
) -> dict[str, Any]:
    """``{"passes": [rows], "prod_head": {…}|None, "round": {…}|None}``.

    ``conn`` is the request-shared connection when the caller has one (the
    status page's ``_request_conn``); ``None`` checks out a pooled one.
    Open passes sort first (oldest), then the last ``closed_limit``
    terminal ones (newest pass first).
    """
    if conn is None:
        with store.pool.connection() as conn:
            roots, node_rows, job_rows, link_rows = _query(conn)
    else:
        roots, node_rows, job_rows, link_rows = _query(conn)
    root_ids = [int(r[0]) for r in roots]

    # node id -> pass root, via the ``plan:<root>`` OPEN tag
    node_root: dict[int, int] = {}
    nodes_by_root: dict[int, list[dict[str, Any]]] = {}
    for rid, parent_id, meta, status, open_tags in node_rows:
        rid = int(rid)
        tags = [str(t) for t in (open_tags or [])]
        root = next(
            (
                int(t[len(_PLAN_PREFIX) :])
                for t in tags
                if t.startswith(_PLAN_PREFIX)
                and t[len(_PLAN_PREFIX) :].isdigit()
                and int(t[len(_PLAN_PREFIX) :]) in root_ids
            ),
            None,
        )
        if root is None:
            continue
        node_root[rid] = root
        nodes_by_root.setdefault(root, []).append(
            {
                "id": rid,
                "parent": int(parent_id) if parent_id else None,
                "meta": dict(meta or {}),
                "status": str(status or "open"),
                "open_tags": tags,
            }
        )

    # release_cut jobs -> their pass root, via the parent node's plan tag
    jobs_by_root: dict[int, list[dict[str, Any]]] = {}
    for jid, node_id, cut in job_rows:
        root = node_root.get(int(node_id or 0), 0)
        if root:
            jobs_by_root.setdefault(root, []).append({"id": int(jid), "cut": cut})

    # src node id -> statuses of its blocked-by targets
    blockers: dict[int, list[str]] = {}
    for src, _dst, dst_status in link_rows:
        blockers.setdefault(int(src), []).append(str(dst_status or "open"))

    verdicts = read_ci_verdicts(store)
    prod = read_prod_head(store)
    round_row = _release.read_round(store)
    repo_default = _release.valid_repo(_setting(store, "release.repo"))

    rows = [
        _row(
            int(r[0]),
            dict(r[1] or {}),
            str(r[4] or "open"),
            r[3] or r[2],
            nodes_by_root.get(int(r[0]), []),
            jobs_by_root.get(int(r[0]), []),
            blockers,
            verdicts,
            prod,
            round_row,
            repo_default,
        )
        for r in roots
    ]
    open_rows = [p for p in rows if p["open"]]
    closed_rows = [p for p in rows if not p["open"]]
    closed_rows.sort(key=lambda p: p["ref_id"], reverse=True)

    prod_head = None
    if prod is not None:
        prod_head = {
            "sha": str(prod["head"]),
            "ago": _ago(prod.get("deployed_at")),
            "at": str(prod.get("deployed_at") or ""),
        }
    round_out = None
    if isinstance(round_row, dict) and isinstance(round_row.get("n"), int):
        round_out = {
            "n": int(round_row["n"]),
            "state": str(round_row.get("state") or ""),
            "branch": str(round_row.get("branch") or ""),
        }
    return {
        "passes": [*open_rows, *closed_rows[:closed_limit]],
        "prod_head": prod_head,
        "round": round_out,
    }


def _setting(store: Any, key: str) -> str | None:
    from precis import settings

    return settings.get_str(key, store=store)


def _row(
    root_id: int,
    meta: dict[str, Any],
    status: str,
    stamp: Any,
    nodes: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    blockers: dict[int, list[str]],
    verdicts: list[dict[str, Any]],
    prod: dict[str, Any] | None,
    round_row: dict[str, Any] | None,
    repo_default: str | None,
) -> dict[str, Any]:
    rel = meta.get("release")
    rel = rel if isinstance(rel, dict) else None
    cuts = _cut_list(jobs, rel)
    cur = cuts[-1] if cuts else None
    sha = str((cur or {}).get("sha") or "")
    rng = str((cur or {}).get("range") or "")
    repo = _release.valid_repo((rel or {}).get("repo")) or repo_default

    verdict = _verdict_for(verdicts, sha, rng) if sha or rng else None
    if verdict is not None:
        gate = {
            "verdict": str(verdict["verdict"]),
            "run_id": str(verdict.get("run_id") or ""),
            "run_url": _run_url(repo, str(verdict.get("run_id") or "")),
            "ago": _ago(verdict.get("at")),
        }
    else:
        gate = {
            "verdict": "pending" if cur else "none",
            "run_id": "",
            "run_url": None,
            "ago": "",
        }

    # Pipeline nodes are the root's direct children — a loop's iteration
    # children parent to the loop, so they never pollute the step roles.
    pipeline = [n for n in nodes if n["parent"] == root_id]
    by_role: dict[str, dict[str, Any]] = {}
    ruling: int | None = None
    for n in pipeline:
        role = _role(n["meta"])
        if role is not None and role not in by_role:
            by_role[role] = n
        ask = n["meta"].get("ask")
        if (
            role != "approval"
            and isinstance(ask, dict)
            and n["status"] not in TERMINAL_TODO_STATUSES
            and ruling is None
        ):
            ruling = n["id"]

    def _active(node: dict[str, Any]) -> bool:
        return all(s in BLOCKER_TERMINAL_STATUSES for s in blockers.get(node["id"], ()))

    steps: list[dict[str, Any]] = []
    active: set[str] = set()
    for role in _STEP_ORDER:
        node = by_role.get(role)
        if node is None:
            continue
        nstatus = node["status"]
        if nstatus in TERMINAL_TODO_STATUSES:
            state = "done" if nstatus == "done" else "closed"
        elif nstatus == "paused" or not _active(node):
            state = "pending"
        else:
            state = "active"
            active.add(role)
        label = role
        if role == "loop":
            loop = node["meta"].get("loop") or {}
            label = f"loop {loop.get('count') or 0}/{loop.get('max', '?')}"
        steps.append(
            {
                "role": role,
                "label": label,
                "state": state,
                "handle": _h("todo", node["id"]),
            }
        )

    loop_node = by_role.get("loop")
    loop_meta = dict((loop_node or {}).get("meta", {}).get("loop") or {})
    approval = by_role.get("approval")
    ask_user = bool(
        approval
        and any(
            t == "ask-user" or t.startswith("ask-user:") for t in approval["open_tags"]
        )
    )
    if status in TERMINAL_TODO_STATUSES:
        step = status
    else:
        step = _current_step(
            active,
            loop=loop_meta,
            verdict=verdict,
            ask_user=ask_user,
            ruling=ruling,
        )

    served: bool | None = None
    if sha and prod is not None:
        head = str(prod["head"]).lower()
        landed = [str(s).lower() for s in (prod.get("landed") or [])]
        served = _prefix_match(sha, head) or any(_prefix_match(sha, s) for s in landed)

    round_label = ""
    if isinstance(round_row, dict) and isinstance(round_row.get("n"), int):
        by_pass = str(round_row.get("pass") or "") == f"td{root_id}"
        by_rn = not round_row.get("pass") and bool(
            cur and cur["rn"] == f"r{round_row['n']}"
        )
        if by_pass or by_rn:
            round_label = f"r{round_row['n']} {round_row.get('state') or ''}".strip()

    return {
        "ref_id": root_id,
        "handle": _h("todo", root_id),
        "status": status,
        "open": status not in TERMINAL_TODO_STATUSES,
        "age": _ago(stamp),
        "at": _abs_ts(stamp),
        "rns": " → ".join(c["rn"] for c in cuts) or "—",
        "range": rng,
        "sha12": sha[:12],
        "cut_at": str((rel or {}).get("cut_at") or ""),
        "gate": gate,
        "loop": (
            f"{loop_meta.get('count') or 0}/{loop_meta.get('max', '?')}"
            if loop_meta
            else ""
        ),
        "step": step,
        "steps": steps,
        "round": round_label,
        "served": served,
    }
