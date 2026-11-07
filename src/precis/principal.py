"""Who made a write, and what a slice session may write (td575705 G1).

Two halves sharing one ContextVar:

- **Principal on writes.** Every write records its principal, server-set
  and never caller-supplied: ``slice:td<t>j<j>:<role>`` (a validated
  slice grant, role resolved from the grant's own todo meta),
  ``install`` (the install bearer), ``user:<abbrev>`` (a web write under
  ``human_write_context``), ``personal:<token id>`` (a personal MCP
  token) and ``worker:<PRECIS_SOURCE>`` for in-process writes with no
  bound request (``cli`` when no source is set). The value lands in
  chunk ``meta.principal`` (comments), ``filed_by.principal`` +
  ``revisions.writer.principal`` (via
  :func:`precis.filing_origin.current_filed_by`) and
  ``ref_tags.principal`` (migration 0287 — ``set_by`` is an FK to the
  closed ``actors`` table and cannot hold these strings). Tag removal
  and deletes leave no row to attribute: nothing is recorded there.

- **Slice scope.** A slice grant is now a boundary as well as
  attribution (gr575691). Reads (``get``/``search``) always pass.
  Writes are limited by the role the grant's todo declares:

    - every role: comment on the grant's own todo; file a new gripe;
    - ``builder``: plus tags on its own todo from
      :data:`SLICE_TAG_ALLOW` (empty until the observe readout names the
      tags real builders reach for);
    - ``generator``: plus ``put(kind='todo')`` children under its own
      todo whose meta keys stay within ``{llm_tier, op}`` with
      ``op={'type': 'run_slice', 'params': {'model', 'effort'}}``, plus
      ``link rel='blocked-by'`` between children of its own todo;
    - ``reviewer``, ``analyst``, ``brief``, ``security-reviewer`` and
      ``diagnose`` (a ``meta.rescue_for`` todo): comment only;
    - any other ``meta.generate.role``: ``comment``; a todo with no
      ``generate`` block: ``builder``. An unreadable grant todo resolves
      ``comment`` — the fail-closed end of the scale.

  ``edit``/``delete``/``tag`` on other refs, jobs, memory, settings and
  unknown verbs are refused — including
  ``put(kind='job', mode='setting', args={'key': 'claim.*'})``.

  ``mcp.slice_scope`` (DB-only setting, default ``observe``) arms it:
  ``off`` skips the check; ``observe`` allows the write, logs a
  ``precis.slice_scope`` line and marks the call's ``tool_calls`` row
  ``error_type='SliceScopeWouldRefuse'`` with the role/rule/verb/kind/
  target k=v line in ``error_head`` (read back via
  ``get(kind='agentlog', id='/scope?…')``); ``enforce`` raises
  :class:`Denied` whose cause carries the same k=v line so the readout
  parses both row shapes identically.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.errors import Denied

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger("precis.slice_scope")

#: ASGI scope key the token-auth middleware stamps on the install bearer
#: (``server._install_token_auth``); binds the ``install`` principal.
SCOPE_INSTALL = "precis.install"

#: DB-only setting arming the slice scope: off | observe | enforce.
SCOPE_SETTING = "mcp.slice_scope"

#: ``tool_calls.error_type`` marker on an observed would-refuse write —
#: an ok row whose call the enforce mode would have refused. The
#: role/rule/verb/kind/target detail rides ``error_head`` as one k=v
#: line, the same shape the enforce Denied's first line carries.
WOULD_REFUSE = "SliceScopeWouldRefuse"

#: Open tags a builder-role slice may set on its own todo. EMPTY until
#: the 24h observe readout names the tags real builders reach for
#: (td575705 §2a; the runbook is docs/runbooks/dark-factory-arming.md).
SLICE_TAG_ALLOW: frozenset[str] = frozenset()

_READ_VERBS = frozenset({"get", "search"})

#: ``meta.generate.role`` values the spec maps onto the two write scopes.
_GENERATOR_ROLES = frozenset({"slicer", "spec-writer", "triage"})
_NAMED_ROLES = frozenset({"reviewer", "analyst", "brief", "security-reviewer"})

#: A bound slice principal: ``slice:<td..grant id>:<role>``.
_SLICE_RE = re.compile(r"^slice:(td[0-9]+(?:[ja][0-9]+)?):([A-Za-z-]+)$")
_TODO_ID_RE = re.compile(r"^td([0-9]+)")

#: The ``put(kind='gripe')`` create surface minus id/link legs: a slice
#: files its gripe whole — tags and prio ride the artifact itself, but
#: link/unlink touch a foreign ref's edge set and untags is dead input.
_NEW_GRIP_KEYS = frozenset(
    {
        "kind",
        "text",
        "body",
        "title",
        "mode",
        "tags",
        "prio",
        "new",
        "observation",
        "project",
        "until",
    }
)

#: The generator's child-mint surface: title + details + the parent pin
#: + the allowlisted meta. Tags, prio, executor/job dispatch keys and
#: plan-mint keys stay refused — the child's own slice decides its tags.
_CHILD_TODO_KEYS = frozenset({"kind", "text", "body", "parent_id", "meta"})

#: Meta keys a generator may write on its children, and the one op shape.
_CHILD_META_KEYS = frozenset({"llm_tier", "op"})
_OP_KEYS = frozenset({"type", "params"})
_OP_PARAM_KEYS = frozenset({"model", "effort"})

_PRINCIPAL: ContextVar[str | None] = ContextVar("precis_write_principal", default=None)

#: The scoped slice grant's minted ``refs`` tuple for the bound call —
#: server-set off the same trusted ASGI stamps as the principal (never
#: caller input), bound in ``tools.core._dispatch_as_user`` beside the
#: principal. ``slice_scope_verdict``'s triage branch re-judges the
#: boundary allowlist against them (plus the live ``fixes`` links).
_GRANT_REFS: ContextVar[tuple[str, ...] | None] = ContextVar(
    "precis_slice_grant_refs", default=None
)


def current() -> str:
    """This call's write principal — the bound stamp, else the in-process
    ``worker:<PRECIS_SOURCE|cli>`` fallback every write-path caller gets."""
    bound = _PRINCIPAL.get()
    if bound is not None:
        return bound
    from precis import agentlog

    return f"worker:{agentlog.current_source_from_env() or 'cli'}"


@contextmanager
def bind(value: str | None) -> Iterator[None]:
    """Bind the server-set principal for one dispatch; ``None`` clears.

    Bound per call in ``tools.core._dispatch_as_user`` from the ASGI
    scope's trusted stamps, and in ``precis_web.deps.human_write_context``
    for browser writes — never from caller input.
    """
    token = _PRINCIPAL.set(value)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)


@contextmanager
def bind_grant_refs(refs: Any) -> Iterator[None]:
    """Bind the scoped grant's stamped ``refs`` for one dispatch;
    falsy clears. ``None``/empty means no scoped grant — the triage
    verdict then falls back to the own todo derived from the principal
    (an in-process dispatch has no ASGI stamps to trust anyway)."""
    token = _GRANT_REFS.set(tuple(str(r) for r in refs) if refs else None)
    try:
        yield
    finally:
        _GRANT_REFS.reset(token)


def worker_comment_meta() -> dict[str, str]:
    """``meta`` entry for worker-written comments: the explicit
    ``worker:<source>`` stamp. Not :func:`current` — a bound slice or
    install principal must not leak onto a record the workers sign."""
    from precis import agentlog

    return {"principal": f"worker:{agentlog.current_source_from_env() or 'cli'}"}


def _slice_todo_id(slice_id: str) -> int | None:
    """The grant's own todo ref_id — the digits after ``td``."""
    m = _TODO_ID_RE.match(slice_id)
    return int(m.group(1)) if m else None


