"""``get(kind='todo', id=<release pass>, view='release')`` — one screen for
a release pass (gr735230).

The release owner used to rebuild the picture from ``view='tree'``, job
counts and git. Everything here is a read of what the pass already
stored: no git, no network, no recompute of the engine-review gate (that
scan reads GitHub; the deploy's own refusal comment is its stored
answer). Where a fact is not in the store the line says ``unknown``.

* **Header** — ``meta.release`` on the pass root (number, cut and head
  sha, the host the cut ran on, else the ``release.cut_host`` pin) and a
  one-line state derived from the nodes.
* **CI** — the newest ``release.ci_verdicts`` row on the head sha, else
  what a ``ci_verdict_poll`` job recorded for that sha, else ``pending``;
  a record of an earlier head prints beside it, never as the verdict.
* **head on main / on prod** — ``yes`` only from a stored record
  (``meta.release.merge_back``; ``meta.release.confirmed`` /
  ``deploy_scope``, ``release.prod_head``), ``no`` only when the deploy
  has not started, else ``unknown``.
* **Nodes** — one line per pass node in pass order (the fix loop's
  iterations after the gate), newest job and its age; a held node adds
  an indented ``held:`` line with the stored reason: ``release_deploy``'s
  refusal comment (the review units it waits on keyed ``td<id>`` /
  ``sha:<sha12>``), ``release_cut``'s ``cut_refusal_logged``, a blocked
  node's newest comment, or the open ask it waits on.
* **asks** — every open ask under the pass, linked by ``blocked-by`` from
  a pass node, or a ``release-review-wait:<key>`` ask of a unit the
  deploy's refusal names — who must answer and the options stored on it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.plans import release as _release
from precis.plans.gates import read_ci_verdicts, read_prod_head
from precis.response import Response
from precis.todo import TERMINAL_TODO_STATUSES, views
from precis.todo.blocked import open_blocked_by
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store

#: STATUS values that close a pass node.
_CLOSED = frozenset(TERMINAL_TODO_STATUSES | {"cancelled"})
#: STATUS values of a finished job (``store._tags_ops._JOB_TERMINAL``).
_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed-out"})
_COMMENT_KIND = "todo_comment"
_WAIT_TAG = "release-review-wait"
_ITERATION_RE = re.compile(r" \(iteration (\d+)\)\s*$")
#: The units a deploy refusal names — the engine-review blocker lines
#: (``release_poll._review_outcome`` / ``_engine_verdicts``): ``engine
#: land <td|sha12>`` and ``on the <td|sha12> land``.
_UNIT_RE = re.compile(
    r"(?:engine land|on the) (td\d+|[0-9a-f]{12})\b(?: \(jo\d+\))?", re.IGNORECASE
)
_TITLE_CAP = 64
_HELD_CAP = 260
#: Roles that carry the overall state, in pass order; the gate and the
#: loop only wait on them.
_WAITING_ROLES = frozenset({"gate", "loop"})
_STATE_WORD = {
    "cut": "cutting the release",
    "poll": "polling CI",
    "digest": "red CI: digest",
    "fix": "red CI: fix",
    "restamp": "red CI: restamp",
    "deploy": "deploy",
    "confirm": "confirming the runtime",
    "merge-back": "merging back into main",
    "resolve": "resolving the merge-back conflict",
}


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _clip(text: str, cap: int) -> str:
    line = (text or "").split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


def _when(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            then = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return then if then.tzinfo else then.replace(tzinfo=UTC)
    return None


def _span(then: datetime | None, now: datetime) -> str:
    """``12m`` / ``3h`` / ``2d`` between ``then`` and ``now``."""
    if then is None:
        return "?"
    mins = max(0.0, (now - then).total_seconds() / 60)
    if mins < 90:
        return f"{mins:.0f}m"
    if mins < 48 * 60:
        return f"{mins / 60:.0f}h"
    return f"{mins / 1440:.0f}d"


def _clock(then: datetime, now: datetime) -> str:
    """``17:24Z``, with the date when it is not today (UTC)."""
    then = then.astimezone(UTC)
    if then.date() == now.astimezone(UTC).date():
        return then.strftime("%H:%MZ")
    return then.strftime("%m-%d %H:%MZ")


def _pass_root(store: Store, ref_id: int) -> int | None:
    """The ``release-pass`` root at or above ``ref_id``, else ``None``."""
    seen: set[int] = set()
    cur: int | None = ref_id
    while cur is not None and cur not in seen and len(seen) < 25:
        seen.add(cur)
        if any(t.value == "release-pass" for t in store.tags_for(cur)):
            return cur
        ref = store.get_ref(kind="todo", id=cur)
        cur = ref.parent_id if ref is not None else None
    return None


def _subtree(store: Store, root: int) -> list[dict[str, Any]]:
    """Every live todo and job under ``root`` (root included), id order."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, kind, depth) AS (
                SELECT ref_id, kind, 0 FROM refs
                 WHERE ref_id = %s AND kind = 'todo' AND retired_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.kind, w.depth + 1
                  FROM refs r JOIN walk w ON r.parent_id = w.ref_id
                 WHERE r.kind IN ('todo', 'job') AND r.retired_at IS NULL
                   AND w.kind = 'todo' AND w.depth < 10
            )
            SELECT r.ref_id, r.parent_id, r.kind, r.title, r.meta,
                   r.created_at, r.updated_at
              FROM walk w JOIN refs r USING (ref_id)
             ORDER BY r.ref_id
            """,
            (root,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "parent_id": r[1],
            "kind": str(r[2]),
            "title": str(r[3] or ""),
            "meta": dict(r[4] or {}),
            "created_at": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]


def _todo_rows(store: Store, ref_ids: list[int]) -> dict[int, dict[str, Any]]:
    """``{id: row}`` of live todos by id (the same row shape as
    :func:`_subtree`)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, parent_id, kind, title, meta, created_at, updated_at
              FROM refs
             WHERE ref_id = ANY(%s) AND kind = 'todo' AND retired_at IS NULL
            """,
            (list(ref_ids),),
        ).fetchall()
    return {
        int(r[0]): {
            "id": int(r[0]),
            "parent_id": r[1],
            "kind": str(r[2]),
            "title": str(r[3] or ""),
            "meta": dict(r[4] or {}),
            "created_at": r[5],
            "updated_at": r[6],
        }
        for r in rows
    }


def _role(meta: dict[str, Any]) -> str:
    """The template role of a pass node, from its op / gate / loop meta."""
    op = meta.get("op")
    params: dict[str, Any] = {}
    if isinstance(op, dict):
        params = op.get("params") or {}
        op = op.get("type")
    if op == "ci_verdict_poll":
        return "restamp" if params.get("restamp") else "poll"
    if op == "release_confirm":
        return "merge-back" if params.get("merge_back") else "confirm"
    if meta.get("merge_back_resolver"):
        # ``templates.mint_merge_back_resolver``: a run_slice off main, not
        # a red-CI fix.
        return "resolve"
    named = {
        "release_cut": "cut",
        "release_deploy": "deploy",
        "ci_red_digest": "digest",
        "ci_rerun": "rerun",
        "run_slice": "fix",
    }
    if isinstance(op, str) and op:
        return named.get(op, op)
    gate = meta.get("gate")
    if isinstance(gate, dict):
        return "approval" if gate.get("kind") == "answer" else "gate"
    if "loop" in meta:
        return "loop"
    return "node"


def _ask_marks(tags: list[str]) -> list[str]:
    """The open tags that say "this waits on an answer": the ask markers,
    and ``asks-triage:routed`` — triage sheds ``ask-user`` when it routes
    an ask to the coordinator (``workers.asks_triage``)."""
    return [
        t
        for t in tags
        if t == "ask-user"
        or t.startswith("ask-user:")
        or t in ("ask-coordinator", "asks-triage:routed")
    ]


def _is_ask(tags: list[str], meta: dict[str, Any]) -> bool:
    """An ask todo. A pass action node that only CARRIES an ask tag stays
    a node: an uncertain halt tags the node being worked
    ``ask-user:uncertain-halt-outcome`` (``executors.claude_inproc``), and
    dropping it from the node list would hide the step the pass is on."""
    if _role(meta) not in ("node", "approval"):
        return False
    return isinstance(meta.get("ask"), dict) or bool(_ask_marks(tags))


def _who(tags: list[str], meta: dict[str, Any]) -> str:
    """Who must answer an ask: the review gate and the deploy approval are
    Reto's, also after triage routed the row; any other coordinator ask
    (or one triage routed there) is the coordinator's; any other ask-user
    is the owner's."""
    ask = meta.get("ask")
    kind = ask.get("kind") if isinstance(ask, dict) else None
    routed = (
        "ask-coordinator" in tags
        or "waiting-for:coordinator" in tags
        or "asks-triage:routed" in tags
    )
    if any(t.startswith(f"{_WAIT_TAG}:") for t in tags) or kind == "approval":
        if routed:
            return "Reto (in the coordinator's queue; the answer is Reto's)"
        return "Reto"
    return "coordinator" if routed else "Reto"


