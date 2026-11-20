"""``release_fix_handoff`` — file a release fixer's marker lines.

A release branch's red is owned by one fixer session, whose prompt tells it
to report defects in the pipeline itself (the lander, the lint, the digest,
CI infrastructure) as gripes and the failures it could not fix as ``unfixed``
lines. A builder lane's precis MCP is read-only or absent, so the session
cannot write either: it ends its final message with marker lines, one per
line, at line start,

    gripe: <what ran, what you expected, what happened>
    unfixed: <test id> — <why>

and :func:`file_markers` — called by ``run_slice``'s poll on the same text it
posts as the ``handoff:`` comment — files them:

* each ``gripe:`` line (text of 20 characters or more, at most
  :data:`GRIPE_CAP` per hand-off) becomes one gripe ``[release-fix] <text>``
  (``STATUS:open``, tags ``origin:release-fix-handoff`` and
  ``cluster:dark-factory``, ``related-to`` the fix todo, text masked by
  :func:`precis.utils.secret_scan.mask_secrets`). Never ``fix:code``: the
  gripe is for triage, not a build chain.
* the ``unfixed:`` lines go in ONE comment on the plan root, so the next
  iteration's fixer and a human see what the pass left red.
* one comment on the fix todo names the gripes filed and the count dropped
  by the cap.

Dedupe is the plan root's ``meta.release_fix_handoff``:
``{"gripes": {<digest>: "gr<N>"}, "jobs": [<job id>, ...]}``. The digest is
the first 16 hex chars of the sha1 of the masked text, lower-cased and
whitespace-collapsed, so one defect named again by a later attempt or a later
iteration of the same release pass (one plan root) is filed once; ``jobs``
holds the sessions already processed, so a re-polled job posts nothing twice.
The gripe's insert, link and the root stamp share one transaction.

Scope: only a release-fix node — its meta carries a release land target and
it is not a reviewer. Everything else's hand-off is left as it is. The caller
wraps the call: a failure here never changes the job's outcome.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Any

from precis.plans import release as _release
from precis.store.types import ChunkInsert, Tag
from precis.utils.secret_scan import mask_secrets

log = logging.getLogger(__name__)

#: ``refs.meta`` key on the plan root holding the dedupe record.
STATE_KEY = "release_fix_handoff"
#: ``refs.meta`` key on a filed gripe naming where it came from.
GRIPE_META_KEY = "release_fix_gripe"
#: Most gripes filed from one hand-off; the rest are only counted.
GRIPE_CAP = 5
#: A gripe line whose text is shorter is not a report.
MIN_TEXT = 20
#: Cap on one ``unfixed:`` line and on how many go in the root comment.
_UNFIXED_LINE_CAP = 300
_UNFIXED_MAX = 20
_TITLE_CAP = 200
#: Processed job ids kept on the plan root (a pass runs a handful).
_JOBS_KEPT = 50
_TITLE_PREFIX = "[release-fix] "
ORIGIN_TAG = "origin:release-fix-handoff"
CLUSTER_TAG = "cluster:dark-factory"

#: A marker opens its line, but a session formats: the brief shows the line
#: in backticks, and a hand-off list bullets or bolds it. Leading
#: indentation, one list bullet and inline-code or emphasis marks around the
#: keyword are tolerated — a marker lost to formatting would vanish silently.
_LEAD = r"^[ \t]*(?:[-*+][ \t]+)?(?P<open>[`*_]*)"
_GRIPE_RE = re.compile(
    _LEAD + r"gripe:[*_]*(?P<text>.*)$", re.IGNORECASE | re.MULTILINE
)
_UNFIXED_RE = re.compile(
    _LEAD + r"unfixed:[*_]*(?P<text>.*)$", re.IGNORECASE | re.MULTILINE
)


def _unwrap(m: re.Match[str]) -> str:
    """The marker's text without the closing half of a mark that opened
    before the keyword (a whole line in backticks or bold); marks inside
    the text, an identifier in backticks say, stay."""
    text = m.group("text").strip()
    for mark in ("`", "**", "__", "*", "_"):
        if (
            mark in m.group("open")
            and text.endswith(mark)
            and text.count(mark) % 2 == 1
        ):
            text = text[: -len(mark)].rstrip()
    return text


def _digest(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def parse_markers(handoff: str) -> tuple[list[str], list[str]]:
    """``(gripe texts, unfixed texts)`` of a hand-off: marker lines that open
    a line (:data:`_LEAD`), text stripped and masked; a gripe text under :data:`MIN_TEXT`
    characters and an empty ``unfixed`` text are dropped, and a gripe text
    repeated within the hand-off counts once."""
    gripes: list[str] = []
    seen: set[str] = set()
    for m in _GRIPE_RE.finditer(handoff or ""):
        raw = _unwrap(m)
        if len(raw) < MIN_TEXT:
            continue
        text = mask_secrets(raw)
        key = _digest(text)
        if key not in seen:
            seen.add(key)
            gripes.append(text)
    unfixed = [
        mask_secrets(t)
        for m in _UNFIXED_RE.finditer(handoff or "")
        if (t := _unwrap(m))
    ]
    return gripes, unfixed


def _is_release_fix(meta: dict[str, Any] | None) -> bool:
    if _release.land_target_of(meta) is None:
        return False
    gen = (meta or {}).get("generate")
    return not (isinstance(gen, dict) and gen.get("role") == "reviewer")


def _state(root_meta: dict[str, Any] | None) -> dict[str, Any]:
    raw = (root_meta or {}).get(STATE_KEY)
    raw = raw if isinstance(raw, dict) else {}
    gripes = raw.get("gripes")
    jobs = raw.get("jobs")
    return {
        "gripes": dict(gripes) if isinstance(gripes, dict) else {},
        "jobs": list(jobs) if isinstance(jobs, list) else [],
    }


def _mint_gripe(
    store: Any,
    *,
    text: str,
    digest: str,
    todo_id: int,
    root: int,
    job_id: int,
    rn: str,
    state: dict[str, Any],
) -> int:
    """Insert the gripe (the ``factory_rescue._file_sig_gripe`` shape: ref,
    ``gripe_body`` chunk, ``STATUS:open``, origin + cluster tags), link it to
    the fix todo and record it in the root's dedupe state — one transaction.
    ``state`` gains the digest only once the transaction committed."""
    title = _clip(_TITLE_PREFIX + " ".join(text.split()), _TITLE_CAP)
    where = f"release {rn}, " if rn else ""
    body = (
        f"{_TITLE_PREFIX}{text}\n\n"
        f"Filed by the dispatcher from the hand-off of td{todo_id} "
        f"({where}job jo{job_id}); the fixer session cannot write to the graph."
    )
    meta = {
        GRIPE_META_KEY: {
            "digest": digest,
            "plan": root,
            "todo": todo_id,
            "job": job_id,
        }
    }
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="gripe", slug=None, title=title, meta=meta, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in (ORIGIN_TAG, CLUSTER_TAG):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.add_link(
            src_ref_id=todo_id,
            dst_ref_id=int(ref.id),
            relation="related-to",
            set_by="system",
            conn=conn,
        )
        gripes = {**state["gripes"], digest: f"gr{ref.id}"}
        store.stamp_ref_meta(
            root, {STATE_KEY: {"gripes": gripes, "jobs": state["jobs"]}}, conn=conn
        )
    state["gripes"] = gripes
    return int(ref.id)


def file_markers(
    store: Any,
    *,
    todo_id: int,
    job_id: int | None,
    handoff: str,
    append_comment: Callable[[Any, int, str], None],
) -> list[int]:
    """File the ``gripe:`` / ``unfixed:`` lines of the hand-off of fix todo
    ``todo_id``; return the gripe ids minted. ``append_comment(store,
    todo_id, text)`` posts a todo comment. A hand-off with no marker line,
    a node outside the scope, or a job already processed does nothing."""
    gripes, unfixed = parse_markers(handoff)
    if not gripes and not unfixed:
        return []
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None or not _is_release_fix(ref.meta):
        return []
    target = _release.land_target_of(ref.meta) or ""
    rn = target.rsplit("/", 1)[-1]
    root = _release.plan_root(store, todo_id)
    root_ref = store.get_ref(kind="todo", id=root)
    state = _state(root_ref.meta if root_ref is not None else None)
    job = int(job_id or 0)
    if job and job in state["jobs"]:
        return []

    dropped = max(0, len(gripes) - GRIPE_CAP)
    filed: list[int] = []
    for text in gripes[:GRIPE_CAP]:
        digest = _digest(text)
        if digest in state["gripes"]:
            continue
        try:
            filed.append(
                _mint_gripe(
                    store,
                    text=text,
                    digest=digest,
                    todo_id=todo_id,
                    root=root,
                    job_id=job,
                    rn=rn,
                    state=state,
                )
            )
        except Exception:
            log.warning(
                "release_fix_handoff: gripe from td%d's hand-off not filed",
                todo_id,
                exc_info=True,
            )

    if unfixed:
        lines = [_clip(t, _UNFIXED_LINE_CAP) for t in unfixed[:_UNFIXED_MAX]]
        if len(unfixed) > _UNFIXED_MAX:
            lines.append(f"(+{len(unfixed) - _UNFIXED_MAX} more not shown)")
        try:
            append_comment(
                store,
                root,
                f"release-fix td{todo_id} left red:\n"
                + "\n".join(f"- {t}" for t in lines),
            )
        except Exception:
            log.warning(
                "release_fix_handoff: unfixed comment for td%d not posted",
                todo_id,
                exc_info=True,
            )

    if filed or dropped:
        note = "filed from this hand-off's gripe: lines: " + (
            ", ".join(f"gr{g}" for g in filed) if filed else "none new"
        )
        if dropped:
            note += f" ({dropped} over the cap of {GRIPE_CAP} not filed)"
        try:
            append_comment(store, todo_id, note)
        except Exception:
            log.warning(
                "release_fix_handoff: node comment on td%d not posted",
                todo_id,
                exc_info=True,
            )

    if job:
        store.stamp_ref_meta(
            root,
            {
                STATE_KEY: {
                    "gripes": state["gripes"],
                    "jobs": [*state["jobs"], job][-_JOBS_KEPT:],
                }
            },
        )
    return filed