def slice_role(store: Store | None, slice_id: str) -> str:
    """The write role a slice grant's own todo assigns (td575705 §2a).

    ``meta.rescue_for`` → ``diagnose`` (comment-only); else
    ``meta.generate.role``: slicer/spec-writer/triage → ``generator``;
    reviewer/analyst/brief/security-reviewer → that name; any other
    role → ``comment``; no ``generate`` block → ``builder``. A missing
    or unreadable grant todo resolves ``comment`` — fail closed.
    """
    todo_id = _slice_todo_id(slice_id)
    try:
        if store is None or todo_id is None:
            return "comment"
        ref = store.get_ref(kind="todo", id=todo_id)
        if ref is None:
            return "comment"
        meta = ref.meta or {}
    except Exception:
        return "comment"
    if meta.get("rescue_for"):
        return "diagnose"
    gen = meta.get("generate")
    role = gen.get("role") if isinstance(gen, dict) else None
    if role is None:
        return "builder"
    role = str(role)
    if role in _GENERATOR_ROLES:
        return "generator"
    if role in _NAMED_ROLES:
        return role
    return "comment"


def for_slice(slice_id: str, role: str) -> str:
    """The ``slice:<grant>:<role>`` principal string."""
    return f"slice:{slice_id}:{role}"


def _parse_slice(text: str | None) -> tuple[str, int, str] | None:
    """``(slice_id, own_todo_ref_id, role)`` of a bound slice principal."""
    if not text:
        return None
    m = _SLICE_RE.match(text)
    if m is None:
        return None
    todo_id = _slice_todo_id(m.group(1))
    if todo_id is None:
        return None
    return m.group(1), todo_id, m.group(2)