def _comments(store: Store, ref_id: int) -> list[Any]:
    """The todo's comment chunks, newest first."""
    chunks = [
        c
        for c in store.chunks.list_chunks_for_ref(ref_id)
        if c.chunk_kind == _COMMENT_KIND and str(c.text or "").strip()
    ]
    return sorted(chunks, key=lambda c: (c.ord, c.id), reverse=True)


class _Node:
    """One pass node with what the screen reads off it."""

    def __init__(self, row: dict[str, Any], status: str, tags: list[str]) -> None:
        self.row = row
        self.id: int = row["id"]
        self.meta: dict[str, Any] = row["meta"]
        self.status = status
        self.tags = tags
        self.role = _role(self.meta)
        m = _ITERATION_RE.search(row["title"])
        self.iteration = int(m.group(1)) if m else 0
        self.job: dict[str, Any] | None = None
        self.job_status = ""
        self.children: list[int] = []

    @property
    def closed(self) -> bool:
        return self.status in _CLOSED

    @property
    def label(self) -> str:
        return f"{self.role}#{self.iteration}" if self.iteration else self.role


def _ahead_note(nodes: list[_Node]) -> str:
    """Says when the branch is past the head the header prints.

    The stored head moves only at a restamp, and this screen reads the
    store, never the remote ref: the fix lands of an iteration whose
    restamp has not run are on the branch but not in the record."""
    waiting = {n.iteration for n in nodes if n.role == "restamp" and not n.closed}
    lands = sum(
        1
        for n in nodes
        if n.role == "fix" and n.status == "done" and n.iteration in waiting
    )
    if not lands:
        return ""
    return (
        f" (recorded at the last restamp; {lands} fix land"
        f"{'' if lands == 1 else 's'} since)"
    )


