"""JobHandler — the offline-work substrate.

A ``job`` ref carries the intent to run something offline. The
worker (executor) runs the actual work; the handler here owns the
MCP-side surface: validate the submit, dedupe by idempotency key,
auto-tag the linked parent (the linked gripe for ``fix_gripe``),
and render the job header + status + summary on ``get``.

See ``precis-job-help`` for the agent-facing surface and
``precis-fix-gripe-help`` for the first concrete job_type.
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers import _todo_guards as todo_guards
from precis.handlers._link_tag_ops import validate_relation
from precis.handlers._link_target import parse_link_target
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import Ref
from precis.workers.executors import (
    DEFAULT_EXECUTOR,
    EXECUTOR_PROVIDES,
    is_known_executor,
)
from precis.workers.job_types import get_job_type, known_job_types

_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


def _idem_lock_key(idem: str) -> int:
    """Hash an idem string to a 64-bit signed integer for advisory lock.

    ``pg_advisory_xact_lock`` takes a ``bigint``. We hash via BLAKE2b
    truncated to 8 bytes and reinterpret as a signed int so the
    value fits Postgres's ``bigint`` range. Stable across processes
    so two workers racing the same idem key serialize correctly.
    The hash is not security-sensitive: collisions just cause two
    unrelated puts to share a lock briefly.
    """
    digest = hashlib.blake2b(idem.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


_JOB_SUMMARY_KIND = "job_summary"
_JOB_EVENT_KIND = "job_event"

# A job's parent is polymorphic (ADR 0044). ``todo`` is the intent lane
# (rotation + ``child-failed`` bubble); the artifact kinds are the compute
# lane — a derived, cache-fillable build step (DFT relax / route / mesh /
# compile) owned by its subject ref, not a task. Behaviour branches on the
# resolved parent kind, not on a declared job-class flag: the distinction is
# emergent from the parent pointer the caller sets anyway.
JOB_PARENT_KINDS: frozenset[str] = frozenset({"todo", "structure", "cad", "draft"})

#: ADR 0044 extension (good-search-coordinator §Substrate fixes #3): a
#: ``kind='job'`` parent is additionally allowed, but ONLY when that
#: parent job is itself a coordinator (``meta.executor ==
#: 'coordinator'``) — a campaign's fan-out children hang under the
#: coordinator that minted them, not under a todo, so their success /
#: failure never auto-closes or bubbles onto anybody's todo (the
#: coordinator reads child terminal status itself on resume). The
#: executor check lives in :meth:`JobHandler.put` after the kind
#: resolves; ordinary jobs don't own child trees.
_JOB_PARENT_KINDS_WITH_COORDINATOR: frozenset[str] = JOB_PARENT_KINDS | frozenset(
    {"job"}
)


class JobHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="job",
        title="Job",
        description=(
            "Offline run of a task — fix this gripe, run a "
            "simulation, benchmark a commit. Numeric id; status "
            "via STATUS: tags; comment timeline via job_event / "
            "job_summary chunks."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "job"
    sense: ClassVar[str] = "job"
    # No default tag: the queued state is set after validation in put.
    default_tags_on_create: ClassVar[tuple[str, ...]] = ()

    def _plugin_owner_kinds(self) -> frozenset[str]:
        """Kinds that opted into owning compute-lane jobs via
        ``KindSpec.can_own_jobs`` (ADR 0044 plugin extension). Lets a
        plugin kind (e.g. catpath's ``pathway``) own its derived build
        job without a core edit to :data:`JOB_PARENT_KINDS`. Empty when
        the hub isn't reachable (defensive — falls back to built-ins)."""
        hub = getattr(self, "hub", None)
        if hub is None:
            return frozenset()
        out: set[str] = set()
        for k in getattr(hub, "kinds", ()) or ():
            try:
                spec = hub.handler_for(k).spec
            except Exception:
                continue
            if getattr(spec, "can_own_jobs", False):
                out.add(k)
        return frozenset(out)

    # ── put: validated submit ───────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        job_type: str | None = None,
        executor: str | None = None,
        params: dict[str, Any] | None = None,
        idem_key: str | None = None,
        parent_id: int | str | None = None,
        model: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Retry surface: ``put(kind='job', id=N, mode='retry')`` re-runs a
        # failed job by clearing the parent todo's failure-bubble so the
        # dispatch worker re-mints a fresh attempt. ``model='sonnet'``
        # additionally swaps the parent's ``LLM:<model>`` tag first, which
        # is what the next tick dispatches with. Handled before the
        # generic ``id is not None`` / ``mode`` rejections below.
        if mode == "retry":
            return self._retry_job(id=id, model=model)
        if id is not None:
            raise BadInput(
                f"put on existing job id={id!r} is not supported",
                next=(
                    f"to re-run a failed job: put(kind='job', id={id}, "
                    "mode='retry'[, model='sonnet']). "
                    f"to mutate id={id}: tag(kind='job', id=N, add=[...]) / "
                    f"link(kind='job', id=N, target=..., mode='add'|'remove')"
                ),
            )
        if mode is not None or untags is not None or unlink is not None:
            raise BadInput(
                "mode= / untags= / unlink= are not accepted on job put",
                next="use tag() / link() / delete() against an existing job",
            )
        if job_type is None or not str(job_type).strip():
            raise BadInput(
                "put(kind='job') requires job_type=",
                next=(
                    "put(kind='job', job_type='fix_gripe', link='gripe:N', rel='fixes')"
                ),
            )
        spec = get_job_type(str(job_type))
        if spec is None:
            raise BadInput(
                f"unknown job_type {job_type!r}; known: {known_job_types()}",
                options=known_job_types(),
            )

        # Resolve executor. Default to the type's first compatible
        # one (in v1 there's only one), reject if the caller asked
        # for one the type doesn't support.
        resolved_executor = executor or _default_executor_for(spec)
        if not is_known_executor(resolved_executor):
            raise BadInput(
                f"unknown executor {resolved_executor!r}",
                options=list(EXECUTOR_PROVIDES.keys()),
            )
        if resolved_executor not in spec.compatible_executors:
            raise BadInput(
                f"job_type {spec.name!r} does not support executor "
                f"{resolved_executor!r}",
                next=(f"compatible executors: {sorted(spec.compatible_executors)}"),
            )
        missing = spec.requires - EXECUTOR_PROVIDES[resolved_executor]
        if missing:
            raise BadInput(
                f"executor {resolved_executor!r} does not provide "
                f"capabilities required by {spec.name!r}: "
                f"{sorted(missing)}",
                next=(
                    "this is an executor / job_type mismatch — file a "
                    "gripe if you think the capability should be added"
                ),
            )

        # Validate params against the type's schema. The schema is
        # tiny for v1 (fix_gripe takes none) so a hand-rolled
        # validator covers it without pulling in jsonschema.
        params = params or {}
        _validate_params(params, spec.params_schema, job_type=spec.name)

        # Idempotency: if the caller didn't supply a key, derive
        # one from the link target so re-submits for the same parent
        # collapse onto the in-flight job.
        if link is not None:
            target = parse_link_target(link, store=self.store)
            relation = validate_relation(rel)
            if rel is None and spec.name == "fix_gripe":
                # fix_gripe wants rel='fixes'; supply it implicitly
                # so a caller who omits rel= still gets the right
                # graph edge.
                relation = validate_relation("fixes")
        else:
            target = None
            relation = validate_relation(rel) if rel is not None else "related-to"
            if spec.name == "fix_gripe":
                raise BadInput(
                    "fix_gripe requires link='gripe:N' rel='fixes'",
                    next=(
                        "put(kind='job', job_type='fix_gripe', "
                        "link='gripe:42', rel='fixes')"
                    ),
                )

        resolved_idem = idem_key or (link if link is not None else None)

        # Per-type submit-time validation. For fix_gripe this is
        # where the repo-resolution check lives (the gripe's
        # ``repo:<name>`` tag must match an entry in
        # ``PRECIS_FIX_REPOS``, or the deployment must carry a
        # ``PRECIS_FIX_REPO_DIR`` fallback). Surfacing the
        # rejection at put time avoids a zombie queued job that
        # would only fail when the runner picked it up.
        # Fires for a linked put (``target`` = the gripe/todo the job
        # acts on, passed as ``gripe_id``) AND for a parent-only put
        # (``target is None`` → ``gripe_id=None``). The latter is how a
        # linkless job_type like ``sandbox_run`` gets its fail-closed
        # gate at put time; ``validate_submit`` signatures accept
        # ``gripe_id=None`` (fix_gripe always carries a link, so it still
        # sees a real id).
        if spec.validate_submit is not None:
            err = spec.validate_submit(
                self.store,
                gripe_id=(target.ref_id if target is not None else None),
                params=params,
            )
            if err is not None:
                raise BadInput(err)

        # Idempotency check moves inside the write transaction below,
        # serialized via ``pg_advisory_xact_lock``. The lookup here
        # used to run in its own connection — two concurrent puts
        # with the same idem key could both see "no row" and both
        # insert. The locked re-check at write time fixes that race.

        # ── tree-position guard (Slice 5: jobs hang off an owner ref) ──
        # Every new job must declare a parent: a ``todo`` (intent lane —
        # rotation + the ``child-failed`` bubble) or, per ADR 0044, the
        # subject artifact it builds (compute lane — DFT relax / route /
        # compile, owned by the structure/cad/draft, not a task). Orphan
        # jobs are a leftover from the pre-tree v1 substrate. The check
        # fires AFTER the existing job_type / executor / link validations
        # so rejection messages from earlier paths stay unchanged for the
        # tests that exercise them — only happy-path puts need parent_id.
        if parent_id is None:
            raise BadInput(
                "put(kind='job') requires parent_id — the todo this job "
                "executes, or the artifact a derived job builds",
                next=(
                    "canonical pattern: put(kind='todo', "
                    "meta={'executor': ..., 'job_type': ...}) then let "
                    "the dispatch worker mint the job under it. For an "
                    "ad-hoc submit: put(kind='job', parent_id=<todo_id>, "
                    "job_type=..., link='gripe:N', rel='fixes'). A derived "
                    "build parents on its subject ref (e.g. a structure)."
                ),
            )
        try:
            parent_int = parent_id if isinstance(parent_id, int) else int(parent_id)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"parent_id must be an integer, got {parent_id!r}",
                next="parent_id=<int> (the parent todo or subject artifact id)",
            ) from exc
        # Resolve + kind-check the parent. Accepts a todo, a build
        # subject (:data:`JOB_PARENT_KINDS`), or a coordinator job
        # (the ADR 0044 extension — see
        # :data:`_JOB_PARENT_KINDS_WITH_COORDINATOR`); the returned kind
        # is what the failure-bubble later branches on. Same
        # NotFound/BadInput shape as the todo-tree guard for missing /
        # soft-deleted parents.
        _, parent_kind = todo_guards.check_job_parent_exists(
            self.store,
            parent_int,
            allowed_kinds=_JOB_PARENT_KINDS_WITH_COORDINATOR
            | self._plugin_owner_kinds(),
        )
        if parent_kind == "job":
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT meta->>'executor' FROM refs WHERE ref_id = %s",
                    (parent_int,),
                ).fetchone()
            parent_executor = row[0] if row else None
            if parent_executor != "coordinator":
                raise BadInput(
                    f"parent_id={parent_int} is a job with executor="
                    f"{parent_executor!r}; a job may only parent on a "
                    "coordinator job (ADR 0044 extension); ordinary jobs "
                    "don't own child trees",
                    next=(
                        "parent the child on the coordinator job that owns "
                        "the fan-out, or on a todo / build subject"
                    ),
                )

        # Compose title + meta + queued tag.
        title = f"{spec.name} ({link or 'unlinked'})"
        meta: dict[str, Any] = {
            "job_type": spec.name,
            "executor": resolved_executor,
            "params": params,
        }
        if resolved_idem is not None:
            meta["idem_key"] = resolved_idem

        parsed_tags: list[Tag] = [Tag.parse_strict("STATUS:queued", kind=self.kind)]
        if tags is not None:
            parsed_tags.extend(Tag.parse_strict(t, kind=self.kind) for t in tags)

        with self.store.tx() as conn:
            # Race-safe idempotency: serialize concurrent puts that
            # share an idem key on a transaction-scoped Postgres
            # advisory lock. The first put to acquire the lock sees
            # "no row" from ``_lookup_idem`` and inserts; the second
            # waits for the first's COMMIT to release the lock, then
            # sees the just-inserted row and returns its id without
            # creating a duplicate.
            if resolved_idem is not None:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_idem_lock_key(resolved_idem),),
                )
                existing = self._lookup_idem(resolved_idem, conn=conn)
                if existing is not None:
                    return Response(
                        body=(
                            f"existing job id={existing} for "
                            f"idem_key={resolved_idem!r} is still active "
                            "(returning that id instead of creating a "
                            "duplicate)"
                        )
                    )

            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=title,
                meta=meta,
                parent_id=parent_int,
                conn=conn,
            )
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation,
                    conn=conn,
                )
                # Side-effect: auto-tag the linked parent for
                # job_types that have one. For fix_gripe the
                # parent is the gripe; bump it to ready_for_fix
                # so the lifecycle reads cleanly even when the
                # human skipped the explicit triage step.
                if spec.name == "fix_gripe":
                    self.store.add_tag(
                        target.ref_id,
                        Tag.parse_strict("STATUS:ready_for_fix"),
                        set_by="agent",
                        replace_prefix=True,
                        conn=conn,
                    )

        return Response(
            body=(
                f"created job id={ref.id} (STATUS:queued, "
                f"job_type={spec.name!r}, executor={resolved_executor!r}). "
                f"poll: get(kind='job', id={ref.id})."
            )
        )

    # ── retry: re-run a failed job via the parent todo ─────────────

    def _retry_job(self, *, id: str | int | None, model: str | None) -> Response:
        """Re-run a failed job by unblocking its parent todo.

        A job failure bubbles a ``child-failed:<job_id>`` open tag onto
        the parent todo, which excludes it from the doable rotation (see
        ``_job_bubble`` + the dispatch candidate guard). Retry clears that
        bubble so the dispatch worker re-mints a fresh job on its next
        sweep. The failed job itself is left in place for forensics — a
        ``STATUS:failed`` child is terminal, so it doesn't block re-mint.

        ``model`` (optional) swaps the parent's ``LLM:<model>`` tag before
        clearing the bubble, so the re-minted tick runs on a different
        tier. Closed-vocab (``opus``/``sonnet``/``haiku``) — validated by
        :meth:`Tag.parse_strict`. Only valid on an LLM-planner todo (one
        already carrying an ``LLM:*`` tag); a code-path executor job has
        no model knob.
        """
        if id is None:
            raise BadInput(
                "put(kind='job', mode='retry') requires id= (the failed job)",
                next="put(kind='job', id=<failed_job_id>, mode='retry', model='sonnet')",
            )
        job_id = self._coerce_id(id)
        # Validates the job exists and is live (not soft-deleted).
        self._resolve_live_ref(job_id)
        status = _status_of(self.store.tags_for(job_id))
        if status not in ("failed", "cancelled"):
            raise BadInput(
                f"job id={job_id} is STATUS:{status or 'unset'}; only a failed "
                "or cancelled job can be retried",
                next="wait for the job to reach a terminal state, or delete it",
            )

        from precis.handlers._job_bubble import _lookup_parent

        parent_id, parent_kind = _lookup_parent(self.store, job_id, conn=None)
        if parent_id is None or parent_kind != "todo":
            raise BadInput(
                f"job id={job_id} has no todo parent to re-dispatch from "
                "(legacy orphan job)",
                next=(
                    "re-create the work as a todo (with LLM:* / meta.executor) "
                    "and let the dispatch worker mint a fresh job under it"
                ),
            )

        new_model_tag: Tag | None = None
        if model is not None:
            has_llm = any(
                str(t).startswith("LLM:") for t in self.store.tags_for(parent_id)
            )
            if not has_llm:
                raise BadInput(
                    f"model= only applies to LLM-planner todos; parent todo "
                    f"#{parent_id} carries no LLM:* tag",
                    next="retry without model=, or set the executor's params by hand",
                )
            # Closed-vocab validation (opus|sonnet|haiku) lives in parse_strict.
            new_model_tag = Tag.parse_strict(f"LLM:{str(model).strip()}", kind="todo")

        bubble = Tag.open(f"child-failed:{job_id}")
        with self.store.tx() as conn:
            if new_model_tag is not None:
                self.store.add_tag(
                    parent_id,
                    new_model_tag,
                    set_by="agent",
                    replace_prefix=True,
                    conn=conn,
                )
            self.store.remove_tag(parent_id, bubble, conn=conn)

        model_note = (
            f", swapped model→{str(model).strip()}" if model is not None else ""
        )
        return Response(
            body=(
                f"retry queued: cleared child-failed:{job_id} on todo "
                f"#{parent_id}{model_note}. The dispatch worker re-mints a "
                f"fresh job on its next sweep (~1 min); the failed job "
                f"#{job_id} stays for forensics. poll: get(kind='todo', "
                f"id={parent_id})."
            )
        )

    # ── tag override: failure-bubble to parent todo ──────────────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Tag a job + bubble ``child-failed:<job_id>`` to the parent todo
        when STATUS:failed is added.

        Slice-5: a job failure surfaces on the parent so the operator
        decides next move (re-dispatch, switch executor, ask user).
        The bubble fires only on the ``STATUS:failed`` add — other
        status transitions don't surface (a success resolves the
        parent via ``auto_check.child_job_succeeded`` instead).
        """
        resp = super().tag(id=id, add=add, remove=remove, **_kw)
        if add and any(a == "STATUS:failed" for a in add):
            from precis.handlers._job_bubble import bubble_job_failure

            job_id = self._coerce_id(id)
            bubble_job_failure(self.store, job_id)
        return resp

    # ── render: header + status + summary + recent events ──────────

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:  # type: ignore[override]
        lines = [f"# job {ref.id}"]
        status = _status_of(tags)
        if status is not None:
            lines.append(f"status: {status}")
        meta = ref.meta or {}
        if meta.get("job_type"):
            lines.append(f"job_type: {meta['job_type']}")
        if meta.get("executor"):
            lines.append(f"executor: {meta['executor']}")
        if meta.get("wall_seconds") is not None:
            lines.append(f"wall_seconds: {meta['wall_seconds']:.1f}")
        if meta.get("branch"):
            lines.append(f"branch: {meta['branch']}")
        if meta.get("sha"):
            lines.append(f"sha: {meta['sha']}")
        lines.append("")
        if ref.title:
            lines.append(ref.title)

        blocks = self.store.list_blocks_for_ref(ref.id)
        for block in blocks:
            kind = block.chunk_kind
            if kind == _JOB_SUMMARY_KIND:
                lines.append("")
                lines.append("## summary")
                lines.append(block.text)
            elif kind == _JOB_EVENT_KIND:
                lines.append("")
                lines.append(f"## event {block.pos}")
                lines.append(block.text)
        return "\n".join(lines)

    # ── helpers ────────────────────────────────────────────────────

    def _lookup_idem(self, idem: str, *, conn: Any = None) -> int | None:
        """Return an active job id for ``idem_key=idem`` if one exists.

        "Active" = `STATUS:queued` or `STATUS:running`. Terminal
        jobs (succeeded / failed / cancelled) don't block a fresh
        attempt — the caller asked for a retry and the substrate
        delivers it.

        When ``conn`` is supplied the lookup runs inside that
        transaction. Used by :meth:`put` after taking the
        ``pg_advisory_xact_lock`` keyed on the idem string so two
        concurrent submits with the same key serialize and only
        one of them creates a row. With ``conn=None`` the lookup
        opens a short-lived pool connection — race-prone but kept
        for callers that just want a "does this exist yet" check
        outside any write path.
        """

        def _query(c: Any) -> int | None:
            rows = c.execute(
                """
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'job' AND r.deleted_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = r.ref_id
                            AND t.namespace = 'STATUS'
                            AND t.value = ANY(%s)
                       )
                 ORDER BY r.ref_id DESC
                 LIMIT 1
                """,
                (idem, list(_TERMINAL_STATUSES)),
            ).fetchall()
            return int(rows[0][0]) if rows else None

        if conn is not None:
            return _query(conn)
        with self.store.pool.connection() as own_conn:
            return _query(own_conn)


# ── small free helpers ────────────────────────────────────────────


def _status_of(tags: list[Tag]) -> str | None:
    for t in tags:
        s = str(t)
        if s.startswith("STATUS:"):
            return s[len("STATUS:") :]
    return None


def _default_executor_for(spec: Any) -> str:
    """Pick a default executor for ``spec`` when caller omits one."""
    if DEFAULT_EXECUTOR in spec.compatible_executors:
        return DEFAULT_EXECUTOR
    # The spec is locally inconsistent if it lists no executors,
    # but the dispatcher in put() catches the empty-set case and
    # surfaces a clear error.
    return next(iter(sorted(spec.compatible_executors)), DEFAULT_EXECUTOR)


def _validate_params(
    params: dict[str, Any], schema: dict[str, Any], *, job_type: str
) -> None:
    """Tiny jsonschema-shaped validator.

    Implements only the bits v1 needs: ``required`` + per-property
    ``type`` (``integer`` / ``string`` / ``object``) +
    ``additionalProperties=False``. Swap for ``jsonschema`` if a
    job_type's schema ever needs richer constraints.
    """
    if not isinstance(params, dict):
        raise BadInput(
            f"params must be a dict for job_type={job_type!r}",
            next="params={...}",
        )
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for key in required:
        if key not in params:
            raise BadInput(
                f"job_type={job_type!r} requires params.{key}",
                next=f"params={{{key!r}: ...}}",
            )
    if schema.get("additionalProperties") is False:
        unknown = set(params) - set(properties)
        if unknown:
            raise BadInput(
                f"job_type={job_type!r} got unknown params: {sorted(unknown)}",
                next=f"allowed params: {sorted(properties)}",
            )
    for key, value in params.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        expected = prop_schema.get("type")
        if expected == "integer" and not isinstance(value, int):
            raise BadInput(
                f"params.{key} must be an integer (got {type(value).__name__})"
            )
        if expected == "string" and not isinstance(value, str):
            raise BadInput(
                f"params.{key} must be a string (got {type(value).__name__})"
            )
        if expected == "object" and not isinstance(value, dict):
            raise BadInput(
                f"params.{key} must be an object (got {type(value).__name__})"
            )


__all__ = ["JobHandler"]