#: Last successfully-read mode — a security control never loosens on a
#: fault: when the ``app_settings`` read fails after the settings cache
#: expires, the mode stays what it last verifiably was (an armed
#: ``enforce`` can never degrade to ``observe`` mid-outage), and a
#: failure with nothing good read yet fails closed on ``enforce``.
_mode_last_good: str | None = None


def slice_scope_mode(store: Store | None) -> str:
    """``off`` | ``observe`` | ``enforce`` — the armed slice-scope mode.

    ``mcp.slice_scope`` is DB-only (no env var): an unset row reads the
    registered default ``observe``; an unknown value observes too and
    warns once — the scope is a security control, so a typo'd mode never
    silently opens it. A failed read keeps :data:`_mode_last_good` —
    the ``default`` layer means "row unset" *or* "read failed" and the
    strict re-read is what tells the two apart.
    """
    global _mode_last_good
    from precis import settings

    st = store if store is not None else settings._STORE
    if st is None:
        return "observe"
    raw, layer = settings.resolve(SCOPE_SETTING, store=st)
    if layer != "db":
        try:
            raw = settings._db_read_strict(st, SCOPE_SETTING)
        except Exception:
            keep = _mode_last_good or "enforce"
            settings._warn_once(
                f"readfail:{SCOPE_SETTING}",
                f"slice-scope: app_settings read failed; keeping mode={keep}",
            )
            return keep
    mode = str(raw or "observe").strip().lower()
    if mode not in ("off", "observe", "enforce"):
        settings._warn_once(
            f"badmode:{SCOPE_SETTING}",
            f"settings: {SCOPE_SETTING} value {raw!r} is not "
            "off|observe|enforce — observing",
        )
        mode = "observe"
    _mode_last_good = mode
    return mode


@dataclass(frozen=True, slots=True)
class SliceVerdict:
    """A slice write the role's grant does not cover — the would-refuse
    record. ``line()`` is the k=v form both the observe ``error_head``
    and the enforce Denied's first line carry, so the ``/scope`` readout
    parses one shape."""

    role: str
    rule: str
    verb: str
    kind: str | None
    target: str | None

    def line(self) -> str:
        return (
            f"role={self.role} rule={self.rule} verb={self.verb} "
            f"kind={self.kind or '-'} target={self.target or '-'}"
        )


def _target_label(args: dict[str, Any]) -> str | None:
    """The raw ``id``/``target`` the call named, bounded for the ledger."""
    v = args.get("id")
    if v is None:
        v = args.get("target")
    if v is None:
        return None
    s = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    return s[:80]


def _resolve_ref_id(runtime: Any, kind: str | None, ident: Any) -> int | None:
    """The live ``ref_id`` an ``id=``/``target=``/``parent_id=`` value
    names, or ``None`` — an unresolvable target is a refusal, not an
    error, and a lookup that itself fails resolves ``None`` the same
    way (fail closed)."""
    store = getattr(runtime, "store", None)
    if store is None or ident is None or isinstance(ident, bool):
        return None
    try:
        if isinstance(ident, int):
            if ident <= 0:
                return None
            ref = store.fetch_refs_by_ids([ident], include_deleted=False).get(ident)
            return int(ref.id) if ref is not None else None
        raw = str(ident).strip()
        if not raw:
            return None
        prefix, sep, rest = raw.partition(":")
        kinds = getattr(getattr(runtime, "hub", None), "kinds", ())
        if sep and prefix in kinds:
            raw = rest
        raw = raw.lstrip("§")
        base = re.split(r"[~/]", raw, maxsplit=1)[0].strip()
        if not base:
            return None
        if base.isdigit():
            n = int(base)
            ref = store.fetch_refs_by_ids([n], include_deleted=False).get(n)
            return int(ref.id) if ref is not None else None
        from precis.utils import handle_registry

        resolved = store.resolve_handle(handle_registry.normalize(base))
        if resolved is not None and resolved.chunk_id is None:
            return int(resolved.ref_id)
        if kind:
            ref = store.get_ref(kind=kind, id=base)
            if ref is not None:
                return int(ref.id)
    except Exception:
        return None
    return None