def _pass_order(by_id: dict[int, _Node], root: int) -> list[_Node]:
    """Depth-first, children in id order — the template's own order: the
    loop's iterations sit between the gate and the deploy."""
    out: list[_Node] = []

    def walk(nid: int) -> None:
        for cid in by_id[nid].children:
            out.append(by_id[cid])
            walk(cid)

    walk(root)
    return out


def _job_text(node: _Node, now: datetime) -> str:
    job = node.job
    if job is None:
        return "no job"
    status = node.job_status or "queued"
    handle = _handle("job", job["id"])
    meta = job["meta"]
    if status in _JOB_TERMINAL:
        then = _when(meta.get("finished_at")) or _when(job["updated_at"])
        return f"{handle} {status} {_span(then, now)} ago"
    if status == "running":
        then = _when(meta.get("started_at")) or _when(job["created_at"])
        return f"{handle} running {_span(then, now)}"
    return f"{handle} {status} {_span(_when(job['created_at']), now)}"


def _newest_ci(
    store: Store, head: str, nodes: list[_Node]
) -> tuple[str, str, str, str]:
    """``(verdict, sha, run_id, prior)`` — the stored CI verdict on the head
    sha (``release.ci_verdicts``), else the newest poll job's own record of
    that sha, else ``pending`` / ``none``. A poll record of an EARLIER head
    (the fix loop restamped the head since) is not the head's verdict: it
    comes back as ``prior`` (``red on <sha9>``), never as the verdict."""
    if not head:
        return "none", "", "", ""
    want = head.lower()[: _release.LANDED_SHA_LEN]
    best: tuple[str, int] | None = None
    pick: dict[str, Any] | None = None
    for i, row in enumerate(read_ci_verdicts(store)):
        if str(row["sha"]).lower()[: _release.LANDED_SHA_LEN] == want:
            key = (str(row.get("at") or ""), i)
            if best is None or key >= best:
                best, pick = key, row
    if pick is not None:
        verdict = str(pick["verdict"])
        if isinstance(pick.get("waived"), dict):
            verdict += " (red waived)"
        return verdict, str(pick["sha"]), str(pick.get("run_id") or ""), ""
    prior = ""
    polls = [n for n in nodes if n.role == "poll" and n.job is not None]
    for n in sorted(polls, key=lambda n: n.job["id"] if n.job else 0, reverse=True):
        rec = (n.job or {}).get("meta", {}).get("ci_verdict_poll")
        if not (isinstance(rec, dict) and rec.get("state")):
            continue
        rsha = str(rec.get("sha") or "")
        if not rsha or rsha.lower()[: _release.LANDED_SHA_LEN] == want:
            return str(rec["state"]), rsha or head, str(rec.get("run_id") or ""), ""
        prior = prior or f"{rec['state']} on {rsha[:9]}"
    if any(n.role in ("poll", "restamp") and not n.closed for n in nodes):
        return "pending", head, "", prior
    return "none", head, "", prior


def _on_main(rel: dict[str, Any]) -> str:
    out = rel.get("merge_back")
    if not isinstance(out, dict) or not out.get("state"):
        return "unknown"
    return "yes" if out["state"] in ("merged", "noop") else "no"


def _on_prod(store: Store, rel: dict[str, Any], deploy: _Node | None) -> str:
    head = str(rel.get("sha") or "").lower()[: _release.LANDED_SHA_LEN]
    if not head:
        return "unknown"
    confirmed = rel.get("confirmed")
    if (
        isinstance(confirmed, dict)
        and str(confirmed.get("sha") or "").lower()[: _release.LANDED_SHA_LEN] == head
    ):
        return "yes"
    if rel.get("deploy_scope"):
        return "yes"
    rec = read_prod_head(store)
    if rec is not None:
        seen = [str(rec["head"])] + [str(s) for s in rec.get("landed") or []]
        if any(s.lower()[: _release.LANDED_SHA_LEN] == head for s in seen):
            return "yes"
    started = rel.get("deploy_started_at")
    if deploy is not None and not deploy.closed and not started:
        return "no"
    return "unknown"


def _units(text: str) -> list[str]:
    """Review-unit keys (``td<id>`` / ``sha:<sha12>``) a refusal names."""
    out: list[str] = []
    for raw in _UNIT_RE.findall(text):
        key = raw.lower() if raw.lower().startswith("td") else f"sha:{raw.lower()}"
        if key not in out:
            out.append(key)
    return out