def _is_child_of(runtime: Any, own_id: int, ident: Any) -> bool:
    """The identifier resolves to a live ref parented to the grant's todo."""
    rid = _resolve_ref_id(runtime, "todo", ident)
    if rid is None or rid == own_id:
        return False
    try:
        ref = runtime.store.fetch_refs_by_ids([rid], include_deleted=False).get(rid)
    except Exception:
        return False
    return ref is not None and ref.parent_id == own_id


def _generator_meta_ok(meta: Any) -> bool:
    """The child's meta stays within ``{llm_tier, op}``, and ``op`` is the
    one dispatch shape ``{type: 'run_slice', params: {model, effort}}``."""
    if meta is None:
        return True
    if not isinstance(meta, dict) or not set(meta) <= _CHILD_META_KEYS:
        return False
    op = meta.get("op")
    if op is None:
        return True
    if not isinstance(op, dict) or not set(op) <= _OP_KEYS:
        return False
    params = op.get("params")
    return (
        op.get("type") == "run_slice"
        and isinstance(params, dict)
        and set(params) <= _OP_PARAM_KEYS
    )


def slice_scope_verdict(
    runtime: Any, verb: str, args: dict[str, Any]
) -> SliceVerdict | None:
    """The refusal verdict for this slice call, or ``None`` when the role's
    grant covers it. Reads and non-slice principals always pass.

    Evaluates the kwargs the handler will actually see: ``args=`` extras
    flatten onto declared handler parameters (an explicit top-level value
    wins the collision), so the effective surface merges both — a
    ``parent_id``/``meta`` smuggled inside ``args={}`` is judged the same
    as the top-level kwarg.

    Layering: the ``reader``/``triage`` roles belong to the request
    boundary (:mod:`precis.slice_reader_scope`, gr574082) — a scoped
    grant binds its scope name as the role (``tools.core._ctx_principal``)
    and the boundary plus the dispatch backstop enforce its allowlist.
    This gate stays the backstop under them: a reader write that reaches
    here refuses (defense in depth), a triage write passed the
    boundary's own allowlist already so the role table must not
    re-refuse it.
    """
    bound = _PRINCIPAL.get()
    parsed = _parse_slice(bound)
    if parsed is None:
        # Reads stay free for anything; but a ``slice:``-shaped principal
        # that fails the grammar is a forged or half-stamped badge —
        # refuse its writes, never allow them.
        if (
            verb not in _READ_VERBS
            and isinstance(bound, str)
            and bound.startswith("slice:")
        ):
            kind = args.get("kind")
            return SliceVerdict(
                role="unparsed",
                rule="principal-shape",
                verb=str(verb),
                kind=str(kind).strip() if kind is not None else None,
                target=_target_label(args),
            )
        return None
    if verb in _READ_VERBS:
        return None
    _slice_id, own_id, role = parsed
    kind = args.get("kind")
    kind_s = str(kind).strip() if kind is not None else None
    expanded: str | None = None
    if kind_s:
        try:
            expanded = runtime._expand_kind_code(kind_s)
        except Exception:
            expanded = kind_s

    def refuse(rule: str) -> SliceVerdict:
        return SliceVerdict(
            role=role,
            rule=rule,
            verb=str(verb),
            kind=expanded or kind_s,
            target=_target_label(args),
        )

    eff: dict[str, Any] = {}
    extras = args.get("__extras__")
    if isinstance(extras, dict):
        eff.update(extras)
    eff.update({k: v for k, v in args.items() if k != "__extras__"})
    keys = {k for k, v in eff.items() if v is not None}

    if role == "reader":
        # The boundary allowlist owns this scope; a write reaching the
        # role table is defense-in-depth, not a covered write.
        return refuse("scope-reader")
    if role == "triage":
        # Defense in depth: re-judge the write through the boundary
        # allowlist itself — the stamped refs ride the bound grant
        # stamps (the own todo when no grant is bound, e.g. an
        # in-process dispatch), the live ``fixes`` links come off the
        # store, and a boundary refusal becomes the verdict.
        from precis import slice_reader_scope
        from precis.utils import handle_registry

        refs = _GRANT_REFS.get()
        if refs is None:
            own = handle_registry.try_format("todo", own_id)
            refs = () if own is None else (own,)
        denied = slice_reader_scope.refusal(
            "triage",
            verb,
            {k: v for k, v in eff.items() if v is not None},
            refs,
            slice_reader_scope.fixes_targets(getattr(runtime, "store", None), refs),
        )
        if denied is not None:
            return refuse("scope-triage")
        return None

    if verb == "put":
        if expanded == "gripe":
            if (
                eff.get("id") is None
                and keys <= _NEW_GRIP_KEYS
                and eff.get("mode") in (None, "create")
            ):
                return None
            return refuse("gripe-shape")
        if expanded == "todo":
            if eff.get("id") is not None:
                if (
                    keys <= {"kind", "id", "text"}
                    and _resolve_ref_id(runtime, "todo", eff["id"]) == own_id
                ):
                    return None
                return refuse("comment-shape")
            if role != "generator":
                return refuse("create-role")
            if not keys <= _CHILD_TODO_KEYS:
                return refuse("create-shape")
            if _resolve_ref_id(runtime, "todo", eff.get("parent_id")) != own_id:
                return refuse("parent-not-own")
            if not _generator_meta_ok(eff.get("meta")):
                return refuse("meta-keys")
            return None
        return refuse("kind")
    if verb == "tag":
        if role != "builder":
            return refuse("tag-role")
        if eff.get("meta") or eff.get("prio") is not None:
            return refuse("tag-meta")
        tag_values = [
            str(t) for t in (*(eff.get("add") or []), *(eff.get("remove") or []))
        ]
        if not set(tag_values) <= SLICE_TAG_ALLOW:
            return refuse("tag-allowlist")
        idents = eff.get("id")
        for ident in idents if isinstance(idents, (list, tuple)) else [idents]:
            if _resolve_ref_id(runtime, "todo", ident) != own_id:
                return refuse("tag-target")
        return None
    if verb == "link":
        if role != "generator":
            return refuse("link-role")
        if eff.get("rel") != "blocked-by" or eff.get("mode") not in (
            None,
            "add",
            "remove",
        ):
            return refuse("link-rel")
        if eff.get("element") is not None or eff.get("meta") is not None:
            return refuse("link-extras")
        idents = eff.get("id")
        ends = list(idents) if isinstance(idents, (list, tuple)) else [idents]
        ends.append(eff.get("target"))
        if any(not _is_child_of(runtime, own_id, i) for i in ends):
            return refuse("link-ends")
        return None
    return refuse("verb")