def _wait_asks(store: Store, key: str, rn: str) -> list[int]:
    """Open ``release-review-wait:<key>`` asks: the per-unit key, or its
    carry form scoped to release ``rn`` (``release_poll._carry_ask_key`` —
    another release's carry ask of the same unit is not this pass's)."""
    like = f"{_WAIT_TAG}:{key}"
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN'
               AND (t.value = %s OR t.value LIKE %s)
               AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rs JOIN tags s USING (tag_id)
                  WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS'
                    AND s.value = ANY(%s))
             ORDER BY r.ref_id
            """,
            (like, f"{like}:carry:{rn}:%", sorted(_CLOSED)),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _refusal(store: Store, node: _Node) -> tuple[str, datetime | None] | None:
    """The stored reason a held node carries, with when it was written:
    the deploy's newest dispatch-refusal comment (it clears
    ``meta.deploy_refusal_seen`` when it dispatches), or the cut's
    ``cut_refusal_logged``."""
    if node.role == "cut":
        note = node.meta.get("cut_refusal_logged")
        return (str(note), None) if note else None
    if node.role != "deploy" or not node.meta.get("deploy_refusal_seen"):
        return None
    for c in _comments(store, node.id):
        text = str(c.text).strip()
        if text.startswith("plan_dispatch:") and "not dispatched" in text:
            return text, _when(c.created_at)
    seen = node.meta["deploy_refusal_seen"]
    return (str(seen[-1]), None) if isinstance(seen, list) and seen else None


def _units_line(
    store: Store, keys: list[str], rn: str, ask_rows: dict[int, dict[str, Any]]
) -> str:
    """``review units waiting: td901 (ask td10), sha:… `` — each unit with
    its open review-wait ask; the asks join ``ask_rows`` (the screen's
    ``asks:`` list) — they sit outside the pass, tagged only by key."""
    parts = []
    for key in keys:
        open_asks = _wait_asks(store, key, rn)
        found = _todo_rows(store, [a for a in open_asks if a not in ask_rows])
        tags = views._all_open_tags(store, list(found))
        for aid, row in found.items():
            ask_rows[aid] = row | {"tags": tags.get(aid, [])}
        asked = ", ".join(f"td{a}" for a in open_asks)
        parts.append(f"{key} (ask {asked})" if asked else key)
    return "review units waiting: " + ", ".join(parts)


def _held_reason(text: str) -> str:
    line = text.split("\n", 1)[0].strip()
    line = re.sub(r"^plan_dispatch:\s*(?:td\d+:\s*)?", "", line)
    return _clip(line, _HELD_CAP)


def render_release(
    store: Store, ref_id: int, *, now: datetime | None = None
) -> Response:
    """The release screen for the pass holding ``ref_id`` (module docs)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    root = _pass_root(store, ref_id)
    if root is None:
        raise BadInput(
            f"td{ref_id} is not in a release pass (no release-pass root above it)",
            next=f"get(kind='todo', id={ref_id}, view='tree') reads any todo's subtree",
        )
    rows = _subtree(store, root)
    if not any(r["id"] == root for r in rows):
        raise BadInput(
            f"the release pass td{root} above td{ref_id} is retired",
            next="search(kind='todo', tags=['release-pass']) lists the passes",
        )
    todo_ids = [r["id"] for r in rows if r["kind"] == "todo"]
    statuses = views._status_of_many(store, [r["id"] for r in rows])
    tags = views._all_open_tags(store, [r["id"] for r in rows])
    by_id: dict[int, _Node] = {}
    jobs: dict[int, dict[str, Any]] = {}
    for r in rows:
        if r["kind"] == "todo":
            by_id[r["id"]] = _Node(r, statuses[r["id"]], tags.get(r["id"], []))
        else:
            jobs[r["id"]] = r
    for r in rows:
        if r["kind"] == "todo" and r["id"] != root and r["parent_id"] in by_id:
            by_id[r["parent_id"]].children.append(r["id"])
    for jid in sorted(jobs):  # id order: the last one wins as the newest
        owner = by_id.get(jobs[jid]["parent_id"])
        if owner is not None:
            owner.job = jobs[jid]
            owner.job_status = statuses.get(jid, "queued")
    root_node = by_id[root]
    rel_raw = root_node.meta.get("release")
    rel: dict[str, Any] = rel_raw if isinstance(rel_raw, dict) else {}
    ordered = _pass_order(by_id, root)
    nodes = [n for n in ordered if not _is_ask(n.tags, n.meta) and n.role != "approval"]
    blockers = open_blocked_by(store, todo_ids)

    def ask_blockers(n: _Node) -> list[int]:
        return [i for k, i in blockers.get(n.id, ()) if k == "todo"]

    # ── asks: under the pass, blocked-by from a pass node, review-wait ──
    ask_rows: dict[int, dict[str, Any]] = {}
    outside = sorted({i for n in nodes for i in ask_blockers(n)} - set(by_id))
    extra = _todo_rows(store, outside)
    extra_status = views._status_of_many(store, list(extra))
    extra_tags = views._all_open_tags(store, list(extra))
    for n in ordered:
        if _is_ask(n.tags, n.meta) and not n.closed:
            ask_rows[n.id] = n.row | {"tags": n.tags}
    for i, row in extra.items():
        t = extra_tags.get(i, [])
        if _is_ask(t, row["meta"]) and extra_status.get(i, "open") not in _CLOSED:
            ask_rows[i] = row | {"tags": t}

    # An ask behind an open blocker (the approval ask waits on the CI gate)
    # cannot be answered yet: a node waiting on it is waiting in order,
    # not held.
    unanswerable = set(open_blocked_by(store, sorted(ask_rows)))

    # ── per-node held reasons ──
    held: dict[int, list[str]] = {}
    refused_at: dict[int, datetime | None] = {}
    for n in nodes:
        if n.closed:
            continue
        stored = _refusal(store, n)
        active = n.job is not None and n.job_status not in _JOB_TERMINAL
        if stored is not None and not active:
            text, at = stored
            refused_at[n.id] = at
            reason = [f"held: {_held_reason(text)}"]
            keys = _units(text)
            if keys:
                reason.append(
                    _units_line(store, keys, str(rel.get("rN") or ""), ask_rows)
                )
            held[n.id] = reason
            continue
        waits = [i for i in ask_blockers(n) if i in ask_rows and i not in unanswerable]
        marks = [t for t in n.tags if t.startswith("waiting-for:")]
        marks += _ask_marks(n.tags)
        if n.status in ("blocked", "paused") or marks:
            newest = _comments(store, n.id)
            why = _held_reason(str(newest[0].text)) if newest else "no reason stored"
            tagged = f"tagged {', '.join(marks)} — " if marks else ""
            held[n.id] = [f"held: {tagged}{why}"]
        elif waits:
            held[n.id] = [
                "held: waiting on ask "
                + ", ".join(
                    f"{_handle('todo', i)} ({_who(ask_rows[i]['tags'], ask_rows[i]['meta'])})"
                    for i in waits
                )
            ]

    # ── header ──
    rn = str(rel.get("rN") or "r?")
    head = str(rel.get("sha") or "")
    cut = str(rel.get("cut_sha") or head)
    host = str(rel.get("host") or "").strip() or _release.cut_host(store) or "-"
    branch = _release.release_branch(rn) if rel else "-"
    lines = [
        f"release {rn}  branch {branch}  cut {cut[:9] or '-'}  "
        f"head {head[:9] or '-'}{_ahead_note(nodes)}  cut host {host}"
    ]
    lines.append(f"state: {_state(root_node, nodes, held, refused_at, now)}")

    # ── CI, main, prod ──
    verdict, vsha, run, prior = _newest_ci(store, head, nodes)
    run_txt = f" (run {run})" if run else ""
    prior_txt = f" (earlier head: {prior})" if prior else ""
    lines.append(f"CI: {verdict} on {vsha[:9] or '-'}{run_txt}{prior_txt}")
    deploy = next((n for n in nodes if n.role == "deploy"), None)
    lines.append(
        f"head on main: {_on_main(rel)}   on prod: {_on_prod(store, rel, deploy)}"
    )

    # ── nodes ──
    width = max((len(n.label) for n in nodes), default=4)
    hwidth = max((len(_handle("todo", n.id)) for n in nodes), default=2)
    for n in nodes:
        lines.append(
            f"{_handle('todo', n.id).ljust(hwidth)} {n.label.ljust(width)} "
            f"{n.status.ljust(8)} {_job_text(n, now)}"
        )
        lines.extend(f"    {x}" for x in held.get(n.id, ()))

    # ── asks ──
    if not ask_rows:
        lines.append("asks: none")
    else:
        lines.append("asks:")
        ask_blocked = open_blocked_by(store, sorted(ask_rows))
        for aid in sorted(ask_rows):
            row = ask_rows[aid]
            ask = row["meta"].get("ask")
            opts = ask.get("options") if isinstance(ask, dict) else None
            answer = (
                " | answer: " + " / ".join(str(o) for o in opts)
                if isinstance(opts, list) and opts
                else ""
            )
            blocked = (
                " | not answerable yet: blocked by "
                + ", ".join(_handle(k, i) for k, i in ask_blocked[aid])
                if ask_blocked.get(aid)
                else ""
            )
            lines.append(
                f"  {_handle('todo', aid)} {_clip(row['title'], _TITLE_CAP)} | "
                f"{_who(row['tags'], row['meta'])}{answer}{blocked}"
            )

    body = "\n".join(lines)
    body += render_next_section(
        [
            (f"get(kind='todo', id={root}, view='tree')", "the whole pass subtree"),
            (f"get(kind='todo', id={root}, view='meta')", "the stored release record"),
        ]
    )
    return Response(body=body)