def role_write_options(role: str) -> list[str]:
    """The ``options=`` a slice-scope Denied lists — what the role may write."""
    options = [
        "put(kind='todo', id=<your todo>, text='…') to comment on it",
        "put(kind='gripe', text='…') to file a new gripe",
    ]
    if role == "builder":
        options.append(
            "tag your own todo — but SLICE_TAG_ALLOW is empty today, "
            "so every tag refuses until the observe readout fills it"
        )
    if role == "generator":
        options += [
            "put(kind='todo', parent_id=<your todo>, meta={'llm_tier':…,"
            " 'op':{'type':'run_slice','params':{'model':…,'effort':…}}})",
            "link(rel='blocked-by') between children of your todo",
        ]
    return options


def check(runtime: Any, verb: str, args: dict[str, Any]) -> SliceVerdict | None:
    """The slice-scope gate one dispatch runs.

    ``off`` skips entirely; ``observe`` logs a ``precis.slice_scope``
    ``would-refuse`` line and returns the verdict so the caller marks
    its own ``tool_calls`` row; ``enforce`` raises :class:`Denied`. A
    non-slice principal, a read verb or a covered write returns ``None``
    at ContextVar-read cost — no settings read, no store lookup.
    """
    bound = _PRINCIPAL.get()
    if bound is None or not bound.startswith("slice:"):
        return None
    if verb in _READ_VERBS:
        return None
    mode = slice_scope_mode(getattr(runtime, "store", None))
    if mode == "off":
        return None
    verdict = slice_scope_verdict(runtime, verb, args)
    if verdict is None:
        return None
    if mode == "enforce":
        raise Denied(
            f"slice scope refused {verdict.line()}",
            next="put it in your final message; the worker records it",
            options=role_write_options(verdict.role),
        )
    log.info("would-refuse principal=%s %s", bound, verdict.line())
    return verdict


__all__ = [
    "SCOPE_INSTALL",
    "SCOPE_SETTING",
    "SLICE_TAG_ALLOW",
    "WOULD_REFUSE",
    "SliceVerdict",
    "bind",
    "bind_grant_refs",
    "check",
    "current",
    "for_slice",
    "role_write_options",
    "slice_role",
    "slice_scope_mode",
    "slice_scope_verdict",
    "worker_comment_meta",
]