def _state(
    root: _Node,
    nodes: list[_Node],
    held: dict[int, list[str]],
    refused_at: dict[int, datetime | None],
    now: datetime,
) -> str:
    """One line: where the pass is, from the first open action node."""
    if root.status in _CLOSED and root.status != "done":
        return f"closed ({root.status})"
    actions = [n for n in nodes if n.role not in _WAITING_ROLES]
    if root.status == "done" or (actions and all(n.closed for n in actions)):
        return "done"
    nxt = next((n for n in actions if not n.closed), None)
    if nxt is None:
        return "waiting on the CI gate"
    it = f" (iteration {nxt.iteration})" if nxt.iteration else ""
    if nxt.id in held:
        at = refused_at.get(nxt.id)
        since = f" since {_clock(at, now)}" if at else ""
        why = held[nxt.id][0].removeprefix("held: ")
        if nxt.role == "deploy" and nxt.meta.get("deploy_refusal_seen"):
            return f"deploy HELD{since}"
        return f"{nxt.role} HELD{since} — {_clip(why, 90)}"
    word = _STATE_WORD.get(nxt.role, nxt.role)
    job = nxt.job
    ended = job is not None and nxt.job_status in _JOB_TERMINAL
    last = f"{_handle('job', job['id'])} {nxt.job_status}" if job is not None else ""
    if nxt.role == "deploy":
        if job is None:
            return "deploy not dispatched yet"
        # A dispatch clears the stored refusal, so a deploy whose job ended
        # with the node still open reads from the job, never as undispatched.
        if ended:
            return f"deploy job {last}; the deploy node is still open"
        return "deploying"
    if ended and nxt.job_status != "succeeded":
        return f"{word}{it} — last job {last}"
    return f"{word}{it}"
