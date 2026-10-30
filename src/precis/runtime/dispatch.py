"""Core dispatch: verb routing, kind/handler resolution, handler
invocation — the in-process MCP verb call (`runtime.dispatch`; not the
`Hub` registration table in `precis/dispatch.py`, nor the dispatch
worker).

``DispatchMixin`` carries ``dispatch()``/``dispatch_with_status()``, the
single-kind ``kind=`` resolution chain (2-char handle-code expansion,
id-prefix/sigil/relative-handle inference), and handler invocation
(extras whitelist, required-kwarg check, default-tags policy). Cross-kind/
source-search fan-out lives in :mod:`precis.runtime.search`; angle
spray+dreamable region in :mod:`precis.runtime.angle`; hint emission in
:mod:`precis.runtime.hints`; error rendering in
:mod:`precis.runtime.error`. ``PrecisRuntime`` (``precis.runtime.core``)
composes all of them via multiple inheritance — every method runs
against the same ``self`` regardless of defining file.

``dispatch_with_status`` is the single chokepoint every verb call passes
through (MCP server, CLI, in-process agent tick), so it's where the
best-effort tool-call ledger write lives
(:meth:`DispatchMixin._record_tool_call`, migration 0133,
:mod:`precis.tool_ledger`).
"""

from __future__ import annotations

import ast
import contextlib
import copy
import functools
import inspect
import json
import logging
import os
import random
import re
import textwrap
import time
from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import psycopg

from precis.errors import (
    BadInput,
    Denied,
    Gone,
    Internal,
    NotFound,
    PrecisError,
    Unsupported,
    Upstream,
)
from precis.protocol import (
    _ALL_VERBS,
    TOLERATES_EXTRA_KWARGS_ATTR,
    ArgsView,
    Handler,
    Verb,
)
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import RESOLVED_FACETS_KEY as _RESOLVED_FACETS_KEY
from precis.runtime._shared import (
    UNCITED_UNSUPPORTED_KINDS as _UNCITED_UNSUPPORTED_KINDS,
)
from precis.runtime._shared import RuntimeShape
from precis.runtime.trust_verb import (
    TRUST_VERBS,
    create_receipt,
    edit_trust,
    pop_trust_payload,
    producer_kwargs,
    trust_line,
)
from precis.runtime.under_facet import UNDER_NEXT
from precis.store._review_ops import trust_refusal
from precis.store._salience import current_background_actor
from precis.store.revision_context import (
    current_revision_context,
    process_actor,
    revision_context,
)
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store.types import ResolvedHandle

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = _ALL_VERBS

#: Top-level ``tools/core.py`` verb kwargs whose declared default is
#: **not** ``None``, or that the verb forwards unconditionally
#: (``edit(mode='find-replace', verdict='approved')``, ``link(mode='add')``,
#: ``search(page=1)`` — and ``page_size``, whose wrapper value is now
#: ``None`` when unset so the paper handler can pick its mode-aware
#: default, but which stays tolerated for callers that do pass it).
#: Every other
#: top-level kwarg defaults to ``None`` and is stripped by
#: :meth:`DispatchMixin._invoke_handler`'s ``clean`` filter unless the
#: caller actually supplied it — so its presence in ``clean`` reliably
#: means "the caller asked for this". These four can't make that promise:
#: they land in ``clean`` on *every* call to that verb regardless of
#: caller intent, including calls to kinds the kwarg has no meaning for
#: (e.g. every non-draft ``edit()`` call carries ``mode='find-replace'``
#: whether or not the caller ever mentioned ``mode=``). The caller-kwarg
#: strictness gate (gr334695, :func:`_handler_accepted_kwargs`) would
#: otherwise reject the (overwhelmingly common) case of a handler that
#: legitimately doesn't use one of these — so they're exempted from that
#: gate entirely rather than requiring every handler to declare them just
#: to stay silent. A handler that *does* care about one of these already
#: declares it explicitly and is unaffected either way.
#: A8: tolerance is per verb — only the wrapper's non-``None`` defaults
#: (``edit(mode=, verdict=)``, ``link(mode=)``, ``search(page=)``) land in
#: ``clean`` unprompted. Anywhere else a ``mode``/``verdict``/``page``/
#: ``page_size`` in ``clean`` was typed by the caller, and a handler that
#: does not read it now refuses instead of silently dropping it.
_WRAPPER_DEFAULTED_KWARGS: dict[str, frozenset[str]] = {
    "edit": frozenset({"mode", "verdict"}),
    "link": frozenset({"mode"}),
    "search": frozenset({"page"}),
}
_DROPPABLE_TOP_LEVEL_KWARGS = frozenset({"mode", "verdict", "page", "page_size"})

#: ``tools/core.py::edit``'s own non-``None`` default for ``mode=``
#: (``"find-replace"``, the file-kind idiom) — the specific value
#: behind the comment above: every MCP ``edit()`` call carries this in
#: ``clean`` whether or not the caller ever mentioned ``mode=``, so it
#: can never be read as "the caller asked for find-replace" versus
#: "the caller didn't think about mode= at all". :meth:`DispatchMixin.
#: _validate_mode` (gr343755) exempts exactly this value from its
#: ``KindSpec.edit_modes`` membership check for that reason — a kind
#: whose only accepted mode is something else entirely (``todo``/
#: ``quest``/``memory``: ``edit_modes=('replace',)``) still has a
#: mode-agnostic edit path (e.g. todo's meta-only ``llm_tier`` park,
#: gr439934) that must not be rejected just because the wrapper always
#: fills this slot in. ``put``'s wrapper default is ``None`` (see
#: ``tools/core.py::put``), so no equivalent exemption is needed there
#: — an omitted ``mode=`` truly arrives as ``None``, which the gate
#: already treats as "not supplied".
_EDIT_MODE_WRAPPER_DEFAULT = "find-replace"

#: Sentinel key used by `precis.server` to forward the MCP tool's
#: ``args={...}`` payload through to the dispatcher without colliding
#: with the explicit positional kwargs. The dispatcher pops it before
#: calling the handler method and validates the keys against the
#: method's accepted-kwargs whitelist.
_EXTRAS_KEY = "__extras__"
#: Recall stamps remembered in-process (kind, id) -> monotonic time: a ref read
#: again inside the hour skips the database entirely. Bounded LRU; the SQL
#: throttle in ``Store.touch_recalled_for`` stays the cross-process guard.
_RECALL_TTL_S = 3600.0
_RECALL_CACHE_MAX = 10_000

#: Address sigils that self-identify a kind, so ``get(id='¶handle')``
#: works without ``kind=`` (the draft skill documents exactly that).
#: Value is ``(kind, keep_sigil)``: ``¶`` stays in ``id=`` because the
#: draft handler matches on a leading ``¶``; ``§`` is stripped to the
#: bare ``slug~n`` the paper handler resolves. Distinct from the
#: ``kind:slug`` colon prefix (also self-identifying) handled alongside.
_SIGIL_KIND: dict[str, tuple[str, bool]] = {
    "¶": ("draft", True),
    "§": ("paper", False),
}

#: A draft chunk handle ``dc<chunk_id>`` and any trailing relative operator;
#: group 1 is the bare ``<chunk_id>`` (existence probe), group 2 the operator.
_DRAFT_DC_RE = re.compile(r"^dc(\d+)(.*)$")
#: Same shape for plan chunk handles ``pe<id>`` — plans have their own
#: handle grammar (``pe<N>`` + relative ops), never ``slug~ord``.
_PLAN_PE_RE = re.compile(r"^pe(\d+)(.*)$")

#: Matches the ``[error:ClassName]`` prefix :meth:`ErrorMixin.render_error`
#: always emits — the tool-call ledger (:mod:`precis.tool_ledger`) reads the
#: error class name back out of the already-rendered body instead of
#: threading the exception object through, so logging it costs one regex
#: match rather than a second code path.
_ERROR_TYPE_RE = re.compile(r"^\[error:(\w+)\]")

#: Per-(kind, verb) recovery hints for "kind does not support verb" — a
#: generic "try get(kind=…)" is a dead-end when the right move is a
#: *different shape entirely*. Drafts are the case that bites: an agent
#: reaches for the universal ``link``/``tag`` verbs, but a draft's
#: cross-references live in prose (the autolinker backlinks them), and it
#: has no whole-ref tag axis. Teach the real move inline.
_VERB_REDIRECTS: dict[tuple[str, str], str] = {
    # ("draft", "link") moved into DraftHandler.link itself — the verb
    # now exists for folder placement, so the prose-ref
    # teaching rides on its BadInput for every other relation.
    ("draft", "tag"): (
        "drafts have no whole-ref tag axis; tag the owning project todo "
        "instead, or use a glossary term / inline markup inside the prose."
    ),
    # gr338669: an agent generalizing from todo's supports_edit reaches
    # for edit(kind='gripe', ...) and gets a bare "does not support"
    # with no idiom to fall back to. Gripe (like every numeric-ref kind,
    # see KindSpec.supports_edit) has no region-edit verb by design —
    # its only text mutation is the put-with-id append-comment idiom
    # (GripeHandler.put), so name that directly rather than a generic
    # "try get(kind=...)".
    ("gripe", "edit"): (
        "gripe has no edit(); append a comment with put(kind='gripe', id=N, text='...')"
    ),
}

#: Kinds that USED to exist, mapped to where their work moved. An agent
#: (or a stale prompt, or an old note) reaching for a retired kind gets
#: ``NotFound: unknown kind`` plus a generic "pick from the options" —
#: true, but it doesn't say that the capability still exists under a
#: different name. Registered here, the unknown-kind error leads with the
#: successor so the retry is one call away rather than a re-discovery.
#: Deliberately a small hand-curated map, not an alias that still
#: dispatches: the kind is *gone* (no handler, no tables), and silently
#: rerouting ``put(kind='nm', …)`` into another kind's op vocabulary
#: would write something the caller didn't ask for.
_RETIRED_KINDS: dict[str, str] = {
    # nm→se merge (docs/backlog/nm-se-merge.md): the nanomachine kind is
    # se's `atomic` manufacturing mode now — same six-level block tree,
    # same generators/bindings/views, one kind.
    "nm": (
        "kind 'nm' was merged into 'se' as its ATOMIC mode "
        "(docs/backlog/nm-se-merge.md): use kind='se' — set_mode "
        "mode='atomic', bind_structure/generate, view='mechanics'/"
        "'literature'. get(kind='skill', id='precis-se-help')"
    ),
}


#: Verbs whose handler may change a ref or link, so the call runs inside a
#: :func:`~precis.store.revision_context.revision_context`.
_WRITE_VERBS = frozenset({"put", "edit", "delete", "tag", "link"})


def _write_context(
    verb: str, kind: str, reason: str | None, *, trust: dict[str, Any] | None = None
) -> contextlib.AbstractContextManager[object]:
    """The revision context a write verb runs in (local-mesh-upkeep §2b).

    Reason: the caller's ``reason=``, else an enclosing context's, else
    ``"<verb>(kind='<kind>')"``. Actor: an enclosing context's (a worker
    calling the runtime), else the background actor, else the process
    actor (a stdio server run for one speaker, :mod:`precis.acting_user`),
    else ``agent``. ``trust`` is the producer part
    (:func:`precis.runtime.trust_verb.producer_kwargs`, migration 0214).
    """
    if verb not in _WRITE_VERBS:
        return contextlib.nullcontext()
    outer = current_revision_context()
    if reason is None and (outer is None or outer.reason is None):
        reason = f"{verb}(kind={kind!r})"
    actor = None
    if outer is None or outer.actor is None:
        actor = current_background_actor() or process_actor() or "agent"
    return revision_context(reason, actor=actor, **(trust or {}))


def _explicit_param_names(func: Any) -> frozenset[str]:
    """Keyword-accessible, non-catch-all parameter names of ``func``.

    Excludes ``self`` and any ``**kwargs`` catch-all. Shared by
    :func:`_forwards_catchall` and :func:`_handler_accepted_kwargs`.
    """
    sig = inspect.signature(func)
    return frozenset(
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and name != "self"
    )


def _catchall_param_name(func: Any) -> str | None:
    """The ``**name`` catch-all parameter of ``func``, or ``None``."""
    sig = inspect.signature(func)
    for name, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return name
    return None


def _forwards_catchall(func: Any, catchall_name: str) -> bool:
    """True if ``func``'s own body forwards its ``**catchall_name``
    catch-all on to another call (the cooperative-inheritance
    ``return super().<verb>(..., **_kw)`` idiom used throughout
    :mod:`precis.handlers`).

    Static (AST) check on ``func``'s source — run once per function and
    memoized by :func:`_handler_accepted_kwargs`, never per call. This is
    the signal that tells a pass-through override (declare a few
    kind-specific kwargs, forward the rest to a shared base
    implementation — genuine case (b) in the gr334695 sweep) apart from a
    terminal implementation whose ``**catchall_name`` is a dead end (case
    (a): exactly where an unrecognized caller kwarg would previously be
    silently dropped instead of erroring). Any failure to introspect the
    source (dynamically built function, C extension, etc.) is treated as
    "does not forward" — the conservative direction, since it only
    stops the accepted-kwargs union early rather than accepting a kwarg
    nothing can prove is actually consumed.
    """
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:  # pragma: no cover — defensive
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg is None
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == catchall_name
                ):
                    return True
    return False


def _eye_extent(args: dict[str, Any]) -> str | None:
    """The ladder rung a ``get`` asks for, or ``None`` for an ordinary get.

    ``extent=`` is the one argument (popped here, top-level or inside
    ``args=``, so the kwargs gate never sees it); a ladder label in
    ``view=`` means the same, because the ladder shipped on that door first
    (``precis-fisheye-help``). Both given and disagreeing is a ``BadInput``
    — the rung is one argument, not two.
    """
    from precis.handlers._eye import is_eye_view

    extent = args.pop("extent", None)
    extras = args.get(_EXTRAS_KEY)
    if extent is None and isinstance(extras, dict):
        extent = extras.pop("extent", None)
    view = args.get("view")
    if extent is not None:
        extent = str(extent).strip()
        if view is not None and str(view) != extent:
            raise BadInput(
                f"extent={extent!r} and view={view!r} both given; the rung is one argument",
                next=f"get(kind=…, id=…, extent={extent!r})",
            )
        return extent
    return str(view) if is_eye_view(view if view is None else str(view)) else None


@functools.cache
def _handler_accepted_kwargs(cls: type[Any], verb: str) -> frozenset[str]:
    """Every kwarg ``cls().<verb>(...)`` can actually consume (gr334695).

    Walks ``cls.__mro__`` starting at the most-derived class that defines
    ``verb`` in its own ``__dict__``, unioning in its explicit parameter
    names. If that override's ``**kwargs`` catch-all statically forwards
    (:func:`_forwards_catchall`), the walk continues to the next class up
    the MRO that also defines ``verb``, unioning its params in too —
    repeating until an override doesn't forward (a genuine dead end) or
    the MRO is exhausted. A class whose ``verb`` has no catch-all at all
    is inherently a dead end: Python itself rejects any extra kwarg
    there, so there is nothing further to union in.

    This is the MRO-aware generalization of the naive "just read the
    resolved method's own signature" check — necessary because
    :mod:`precis.handlers` is built on cooperative multiple inheritance
    (a subclass declares its own kwargs and forwards the rest to a
    shared base like ``NumericRefHandler``/``CacheBackedHandler`` for
    some verbs, while fully owning — and never forwarding — others). A
    plain per-method check would either reject legitimate forwarded
    kwargs (false positive, breaking real calls) or blindly union every
    ancestor regardless of whether the resolved override actually calls
    it (false negative, reintroducing the exact silent-swallow bug this
    exists to catch).

    Cached per ``(cls, verb)`` — class shape is fixed for the process
    lifetime, and this walks source/AST, too expensive to repeat on the
    hot dispatch path per call.
    """
    accepted: set[str] = set()
    for klass in cls.__mro__:
        raw = klass.__dict__.get(verb)
        if raw is None:
            continue
        func = inspect.unwrap(raw)
        accepted |= _explicit_param_names(func)
        catchall = _catchall_param_name(func)
        if catchall is None or not _forwards_catchall(func, catchall):
            break
    return frozenset(accepted)


#: The two spellings of the one prose slot a put/edit takes. The verbs
#: declare both at the top level (``body=`` because finding's write shape
#: made it canonical, ``text=`` because every numeric-ref/file kind uses
#: it), so callers generalise the wrong spelling across kinds — the
#: gr484230 near-miss: ``put(kind='gripe', body=…)`` learned from
#: ``put(kind='finding', body=…)``.
_PROSE_FIELD_SPELLINGS: tuple[str, str] = ("text", "body")


def _alias_prose_field(
    cls: type[Handler], verb: str, kind: str, clean: dict[str, Any]
) -> None:
    """Rewrite the other prose spelling onto the one a handler declares.

    When ``cls.<verb>`` accepts exactly one of :data:`_PROSE_FIELD_SPELLINGS`,
    a supplied other spelling is the same slot misnamed — alias it. A
    handler declaring both keeps them distinct (todo put: ``text=`` the
    title, ``body=`` the details; finding put runs its own text→body
    alias), and both spellings supplied to a single-spelling handler is
    a BadInput: two prose values can't collapse into one slot silently.
    """
    accepted = _handler_accepted_kwargs(cls, verb)
    declared = [s for s in _PROSE_FIELD_SPELLINGS if s in accepted]
    if len(declared) != 1:
        return
    canon = declared[0]
    alias = "body" if canon == "text" else "text"
    if alias not in clean:
        return
    if canon in clean:
        raise BadInput(
            f"{verb}(kind={kind!r}): {canon}= and {alias}= are the same "
            "prose field on this kind — pass one, not both",
            next=f"{verb}(kind={kind!r}, {canon}='...')",
        )
    clean[canon] = clean.pop(alias)


#: Top-level parameters each slim verb advertises (30-day prod ledger).
CORE_PARAMS: dict[str, tuple[str, ...]] = {
    "put": (
        "kind",
        "id",
        "text",
        "title",
        "body",
        "mode",
        "tags",
        "untags",
        "link",
        "unlink",
        "rel",
        "meta",
        "args",
    ),
    "edit": (
        "kind",
        "id",
        "mode",
        "text",
        "body",
        "find",
        "where",
        "before",
        "after",
        "match",
        "nth",
        "dry_run",
        "reason",
        "args",
    ),
    "search": (
        "kind",
        "q",
        "page_size",
        "mode",
        "view",
        "tags",
        "scope",
        "status",
        "sort",
        "args",
    ),
}


_LIST_ANN_RE = re.compile(r"\b(list|List|Sequence)\b")
_DICT_ANN_RE = re.compile(r"\b(dict|Dict|Mapping)\b")
_SCALAR_ANN_RE = re.compile(r"\b(int|float|bool)\b")
_STR_ANN_RE = re.compile(r"\bstr\b")


def coerce_json_container(value: Any, annotation: Any) -> Any:
    """Parse a JSON-string ``value`` when ``annotation`` wants a list/dict.

    Mirrors FastMCP's ``pre_parse_json`` for declared fields, which does not
    run for keys the schema doesn't declare (the slim verbs' ``args=`` keys
    and legacy top-level extras). Returns ``value`` unchanged unless it is a
    ``str`` that ``json.loads`` to a list (annotation mentions list) or dict
    (annotation mentions dict), or to an int/float/bool when the annotation
    names that scalar and not ``str``: a client has no schema type for an
    undeclared top-level key, so ``prio=9`` arrives as ``'9'``.
    """
    if not isinstance(value, str) or annotation is inspect.Parameter.empty:
        return value
    ann = annotation if isinstance(annotation, str) else repr(annotation)
    wants_list = bool(_LIST_ANN_RE.search(ann)) or "list[" in ann
    wants_dict = bool(_DICT_ANN_RE.search(ann)) or "dict[" in ann
    wants_scalar = bool(_SCALAR_ANN_RE.search(ann)) and not _STR_ANN_RE.search(ann)
    if not (wants_list or wants_dict or wants_scalar):
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if (isinstance(parsed, list) and wants_list) or (
        isinstance(parsed, dict) and wants_dict
    ):
        return parsed
    if wants_scalar and isinstance(parsed, (int, float)):
        scalar = type(parsed).__name__
        if re.search(rf"\b{scalar}\b", ann) or (scalar == "int" and "float" in ann):
            return parsed
    return value


def _coerce_extras(cls: type[Any], verb: str, extras: dict[str, Any]) -> dict[str, Any]:
    """``extras`` with JSON-string values parsed where the handler's own
    parameter is annotated list/dict (first declaring class on the MRO)."""
    out = dict(extras)
    for klass in cls.__mro__:
        raw = klass.__dict__.get(verb)
        if raw is None:
            continue
        try:
            params = inspect.signature(inspect.unwrap(raw)).parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        for name, value in extras.items():
            if name in params and out[name] is value:
                out[name] = coerce_json_container(value, params[name].annotation)
    return out


def _typed_kwargs(cls: type[Any], verb: str, names: Any) -> list[str]:
    """``name: type = default`` for each of ``names``, from ``cls``'s MRO.

    The accepted-keys list in an unknown-key ``BadInput`` carries types and
    defaults so a caller can fix the call in one round trip (the schema
    diet moved kind-specific kwargs under ``args=``, so this list is the
    in-band reference). The first class on the MRO declaring a name wins.
    """
    found: dict[str, str] = {}
    for klass in cls.__mro__:
        raw = klass.__dict__.get(verb)
        if raw is None:
            continue
        try:
            params = inspect.signature(inspect.unwrap(raw)).parameters
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        for name, p in params.items():
            if name not in names or name in found:
                continue
            ann = (
                ""
                if p.annotation is inspect.Parameter.empty
                else f": {inspect.formatannotation(p.annotation)}"
            )
            default = (
                "" if p.default is inspect.Parameter.empty else f" = {p.default!r}"
            )
            found[name] = f"{name}{ann}{default}"
    return [found.get(n, n) for n in sorted(names)]


def _tick_disabled_hint(kind: str) -> str | None:
    """The per-tick disable hint for ``kind``, or ``None``.

    Reads the thread-scoped in-process tick ContextVar
    (:func:`precis.utils.inproc_context.current`): if the active tick prohibits
    ``kind``, returns its contextual hint (what to do instead), else ``None``.
    Unset outside an in-process agent tick — the MCP server / CLI / tests never
    bind it — so this is a no-op there. The import is local so ``runtime`` keeps
    no import-time coupling to the loop context (``inproc_context`` is stdlib-
    only, but the read is per-call and cheap either way)."""
    from precis.utils.inproc_context import current

    ctx = current()
    if ctx is None or not ctx.disabled_kinds:
        return None
    return dict(ctx.disabled_kinds).get(kind)


#: Agent-authored text args of put/edit/supersede, across every kind's verbs
#: (collected from the handlers' signatures). ``meta`` / ``params`` /
#: ``items`` / ``terms`` are dict/list containers whose string values are
#: scanned recursively.
_SECRET_GATED_ARGS = (
    "text",
    "body",
    "title",
    "new_text",
    "new_title",
    "rule",
    "warrant",
    "reason",
    "note",
    "comment",
    "caption",
    "motivation",
    "testable_by",
    "belief",
    "evidence",
    "source_quote",
    "verifier_caveats",
    "unacquirable_note",
    "description",
    "abstract",
)
_SECRET_GATED_CONTAINERS = ("meta", "params", "items", "terms")


def _string_leaves(label: str, value: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(value, str):
        out.append((label, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            _string_leaves(f"{label}[{k}]", v, out)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _string_leaves(f"{label}[{i}]", v, out)


#: Env var the worker sets on a spawned ``precis serve`` to deny verbs for the
#: run's permission envelope (``precis.workers.envelope.DENY_VERBS_ENV`` — the
#: same literal, pinned by a test; not imported to keep runtime free of
#: workers). Both MCP profiles funnel through
#: :meth:`DispatchMixin.dispatch_with_status`, so unlike ``permissions.deny``
#: this cannot be erased by a tool-name mismatch.
_DENY_VERBS_ENV = "PRECIS_MCP_DENY_VERBS"

#: Scoped carve-out for the flat verb deny, written alongside it by the
#: dream pass (``workers/dream_agent._DREAM_EXEMPT_ENV``): ``<kind>:<ns>``
#: means a denied verb is still allowed when every ref the call *mutates*
#: is that kind carrying a tag in that namespace. ``link`` mutates only its
#: source — the dream may hang a ``contradicts``/``related-to`` edge off
#: its own dream memory to any target, but cannot rewrite the target.
_DENY_EXEMPT_ENV = "PRECIS_MCP_DENY_EXEMPT"


def _denied_verbs_env() -> frozenset[str]:
    """The run's denied verbs as a set; empty when the env var is unset."""
    raw = os.environ.get(_DENY_VERBS_ENV)
    if not raw:
        return frozenset()
    return frozenset(v.strip() for v in raw.split(",") if v.strip())


def _put_denied() -> bool:
    """Whether this run is read-scoped — i.e. it may not write at all.

    The gripe handler treats this as the read-only signal *independent of
    the connection's actual grants*: tier-2 (``SET ROLE agent_ro``,
    ``envelope.db_role``) is dark by default
    (``pool._db_role_enforced``), so an envelope-denied run usually holds
    an *rw* connection — keying the ro path off the grants probe alone
    would take the uncapped writer path (gr516603 review).

    Since gr512233 the signal is ``runtime.scope.current_scope()`` —
    the envelope deny naming ``put``, ``serve --read-only``,
    ``PRECIS_MCP_READ_ONLY`` and the boot probe verdict all land the
    same read scope (the spec's option-B ruling: a read token/run gets
    the same capped gripe channel a ``write:none`` run gets).
    """
    from precis.runtime.scope import current_scope

    return current_scope() == "read"


def _exempt_ref_ids(verb: str, args: dict[str, Any]) -> list[tuple[str, Any]]:
    """The ``(kind, id)`` refs a denied verb would *mutate*.

    ``edit``/``delete``/``tag`` mutate their (kind, id) target; ``link``
    mutates only the source ref (the target gains an inbound edge, never
    a changed body/tag/meta). ``put``/``supersede`` create, so they have
    no mutated-ref check here (put is never in the dream's deny list).
    """
    kind = args.get("kind")
    ref_id = args.get("id")
    if verb in ("edit", "delete", "tag", "link"):
        return [(kind, ref_id)] if kind is not None and ref_id is not None else []
    return []


def _ref_carries_ns(hub: Any, kind: str, ref_id: Any, ns: str) -> bool:
    """True when ``kind:ref_id`` is a live ref of ``kind`` tagged ``ns:*``."""
    try:
        ref = hub.store.get_ref(kind=kind, id=ref_id)
    except Exception:
        return False
    if ref is None:
        return False
    for tag in hub.store.tags_for(int(ref.id)):
        if str(tag).startswith(f"{ns}:"):
            return True
    return False


#: Search kinds the categorizer coverage note can appear on (gr515633
#: review): the axes' populations are paper+patent refs (finding when
#: opted in) and their chunks — on any other kind the note's math names
#: a corpus the search didn't even touch.
_CATEGORIZER_NOTE_KINDS = frozenset({"paper", "patent", "finding"})


def _with_categorizer_filter_notes(
    runtime: Any, args: dict[str, Any], response: Response
) -> Response:
    """Append the categorizer coverage note to a search response (gr515633).

    ``tags=[<NS>:<value>]`` on a below-threshold categorizer axis is an
    AND/exclusive filter over a partially-classified corpus — the
    unclassified share is dropped without a word. The note names it.
    Best-effort: a coverage-read failure (no store, pre-migration schema,
    DB blip) leaves the response untouched rather than breaking a search.
    """
    store = runtime.hub.store
    if store is None or not args.get("tags"):
        return response
    if _ERROR_TYPE_RE.match(response.body or ""):
        return response
    kind_arg = args.get("kind")
    kinds = (
        {runtime._expand_kind_code(k.strip()) for k in kind_arg.split(",") if k.strip()}
        if isinstance(kind_arg, str)
        else set()
    )
    if not kinds or not kinds <= _CATEGORIZER_NOTE_KINDS:
        return response
    try:
        from precis.categorizers import categorizer_filter_notes

        notes = categorizer_filter_notes(store, args.get("tags"))
    except Exception:
        log.exception("categorizer filter-note read failed; leaving body as-is")
        return response
    if not notes:
        return response
    return replace(response, body=response.body + "\n\n" + "\n".join(notes))


def _ro_gripe_create(verb: str, args: dict[str, Any], hub: Any) -> bool:
    """True when ``args`` is a create-only ``put(kind='gripe', ...)``.

    The write:none carve-out (gr516603, Reto 2026-10-21 in
    ``review-container-readonly-role.md``): a read-only run may still
    file a gripe. An ``id`` — top-level or smuggled inside ``args=``,
    which later flattens into kwargs — makes the call a comment on an
    existing ref and stays denied. ``kind='gr'`` resolves through the
    handle registry, mirroring :meth:`DispatchMixin._expand_kind_code`.
    What the create can write stays bounded by the DB role and
    ``file_gripe_readonly``.
    """
    if verb != "put" or args.get("id") is not None:
        return False
    kind = args.get("kind")
    if not isinstance(kind, str):
        return False
    if kind != "gripe":
        if kind in hub.kinds:
            return False
        try:
            from precis.utils import handle_registry

            resolved, is_chunk = handle_registry.kind_for_code(kind)
        except Exception:
            return False
        if is_chunk or resolved != "gripe":
            return False
    extras = args.get(_EXTRAS_KEY)
    if isinstance(extras, dict) and extras.get("id") is not None:
        return False
    return True


def _refuse_envelope_denied_verb(verb: str, args: dict[str, Any], hub: Any) -> None:
    """Raise :class:`Denied` when the run's envelope forbids ``verb``.

    The flat env deny holds unless the scoped exemption
    (:data:`_DENY_EXEMPT_ENV`, ``<kind>:<ns>``) names this call: every ref
    the verb mutates must be that kind tagged ``ns:*``. The dream runs
    with ``memory:DREAM`` — it may edit/retire/link its own DREAM-tagged
    memories (the Step-7 correction flow) while every other ref stays
    read-only (gr179501's crafted-summary box, kept). The other opening
    is the ro gripe carve-out (:func:`_ro_gripe_create`).
    """
    if verb not in _denied_verbs_env():
        return
    if _ro_gripe_create(verb, args, hub):
        return
    exempt = os.environ.get(_DENY_EXEMPT_ENV)
    if exempt:
        e_kind, _, e_ns = exempt.partition(":")
        targets = _exempt_ref_ids(verb, args)
        if (
            e_kind
            and e_ns
            and targets
            and all(
                k == e_kind and _ref_carries_ns(hub, e_kind, rid, e_ns)
                for k, rid in targets
            )
        ):
            return
    raise Denied(
        f"{verb} is denied by this run's permission envelope",
        next="this run is read-only or scoped; report the change you "
        "would make instead of making it",
    )


def _refuse_agent_secrets(verb: str, args: dict[str, Any], hub: Any = None) -> None:
    """Refuse a credential in an agent's put/edit/supersede text, for every
    kind whose handler does not set ``stores_opaque_text``.

    delete/tag join the scan only for ``reason=`` (gr504803) — the one
    agent-authored string either verb takes.

    The MCP/CLI verb boundary is where an agent can still rephrase, so it is
    refused here (:func:`precis.utils.secret_scan.refuse_secrets`). The
    memory/todo/gripe handlers themselves *mask* (``mask_secrets``) instead,
    for the automated writers (alert / forensics workers, importers, web UI)
    that call them directly and would otherwise lose the record. A kind with
    no resolvable handler is gated (default-on).
    """
    if verb not in ("put", "edit", "supersede", "delete", "tag"):
        return
    kind = args.get("kind")
    if isinstance(kind, str) and hub is not None:
        handler = hub.handler_for(kind)
        if handler is not None and getattr(handler, "stores_opaque_text", False):
            return
    from precis.utils.secret_scan import refuse_secrets

    extras = args.get("__extras__") or {}
    fields: list[tuple[str, str]] = []
    for k in _SECRET_GATED_ARGS:
        v = args.get(k, extras.get(k) if isinstance(extras, dict) else None)
        if isinstance(v, str):
            fields.append((k, v))
    for k in _SECRET_GATED_CONTAINERS:
        v = args.get(k)
        if v is None and isinstance(extras, dict):
            v = extras.get(k)
        _string_leaves(k, v, fields)
    refuse_secrets(fields)


# ── library-exception boundary map (M3, gr494831) ─────────────────────────
#
# A transient infrastructure fault is not a precis bug. Each row maps an
# exception class (first isinstance match wins, most specific first; the
# ``__cause__``/``__context__`` chain is walked so a wrapped fault still
# maps) to the Upstream message + retry hint the agent sees. The library's
# own text never reaches the agent: it can carry SQL fragments, hosts,
# paths or driver internals (F10). tests/test_dispatch_boundary_map.py has
# one test per row.

_RETRY_NEXT = "retry the same call once; if it fails again, file a gripe naming it"


def _boundary_rows() -> list[tuple[type[BaseException], str, str]]:
    """``(exception class, agent-facing cause, next)`` rows, most specific
    first. Built lazily so an absent optional library just drops its rows."""
    rows: list[tuple[type[BaseException], str, str]] = []
    try:
        import psycopg.errors as pge
        import psycopg_pool

        rows += [
            (
                pge.LockNotAvailable,
                "the database row or table is locked by another writer",
                _RETRY_NEXT,
            ),
            (
                psycopg_pool.PoolTimeout,
                "the database connection pool is saturated",
                _RETRY_NEXT,
            ),
            (
                pge.QueryCanceled,
                "the database cancelled the query (statement timeout)",
                "narrow the call (smaller q=/limit) and retry once; "
                "if it fails again, file a gripe naming it",
            ),
            (
                pge.DeadlockDetected,
                "the database aborted the call to break a deadlock",
                _RETRY_NEXT,
            ),
            (
                pge.SerializationFailure,
                "the database aborted the call on a concurrent update",
                _RETRY_NEXT,
            ),
            (
                psycopg.OperationalError,
                "the database connection failed",
                _RETRY_NEXT,
            ),
        ]
    except ImportError:  # pragma: no cover — psycopg is a hard dependency
        pass
    try:
        import httpx

        rows += [
            (
                httpx.TimeoutException,
                "an upstream service timed out",
                _RETRY_NEXT,
            ),
            (
                httpx.HTTPStatusError,
                "an upstream service answered with an error status",
                _RETRY_NEXT,
            ),
            (
                httpx.TransportError,
                "an upstream service could not be reached",
                _RETRY_NEXT,
            ),
        ]
    except ImportError:  # pragma: no cover — httpx is a hard dependency
        pass
    rows += [
        (TimeoutError, "an upstream call timed out", _RETRY_NEXT),
        (ConnectionError, "an upstream connection failed", _RETRY_NEXT),
    ]
    return rows


def map_boundary_exception(exc: BaseException) -> Upstream | None:
    """Map a library exception to ``Upstream`` + retry hint, else ``None``
    (the caller then falls back to ``Internal``). Walks the cause chain."""
    rows = _boundary_rows()
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for cls, cause, nxt in rows:
            if isinstance(cur, cls):
                return Upstream(f"{cause} ({type(cur).__name__})", next=nxt)
        # Explicit ``raise ... from`` links only: ``__context__`` is "raised
        # while handling", so a handler bug inside a recovery path would
        # otherwise read as a transient fault and never reach Internal.
        cur = cur.__cause__
    return None


#: Driver/transport noise that survives into handler-written Upstream text
#: (``ConnectTimeout: _ssl.c:993``, ``[Errno 61] ...``).
_LIB_NOISE_RE = re.compile(r"\(_ssl\.c:\d+\)|\[Errno -?\d+\]\s*|_ssl\.c:\d+")


def scrub_upstream(err: PrecisError) -> None:
    """Stop an ``Upstream`` leaking library text.

    Handlers write ``Upstream(f"... {exc}")``. When the error chains to a
    non-precis exception (``from exc``, or raised inside its ``except``),
    any ``str(cause)`` of 8+ characters embedded in the message is replaced
    by the cause's type name, and driver noise is stripped. A missing
    ``next=`` gets the retry hint only when a library exception is chained
    (transient); otherwise a neutral gripe hint, since a missing key or a
    4xx will not heal on retry. No-op for other classes."""
    if not isinstance(err, Upstream):
        return
    lib = err.__cause__ or err.__context__
    if lib is not None and isinstance(lib, PrecisError):
        lib = None
    if lib is not None:
        text = str(lib).strip()
        if len(text) >= 8 and text in err.cause:
            err.cause = err.cause.replace(text, type(lib).__name__)
    err.cause = _LIB_NOISE_RE.sub("", err.cause).strip()
    if err.next is None:
        err.next = (
            _RETRY_NEXT
            if lib is not None
            else "if this persists, file a gripe naming the call and this message"
        )


# ── transient retry (gr494831 step 1) ──────────────────────────────────
#
# A lock timeout, saturated pool or serialization abort usually heals in a
# moment, so the dispatcher retries the call itself instead of handing the
# agent a "retry once" chore. What may be replayed:
#
# * ``get`` / ``search`` on a local-database kind (``_LOCAL_READ_KINDS``) or
#   with no kind. Kinds that reach a paid or outside service are excluded:
#   a replay could bill twice.
# * ``put(kind='job', idem_key=...)`` — JobHandler.put dedupes on an active
#   ``meta.idem_key``, so a replay returns the existing ref. Only jobs
#   dedupe on it; a job that already finished before the replay is the one
#   accepted gap (backoff is at most ~2.4s).
# * never ``edit`` / ``delete`` / ``tag`` / ``link``, nor any other ``put``:
#   a handler may have committed one of several steps before the fault,
#   and a replay would double-apply it.
#
# Attempts total ``_RETRY_ATTEMPTS`` (the first included); backoff is full
# jitter over a doubling cap, ~2.4s worst-case sleep in all. Each attempt
# gets a deep copy of the args so a handler mutating nested values cannot
# change the replay's input.

_RETRY_ATTEMPTS = 3
_RETRY_BASE_S = 0.4
_RETRY_VERBS = frozenset({"get", "search"})
#: Kinds whose get/search only read the local database. Kinds that call a
#: paid or outside service (perplexity-*, llm, web, websearch, ...) may bill
#: again on a replay, so they stay out; so does any kind not listed.
_LOCAL_READ_KINDS = frozenset(
    {
        "paper",
        "memory",
        "gripe",
        "todo",
        "finding",
        "skill",
        "job",
        "draft",
        "concept",
        "quest",
        "fleet",
        "agentlog",
        "alert",
        "checklist",
        "component",
        "pcb",
        "provenance",
        "tag",
        "calc",
        "math",
    }
)


def _transient_db_classes() -> tuple[type[BaseException], ...]:
    import psycopg.errors as pge
    import psycopg_pool

    return (
        pge.LockNotAvailable,
        psycopg_pool.PoolTimeout,
        pge.SerializationFailure,
        pge.DeadlockDetected,
    )


def _is_transient_db_fault(exc: BaseException) -> bool:
    """True when ``exc`` or an explicit ``raise ... from`` cause is one of
    the retryable database faults."""
    classes = _transient_db_classes()
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, classes):
            return True
        cur = cur.__cause__
    return False


def _replay_safe(verb: str, args: dict[str, Any]) -> bool:
    """May this call be re-run after a transient fault? See the rule above."""
    kind = args.get("kind")
    if verb in _RETRY_VERBS:
        return kind is None or kind in _LOCAL_READ_KINDS
    return verb == "put" and kind == "job" and bool(args.get("idem_key"))


def _retry_sleep(attempt: int) -> None:
    """Full-jitter backoff before retry number ``attempt`` (1-based)."""
    time.sleep(random.uniform(0, _RETRY_BASE_S * 2**attempt))


class DispatchMixin(RuntimeShape):
    """Verb dispatch, kind/handler resolution, and handler invocation."""

    def _dispatch_with_retry(self, verb: str, args: dict[str, Any]) -> Response:
        """:meth:`_dispatch_inner` with transient database faults retried.

        A persisting fault surfaces as ``Upstream`` whose text says how many
        times the call was retried; a replay-unsafe call (see the rule
        above) is never retried and surfaces through the normal mapping."""
        safe = _replay_safe(verb, args)
        attempt = 1
        while True:
            try:
                return self._dispatch_inner(verb, copy.deepcopy(args))
            except Exception as exc:
                if not _is_transient_db_fault(exc):
                    raise
                if not safe or attempt >= _RETRY_ATTEMPTS:
                    if attempt == 1:
                        raise
                    mapped = map_boundary_exception(exc)
                    cause = mapped.cause if mapped else "a transient database fault"
                    raise Upstream(
                        f"{cause}; retried {attempt - 1} times and it persisted",
                        next="wait a minute, then retry the call; if it still "
                        "fails, file a gripe naming it",
                    ) from exc
                log.warning(
                    "transient %s in %s; retry %d/%d",
                    type(exc).__name__,
                    verb,
                    attempt,
                    _RETRY_ATTEMPTS - 1,
                )
                _retry_sleep(attempt)
                attempt += 1

    def dispatch(self, verb: str, args: dict[str, Any], *, fold: bool = False) -> str:
        """Run one verb call. Returns the rendered string for the agent.

        Errors are caught and rendered as text — they never propagate
        out (MCP expects a string return). Tests rely on this shape;
        callers that need the protocol-level error flag (e.g. the MCP
        tool wrappers) should use :meth:`dispatch_with_status`.

        ``fold`` opts into the write-time fold — see
        :meth:`dispatch_with_status`.
        """
        body, _is_error = self.dispatch_with_status(verb, args, fold=fold)
        return body

    def _chunk_exactly_readable(self, resolved: ResolvedHandle) -> bool:
        handler = self.hub.handler_for(resolved.kind)
        if (
            handler is not None
            and getattr(handler.spec, "supports_chunk_selectors", None) is False
        ):
            return True
        return self._kind_supports_chunk_selectors(
            resolved.kind, resolved.public_id, resolved.ref_id
        )

    def _dereference_write_args(
        self, verb: str, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        if verb not in ("put", "edit"):
            return args, None
        text = args.get("text")
        if not isinstance(text, dict) or "$ref" not in text:
            return args, None
        store = self.store
        if store is None:
            raise BadInput(
                "text $ref needs a store-backed deployment",
                next="pass literal text= instead",
            )
        from precis.runtime.handle_deref import expand_text_reference

        expanded = expand_text_reference(
            text, store=store, chunk_readable=self._chunk_exactly_readable
        )
        if expanded is None:
            return args, None
        dispatch_args = dict(args)
        dispatch_args["text"] = expanded.text
        return dispatch_args, expanded.ack

    def dispatch_with_status(
        self, verb: str, args: dict[str, Any], *, fold: bool = False
    ) -> tuple[str, bool]:
        """Run one verb call and report whether it errored.

        Returns ``(body, is_error)``. ``body`` is the same rendered text
        that :meth:`dispatch` returns; ``is_error`` is True when the
        call raised a :class:`PrecisError` or unhandled exception.
        Lets the MCP tool wrapper raise so FastMCP sets the protocol-
        level ``isError`` flag while keeping a single source of truth
        for the rendering. (MCP critic MAJOR — errors silently masked
        as content because ``isError`` was never set.)

        ``fold`` opts into the agent-context result budget. Agent-serving
        surfaces pass it explicitly; direct in-process callers leave it off.
        An oversized result refuses with a targeted-read next step instead
        of returning a partial body or continuation cursor. The hard
        ``PRECIS_MAX_BODY_BYTES`` transport cap applies either way.
        """
        started = time.monotonic()
        with self.hub.request_scope(verb=verb):
            try:
                if verb not in _VERBS:
                    raise BadInput(
                        f"unknown verb: {verb}",
                        options=list(_VERBS),
                    )
                dispatch_args, deref_ack = self._dereference_write_args(verb, args)
                _refuse_envelope_denied_verb(verb, dispatch_args, self.hub)
                _refuse_agent_secrets(verb, dispatch_args, self.hub)
                response = self._dispatch_with_retry(verb, dispatch_args)
                if deref_ack is not None:
                    response = replace(response, body=f"{deref_ack}\n{response.body}")
                # gr515633 ask 2: a ``tags=`` that names a categorizer
                # axis silently drops the still-unclassified share of the
                # corpus (the filter is AND/exclusive) — append the
                # coverage note while the axis sits below the
                # filter-ready bar. Best-effort: never fails a search.
                if verb == "search":
                    response = _with_categorizer_filter_notes(self, args, response)
                body = self._render(response)
                from precis._result_budget import check_result_budget

                check_result_budget(
                    body,
                    verb=verb,
                    kind=args.get("kind"),
                    context_limited=fold,
                    targeted_hint=response.result_budget_hint,
                )
                if verb == "get" and self.store is not None:
                    from precis.runtime.handle_deref import record_exact_read

                    record_exact_read(
                        args.get("id"), store=self.store, explicit_kind=args.get("kind")
                    )
                self._record_tool_call(
                    verb, args, body, False, started, response.result_count
                )
                return body, False
            except PrecisError as e:
                scrub_upstream(e)
                self._maybe_add_skill_hint(e, verb, args)
                self._maybe_add_schema_drift_hint(e)
                self._maybe_add_kind_skills_hint(e, verb, args)
                body = self._render_failure(e)
                self._record_tool_call(verb, args, body, True, started)
                return body, True
            except Exception as e:
                # F10: full traceback (with SQL fragments, Python
                # signatures, file paths) goes to the server log only.
                # The user-visible body keeps the exception *type* —
                # enough signal for the LLM to recover ("UndefinedTable
                # → run migrations") — but strips the message body that
                # leaks internals. Specific exception classes that have
                # a clean recovery story should be caught upstream and
                # converted to a typed PrecisError (Unavailable,
                # NotFound, etc.) before reaching this fallback.
                log.exception("internal error in %s", verb)
                mapped = map_boundary_exception(e)
                if mapped is not None:
                    self._maybe_add_skill_hint(mapped, verb, args)
                    body = self._render_failure(mapped)
                    self._record_tool_call(verb, args, body, True, started)
                    return body, True
                body = self._render_failure(
                    Internal(
                        f"internal error in {verb}: {type(e).__name__} "
                        "(see server log)",
                        next=self._schema_drift_note(e)
                        or "file a gripe naming this call; it is a server bug",
                    )
                )
                self._record_tool_call(verb, args, body, True, started)
                return body, True

    def _record_tool_call(
        self,
        verb: str,
        args: dict[str, Any],
        body: str,
        is_error: bool,
        started: float,
        result_count: int | None = None,
    ) -> None:
        """Best-effort tool-call ledger write (migration 0133).

        Fires after the verb has already fully run and its body is
        rendered — the one extra INSERT never gates the caller's
        result, and any failure here (missing table on a pre-0133 DB,
        a bad/absent store, a connection hiccup) is swallowed. A
        logging problem must never break the tool call it measures,
        mirroring :mod:`precis.route_log`'s dark-by-construction rule.

        ``args`` is read, never mutated — by the time this runs,
        ``_dispatch_inner`` has already consumed its own private copy
        (``dict(args)``), so the caller's original top-level kwarg
        names are still exactly what came in. Values reach the ledger
        only through :func:`~precis.tool_ledger.arg_sample`'s bounded,
        masked, credential-key-dropping render (migration 0209) — never
        raw.
        """
        store = self.store
        if store is None:
            return
        try:
            from precis import serve_ledger
            from precis.agentlog import current_from_env, current_source_from_env
            from precis.tool_ledger import (
                ToolCallRecord,
                _mint_session_key,
                arg_sample,
                current_client,
                error_head,
                record_call,
            )

            error_type: str | None = None
            if is_error:
                m = _ERROR_TYPE_RE.match(body)
                error_type = m.group(1) if m else "Unknown"
            kind = args.get("kind")
            session_id, seq = _mint_session_key()
            # The raw wire arguments (CallToolRequest params.arguments,
            # bound by the wrapper via serve_ledger) say exactly which
            # keys the caller sent — including explicitly-null ones,
            # which the payload can't tell from a defaulted None
            # (gr504415). Without a live MCP request (CLI, tests) the
            # fallback keeps the old rule: None-valued keys are wrapper
            # defaults, not caller input — tools/core.py's put/edit/get
            # pass every declared kwarg defaulted to None, so recording
            # bare key presence would log the same static ~70-key list
            # for every put — useless for arg-shape mining.
            raw = serve_ledger.current_raw_args()
            input_keys = (
                sorted(str(k) for k in raw)
                if raw is not None
                else sorted(str(k) for k, v in args.items() if v is not None)
            )
            record_call(
                store,
                ToolCallRecord(
                    verb=verb,
                    kind=str(kind) if kind is not None else None,
                    input_keys=input_keys,
                    outcome="error" if is_error else "ok",
                    error_type=error_type,
                    result_count=result_count,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    agentlog_id=current_from_env(),
                    # The producing JOB ("dream", "plan_tick"), not the model
                    # tier (gr537773) — the tier lives on agentlog.model.
                    source=current_source_from_env(),
                    profile=os.environ.get("PRECIS_MCP_PROFILE", "typed"),
                    session_id=session_id,
                    seq=seq,
                    result_bytes=len(body.encode("utf-8", errors="replace")),
                    error_head=error_head(body) if is_error else None,
                    arg_sample=arg_sample(args),
                    client=current_client(),
                ),
            )
        except Exception:
            log.debug("tool_ledger: _record_tool_call failed", exc_info=True)

    def _maybe_add_schema_drift_hint(self, err: PrecisError) -> None:
        """Append the schema-drift recovery hint to an already-typed error.

        Handlers (and :meth:`_invoke_handler`'s defaulted-kind wrap)
        sometimes re-raise a psycopg schema error inside a
        ``PrecisError`` — the drift note must ride on that envelope
        too, not only on the raw-exception fallback path.
        """
        note = self._schema_drift_note(err)
        if note is None:
            return
        if err.next is None:
            err.next = note
        elif isinstance(err.next, str):
            err.next = [err.next, note]
        else:
            err.next.append(note)

    def _schema_drift_note(self, exc: BaseException) -> str | None:
        """Actionable recovery hint when ``exc`` chains to a
        schema-shape DB error, else ``None``.

        The gr281493 outage family (dozens of duplicate gripes filed
        blind): a long-lived server keeps serving after a deploy
        migrates the DB under it, and every ref-backed verb then dies
        with an opaque ``[error:Internal] … UndefinedColumn (see server
        log)`` — agents can't tell a code bug from "the process is
        stale, restart it". When the exception chain carries
        ``psycopg.errors.UndefinedColumn``/``UndefinedTable``, compare
        the migration head captured at runtime construction
        (:data:`~precis.runtime.core.PrecisRuntime.boot_migration_head`)
        against the live ``_migrations`` ledger and name the real
        recovery. Best-effort: any probe failure (no store, ledger
        query fails, no boot head) returns ``None`` and the generic
        envelope stands.
        """
        try:
            from psycopg.errors import UndefinedColumn, UndefinedTable
        except Exception:  # pragma: no cover — psycopg always present
            return None
        seen: set[int] = set()
        cur: BaseException | None = exc
        found = False
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, (UndefinedColumn, UndefinedTable)):
                found = True
                break
            cur = cur.__cause__ or cur.__context__
        if not found:
            return None

        boot = getattr(self, "boot_migration_head", None)
        store = self.store
        if boot is None or store is None:
            return None
        try:
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT max(version) FROM public._migrations "
                    "WHERE plugin = 'precis'"
                ).fetchone()
            db_head = row[0] if row else None
        except Exception:
            log.debug("schema-drift probe failed", exc_info=True)
            return None
        if db_head is None or db_head == boot:
            return None
        if db_head > boot:
            return (
                f"the database is at migration {db_head} but this server "
                f"process booted with migrations up to {boot} — the process "
                "predates a migration and is serving stale code. Restart "
                "the precis MCP server / worker; no retry will succeed "
                "until then."
            )
        return (
            f"this build ships migration {boot} but the database head is "
            f"{db_head} — the database is behind the code. Run `precis "
            "migrate` (scripts/deploy does this), then retry."
        )

    @staticmethod
    def _hoist_spray_args(args: dict[str, Any]) -> None:
        """``args={'angle':…,'like':…}`` → top-level ``angle=``/``like=``.

        The angle spray's knobs are verb-level ``search`` params, not a
        kind's ``args=`` keys — no handler's ``search`` accepts them, so
        nested they always died as ``keys ['angle'] not accepted by
        <kind>.search`` (gr501176, gr501177). A caller who nests them
        plainly wants the spray: hoist so the ``_dispatch_angle``
        intercept fires. ``n``/``top_k`` ride along only when a spray
        trigger is present; alone they're legitimately refused by the
        kind's extras gate. Runs before ``_resolve_under`` so the
        under=/angle= refusal sees the hoisted keys too.
        """
        extras = args.get(_EXTRAS_KEY)
        if not isinstance(extras, dict):
            return
        if "angle" not in extras and "like" not in extras:
            return
        for key in ("angle", "like", "n", "top_k"):
            if key in extras:
                args[key] = extras.pop(key)

    @staticmethod
    def _hoist_source_facet_args(args: dict[str, Any]) -> None:
        """``args={'uncited':…,'cited':…,'hubbed':…}`` → top-level.

        The cite facets are dispatcher-consumed search params (resolved
        into ``exclude_ref_ids``/``include_ref_ids`` by
        :meth:`_resolve_source_facets`), never a handler's ``args=``
        keys — nested, they died at the extras whitelist as
        ``keys ['uncited'] not accepted by paper.search`` (gr508686's
        7d ledger). Same hoist as ``under=``/``axis=``/``depth=``
        (:meth:`_resolve_under` pops them out of extras) and the spray
        knobs (:meth:`_hoist_spray_args`). A key present at both levels
        is ambiguous — refuse it rather than pick a winner."""
        extras = args.get(_EXTRAS_KEY)
        if not isinstance(extras, dict):
            return
        for key in ("uncited", "cited", "hubbed"):
            if extras.get(key) is None:
                continue
            if args.get(key) is not None:
                raise BadInput(
                    f"{key}= was passed twice — top-level and inside args=",
                    next=f"pass {key}= once, at the top level",
                )
            args[key] = extras.pop(key)

    def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
        """``search(uncited=/cited=/hubbed=...)`` resolution wrapper around
        :meth:`_dispatch_inner_core`.

        Resolves the source facets into merged ``args['exclude_ref_ids']``/
        ``args['include_ref_ids']`` BEFORE any search-shape interception
        branches off, so every retrieval path sees the same filter — then
        prepends the agent-facing note(s) to whatever ``Response`` the core
        method returns, regardless of which internal early-return branch
        produced it. Kept as a thin wrapper (not folded into the core
        method) precisely because there are so many early returns there:
        one wrapping call guarantees the footer is never forgotten on a
        future branch.

        The note is **prepended** so it leads the complete bounded result
        rather than appearing after all hits.
        """
        note: str | None = None
        listing_kinds: tuple[list[str] | None] | None = None
        if verb == "search":
            self._hoist_spray_args(args)
            self._hoist_source_facet_args(args)
            note, listing_kinds = self._resolve_under(args)
        if verb == "search" and (
            args.get("uncited") is not None
            or args.get("cited") is not None
            or args.get("hubbed") is not None
        ):
            self._reject_source_facet_unfiltered_shape(args)
            facet_note = self._resolve_source_facets(args)
            note = "\n".join(n for n in (note, facet_note) if n) or None
        if listing_kinds is not None:
            response = self._under_listing(args, listing_kinds[0])
        else:
            response = self._dispatch_inner_core(verb, args)
        if note is not None:
            from dataclasses import replace as _replace

            response = _replace(response, body=f"{note}\n\n{response.body}")
        return response

    def _resolve_under(
        self, args: dict[str, Any]
    ) -> tuple[str | None, tuple[list[str] | None] | None]:
        """One ``under=`` on search; the target picks the tree.

        * a taxon (``tn42``, ``taxon:42``, a path like ``technique/afm``,
          or a list of them) → refs with an ``instance-of`` link into the
          node's subtree (a list intersects; ``axis=``/``depth=`` shape the
          walk; see :mod:`precis.runtime.under_facet`);
        * any other handle (``me5``, ``kind:id``) → the descendants of that
          ref on the ``part-of`` / ``contains`` tree (``depth=1`` = direct
          members). The folder scope (``folder=``) walks ``refs.parent_id``
          instead and is untouched.

        Both resolve into ``args['include_ref_ids']`` — the channel every
        retrieval path reads — by intersection with any earlier facet,
        exactly as ``cited=`` does. A kind whose handler sets
        ``owns_under`` (todo, taxon) keeps its own ``under=``; a cross-kind
        search with a non-taxon target leaves ``under=`` alone.

        Returns ``(note, listing)``: ``listing`` is ``(kinds,)`` when a
        taxon ``under=`` has no ``q=`` and the caller should list the set
        newest first (:meth:`_under_listing`), else ``None``."""
        kind = args.get("kind")
        k = kind.strip() if isinstance(kind, str) else ""
        cross = not k or k in ("*", "all") or "," in k
        if not cross:
            handler = self.hub.handler_for(self._expand_kind_code(k))
            if handler is not None and handler.owns_under:
                return None, None  # this kind defines its own under=/depth=
        extras = args.get(_EXTRAS_KEY)
        sources = [args, extras] if isinstance(extras, dict) else [args]

        def _peek(name: str) -> Any:
            for src in sources:
                if src.get(name) is not None:
                    return src[name]
            return None

        under = _peek("under")
        taxon = under is not None and self._under_is_taxon(under)
        if cross and not taxon:
            return None, None  # each handler's own under= (if any) stays put
        if taxon and "," in k:
            owners = [
                x.strip()
                for x in k.split(",")
                if getattr(
                    self.hub.handler_for(self._expand_kind_code(x.strip())),
                    "owns_under",
                    False,
                )
            ]
            if owners:
                raise BadInput(
                    f"kind={owners[0]!r} has its own under=; a taxon under= "
                    "cannot share a kind list with it",
                    next=f"search kind={owners[0]!r} on its own, or drop it from kind=",
                )
        for src in sources:
            for key in ("under", "depth", "axis"):
                if src.get(key) is not None:
                    args[key] = src.pop(key)
        under = args.pop("under", None)
        depth = args.pop("depth", None)
        axis = args.pop("axis", None)
        if under is not None or depth is not None or axis is not None:
            self._note_channel_value(args, "include_ref_ids")
            for _k, _v in (("under", under), ("depth", depth), ("axis", axis)):
                if _v is not None:
                    self._note_facet_origin(args, "include_ref_ids", _k, _v)
        if under is None:
            if depth is not None or axis is not None:
                raise BadInput(
                    f"{'depth' if depth is not None else 'axis'}= needs under=",
                    next="search(kind='memory', q='…', args={'under': 'me5', 'depth': 1})",
                )
            return None, None
        n_depth: int | None = None
        if depth is not None:
            try:
                n_depth = int(depth)
            except (TypeError, ValueError):
                n_depth = 0
            if isinstance(depth, bool) or n_depth < 1:
                raise BadInput(
                    f"depth= must be a positive integer, got {depth!r}",
                    next="depth=1 lists direct members; omit it for the whole subtree",
                )
        view = str(args.get("view") or "").strip()
        if (
            view in ("dreamable", "stubs", "chase-queue")
            or "angle" in args
            or "like" in args
        ):
            raise Unsupported(
                "under= is not supported with view=/angle=/like= — that search "
                "shape picks its own seed and target set",
                next="search(kind='finding', under='technique/afm', q='...')",
            )
        kinds = (
            None
            if k in ("", "*", "all")
            else [self._expand_kind_code(x.strip()) for x in k.split(",")]
        )
        bad_kinds = sorted(set(kinds or ()) & _UNCITED_UNSUPPORTED_KINDS)
        if bad_kinds:
            raise Unsupported(
                f"under= is not supported for kind={bad_kinds[0]!r} — its search "
                "has no include-by-ref_id wiring",
                next="drop that kind from kind=, or search a kind that has one",
            )
        store = self.store
        if store is None:
            raise Unsupported("under= needs a store-backed deployment")
        if taxon:
            return self._resolve_under_taxon(args, under, axis, n_depth, kinds)
        if axis is not None:
            raise BadInput(
                f"axis= applies only when under= is a taxon, not {under!r}",
                next="drop axis=, or under='tn42' to walk a taxon subtree",
            )
        from precis.utils import handle_registry

        raw = str(under).strip()
        resolved = store.resolve_handle(raw)
        ref_id = int(resolved.ref_id) if resolved is not None else None
        if ref_id is None:
            try:
                from precis.utils.link_target import parse_link_target

                ref_id = parse_link_target(raw, store=store).ref_id
            except PrecisError:
                ref_id = None
        if ref_id is None:
            raise NotFound(
                f"under={raw!r} resolves to no live ref",
                next=UNDER_NEXT,
            )
        below = store.part_of_descendants(ref_id, depth=n_depth)
        self._intersect_include_ref_ids(args, below)
        name = handle_registry.try_format(
            resolved.kind if resolved is not None else "memory", ref_id
        )
        n = len(below)
        scope = "direct members" if n_depth == 1 else "descendants"
        return f"_(under={name or raw}: restricted to {n} {scope} on part-of)_", None

    def _under_is_taxon(self, under: Any) -> bool:
        """Whether an ``under=`` target names a taxon node (→ instance-of
        members) rather than another ref (→ part-of descendants). A list
        is always taxa (it intersects; each entry is checked on resolve).
        A bare string that is no handle is a taxon path."""
        if isinstance(under, (list, tuple)):
            return True
        if self.hub.handler_for("taxon") is None:
            return False
        from precis.utils import handle_registry

        raw = str(under).strip()
        store = self.store
        resolved = store.resolve_handle(raw) if store is not None else None
        if resolved is not None:
            return bool(resolved.kind == "taxon")
        if ":" in raw:
            return raw.split(":", 1)[0] == "taxon"
        if raw.isdigit():
            return store is not None and (
                store.get_ref(kind="taxon", id=int(raw)) is not None
            )
        parsed = handle_registry.parse(raw)
        if parsed is not None:
            return bool(parsed[0] == "taxon")
        return True

    def _resolve_under_taxon(
        self,
        args: dict[str, Any],
        under: Any,
        axis: Any,
        depth: int | None,
        kinds: list[str] | None,
    ) -> tuple[str, tuple[list[str] | None] | None]:
        from precis.runtime.under_facet import resolve_under

        q = args.get("q")
        has_q = q is not None and bool(str(q).strip())
        extras = args.get(_EXTRAS_KEY)
        if not has_q:
            # The newest-first listing honours only include/exclude_ref_ids;
            # refuse the narrowing args it cannot apply rather than drop them.
            for name in (
                "since",
                "until",
                "sort",
                "tag",
                "tags",
                "kinds",
                "status",
                "folder",
                "scope",
                "mode",
                "view",
            ):
                if args.get(name) is not None or (
                    isinstance(extras, dict) and extras.get(name) is not None
                ):
                    raise BadInput(
                        f"{name}= is not applied to an under= listing without q=",
                        next=f"add q= to use {name}=, or drop {name}=",
                    )
        note, ids = resolve_under(self.hub, under, axis=axis, depth=depth)
        self._intersect_include_ref_ids(args, set(ids))
        return note, (None if has_q else (kinds,))

    def _under_listing(self, args: dict[str, Any], kinds: list[str] | None) -> Response:
        """Newest-first page of a taxon ``under=`` set when no ``q=`` ranks it."""
        from precis.utils import handle_registry

        store = self.store
        assert store is not None  # checked in _resolve_under
        ids = set(args.get("include_ref_ids") or ())
        exclude = args.get("exclude_ref_ids")
        if exclude:
            ids -= set(exclude)
        page_size = int(args.get("page_size") or 10)
        page = max(1, int(args.get("page") or 1))
        rows, total = store.list_refs_newest(
            sorted(ids), kinds=kinds, limit=page_size, offset=(page - 1) * page_size
        )
        lines = [f"{total} instance{'s' if total != 1 else ''}, newest first"]
        if total > page_size:
            lines[0] += f" (page {page}; page=N for more, or add q= to rank)"
        for rid, kd, title in rows:
            lines.append(f"{handle_registry.format_handle(kd, rid)} {title}".rstrip())
        return Response(body="\n".join(lines))

    def _reject_source_facet_unfiltered_shape(self, args: dict[str, Any]) -> None:
        """Refuse ``uncited=``/``cited=``/``hubbed=`` on the search shapes
        that :meth:`_dispatch_inner_core` intercepts and returns from
        *before* any ``exclude_ref_ids``/``include_ref_ids`` is consulted.

        ``view='dreamable'``, ``view='stubs'``, ``view='chase-queue'`` and
        the ``angle=``/``like=`` spray each pick their own seed and target
        set and never read ``exclude_ref_ids``/``include_ref_ids`` — and
        their default target set includes ``paper``, the very kind these
        facets are built from. Left alone they would return fully
        unfiltered hits *underneath a note claiming a filter ran*, which is
        worse than no feature at all: the note actively attests that a
        filter ran. A caller cannot see the difference from the output, so
        this must fail loudly. Same stance as the ``UNCITED_UNSUPPORTED_KINDS``
        guard, for the same reason.
        """
        facets = [f for f in ("uncited", "cited", "hubbed") if args.get(f) is not None]
        if not facets:
            return
        view = str(args.get("view") or "").strip()
        shape = (
            view
            if view in ("dreamable", "stubs", "chase-queue")
            else "angle="
            if "angle" in args
            else "like="
            if "like" in args
            else None
        )
        if shape is None:
            return
        facet_desc = "/".join(f"{f}=" for f in facets)
        raise Unsupported(
            f"{facet_desc} is not supported with {shape} — that search "
            "shape picks its own seed and target set and has no "
            "exclude/include-by-ref_id wiring",
            next=(
                f"drop {facet_desc}, or use a plain search(q=..., "
                f"{facets[0]}=...) which filters across every citeable kind"
            ),
        )

    @staticmethod
    def _note_facet_origin(
        args: dict[str, Any], channel: str, key: str, value: Any
    ) -> None:
        """Record that ``key=value`` was rewritten into ``channel``.

        ``uncited=``/``cited=``/``hubbed=``/``under=`` resolve into
        ``args['exclude_ref_ids']``/``args['include_ref_ids']`` and are
        popped — the record lets ``SearchMixin._search_call_text``
        re-render the caller's own keys in ``next=`` hints instead of
        leaking the resolved ref-id lists. A caller-supplied channel
        key (``exclude_ref_ids=``) is itself recorded as an origin the
        first time dispatch rewrites it, so the hint still names the
        value the caller actually passed.
        """
        origins = args.setdefault(_RESOLVED_FACETS_KEY, {})
        origins.setdefault(channel, []).append((key, value))

    def _note_channel_value(self, args: dict[str, Any], channel: str) -> None:
        origins = args.setdefault(_RESOLVED_FACETS_KEY, {})
        if channel not in origins and channel in args:
            origins[channel] = [(channel, args[channel])]

    def _resolve_source_facets(self, args: dict[str, Any]) -> str | None:
        """Resolve ``uncited=``/``cited=``/``hubbed=`` into
        ``args['exclude_ref_ids']``/``args['include_ref_ids']``, in that
        order, and return the combined agent-facing note (``None`` when no
        facet was present).

        ``cited=`` and ``uncited=`` are mutually exclusive (one asks for
        the closure excluded, the other for the closure alone) — raises
        ``BadInput`` when both are present. ``hubbed=`` composes with
        either: ``hubbed=True`` intersects into ``include_ref_ids`` (both
        restrictions must hold), ``hubbed=False`` unions into
        ``exclude_ref_ids`` (either exclusion drops the hit).
        """
        if args.get("cited") is not None and args.get("uncited") is not None:
            raise BadInput(
                "cited= and uncited= are mutually exclusive",
                next=(
                    "pass cited=<draft> to restrict to already-cited sources, "
                    "or uncited=<draft> to exclude them — not both"
                ),
            )
        notes: list[str] = []
        if args.get("uncited") is not None:
            notes.append(self._resolve_uncited_exclude(args))
        if args.get("cited") is not None:
            notes.append(self._resolve_cited_include(args))
        if args.get("hubbed") is not None:
            notes.append(self._resolve_hubbed_facet(args))
        return "\n".join(notes) if notes else None

    def _resolve_uncited_exclude(self, args: dict[str, Any]) -> str:
        """Resolve ``search(uncited=<draft>)`` into
        ``args['exclude_ref_ids']``.

        Pops the raw ``uncited=`` token, replaces it with a merged, sorted
        ``list[int]`` under ``exclude_ref_ids`` — the exact channel every
        retrieval path (source-search, cross-kind, single-kind) reads, so
        the filter can't drift between paths. The exclusion set is
        :func:`precis.backfill.candidates.draft_cited_ref_ids`: every
        source the draft directly cites, plus a cited claim hub's
        evidence-**supporter** papers (originators+corroborators; a
        contradicting paper keeps surfacing). Raises when ``uncited=``
        doesn't resolve to a live draft — must never silently degrade to
        an empty exclusion set, which would make every hit look "new".

        Returns the agent-facing note reporting how many refs were
        excluded.
        """
        from precis.backfill.candidates import draft_cited_ref_ids, resolve_draft_ref_id
        from precis.utils import handle_registry

        token = args.pop("uncited")
        store = self.store
        if store is None:
            raise Unsupported("uncited= needs a store-backed deployment")
        draft_ref_id = resolve_draft_ref_id(store, str(token))
        cited = draft_cited_ref_ids(store, draft_ref_id)
        self._note_channel_value(args, "exclude_ref_ids")
        self._note_facet_origin(args, "exclude_ref_ids", "uncited", token)
        merged: set[int] = set(args.get("exclude_ref_ids") or ())
        merged |= cited
        args["exclude_ref_ids"] = sorted(merged)
        handle = (
            handle_registry.try_format("draft", draft_ref_id) or f"draft:{draft_ref_id}"
        )
        n = len(cited)
        return (
            f"_(uncited={handle}: {n} already-cited source{'s' if n != 1 else ''} "
            "excluded)_"
        )

    def _resolve_cited_include(self, args: dict[str, Any]) -> str:
        """Resolve ``search(cited=<draft>)`` into ``args['include_ref_ids']``
        — the inclusion mirror of :meth:`_resolve_uncited_exclude`: same
        closure (:func:`~precis.backfill.candidates.draft_cited_ref_ids`),
        same non-draft/unresolvable rejection, but restricts hits to the
        closure instead of dropping it.

        Intersects into any ``include_ref_ids`` a prior facet in this same
        call already set (today only :meth:`_resolve_hubbed_facet` could
        run first, but it's resolved after ``cited=`` — see
        :meth:`_resolve_source_facets` — so this is future-proofing, not
        live behaviour): two inclusion restrictions must both hold, not
        either. An empty closure sets ``include_ref_ids = []`` — ANY(ARRAY[])
        matches nothing, which is correct: "restricted to 0 already-cited
        sources" must return zero hits, not fall through unfiltered.
        """
        from precis.backfill.candidates import draft_cited_ref_ids, resolve_draft_ref_id
        from precis.utils import handle_registry

        token = args.pop("cited")
        store = self.store
        if store is None:
            raise Unsupported("cited= needs a store-backed deployment")
        draft_ref_id = resolve_draft_ref_id(store, str(token))
        cited = draft_cited_ref_ids(store, draft_ref_id)
        self._note_channel_value(args, "include_ref_ids")
        self._note_facet_origin(args, "include_ref_ids", "cited", token)
        self._intersect_include_ref_ids(args, cited)
        handle = (
            handle_registry.try_format("draft", draft_ref_id) or f"draft:{draft_ref_id}"
        )
        n = len(cited)
        return (
            f"_(cited={handle}: restricted to {n} already-cited "
            f"source{'s' if n != 1 else ''})_"
        )

    def _resolve_hubbed_facet(self, args: dict[str, Any]) -> str:
        """Resolve ``search(kind='paper', hubbed=True|False)`` into
        ``args['include_ref_ids']``/``args['exclude_ref_ids']``.

        Paper-only (single ``kind='paper'`` — cross-kind fan-out is out of
        scope: the hubbed set is paper ref_ids only, so composing it into
        the shared exclude/include channel for every fanned-out kind would
        either be a harmless no-op (exclude) or silently zero out every
        non-paper kind's hits (include) — neither is what an agent asking
        for ``hubbed=`` on a mixed fan-out would expect). Raises
        ``BadInput`` naming the offending ``kind=`` otherwise.

        ``hubbed=True`` intersects the corpus-wide hubbed-paper set
        (:func:`~precis.backfill.candidates.hubbed_paper_ref_ids`, one
        query) into ``include_ref_ids``; ``hubbed=False`` unions it into
        ``exclude_ref_ids``.
        """
        hubbed = args.pop("hubbed")
        if not isinstance(hubbed, bool):
            raise BadInput(
                f"hubbed= must be a boolean, got {hubbed!r}",
                next="search(kind='paper', q='...', hubbed=True) or hubbed=False",
            )
        kind = args.get("kind")
        resolved_kind = self._expand_kind_code(str(kind)) if kind is not None else None
        if resolved_kind != "paper":
            raise BadInput(
                f"hubbed= is only supported on kind='paper', got kind={kind!r}",
                next="search(kind='paper', q='...', hubbed=True)",
            )
        store = self.store
        if store is None:
            raise Unsupported("hubbed= needs a store-backed deployment")
        from precis.backfill.candidates import hubbed_paper_ref_ids

        hubbed_ids = hubbed_paper_ref_ids(store)
        is_true = hubbed
        n = len(hubbed_ids)
        if is_true:
            self._note_channel_value(args, "include_ref_ids")
            self._note_facet_origin(args, "include_ref_ids", "hubbed", hubbed)
            self._intersect_include_ref_ids(args, hubbed_ids)
            return f"_(hubbed=true: restricted to {n} hubbed paper{'s' if n != 1 else ''})_"
        self._note_channel_value(args, "exclude_ref_ids")
        self._note_facet_origin(args, "exclude_ref_ids", "hubbed", hubbed)
        merged: set[int] = set(args.get("exclude_ref_ids") or ())
        merged |= hubbed_ids
        args["exclude_ref_ids"] = sorted(merged)
        return f"_(hubbed=false: {n} hubbed paper{'s' if n != 1 else ''} excluded)_"

    @staticmethod
    def _intersect_include_ref_ids(args: dict[str, Any], ids: set[int]) -> None:
        """Merge ``ids`` into ``args['include_ref_ids']`` by intersection —
        two inclusion restrictions in the same call must both hold. Sets it
        fresh (sorted) when no prior facet touched the channel yet."""
        existing = args.get("include_ref_ids")
        merged = set(ids) if existing is None else set(existing) & set(ids)
        args["include_ref_ids"] = sorted(merged)

    def _refuse_superseded_delete(self, args: dict[str, Any]) -> None:
        """Require an explicit survivor address before any destructive redirect.

        Check every original address before normalization (and before a batch
        can write). Same-row kind conversions are not merges.
        """
        if self.store is None:
            return
        identifiers = args.get("id")
        for ident in (
            identifiers if isinstance(identifiers, (list, tuple)) else [identifiers]
        ):
            if not isinstance(ident, str):
                continue
            raw = ident.strip()
            prefix, sep, rest = raw.partition(":")
            if sep and prefix in self.hub.kinds:
                raw = rest
            if raw.startswith("§"):
                raw = raw[1:]
            base = handle_registry.normalize(re.split(r"[~/]", raw, maxsplit=1)[0])
            parsed = handle_registry.parse(base)
            if parsed is None or parsed[1]:
                continue
            resolved = self.store.resolve_handle(base)
            if resolved is not None:
                self._check_delete_redirect(base, resolved)

    @staticmethod
    def _check_delete_redirect(original: str, resolved: ResolvedHandle) -> None:
        """Check the exact resolution that would rewrite the delete target."""
        parsed = handle_registry.parse(original)
        if (
            parsed is not None
            and resolved.redirected_from is not None
            and resolved.ref_id != parsed[2]
        ):
            survivor = handle_registry.format_handle(resolved.kind, resolved.ref_id)
            raise BadInput(
                f"{original} was merged into {survivor}; delete requires the survivor's handle",
                next=f"delete(kind={resolved.kind!r}, id={survivor!r})",
            )

    def _dispatch_inner_core(self, verb: str, args: dict[str, Any]) -> Response:
        """Orchestrate one verb call.

        Three responsibilities, each delegated to a helper:
          1. Cross-kind fan-out (``kind='*'`` / comma-list).
          2. Single-kind resolution including ``kind=`` defaulting
             for ``search`` calls that omit it.
          3. Handler invocation with extras whitelist + defaulted-kind
             error annotation.
        """
        # D1: accept URI-style ``id='kind:slug[~sel]'`` on input. Extract
        # the kind prefix into ``args['kind']`` (if not already set) and
        # leave the unprefixed identifier in ``args['id']``. Validation
        # that any explicit ``kind=`` matches the prefix lives in the
        # helper. Output stays kind-explicit — this is an *input*
        # convenience that mirrors the canonical ``kind:identifier``
        # grammar already used by ``link=`` / ``unlink=``.
        if verb == "delete":
            self._refuse_superseded_delete(args)
        self._maybe_split_prefixed_id(args, verb=verb)
        kind = args.pop("kind", None)

        # Broad usability pass 2026-05-30 (#6): when an agent passes a
        # tag-shaped string as ``q=`` with no ``tags=`` filter, the
        # semantic search is statistically guaranteed to drown the
        # intended tagged refs in unrelated paper hits. Catch the
        # likely intent at the boundary and emit a deduplicated tip.
        if verb == "search":
            self._maybe_hint_tag_shaped_q(args)

        # Focus region: ``search(view='dreamable')`` is the salience
        # seed + its ANN ring (docs/backlog/dreaming.md, §view='dreamable'),
        # not the lexical+RRF path. Intercept before kind resolution —
        # it picks its own seed and cross-kind target set.
        if verb == "search" and str(args.get("view") or "").strip() == "dreamable":
            return self._dispatch_dreamable(kind, dict(args))

        # Backlog view: ``search(view='stubs')`` is the "papers we still
        # need to get" list — paper refs with an external id but no PDF
        # yet (the stub surfaces). ``kind='datasheet'`` switches to the
        # manual-fill datasheet stub backlog (gr520099); ``q=`` is
        # ignored. Intercept before kind resolution.
        if verb == "search" and str(args.get("view") or "").strip() == "stubs":
            return self._dispatch_stubs(kind, dict(args))

        # Chase-queue view: ``search(view='chase-queue')`` is the DOI-only,
        # never-tried-first slice of the same stub backlog — a tighter feed
        # for "what should I go find a DOI-resolvable PDF for right now"
        # (the stub surfaces). Paper-only; ignores ``q=``.
        # Intercept before kind resolution, same as ``view='stubs'``.
        if verb == "search" and str(args.get("view") or "").strip() == "chase-queue":
            return self._dispatch_chase_queue(kind, dict(args))

        # Count view: ``search(kind=K, view='count', group_by=...)`` is
        # the filtered total + facet counts on numeric-ref kinds
        # (gr472990). Intercepted here — before the angle/folder/
        # source-search branches — so args this shape can't honour fail
        # loudly instead of falling through to a hits-shaped response
        # that silently dropped ``view``. ``since=``/``until=`` are
        # *admitted* here (mcp-introspection gap closures 2026-10-14):
        # the shared count path bounds refs.created_at with them, so
        # they must not fall through to the source-search primitive,
        # which would answer a different question on a different table.
        if verb == "search" and str(args.get("view") or "").strip() == "count":
            _count_extras = args.get(_EXTRAS_KEY)
            bad_combo = next(
                (
                    a
                    for a in ("angle", "like", "sort")
                    if args.get(a) is not None
                    or (
                        isinstance(_count_extras, dict)
                        and _count_extras.get(a) is not None
                    )
                ),
                None,
            )
            if bad_combo is not None:
                raise BadInput(
                    f"{bad_combo}= is not supported with view='count'",
                    next=(
                        "view='count' filters with q=/tags=/status=/link=/"
                        "folder=/since=/until= and facets with "
                        "args={'group_by': 'state' | 'meta.<key>' | 'tag:<axis>'}"
                    ),
                )
            if kind is None or self._is_cross_kind_request(str(kind)):
                eligible = [
                    k
                    for k in sorted(self.hub.kinds)
                    if (h := self.hub.handler_for(k)) is not None
                    and (m := getattr(h, "search", None)) is not None
                    and "view" in self._accepted_kwargs(m)
                ]
                raise BadInput(
                    "view='count' counts one kind at a time — pass kind=",
                    options=eligible,
                    next=(
                        "search(kind='job', view='count', "
                        "args={'group_by': 'meta.job_type,state'})"
                    ),
                )
            # ``folder=`` normally routes to the cross-kind fan-out for
            # post-filtering; for a count it folds into the same
            # include_ref_ids channel under=/uncited= already used.
            if args.get("folder") is not None:
                folder_scope, _ = self._resolve_folder_scope(args.pop("folder"))
                self._intersect_include_ref_ids(args, folder_scope)

        # Introspection views (mcp-introspection gap closures 2026-10-14):
        # ``view='meta'`` = per-kind meta-key inventory, ``view='tags'`` =
        # per-kind tag axis/value counts — the reads agents used to make
        # through raw refs/ref_tags SQL.
        # An id-less ``get(kind=K, view='meta'|'tags')`` is the same
        # question asked on the wrong verb (gr492452) — serve it here
        # instead of each numeric-ref handler rejecting the view.
        if (verb == "search" or (verb == "get" and args.get("id") is None)) and str(
            args.get("view") or ""
        ).strip() in (
            "meta",
            "tags",
        ):
            return self._dispatch_introspect_view(
                str(args.get("view") or "").strip(), kind, dict(args)
            )

        # Plan view (gr503028): ``search(kind=K, q=…, view='plan')``
        # renders the route a kind-scoped search *would* take — mode,
        # embedder, ANN index + why, lexical path, candidate caps —
        # without running the ranking. Intercepted before kind
        # resolution like the other view= shapes; kind='*'/omitted
        # renders one block per fanned-out kind.
        if verb == "search" and str(args.get("view") or "").strip() == "plan":
            return self._dispatch_search_plan(kind, dict(args))

        # Angle spray: ``search`` with ``angle=`` or ``like=`` is the
        # diverse-cone semantic sampler (docs/backlog/dreaming.md), not
        # the lexical+RRF path. Intercept before kind resolution — it
        # owns its own seed resolution and cross-kind target set.
        if verb == "search" and ("angle" in args or "like" in args):
            return self._dispatch_angle(kind, dict(args))

        # Compact keywords-only TOON: ``search(view='keywords', ...)``
        # — discovery shape that returns just the keyword arrays for
        # the top hits (no preview text). Cross-kind by default (so
        # ``view='keywords'`` alone works as "what topics span the
        # corpus"); a specific ``kind=`` narrows the fan-out the same
        # way the cross-kind path does.
        if verb == "search" and str(args.get("view") or "").strip() == "keywords":
            return self._dispatch_cross_kind(
                kind if kind is not None else _CROSS_KIND_WILDCARD,
                dict(args),
            )

        # ``folder=`` scope: any folder-scoped search runs
        # through the cross-kind fan-out — the structured SearchHit
        # stream is what makes subtree post-filtering possible — even
        # when a single ``kind=`` was named (it becomes a one-kind
        # "comma list").
        if verb == "search" and args.get("folder") is not None:
            return self._dispatch_cross_kind(
                (
                    self._expand_kind_code(str(kind))
                    if kind is not None
                    else _CROSS_KIND_WILDCARD
                ),
                dict(args),
            )

        # Source search (the cross-kind primitive): a ``sort=`` /
        # ``since=`` / ``until=`` search routes to the chunk-level
        # cross-kind primitive — one store query over ``refs.kind =
        # ANY(...)`` that RRF-fuses lexical+semantic, collapses to one
        # best chunk per ref, bounds by ``refs.created_at``, and orders
        # by relevance (default) or recency. Distinct from the per-handler
        # fan-out below. Intercept before kind resolution so it composes
        # with a single kind, a comma-list, a wildcard, or an omitted kind.
        # ``_routes_to_source_search`` (SearchMixin) owns the full
        # condition — numeric_browse, view='count', kind-owned under= and
        # a queryless kind='job' since=/until= all keep their own path.
        if self._routes_to_source_search(verb, kind, args):
            return self._dispatch_source_search(kind, dict(args))

        # Cross-kind: ``kind='*'`` or comma-list. Other verbs keep the
        # single-kind contract — multi-kind get is meaningless and
        # multi-kind put would silently scatter writes.
        if verb == "search" and self._is_cross_kind_request(kind):
            return self._dispatch_cross_kind(kind, dict(args))

        # Resolve the kind. ``_resolve_kind`` may itself short-circuit
        # to a cross-kind merge when ``kind`` is None on a search call
        # and the corpus has >=2 search-supporting kinds.
        resolved_kind, kind_was_defaulted, cross_kind_resp = self._resolve_kind(
            verb, kind, args
        )
        if cross_kind_resp is not None:
            return cross_kind_resp

        # ``uncited=``/``cited=`` (resolved above into
        # ``args['exclude_ref_ids']``/``args['include_ref_ids']``) combined
        # with an EXPLICIT single-kind request for a citeable kind that has
        # no SQL-level exclude/include wiring (patent's local+OPS-remote
        # search, edgar's filing search — see
        # :data:`~precis.runtime._shared.UNCITED_UNSUPPORTED_KINDS`) must
        # fail loudly rather than silently return hits that might already
        # be (mis)cited. The default wildcard cross-kind fan-out handles
        # this kind pair differently (drops + footer-notes them, in
        # ``_dispatch_cross_kind``) since raising there would break the
        # common unscoped ``search(q=..., uncited=...)`` call entirely.
        if (
            verb == "search"
            and (
                args.get("exclude_ref_ids") is not None
                or args.get("include_ref_ids") is not None
            )
            and resolved_kind in _UNCITED_UNSUPPORTED_KINDS
        ):
            facet = "cited=" if args.get("include_ref_ids") is not None else "uncited="
            raise Unsupported(
                f"{facet} is not supported for kind={resolved_kind!r} — its "
                "search has no exclude/include-by-ref_id wiring yet",
                next=(
                    f"drop {facet} for this kind, or use "
                    "sort=/since=/until= (the cross-kind source-search "
                    "primitive) which excludes uniformly across every kind"
                ),
            )

        try:
            handler = self._resolve_handler(resolved_kind, verb, raw_id=args.get("id"))
            # gr514482: edit(id=[...]) is uniform across kinds — the
            # handler's ``edit_id_list`` picks native / repeat / refuse;
            # a list never silently edits only the first id.
            if verb == "edit" and isinstance(args.get("id"), list):
                response = self._dispatch_edit_id_list(
                    handler,
                    kind=resolved_kind,
                    kind_was_defaulted=kind_was_defaulted,
                    args=args,
                )
                if response is not None:
                    return response
            return self._invoke_handler(
                handler,
                verb,
                kind=resolved_kind,
                kind_was_defaulted=kind_was_defaulted,
                args=args,
            )
        except NotFound as exc:
            self._maybe_hint_lcsc(exc, resolved_kind, args)
            raise

    @staticmethod
    def _maybe_hint_lcsc(
        exc: NotFound, resolved_kind: str, args: dict[str, Any]
    ) -> None:
        """Append a ``kind='part'`` pointer when the failed call named a
        C-number (gr485250). An id like ``C2655145`` — or a pasted
        lcsc.com/jlcpcb.com product URL — is an LCSC part regardless of
        the kind it was looked up under; the part kind answers it live
        even on a catalog miss, so the suggestion is load-bearing, not
        decorative."""
        if resolved_kind == "part":
            return
        from precis.pcb.catalog import lcsc_id

        lcsc = next(
            (
                found
                for v in (args.get("id"), args.get("q"))
                if v is not None and (found := lcsc_id(v)) is not None
            ),
            None,
        )
        if lcsc is None:
            return
        hint = (
            f"{lcsc} looks like an LCSC/JLCPCB part number — "
            f"get(kind='part', id='{lcsc}') resolves it (live lookup on a "
            "catalog miss); search(kind='part', q=...) too"
        )
        if exc.next is None:
            exc.next = hint
        elif isinstance(exc.next, str):
            exc.next = [exc.next, hint]
        elif hint not in exc.next:
            exc.next.append(hint)

    def _resolve_kind(
        self,
        verb: str,
        kind: Any,
        args: dict[str, Any],
    ) -> tuple[str, bool, Response | None]:
        """Pin ``kind`` to a single string, or short-circuit to cross-kind.

        Returns ``(kind, defaulted, cross_kind_response)``. When the
        third element is non-None, the caller must return it directly
        — the resolver decided to fan out across kinds because no
        single defensible default exists.

        Raises ``BadInput`` when ``kind`` is missing for a non-search
        verb, or when ``search`` has no usable default and no eligible
        cross-kind targets.
        """
        if kind is not None:
            return self._expand_kind_code(str(kind)), False, None

        if verb in ("get", "link") and (
            inferred := self._kind_of_bare_id(args.get("id"))
        ):
            # gr492997: ref ids are globally unique, so a bare numeric
            # get(id=N) names its kind unambiguously. gr518636 extends
            # the same inference to link — a bare int id= names its
            # (kind, ref) just as unambiguously, and a self-identifying
            # id= (handle or 'kind:id') already routed through
            # ``_maybe_split_prefixed_id`` before this point.
            return inferred, False, None

        if verb != "search":
            raise BadInput(
                "missing kind=",
                options=sorted(self.hub.kinds),
                next=self._missing_kind_hints(verb, args),
            )

        # ``search()`` without ``kind=`` defaults to cross-kind
        # fan-out across every search-hits-capable kind. Earlier
        # versions defaulted to the most-recently-touched single
        # kind as a 7B affordance, but a "what do I know about X"
        # query is the natural shape of an unscoped search and
        # the user should see hits from every corner of the corpus
        # — biasing toward the last-touched kind hid useful answers
        # in the other kinds. The MCP critic flagged the gap as a
        # design hole (gripe:3681 #2, 2026-05-01); this commit
        # closes it by reversing the precedence: cross-kind first,
        # single-kind fallback only when the hub has <2 eligible
        # kinds. (MCP critic MAJOR-C 2026-05-02.)
        search_kinds: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            if handler.spec.supports_search:
                search_kinds.append(k)
        cross_kind = self._cross_kind_kinds()
        if len(cross_kind) >= 2:
            return (
                _CROSS_KIND_WILDCARD,
                False,
                self._dispatch_cross_kind(_CROSS_KIND_WILDCARD, dict(args)),
            )

        # ≤1 search-hits-capable kind in this build: fall back to
        # the most-recently-touched kind so a single-kind deployment
        # still works without forcing the agent to spell ``kind=``.
        defaulted = self._default_search_kind(search_kinds)
        if defaulted is not None:
            return defaulted, True, None

        raise BadInput(
            "missing kind= and no defensible default available",
            options=search_kinds,
            next=(
                "pass kind=<one of the listed kinds>, or use "
                "kind='*' / kind='all' / kind='paper,memory' for cross-kind merge"
            ),
        )

    def _expand_kind_code(self, kind: str) -> str:
        """Accept a 2-char handle code as ``kind=``.

        ``kind='dr'`` ≡ ``kind='draft'``, ``kind='pa'`` ≡ ``kind='paper'`` —
        the same registry that legends a handle now also resolves the code
        when it's passed as ``kind=``, so the ``kinds`` table is one legend
        for *reading* handles and *choosing* the kind.

        A literal that's already a registered kind always wins (never
        shadowed by a code), and only **record** codes expand: chunk codes
        (``dc``/``pc``) are address-only — you ``get``/``edit`` a chunk by
        its handle, never ``put(kind='dc', …)`` — so a code the registry
        flags ``is_chunk`` is left untouched (and falls through to the
        normal unknown-kind error). Anything that isn't a known code is
        returned verbatim."""
        if kind in self.hub.kinds:
            return kind
        try:
            resolved, is_chunk = handle_registry.kind_for_code(kind)
        except KeyError:
            return kind
        return resolved if not is_chunk else kind

    def _kind_of_bare_id(self, raw: Any) -> str | None:
        """Kind of the live ref a bare positive int ``id`` names, else None."""
        if isinstance(raw, bool):
            return None
        if isinstance(raw, str) and raw.strip().isdigit():
            raw = int(raw)
        if not isinstance(raw, int) or raw <= 0:
            return None
        if self.store is None:
            return None
        ref = self.store.fetch_refs_by_ids([raw], include_deleted=False).get(raw)
        if ref is None or ref.kind not in self.hub.kinds:
            return None
        return str(ref.kind)

    def _generic_ref_hint(self, kind: str, raw_id: Any) -> str | None:
        """The ``next`` lead for ``kind='ref'`` (gr511442); None otherwise.

        ``'ref'`` is what agents reach for meaning "any ref" — ids share
        one global ref space, but there is no generic kind: a ref is
        addressed by its own kind name or its typed handle
        (``get(kind='paper', id='pa123')``). When the call's ``id=`` names
        a live ref the reply names the real kind outright; a well-formed
        handle already carries its kind, and a bare numeric ``get(id=N)``
        infers it (:meth:`_kind_of_bare_id`).
        """
        if kind != "ref":
            return None
        real = self._kind_of_bare_id(raw_id)
        if real is not None:
            handle = handle_registry.try_format(real, int(str(raw_id).strip()))
            return f"id={raw_id} is a live {real}: get(kind={real!r}, id={raw_id})" + (
                f" — or get(id={handle!r}), the handle carries the kind"
                if handle
                else ""
            )
        if isinstance(raw_id, str):
            parsed = handle_registry.parse(raw_id.strip())
            if parsed is not None and not parsed[1]:
                return (
                    f"{raw_id!r} is a {parsed[0]} handle — "
                    f"get(id={raw_id.strip()!r}) works, no kind= needed"
                )
        return (
            "'ref' is a generic noun, not a kind — name the ref's own kind: "
            "get(kind='paper', id='pa123'), get(kind='todo', id=12); a bare "
            "numeric get(id=N) or a handle get(id='pa123') needs no kind="
        )

    def _missing_kind_hints(self, verb: str, args: dict[str, Any]) -> list[str]:
        """Recovery hints for a non-``search`` verb called without ``kind=``.

        Used to lead with :meth:`Store.most_recent_kind` — "you were
        last working on kind=X" — but that query is
        ``ORDER BY refs.updated_at DESC`` over the *whole corpus*, with
        no session/connection scoping column to filter on. On a
        single-tenant deployment that reads as "your session", but this
        MCP is multi-session (many concurrent agents against one prod
        DB — see any ``scripts/inflight`` table): the "most recently
        updated ref" is often another session's write, so the hint
        actively misdirects ("you were last working on kind='alert'" in
        a session that never touched ``alert``, gr311329). Without a
        real per-session touch log there is no session-accurate kind to
        offer, so this now falls straight back to the generic "pick
        one" — honest is better than a plausible-sounding guess. The
        per-verb help-skill pointer is still appended by
        :meth:`_maybe_add_skill_hint` after this.
        """
        if verb == "link":
            # gr518636: link(mode='add', rel='…') with no kind=/id=/target=
            # bounced on bare "missing kind=" — the refusal must name the
            # whole call shape, with the caller's own rel= filled in.
            rel = args.get("rel")
            rel_arg = f", rel={rel!r}" if isinstance(rel, str) and rel.strip() else ""
            return [
                f"link(kind='<source kind>', id=<source id>, "
                f"target='<kind>:<identifier>'{rel_arg})",
                "a typed id needs no kind=: link(id='td12', target='todo:34') "
                "or id='todo:12' self-identify",
            ]
        return ["pass kind=<one of the listed options>"]

    def _resolve_handler(self, kind: str, verb: str, *, raw_id: Any = None) -> Handler:
        """Look up the handler for ``kind`` and verify it supports ``verb``.

        Raises:
            NotFound: ``kind`` is not registered at all (unknown name).
                Options carries only the verb-supporting kinds so an
                agent retrying against a suggested kind doesn't
                cascade into a second error (MCP critic MAJOR #12).
            Unsupported: handler is registered-but-disabled for this
                build (missing env var, missing optional dep), OR
                handler exists but does not implement ``verb``. The
                first variant names the missing precondition so the
                agent can route to the operator instead of guessing
                — see broad usability pass 2026-05-30 (#8). The
                second variant enumerates the verbs this kind *does*
                support so the recovery hint is sharp.
            Unsupported: the kind is prohibited *for the duration of the
                active in-process tick* (the ContextVar gate, below).
        """
        # Per-tick prohibition (in-process agent loop only): a background pass
        # may disable a kind for one tick via the tick ContextVar — plan_tick
        # gates the draft's colliding prose-file kind so the planner writes into
        # the draft, not a freestanding file. This is the in-process twin of the
        # claude path's PRECIS_KINDS_DISABLED env entry (the spawned MCP server
        # honors *that* at construction; the in-process Hub is built once at
        # boot, so the per-tick prohibition has to be a per-call check). No-op
        # outside a tick — the ContextVar is unset for the MCP server / CLI /
        # tests, so this is byte-identical there.
        tick_hint = _tick_disabled_hint(kind)
        if tick_hint is not None:
            raise Unsupported(
                f"kind {kind!r} is disabled for this tick ({tick_hint})",
                next=tick_hint,
            )
        handler = self.hub.handler_for(kind)
        if handler is None:
            # Distinguish "registered-but-disabled in this build" from
            # "unknown kind". The hub records every gated-out kind in
            # ``loadabilities``; if ``kind`` is in there, the right
            # error class is ``Unsupported`` (the agent can't fix it
            # by retrying — the operator has to enable the kind),
            # and the breadcrumb should name the missing precondition.
            verdict = getattr(self.hub, "loadabilities", {}).get(kind)
            if verdict is not None and not verdict.loaded:
                reason = verdict.reason or "disabled"
                raise Unsupported(
                    f"kind {kind!r} is registered but disabled in this build "
                    f"({reason})",
                    next=(
                        "see get(kind='skill', id='precis-kinds-disabled-help') "
                        "and precis-overview Needs column"
                    ),
                )
            # Broad usability pass 2026-05-30 (#10): the previous
            # ``options:`` trailer silently filtered to kinds that
            # support the calling verb — agents reading the list
            # could conclude the omitted kinds didn't exist at all
            # (precis-help shows 17 total; the options here show
            # 12 for search). Name the filter in ``next:`` so a
            # reader knows the list is verb-scoped, not the full
            # registry.
            verb_kinds = self._kinds_for_verb(verb)
            # Round-2 picky N-2, 2026-05-30: when no kinds support
            # this verb in the current build (e.g. ``edit`` with no
            # file kinds wired — markdown/plaintext/tex/python all
            # need PRECIS_ROOT/PRECIS_PYTHON_ROOTS), the previous
            # breadcrumb said *"options above are kinds that support
            # verb='edit'"* — but no options were printed above, so
            # the agent was told to consult a list that wasn't there.
            # Distinguish the empty case explicitly.
            if verb_kinds:
                next_hint = (
                    f"options above are kinds that support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') for the complete "
                    f"kind table"
                )
            else:
                next_hint = (
                    f"no kinds in this build support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') lists every "
                    f"kind and the verbs each one accepts. The most "
                    f"likely cause is a missing env var "
                    f"(see get(kind='skill', id='precis-kinds-disabled-help'))."
                )
            # Federation hint: if any other process in the cluster
            # currently advertises this kind via ``kind_provider``,
            # name the hosts so the caller knows where to route. Pure
            # informational — the local process still rejects the
            # call. Skipped on stateless boots (no store) and on any
            # query error (kind_provider may be absent on a fresh DB
            # that hasn't run migration 0022 yet).
            route_hint: str | None = None
            store = getattr(self.hub, "store", None)
            if store is not None:
                try:
                    hosts = store.find_kind_providers(kind)
                except Exception:  # pragma: no cover - missing table / DB error
                    hosts = []
                if hosts:
                    route_hint = (
                        f"kind {kind!r} routes through host(s): {', '.join(hosts)}"
                    )
            if route_hint is not None:
                next_hint = f"{route_hint}; {next_hint}"
            # A RETIRED kind leads with its successor (:data:`_RETIRED_KINDS`)
            # — "unknown kind" is true but unhelpful when the capability
            # simply moved. The generic breadcrumb still trails it, so the
            # caller keeps the full kind table one call away. gr511442:
            # ``kind='ref'`` gets the same treatment — there is no generic
            # 'ref' kind, so the lead names the typed call shape (the real
            # kind when the numeric id resolves to a live ref).
            leads = [
                h
                for h in (
                    self._generic_ref_hint(kind, raw_id),
                    _RETIRED_KINDS.get(kind),
                )
                if h is not None
            ]
            raise NotFound(
                f"unknown kind: {kind}",
                options=verb_kinds,
                next=[*leads, next_hint] if leads else next_hint,
            )

        if not handler.spec.supports(verb):
            verbs = [v for v in _VERBS if handler.spec.supports(v)]
            # ``options`` enumerates the supported verbs as the
            # recovery vocabulary; ``next`` gives a concrete
            # *callable* shape rather than re-listing the same
            # names so the LLM can copy-paste-execute. Pick ``get``
            # as the safest recovery suggestion when available —
            # every kind supports it and a minimum-arg ``get(kind=
            # X)`` either returns a list view (numeric/file kinds)
            # or fails with a kind-specific BadInput pointing at
            # the right next step (calc/math/web/etc. requiring
            # ``q=`` or ``id=``). Either way the LLM lands one
            # call closer to the answer.
            recovery = "get" if "get" in verbs else (verbs[0] if verbs else None)
            # A kind-specific redirect (e.g. how to "link" a draft) beats
            # the generic "try get(kind=…)" — it's the actual recovery the
            # agent needs, so lead with it.
            redirect = _VERB_REDIRECTS.get((kind, verb))
            if recovery is None and redirect is None:
                # Defensive: shouldn't happen — a kind with no
                # supported verbs would be useless. Drop the next:
                # trailer rather than render a meaningless one.
                raise Unsupported(
                    f"{kind} does not support {verb}",
                    options=verbs,
                    next=f"get(kind='skill', q={kind!r}) lists what {kind} does",
                )
            next_hints: list[str] = []
            if redirect is not None:
                next_hints.append(redirect)
            if recovery is not None:
                next_hints.append(f"try {recovery}(kind={kind!r})")
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=next_hints[0] if len(next_hints) == 1 else next_hints,
            )
        return handler

    def _dispatch_edit_id_list(
        self,
        handler: Handler,
        *,
        kind: str,
        kind_was_defaulted: bool,
        args: dict[str, Any],
    ) -> Response | None:
        """Route ``edit(id=[...])`` by the handler's ``edit_id_list``.

        ``'native'`` returns ``None`` so the caller falls through to
        :meth:`_invoke_handler` and the handler's own edit takes the
        list (the ``batch_ids`` numeric-ref kinds; paper's
        ``args={'clear_stamp': …}`` door). ``'repeat'`` replays the same
        edit once per id inside ``store.atomic()`` — the full per-id
        invocation path (reason=/trust/kwargs gates included), one
        transaction, the first error rolling every id back — for kinds
        whose edit is store-backed but scalar-id and cheap enough for
        one transaction. Anything else (``'refuse'``, or ``None`` for a
        kind that hasn't declared yet) refuses with a ``BadInput``
        naming the per-id call: file-backed kinds
        (plaintext/markdown/tex/python) can't honour the rollback claim,
        structure's per-design relax is too long for one transaction,
        and a list must never silently edit only the first id
        (gr514482).
        """
        if handler.edit_id_list == "native":
            return None
        ids: list[Any] = list(args["id"])
        first = next((i for i in ids if i is not None), "<id>")
        if handler.edit_id_list != "repeat":
            raise BadInput(
                f"edit(kind={kind!r}) takes one id= at a time, not a list",
                next=(f"call edit once per id: edit(kind={kind!r}, id={first!r}, ...)"),
            )
        if self.store is None:
            raise Unsupported(
                f"edit(kind={kind!r}, id=[...]) needs a store for its "
                "one-transaction batch",
                next=(f"call edit once per id: edit(kind={kind!r}, id={first!r}, ...)"),
            )
        from precis.handlers._numeric_ref import BATCH_ID_CAP

        if not ids:
            raise BadInput(
                "id=[] is empty",
                next=f"edit(kind={kind!r}, id=<id>, ...)",
            )
        if len(ids) > BATCH_ID_CAP:
            raise BadInput(
                f"id=[...] takes at most {BATCH_ID_CAP} ids, got {len(ids)}",
                next=f"split into calls of {BATCH_ID_CAP} or fewer",
            )

        # De-dupe, order kept — ``_coerce_ids`` does the same for the
        # native batch kinds. The key is the resolved pk wherever one
        # decodes (int, decimal string, ``kind:N``, universal handle —
        # ``handle_registry.parse`` folds the prefix's case, so 'dc5' ==
        # 'DC5' and 'fo5' == 5): two spellings of one ref must collapse,
        # or the second pass re-edits it — a find-replace that already
        # applied fails and rolls the whole batch back.
        def _dedupe_key(raw: Any) -> tuple[str, Any]:
            if isinstance(raw, bool):
                return ("s", raw)
            if isinstance(raw, int):
                return ("pk", raw)
            if isinstance(raw, str):
                s = raw.strip()
                parsed = handle_registry.parse(s)
                if parsed is not None:
                    return ("pk", parsed[2])
                head, _, tail = s.partition(":")
                if head and tail.isdigit():
                    return ("pk", int(tail))
                if s.isdigit():
                    return ("pk", int(s))
            return ("s", raw)

        uniq: list[Any] = []
        seen: set[tuple[str, Any]] = set()
        for raw in ids:
            key = _dedupe_key(raw)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(raw)
        lines: list[str] = []
        with self.store.atomic():
            for raw in uniq:
                label = (
                    handle_registry.try_format(kind, raw)
                    if isinstance(raw, int) and not isinstance(raw, bool)
                    else None
                ) or str(raw)
                per_args = copy.deepcopy(args)
                per_args["id"] = raw
                try:
                    resp = self._invoke_handler(
                        handler,
                        "edit",
                        kind=kind,
                        kind_was_defaulted=kind_was_defaulted,
                        args=per_args,
                    )
                except PrecisError as exc:
                    exc.cause = (
                        f"{label}: {exc.cause} — batch of {len(uniq)} "
                        "rolled back, nothing applied"
                    )
                    exc.args = (exc.cause,)
                    raise
                # Full per-id body, not just its first line: structure's
                # preflight caveats and a draft dry_run's diff live in
                # later lines (gripe:285774 — caveats are never dropped
                # silently).
                lines.append(f"{label}:\n{textwrap.indent(resp.body or 'done', '  ')}")
        head = f"edited {len(uniq)} {kind} in one transaction"
        return Response(body=head + "\n" + "\n".join(lines))

    def _invoke_handler(
        self,
        handler: Handler,
        verb: str,
        *,
        kind: str,
        kind_was_defaulted: bool,
        args: dict[str, Any],
    ) -> Response:
        """Call ``handler.<verb>`` with extras-whitelisted kwargs.

        ``args=`` extras forwarded by the MCP boundary are validated
        against the handler's signature *before* the call so
        ``**_kw`` doesn't swallow typos silently. The top-level kwargs in
        ``args`` get the same treatment via :func:`_handler_accepted_kwargs`
        (gr334695) — any kwarg that isn't an explicit parameter anywhere
        along the handler's cooperative-inheritance forwarding chain, and
        isn't opted out via ``@tolerates_extra_kwargs``, raises rather than
        landing in a dead ``**_kw``. Errors raised by the handler are
        annotated with ``(searched kind=…)`` when the caller omitted
        ``kind=`` and we defaulted, so failures stay traceable to the
        specific kind that was tried.
        """
        method = getattr(handler, verb)

        # The eye ladder is one door on every kind (fisheye-everywhere
        # in-scope 2): a ``get`` carrying ``extent=`` — or a ladder label
        # in ``view=``, the spelling the ladder shipped on — goes to
        # ``Handler.eye`` instead of ``get``, so no handler has to accept
        # the kwarg and the kwargs gate below never sees it.
        if verb == "get":
            extent = _eye_extent(args)
            if extent is not None:
                # ``ring=`` narrows the rings a link/doc eye renders
                # (``fisheye-everywhere.md`` in-scope 1); popped like
                # ``extent=`` so the kwargs gate never sees it.
                ring = args.pop("ring", None)
                extras = args.get(_EXTRAS_KEY)
                if ring is None and isinstance(extras, dict):
                    ring = extras.pop("ring", None)
                # gr524663: ``handler.eye`` takes only id/extent/q/ring —
                # any other payload or args= key (``kinds``, ``spec``,
                # ``tag``, ``project``, ``full``…) silently did nothing.
                # Refuse them the way find-in-ref refuses strays below.
                stray_eye = sorted(
                    k
                    for k, v in args.items()
                    if k not in ("id", "view", "q", _EXTRAS_KEY) and v is not None
                )
                if isinstance(extras, dict):
                    stray_eye += sorted(
                        f"args[{k!r}]" for k, v in extras.items() if v is not None
                    )
                if stray_eye:
                    raise BadInput(
                        f"get(kind={kind!r}): extent= (the fisheye ladder) "
                        f"doesn't combine with {stray_eye!r} — drop them, "
                        "or drop extent=",
                        next=f"get(kind={kind!r}, id=<ref>, extent={extent!r})",
                    )
                try:
                    response = handler.eye(
                        id=args.get("id"), extent=extent, q=args.get("q"), ring=ring
                    )
                    self._stamp_recalled(kind, args.get("id"))
                    self._bump_ref_read(kind, args.get("id"))
                    return response
                except PrecisError as exc:
                    if kind_was_defaulted:
                        exc.cause = f"(searched kind={kind!r}) {exc.cause}"
                    raise

        # gr486840 find-in-ref: ``get(kind=K, id=N, q='…')`` on a kind
        # that opts in (``Handler.supports_find_in_ref``) routes to
        # ``handler.find_in_ref`` here — before the extras/kwargs gates
        # and before any subclass ``view=`` interception — so ``q`` can
        # never be silently dropped the way it was when it rode into
        # ``get`` as a kwarg (the gripe's ``get(kind='memory', id, q=…)``
        # returning the whole body). Compute-style kinds (calc, the
        # cache-backed fetchers) don't opt in: their ``q`` still flows
        # to ``get`` unchanged. Kinds that neither opt in nor declare
        # ``q`` on ``get`` refuse it at the strictness gate below, so
        # every ``q`` either finds, computes, or errors — never vanishes.
        if (
            verb == "get"
            and args.get("q") is not None
            and getattr(handler, "supports_find_in_ref", False)
        ):
            # q + any other top-level kwarg (of_kind=, spec=, page=, …)
            # can't ride silently into a find that ignores them.
            stray = sorted(k for k in args if k not in ("id", "view", "q", _EXTRAS_KEY))
            if stray:
                raise BadInput(
                    f"get(kind={kind!r}): q= (find-in-ref) doesn't combine "
                    f"with {stray!r} — drop them, or drop q=",
                    next=(
                        f"get(kind={kind!r}, id=<ref>, "
                        f"q={str(args.get('q')).strip()!r})"
                    ),
                )
            extras = args.get(_EXTRAS_KEY)
            context = None
            if isinstance(extras, dict):
                unknown = sorted(set(extras) - {"context"})
                if unknown:
                    raise BadInput(
                        f"keys {unknown!r} not accepted by {kind} find-in-ref; "
                        "its args= key is 'context'",
                        next="args={'context': 5} widens the ±3-line window",
                    )
                context = extras.get("context")
            try:
                response = handler.find_in_ref(
                    id=args.get("id"),
                    q=args["q"],
                    view=args.get("view"),
                    **({"context": context} if context is not None else {}),
                )
            except PrecisError as exc:
                if kind_was_defaulted:
                    exc.cause = f"(searched kind={kind!r}) {exc.cause}"
                raise
            self._stamp_recalled(kind, args.get("id"))
            self._bump_ref_read(kind, args.get("id"))
            if kind_was_defaulted:
                response = self._tag_defaulted_kind(response, kind)
            return response

        # local-mesh-upkeep §2b: edit's reason= belongs to the revision
        # log, not the handler — consume it before the kwargs gate. put's
        # reason= is consumed the same way unless the handler's own put
        # declares it (paper/message put: an acquisition reason, not a
        # revision reason). delete/tag reason= is handler-declared
        # instead (gr503546 / gr504803: the numeric-ref handlers write
        # their own ref_events audit row), so neither verb pops here —
        # a kind whose handler doesn't declare it refuses reason= at the
        # kwargs gate below.
        reason = None
        if verb == "edit" or (
            verb == "put" and "reason" not in self._accepted_kwargs(method)
        ):
            reason = args.pop("reason", None)

        # graph-node-trust-state slice 1: the typed trust payload and the
        # trust-only edit are consumed before the handler's kwargs gate.
        trust_payload = (
            pop_trust_payload(args, _EXTRAS_KEY) if verb in TRUST_VERBS else None
        )
        if verb == "edit" and args.get("mode") == "trust":
            outer = current_revision_context()
            return edit_trust(
                self.store,
                kind=kind,
                ident=args.get("id"),
                payload=trust_payload,
                outer=outer,
                actor=(outer.actor if outer is not None else None)
                or current_background_actor()
                or process_actor()
                or "agent",
            )

        # The resolved-facets marker (``uncited=``/``under=`` origins
        # recorded for ``next=`` hint rendering) is dispatch-internal —
        # never let it reach a handler kwargs gate.
        args.pop(_RESOLVED_FACETS_KEY, None)
        extras = args.pop(_EXTRAS_KEY, None)
        if extras:
            accepted = self._accepted_kwargs(method)
            # Handlers that opt into an explicit ``args: dict``
            # parameter (today: ``random.get`` — slug minting takes
            # ``len`` / ``alphabet`` inside ``args=``) want the whole
            # extras dict passed through, NOT flattened into top-level
            # kwargs. Without this branch, ``get(kind='random',
            # view='slug', args={'len': 4})`` errored with
            # ``args= keys ['len'] not accepted by random.get`` and the
            # error breadcrumb confusingly suggested using ``args`` or
            # ``view`` as args-dict keys (round-2 picky F-1). Detect
            # the opt-in by signature membership and forward extras
            # via the ``args`` kwarg unchanged.
            if "args" in accepted:
                # gr475332: the passthrough used to skip key validation
                # entirely, so ``args={'bogus': 1}`` vanished. A handler
                # that declares ``ARGS_KEYS[verb]`` has every key it reads
                # out of ``args`` checked here; one with no declaration
                # stays lenient (a test pins that none in-repo do).
                declared = (getattr(type(handler), "ARGS_KEYS", None) or {}).get(verb)
                if declared is not None:
                    unknown = sorted(k for k in extras if k not in declared)
                    # gr484234: an args= key that actually names a real
                    # {kind}.{verb} kwarg is a misfiled top-level argument,
                    # not a bad args= key — promote it (an explicit
                    # top-level value wins the collision), the same lift
                    # mcp_slim applies to full-verb params one layer up.
                    # ``args`` itself is never promoted; what stays in
                    # ``unknown`` is genuinely unknown and rejected below.
                    promotable = [k for k in unknown if k in accepted and k != "args"]
                    if promotable:
                        promoted = _coerce_extras(
                            type(handler),
                            verb,
                            {k: extras.pop(k) for k in promotable},
                        )
                        for k, v in promoted.items():
                            if k not in args or args[k] is None:
                                args[k] = v
                        unknown = sorted(set(unknown) - set(promotable))
                    if unknown:
                        raise BadInput(
                            f"keys {unknown!r} not accepted by {kind}.{verb}; "
                            f"its args= keys are listed below",
                            options=_typed_kwargs(type(handler), verb, declared),
                        )
                # Keys that shadow a named parameter of the handler (python.get
                # folds them back in) get the same JSON-string coercion as the
                # flattened path.
                extras = _coerce_extras(type(handler), verb, extras)
                # Reads go through ``ArgsView``: a key the handler never
                # declared raises instead of reading back as None.
                args["args"] = (
                    ArgsView(extras, declared, f"{kind}.{verb} args=")
                    if declared is not None
                    else dict(extras)
                )
            else:
                unknown = self._unknown_extras(method, extras)
                if unknown:
                    # Core params are top-level on the slim schema; list
                    # only what belongs inside args=.
                    core = set(CORE_PARAMS.get(verb, ())) | {"args"}
                    accepted_kwargs = _typed_kwargs(
                        type(handler), verb, {k for k in accepted if k not in core}
                    )
                    raise BadInput(
                        f"keys {unknown!r} not accepted by {kind}.{verb}; "
                        f"its args= keys are listed below",
                        options=accepted_kwargs,
                        # gr478557: gripe/todo comment via id= + text=, not args=.
                        next=(
                            f"to comment: put(kind={kind!r}, id=N, text='...') "
                            "with no args"
                            if verb == "put"
                            and getattr(handler, "supports_comment", False)
                            else None
                        ),
                    )
                args.update(_coerce_extras(type(handler), verb, extras))

        self._apply_default_tags_policy(handler, verb, args)

        # Strip None args so handlers see absence as missing.
        clean = {k: v for k, v in args.items() if v is not None}

        # ``text=``/``body=`` near-miss (gr484230): one alias rewrite for
        # every kind, before the strictness gate below would refuse the
        # other spelling outright.
        _alias_prose_field(type(handler), verb, kind, clean)

        self._validate_mode(handler, verb, kind, clean)

        # gr334695: a caller kwarg that isn't one of this handler's own
        # explicit params, nor forwarded anywhere via its own **kwargs
        # catch-all (walking the cooperative-inheritance chain — see
        # :func:`_handler_accepted_kwargs`), would otherwise vanish into
        # a dead ``**_kw`` with no error and no effect — the exact shape
        # of gr333433 (todo put silently swallowed executor=/job_type=/
        # params=) and gr334153 (draft edit silently swallowed mode=/
        # where=, a data-loss bug). A verb method that deliberately wants
        # to ignore extras (a read-only stub that rejects the call
        # regardless of kwargs, or a kind that must stay lenient by
        # design) opts out with ``@tolerates_extra_kwargs``.
        if not getattr(method, TOLERATES_EXTRA_KWARGS_ATTR, False):
            accepted_top = _handler_accepted_kwargs(type(handler), verb)
            unknown_top = sorted(
                k
                for k in clean
                if k not in accepted_top
                and k not in _WRAPPER_DEFAULTED_KWARGS.get(verb, ())
            )
            if unknown_top:
                # Core params are top-level on the slim schema; the
                # options list names only what goes inside args=.
                core = set(CORE_PARAMS.get(verb, ())) | {"args"}
                accepted_list = _typed_kwargs(
                    type(handler), verb, {k for k in accepted_top if k not in core}
                )
                hint = (
                    f" — {kind}: the title is the first line of text="
                    if "title" in unknown_top and "text" in accepted_top
                    else ""
                )
                dropped = [k for k in unknown_top if k in _DROPPABLE_TOP_LEVEL_KWARGS]
                raise BadInput(
                    f"{verb}(kind={kind!r}) does not accept {unknown_top!r}"
                    f"{hint}; its args= keys are listed below",
                    options=accepted_list,
                    next=(
                        f"{dropped} has no effect on {verb}(kind={kind!r}) — "
                        "drop it (paging/mode belong to search and the verbs "
                        "that list them)"
                        if dropped
                        else None
                    ),
                )

        # F7: catch handler-signature-required kwargs that the caller
        # forgot, before ``method(**clean)`` raises a raw TypeError and
        # leaks Python signature internals through the [error:Internal]
        # envelope. Per-handler BadInput paths (e.g. NumericRefHandler.
        # link's "requires target=" check) still fire for *semantic*
        # requirements like "id must be paired with target"; this gate
        # only catches truly-missing keyword-only args with no default.
        missing = self._missing_required_kwargs(method, clean)
        if missing:
            accepted_kwargs = sorted(
                k for k in self._accepted_kwargs(method) if k != "args"
            )
            missing_str = ", ".join(f"{m}=" for m in missing)
            raise BadInput(
                f"{verb}(kind={kind!r}) requires {missing_str}",
                options=accepted_kwargs,
                next=f"get(kind='skill', id='precis-{verb}-help')",
            )

        try:
            with _write_context(
                verb,
                kind,
                reason,
                trust=producer_kwargs(
                    verb,
                    kind,
                    type(handler).trust_origin,
                    trust_payload,
                    current_revision_context(),
                )
                if verb in _WRITE_VERBS
                else None,
            ) as wctx:
                response = method(**clean)
            act = getattr(wctx, "trust_act", None)
            if act is not None and act.target_id is None:
                response = replace(
                    response,
                    body=f"{response.body}\n{create_receipt(self.store, act)}",
                )
        except PrecisError as exc:
            if kind_was_defaulted:
                exc.cause = f"(searched kind={kind!r}) {exc.cause}"
            raise
        except Exception as exc:
            # A trust guard's refusal (migration 0214) is the caller's
            # input, not an internal error.
            if isinstance(exc, psycopg.errors.CheckViolation):
                refusal = trust_refusal(exc)
                if refusal is not None:
                    raise refusal from exc
            # Non-Precis exceptions get wrapped as ``Internal`` at
            # the dispatcher boundary; do the wrap here when the
            # kind was defaulted so the annotation lands on the
            # final rendered error rather than being lost in the
            # generic ``internal error: ...`` shape.
            if kind_was_defaulted:
                mapped = map_boundary_exception(exc)
                if mapped is not None:
                    mapped.cause = f"(searched kind={kind!r}) {mapped.cause}"
                    raise mapped from exc
                log.exception("internal error in defaulted-kind search")
                raise Internal(
                    f"(searched kind={kind!r}) internal error: "
                    f"{type(exc).__name__} (see server log)",
                    next="pass kind=<one kind> to search one kind; "
                    "file a gripe naming this call",
                ) from exc
            raise

        if verb == "get":
            # gr524009: a read counts on every focused get — any view,
            # not only the bare one ``_stamp_recalled`` covers.
            self._bump_ref_read(kind, args.get("id"))
            if args.get("view") is None:
                self._stamp_recalled(kind, args.get("id"))
                response = self._append_trust_line(
                    handler, kind, args.get("id"), response
                )
        if kind_was_defaulted:
            response = self._tag_defaulted_kind(response, kind)
        if verb == "get" and self._is_id_empty(args.get("id")):
            response = self._maybe_append_kind_skill_footer(response, kind)
        return response

    def _append_trust_line(
        self, handler: Handler, kind: str, ident: Any, response: Response
    ) -> Response:
        """Close a single-ref ``get`` of a converted kind with its trust
        stamp and content sha (graph-node-trust-state slice 1): the sha is
        what ``edit(mode='trust')`` names as ``expected_sha``. Best-effort:
        a failed lookup never fails the read."""
        if (
            type(handler).trust_origin is None
            or ident is None
            or isinstance(ident, (list, tuple))
            or (isinstance(ident, str) and ident.strip().startswith("/"))
        ):
            return response
        store = self.store
        if store is None:
            return response
        try:
            line = trust_line(store, kind, ident)
        except Exception:
            log.debug("trust line for %s %r failed", kind, ident, exc_info=True)
            return response
        if line is None:
            return response
        # gr499041: the identical stamp+sha footer on a re-read carries no
        # new information; suppress it for a session that saw it recently.
        from precis.utils.hint_ledger import was_shown

        if was_shown(line):
            return response
        return replace(response, body=f"{response.body.rstrip()}\n\n{line}")

    def _stamp_recalled(self, kind: str, ident: Any) -> None:
        """Record an agent read of one ref's own view (``refs.last_recalled_at``).

        Only a single concrete id of a ref-backed kind counts (``Handler.
        stamps_recall``) — not a batch, a ``/path`` list view, a search hit or
        a neighbour line. An in-process TTL cache absorbs repeat reads, and the
        stamp itself is ONE throttled UPDATE (no id lookup). Best-effort: a
        failed stamp never fails the read."""
        try:
            if ident is None or isinstance(ident, (list, tuple)):
                return
            if isinstance(ident, str) and ident.strip().startswith("/"):
                return
            store = self.store
            handler = self.hub.handler_for(kind)
            if store is None or handler is None or not handler.stamps_recall:
                return
            key = (kind, str(ident).strip())
            cache = self.__dict__.setdefault("_recall_seen", OrderedDict())
            now = time.monotonic()
            seen = cache.get(key)
            if seen is not None and now - seen < _RECALL_TTL_S:
                cache.move_to_end(key)
                return
            store.touch_recalled_for(kind, key[1])
            cache[key] = now
            cache.move_to_end(key)
            while len(cache) > _RECALL_CACHE_MAX:
                cache.popitem(last=False)
        except Exception:
            log.debug("recall stamp skipped for %s %r", kind, ident, exc_info=True)

    def _bump_ref_read(self, kind: str, ident: Any) -> None:
        """Count one read of the focus ref in ``ref_reads`` (gr524009).

        Fires once per ``get`` that names a single concrete id — any
        view, any fisheye rung, a chunk or range get inside the ref —
        after the response is built, so the bump never breaks a read and
        a failed render never counts. Neighbour rows, 2hop counts,
        search hits and worker passes never reach here (the bump lives
        at the verb entry, never in a Store fetch helper). ``actor`` is
        ``human`` only when the call runs inside a browser write context
        (``human_write_context`` sets ``trust_origin='human'`` with the
        ``user:<abbrev>`` actor — the /eye route); every other caller is
        ``agent``, debounced on the acting user, the MCP session id or
        the declared client, in that order."""
        try:
            if ident is None or isinstance(ident, (list, tuple)):
                return
            if isinstance(ident, str) and ident.strip().startswith("/"):
                return
            store = self.store
            handler = self.hub.handler_for(kind)
            if store is None or handler is None or not handler.stamps_recall:
                return
            outer = current_revision_context()
            if outer is not None and outer.trust_origin == "human" and outer.actor:
                store.bump_ref_read_for(
                    kind, str(ident).strip(), actor="human", reader=outer.actor
                )
                return
            from precis.tool_ledger import _session_id, current_client

            reader = (
                (outer.actor if outer is not None else None)
                or current_background_actor()
                or process_actor()
                or _session_id()
                or current_client()
                or "agent"
            )
            store.bump_ref_read_for(
                kind, str(ident).strip(), actor="agent", reader=reader
            )
        except Exception:
            log.debug("ref_read bump skipped for %s %r", kind, ident, exc_info=True)

    @staticmethod
    def _validate_mode(
        handler: Handler, verb: str, kind: str, clean: dict[str, Any]
    ) -> None:
        """Validate a supplied ``mode=`` against ``KindSpec.modes``/
        ``edit_modes`` before the handler ever sees it (gr343755).

        Only ``put``/``edit`` carry a ``mode=`` vocabulary; other verbs
        are untouched. A no-op when ``mode=`` wasn't supplied (``clean``
        already has ``None`` values stripped, so "supplied" here means
        "the caller passed a non-``None`` value").

        Reads the three-way sentinel on the relevant ``KindSpec`` field
        (``modes`` for ``put``, ``edit_modes`` for ``edit``):

        - ``None`` (no ``mode=`` concept declared): leaves the current
          behaviour untouched — a handler that parses ``mode=`` itself
          (or ignores it) keeps doing so; this gate doesn't second-guess
          a kind that hasn't opted into the declared vocabulary.
        - ``()`` (declared but rejects every value): raises, naming the
          kind — the same "not accepted" shape as an unrecognised
          top-level kwarg.
        - non-empty tuple: membership check; raises listing the allowed
          values when ``mode`` isn't one of them.

        ``edit`` additionally exempts
        :data:`_EDIT_MODE_WRAPPER_DEFAULT` — see its docstring for why
        that specific value can never be trusted as caller intent.
        """
        if verb not in ("put", "edit"):
            return
        mode = clean.get("mode")
        if mode is None:
            return
        if verb == "edit" and mode == _EDIT_MODE_WRAPPER_DEFAULT:
            return
        allowed = handler.spec.edit_modes if verb == "edit" else handler.spec.modes
        if allowed is None:
            return
        if mode in allowed:
            return
        if allowed:
            options_str = " | ".join(repr(m) for m in allowed)
            raise BadInput(
                f"{verb}(kind={kind!r}) only supports mode={options_str}, got {mode!r}",
                options=list(allowed),
                next=f"{verb}(kind={kind!r}, ..., mode={allowed[0]!r})",
            )
        raise BadInput(
            f"mode= is not accepted on {verb} for kind={kind!r}, got {mode!r}",
            next=f"omit mode= — {verb}(kind={kind!r}, ...) doesn't take one",
        )

    @staticmethod
    def _is_id_empty(id_val: Any) -> bool:
        """Whether a ``get()`` call's ``id=`` is effectively absent —
        the shape every kind's handler treats as "render the kind's own
        list/help landing surface" (``se``'s ``_render_list``, ``paper``'s
        ``_render_list_papers``, etc.), rather than reaching for a
        specific ref."""
        return id_val is None or (
            isinstance(id_val, str) and id_val.strip() in ("", "/")
        )

    def _maybe_append_kind_skill_footer(
        self, response: Response, kind: str
    ) -> Response:
        """Append the kind's skill-graph breadcrumb to a no-``id=``
        ``get()`` read (docs/backlog/skill-graph.md slice 4 — the kind's
        help/landing surface, whatever shape a specific handler gives it).

        Best-effort: :func:`~precis.skill_index.kind_skills.kind_skill_hint`
        already degrades to ``None`` on its own build failure, but the
        import + call is wrapped again here so a future refactor of that
        module can't turn "no hint" into "no response" for every kind's
        landing read.
        """
        try:
            from precis.skill_index.kind_skills import kind_skill_hint

            hint = kind_skill_hint(kind)
        except Exception:
            hint = None
        if hint is None:
            return response
        from dataclasses import replace as _replace

        return _replace(response, body=f"{response.body}\n\n{hint}")

    def _apply_default_tags_policy(
        self,
        handler: Handler,
        verb: str,
        args: dict[str, Any],
    ) -> None:
        """Apply ``PRECIS_DEFAULT_TAGS`` policy at the dispatch boundary.

        No-op if ``defaults`` is empty (env unset) or
        ``handler.spec.note_like`` is False (ingested kinds, fetched
        caches, generators don't accumulate session-context tags).
        Verb ``put`` on a note-like kind: merges defaults into
        ``args['tags']`` (caller's explicit-first ordering preserved, no
        dupes) and emits an info hint. Verb ``tag``: emits a suggestion
        hint for defaults missing from ``args.get('add')`` — **not**
        mutated, since ``tag`` is the agent's explicit op and silent
        mutation would surprise both agent and operator. Any other verb
        (get/search/edit/delete/link): no-op.

        Mutates ``args`` in place (``put`` only); returns ``None``.
        """
        defaults = self.default_tags_resolved
        if not defaults:
            return
        spec = handler.spec
        if not getattr(spec, "note_like", False):
            return

        from precis import default_tags as _dt
        from precis.hints import Hint

        if verb == "put":
            added = _dt.apply_to_put_args(args, defaults)
            if added:
                self.hub.emit_hint(
                    Hint(
                        text=("Added PRECIS_DEFAULT_TAGS to put: " + ", ".join(added)),
                        topic="default_tags.merged",
                    )
                )
        elif verb == "tag":
            missing = _dt.suggest_missing(args.get("add"), defaults)
            if missing:
                self.hub.emit_hint(
                    Hint(
                        text=(
                            "PRECIS_DEFAULT_TAGS suggested for tag add: "
                            + ", ".join(missing)
                        ),
                        topic="default_tags.suggested",
                    )
                )

    @staticmethod
    def _accepted_kwargs(method: Any) -> set[str]:
        """Return the set of explicit keyword names accepted by ``method``.

        ``self`` and any VAR_KEYWORD (``**kw``) catch-all are excluded
        — the catch-all is what we're working around. Used for the
        args= validation gate so ``**_kw`` no longer swallows typos.
        """
        sig = inspect.signature(method)
        return {
            name
            for name, p in sig.parameters.items()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name != "self"
        }

    @classmethod
    def _unknown_extras(cls, method: Any, extras: dict[str, Any]) -> list[str]:
        """Return the args= keys that aren't on the handler's signature."""
        accepted = cls._accepted_kwargs(method)
        return sorted(k for k in extras if k not in accepted)

    @staticmethod
    def _missing_required_kwargs(method: Any, clean: dict[str, Any]) -> list[str]:
        """Return required kwargs of ``method`` missing from ``clean``.

        A parameter is required when it has no default value AND is
        keyword-accessible (``POSITIONAL_OR_KEYWORD`` or ``KEYWORD_ONLY``).
        ``self`` and the magic ``args`` extras-passthrough parameter
        are excluded; ``**kw`` catch-alls don't count as required.

        Used by :meth:`_invoke_handler` to convert what would have been
        a raw ``TypeError: ... missing 1 required keyword-only
        argument: 'id'`` into a clean ``BadInput`` envelope (F7).
        """
        sig = inspect.signature(method)
        missing: list[str] = []
        for name, p in sig.parameters.items():
            if name in ("self", "args"):
                continue
            if p.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            if name not in clean:
                missing.append(name)
        return missing

    def _render_failure(self, err: PrecisError) -> str:
        """Keep the error envelope first, then any required resolution receipts."""
        body = self.render_error(err)
        return body + "".join(
            f"\n[{h.level}] {h.text}" for h in self.hints.collect(required_only=True)
        )

    def _render(self, response: Response) -> str:
        out = [response.body]
        hints = self.hints.collect()
        for h in hints:
            out.append(f"\n[{h.level}] {h.text}")
        if response.cost:
            # Handlers return a fully-formatted cost string like
            # ``[cost: ~$0.0020 — cached]``. Don't prepend "— cost:" —
            # that produced the double-"cost:" trailer flagged by the
            # MCP critic ("— cost: [cost: ~$0.0020]").
            out.append(f"\n{response.cost}")
        return "".join(out)

    def _default_search_kind(self, search_kinds: list[str]) -> str | None:
        """Pick a sensible default kind for ``search()`` calls without one.

        Strategy:
          1. If the store has any live ref in a search-supporting
             kind, use the kind of the most recently updated one —
             this biases the default toward "what was the agent just
             working on?".
          2. Otherwise (no store, empty store), return None and let
             the caller raise the canonical missing-kind error.

        Returning None signals "I don't have a defensible default" so
        the dispatcher falls through to the explicit BadInput.
        """
        if self.store is None or not search_kinds:
            return None
        try:
            return self.store.most_recent_kind(kinds=search_kinds)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("most_recent_kind lookup failed")
            return None

    @staticmethod
    def _tag_defaulted_kind(response: Response, kind: str) -> Response:
        """Prepend a ``(searched kind=...)`` annotation to a response.

        Surfaced when the caller omitted ``kind=`` and the runtime
        defaulted to the most recently touched search-supporting
        kind. Naming it explicitly lets the caller see and steer
        the choice on retry.
        """
        from dataclasses import replace

        annotated = f"(searched kind={kind!r})\n{response.body}"
        return replace(response, body=annotated)

    def _kinds_for_verb(self, verb: str) -> list[str]:
        """Return the active kinds whose KindSpec supports ``verb``.

        Used by error paths so an "unknown kind" reply on a search
        request lists only kinds that *do* support search — agents
        that retry against the suggested options shouldn't cascade
        into a second error. (MCP critic MAJOR #12.)
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            if handler.spec.supports(verb):
                out.append(k)
        return out

    def _infer_sigil_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a leading address sigil (``¶`` → draft,
        ``§`` → paper). ``¶`` is kept in ``id`` (the draft handler matches
        on it); ``§`` is stripped to the bare ``slug~n`` paper resolves.

        Leaves ``id`` alone when the implied kind isn't in this build
        (fail downstream with a real "no such kind", not a silent
        mis-route). Raises on an explicit ``kind=`` that contradicts the
        sigil, mirroring the colon-prefix conflict check.
        """
        kind, keep_sigil = _SIGIL_KIND[ident[:1]]
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind not in live_kinds:
            return
        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != kind:
            raise BadInput(
                f"id={ident!r} sigil implies kind={kind!r}, "
                f"conflicts with kind={existing_kind!r}",
                next=f"drop kind= — id={ident!r} already names the kind",
            )
        args["kind"] = kind
        if not keep_sigil:
            args["id"] = ident[1:]

    def _infer_slug_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a bare slug address (no ``kind:`` prefix, no
        sigil) when the slug uniquely identifies one live ref — e.g.
        ``wu22c~312`` → ``kind='paper'``. The ``~selector`` / ``/view``
        suffix stays on ``id`` for the handler to parse. No-op when the
        store is absent, ``kind=`` was already given, or the base slug is
        ambiguous / unknown (so a non-slug id falls through to the normal
        missing-kind error unchanged)."""
        if args.get("kind") is not None or self.store is None:
            return
        base = re.split(r"[~/?]", ident, maxsplit=1)[0].strip()
        if not base:
            return
        try:
            kind = self.store.kind_for_slug(base)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("kind_for_slug lookup failed")
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind is not None and kind in live_kinds:
            args["kind"] = kind

    def _maybe_route_draft_chunk(self, args: dict[str, Any], ident: str) -> bool:
        """Universal handles: route a draft chunk handle ``dc<id>`` (optionally with a
        relative operator ``^`` / ``+N`` / ``-lo..hi``) to the draft handler,
        which resolves it (drafts have no slug, so they can't go through the
        generic ``slug~ord`` chunk-handle rewrite). Confirms the base chunk
        exists so a bogus ``dc999`` falls through to a clean not-found.
        Returns ``True`` if it routed."""
        if self.store is None:
            return False
        m = _DRAFT_DC_RE.match(ident)
        if m is None:
            return False
        # A trailing operator must be a valid relative handle, else this is
        # not a draft address (``dc42garbage`` falls through). A ``~<sel>``
        # table selector (stable row/column ids — spreadsheet-tables 1a)
        # is a valid suffix too; the draft handler resolves it.
        suffix = m.group(2)
        if suffix:
            if suffix.startswith("~"):
                if not re.fullmatch(handle_registry.TABLE_SEL_PATTERN, suffix[1:]):
                    return False
            elif handle_registry.parse_relative(ident) is None:
                return False
        explicit = args.get("kind")
        if explicit is not None and explicit != "draft":
            return False
        try:
            if self.store.drafts.get_draft_chunk("dc" + m.group(1)) is None:
                return False
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("draft chunk routing lookup failed")
            return False
        args["kind"] = "draft"
        args["id"] = ident
        return True

    def _maybe_route_plan_chunk(self, args: dict[str, Any], ident: str) -> bool:
        """Route a plan chunk handle ``pe<id>`` (optionally with a relative
        operator) to the plan handler, which parses the handle itself —
        same idea as ``_maybe_route_draft_chunk``: a plan node's handle IS
        its address, so the generic ``slug~ord`` rewrite must not see it
        (gr495745)."""
        if self.store is None:
            return False
        m = _PLAN_PE_RE.match(ident)
        if m is None:
            return False
        if m.group(2) and handle_registry.parse_relative(ident) is None:
            return False
        explicit = args.get("kind")
        if explicit is not None and explicit != "plan":
            return False
        try:
            if (
                self.store.drafts.get_draft_chunk(f"pe{m.group(1)}", kind="plan")
                is None
            ):
                return False
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("plan chunk routing lookup failed")
            return False
        args["kind"] = "plan"
        args["id"] = ident
        return True

    def _maybe_infer_kind_from_relative(self, args: dict[str, Any], ident: str) -> bool:
        """Relative-handle navigation: route ``pc10+1`` / ``pc10-2..3``.

        Resolves the relative chunk handle to its kind + the per-kind chunk
        selector (e.g. ``slug~ord`` for a paper) and rewrites ``args`` so the
        existing per-kind ``get`` renders the target with no change. Returns
        ``True`` if it routed a relative handle, ``False`` otherwise (not a
        relative handle, unresolvable, out of range, or an explicit ``kind=``
        that disagrees) so the caller falls through untouched.
        """
        if self.store is None:
            return False
        try:
            resolved = self.store.chunks.resolve_relative(ident)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("resolve_relative lookup failed")
            return False
        if resolved is None:
            return False
        kind, selector = resolved
        explicit = args.get("kind")
        if explicit is not None and explicit != kind:
            return False
        args["kind"] = kind
        args["id"] = selector
        return True

    def _kind_supports_chunk_selectors(
        self, kind: str, public_id: str, ref_id: int
    ) -> bool:
        """Whether ``kind``'s ``get`` understands the ``slug~ord`` chunk
        selector this dispatcher synthesizes for a resolved chunk handle.

        Reads the explicit :attr:`KindSpec.supports_chunk_selectors` opt-in
        when the handler has declared it; falls back to the historical
        public_id-vs-ref_id numeric proxy (True for slug-addressed kinds,
        False for numeric-id kinds) when it hasn't. The fallback preserves
        existing routing for every hand-rolled slug-document handler
        (paper/patent/edgar/markdown/plaintext/tex/…) without requiring each
        one to declare the flag — but it's no longer the *only* signal: a
        kind that IS slug-addressed but whose ``get`` has zero ``~ord``
        grammar (gr311336: news, via ``CacheBackedHandler``) can now declare
        ``supports_chunk_selectors=False`` explicitly to opt out, and a
        kind that implements the grammar (news, once fixed) can declare
        ``True`` regardless of its id shape.
        """
        handler = self.hub.handler_for(kind) if self.hub is not None else None
        declared = (
            getattr(handler.spec, "supports_chunk_selectors", None) if handler else None
        )
        if declared is not None:
            return declared
        return public_id != str(ref_id)

    def _maybe_infer_kind_from_handle(
        self, args: dict[str, Any], ident: str, *, verb: str | None = None
    ) -> bool:
        """Universal-handle surface dispatch: route a universal handle.

        If ``ident`` is a well-formed, resolvable record handle, sets
        ``args['kind']`` from its 2-char type code and rewrites
        ``args['id']`` to the per-kind public id (slug or ``str(ref_id)``)
        so the existing handler resolves it unchanged. A trailing
        chunk/view selector (``pa123~0..5``, ``pa123/toc``) is reattached
        to the public id, parsed as on a slug.

        A soft-deleted/never-existed handle has nothing for
        ``resolve_handle`` (a live-row lookup) to return, but still
        *parses* — the syntactic fallback below routes those to the
        per-kind handler instead of bare-slug inference (gr192827: a
        soft-deleted gripe's own handle produced a misleading "id must be
        an integer" instead of ``Gone``).

        Returns ``True`` if routed, ``False`` otherwise (non-handle,
        unknown/chunk handle, or a disagreeing explicit ``kind=``), so
        the caller falls through to bare-slug inference untouched. One
        exception to a disagreeing ``kind=``: a convert redirect — the
        skills teach ``get(kind='paper', id='pa<id>')``, and that call
        keeps working after the ref became a datasheet (gr515477).
        """
        if self.store is None:
            return False
        # Split off a trailing ``~selector`` / ``/view`` so the base handle
        # parses; the suffix is reattached to the resolved public id below.
        mm = re.match(r"^([a-zA-Z]{2}\d+)([~/].*)$", ident.strip())
        base, suffix = (mm.group(1), mm.group(2)) if mm else (ident, "")
        normalized = handle_registry.normalize(base)
        if not handle_registry.is_well_formed(normalized):
            return False
        resolved = self.store.resolve_handle(normalized)
        if resolved is None:
            return self._maybe_route_unresolved_record_handle(args, normalized, suffix)
        if verb == "delete":
            # A merge can commit after preflight. Never rewrite a delete from
            # this exact resolution without checking it as well.
            self._check_delete_redirect(normalized, resolved)
        explicit = args.get("kind")
        if explicit is not None and explicit != resolved.kind:
            # A converted ref resolves under its NEW kind
            # (resolve_handle's converted_from_kind redirect, which
            # already emitted the nudge hint); when the caller's
            # explicit kind still names the handle's decoded —
            # pre-convert — kind, route it through instead of
            # NotFound-ing on the old handler. A merge redirect can't
            # reach this branch (merge keeps kind, so a disagreeing
            # explicit is a real mismatch there).
            parsed = handle_registry.parse(normalized)
            if (
                resolved.redirected_from is None
                or parsed is None
                or explicit != parsed[0]
            ):
                return False
        if resolved.chunk_id is not None:
            # Chunk handle → per-kind chunk selector. Slug-document kinds take
            # ``slug~ord`` (ChunkRow.ord == chunks.ord). Numeric-chunk kinds
            # (gripe/message/…) have no ``~ord`` selector yet, so fall through
            # (a chunk handle has no slug match → natural NotFound). A chunk
            # handle takes no further selector.
            if (
                suffix
                or resolved.chunk_ord is None
                or not self._kind_supports_chunk_selectors(
                    resolved.kind, resolved.public_id, resolved.ref_id
                )
            ):
                # A kind that explicitly opts out of chunk selectors wants
                # the raw handle at its own handler — pe<N> IS a plan
                # node's address (gr495745), so the slug~ord rewrite would
                # only produce a bogus slug.
                handler = self.hub.handler_for(resolved.kind) if self.hub else None
                if (
                    handler is not None
                    and getattr(handler.spec, "supports_chunk_selectors", None) is False
                ):
                    args["kind"] = resolved.kind
                    args["id"] = ident
                    return True
                return False
            args["kind"] = resolved.kind
            args["id"] = f"{resolved.public_id}~{resolved.chunk_ord}"
            return True
        # resolve_handle already emitted the merge-redirect hint (via the
        # store's wired hint bus) if it followed a supersede; nothing to do here.
        door_extras = args.get(_EXTRAS_KEY)
        ref_door = isinstance(door_extras, dict) and (
            "redirect_to" in door_extras
            or "retire" in door_extras
            or "merge_into" in door_extras
        )
        if resolved.redirected_from is not None and ref_door:
            # The paper redirect_to, retire and merge_into doors operate
            # ON the merged tombstone: rewriting id= to the survivor (the
            # transparent redirect below) would land the re-point /
            # un-retire / merge on the wrong ref — a merge_into aimed at
            # a tombstone would fold the *survivor* instead of refusing
            # "already merged". Route the decoded tombstone ref_id
            # through instead so the handler can resolve it with
            # include_deleted (gr501446, gr509441, gr509659).
            parsed = handle_registry.parse(normalized)
            # kind is the tombstone's own — decoded from the handle, not
            # resolved.kind: the survivor may have been converted to a
            # different kind, and the door still edits the dead ref.
            args["kind"] = parsed[0] if parsed else resolved.kind
            args["id"] = (str(parsed[2]) if parsed else normalized) + suffix
            return True
        args["kind"] = resolved.kind
        if ref_door:
            # The paper retire / redirect_to / merge_into doors resolve
            # the ref themselves (``_resolve_ref_incl_retired``), so route
            # the decoded ref_id: a ``pa<id>`` handle must resolve by ref
            # id, never re-resolve through the cite_key — which can spell
            # like another kind's handle (``dr00``, gr524004).
            args["id"] = str(resolved.ref_id) + suffix
            return True
        args["id"] = resolved.public_id + suffix
        return True

    def _maybe_route_unresolved_record_handle(
        self, args: dict[str, Any], normalized: str, suffix: str
    ) -> bool:
        """Syntactic fallback for a well-formed record handle whose row
        ``resolve_handle`` can't find (soft-deleted or never existed).

        ``resolve_handle`` is a DB lookup, so it returns ``None`` for both
        a soft-deleted ref and a never-existed ref_id. Without this
        fallback both fell through to bare-slug inference, landing on
        ``_coerce_id``'s "id must be an integer" instead of the accurate
        ``Gone``/``NotFound`` the per-kind handler would raise given the
        bare integer id (gr192827).

        :func:`handle_registry.parse` needs no DB row — decodes the
        2-char code + decimal body from the string alone — so it still
        yields ``(kind, is_chunk, pk)``. Routed only for non-chunk
        handles (a chunk has no per-kind selector to synthesize without
        the row). Kinds where ``KindSpec.is_numeric`` (memory, todo,
        gripe, …) have a public id that IS ``str(ref_id)``, so
        ``str(pk)`` works as ``id=`` the same way a live handle's
        ``public_id`` would — routed through to the per-kind handler so
        it can still tell a soft-deleted row (``Gone``) from a
        never-existed one (``NotFound``). Slug-addressed kinds (e.g.
        paper) have no live row to read a slug from, so there's nothing
        to hand the per-kind handler — raise the honest ``NotFound``
        directly instead of falling through to bare-slug inference
        (which also finds nothing) and on to a "missing kind=" ``
        BadInput`` a caller can't act on by adding ``kind=`` (gr311329:
        a nonexistent ``pa5`` misdirected the caller to spell out a
        kind= that was never the problem). Slug kinds have no
        Gone-detection today regardless of routing (``get_ref`` already
        filters out soft-deleted rows before any handler sees them), so
        raising here loses nothing.
        """
        parsed = handle_registry.parse(normalized)
        if parsed is None:
            return False
        kind, is_chunk, pk = parsed
        explicit = args.get("kind")
        if explicit is not None and explicit != kind:
            return False
        if is_chunk:
            # gr534656: a ``pc<N>``-style handle that resolve_handle
            # missed fell through to the slug path, producing "paper
            # slug 'pc617977' not found" — misleading, since the input
            # was a *chunk* handle, not a slug. Name the three real
            # causes (never existed / another kind's chunk space / a
            # soft-deleted owner) off one indexed chunks lookup.
            raise self._unresolved_chunk_handle_error(normalized + suffix, kind, pk)
        handler = self.hub.handler_for(kind) if self.hub is not None else None
        if handler is None:
            return False
        extras = args.get(_EXTRAS_KEY)
        if isinstance(extras, dict) and (
            "retire" in extras or "merge_into" in extras or "redirect_to" in extras
        ):
            # The paper retire, merge_into and redirect_to doors resolve
            # tombstones themselves (_resolve_ref_incl_retired), so a
            # soft-deleted ``pa<id>`` still routes: without this,
            # ``args={'retire': False}`` — the undo —,
            # ``args={'merge_into': …}`` on a retired loser and
            # ``args={'redirect_to': …}`` on a half-tombstone (retired
            # with no superseded_by, gr524021) died on the NotFound below
            # before the handler could see them (gr509441, gr509659).
            args["kind"] = kind
            args["id"] = str(pk) + suffix
            return True
        if not handler.spec.is_numeric:
            raise self._unresolved_record_handle_error(kind, normalized + suffix, pk)
        args["kind"] = kind
        args["id"] = str(pk) + suffix
        return True

    def _unresolved_record_handle_error(
        self, kind: str, handle: str, pk: int
    ) -> PrecisError:
        """The error for a record handle ``resolve_handle`` missed.

        gr534656: the decimal body is one global sequence across kinds,
        so ``pa513234`` can name a live memory — the recovery then is
        the other kind's handle, not a blind search. One indexed
        ``refs`` lookup (the gripe's "keep it cheap" budget) tells the
        three cases apart: another kind's id space (name it + the real
        handle), a soft-deleted ref of the *same* kind (``Gone`` — slug
        kinds had no Gone-detection before this lookup), or a
        never-existed id (the plain search hint).
        """
        other = None
        if self.store is not None:
            try:
                other = self.store.fetch_refs_by_ids([pk], include_deleted=True).get(pk)
            except Exception:  # pragma: no cover — advisory lookup
                log.exception("unresolved-handle id-space lookup failed")
        if other is not None and other.kind != kind:
            real = handle_registry.try_format(other.kind, pk)
            state = "live" if other.retired_at is None else "deleted"
            return NotFound(
                f"{kind} {handle!r} not found — ref {pk} is a {state} "
                f"{other.kind!r} ref, not a {kind!r}"
                + (f" (its handle is {real})" if real else ""),
                next=(
                    f"get(id={real!r}) for the {other.kind} that id "
                    "actually names, or "
                    f"search(kind={kind!r}, q='...') to find a {kind}"
                ),
            )
        if other is not None and other.retired_at is not None:
            return Gone(
                f"{kind} {handle!r} was soft-deleted",
                next=f"search(kind={kind!r}, q='...') to find existing",
            )
        from precis.utils.slug_ref import nearest_handles

        return NotFound(
            f"{kind} {handle!r} not found",
            next=f"search(kind={kind!r}, q='...') to find existing",
            options=(
                nearest_handles(self.store, kind, pk)
                if self.store is not None
                else None
            )
            or None,
        )

    def _unresolved_chunk_handle_error(
        self, handle: str, kind: str, chunk_id: int
    ) -> PrecisError:
        """The error for a chunk handle ``resolve_handle`` missed.

        gr534656: ``pc617977`` reads like a slug to the fallback below,
        which is how agents got "paper slug 'pc617977' not found" and
        kept re-issuing the same call. The input was a chunk handle, so
        name the space. One indexed ``chunks`` lookup
        (``chunk_owner_kind``, which includes retired owners) splits
        the cases: another kind's chunk space (``dc<N>`` on a paper
        chunk — mint the right code), a chunk whose owner was deleted
        (``Gone``), or a chunk that never existed."""
        owner = None
        if self.store is not None:
            try:
                owner = self.store.chunks.chunk_owner_kind(chunk_id)
            except Exception:  # pragma: no cover — advisory lookup
                log.exception("unresolved-chunk owner lookup failed")
        if owner is not None and owner != kind:
            real = handle_registry.try_format(owner, chunk_id, chunk=True)
            return NotFound(
                f"{handle!r} is a {kind}-chunk handle, but chunk "
                f"{chunk_id} belongs to a {owner!r}"
                + (f" — its handle is {real}" if real else ""),
                next=(
                    f"get(id={real!r})" if real else f"search(kind={owner!r}, q='...')"
                ),
            )
        if owner is not None:
            return Gone(
                f"{handle!r} is a {kind}-chunk handle; chunk {chunk_id} "
                "exists but its owning ref was deleted",
                next=f"search(kind={kind!r}, q='...') for a live {kind}",
            )
        return NotFound(
            f"{handle!r} is a {kind}-chunk handle; no chunk {chunk_id} exists",
            next=(
                f"for the {kind} itself pass its slug or record handle, "
                f"or search(kind={kind!r}, q='...') to find one"
            ),
        )

    def _infer_batch_kind(
        self, args: dict[str, Any], idents: Any, *, verb: str | None = None
    ) -> None:
        """``get(id=['pc1', 'pc2'])`` with no ``kind=``: infer the kind per
        handle via the single-id path. One shared kind → that kind's batch
        get with the rewritten public ids; mixed kinds → ``BadInput``
        naming them. Items that don't self-identify leave the call as is."""
        if args.get("kind") is not None or not idents:
            return
        if not all(isinstance(i, str) for i in idents):
            return
        kinds: list[str] = []
        rewritten: list[str] = []
        for ident in idents:
            sub: dict[str, Any] = {"id": ident}
            self._maybe_split_prefixed_id(sub, verb=verb)
            if sub.get("kind") is None:
                return
            kinds.append(sub["kind"])
            rewritten.append(sub["id"])
        distinct = sorted(set(kinds))
        if len(distinct) > 1:
            raise BadInput(
                f"id= list mixes kinds {distinct!r}; one batch needs one kind",
                next="split into one call per kind, e.g. "
                + ", ".join(f"get(kind={k!r}, id=[...])" for k in distinct),
            )
        args["kind"] = distinct[0]
        args["id"] = rewritten

    def _maybe_split_prefixed_id(
        self, args: dict[str, Any], *, verb: str | None = None
    ) -> None:
        """D1: extract a self-identifying kind from ``id=`` into
        ``args['kind']``.

        Two grammars: ``kind:identifier[~selector]`` colon prefix (same
        grammar ``link=``/``unlink=`` use — ``id='paper:chung19~4'`` →
        ``kind='paper', id='chung19~4'``; ``id='chung19~4'`` unchanged, no
        colon) or a leading **address sigil**, which *is* the kind tag
        (``id='¶YP377G'`` → ``kind='draft'``, sigil kept;
        ``id='§chung19~4'`` → ``kind='paper'``, sigil stripped).

        Fires only when ``id`` is a string with exactly one ``:`` before
        any ``/``/``~``/``?``, the prefix is a live kind, and any
        already-set ``kind=`` matches the prefix (else ``BadInput``, never
        silent override). Path views (``id='/recent'``) are skipped.
        Anything else passes through unchanged.
        """
        ident = args.get("id")
        if isinstance(ident, (list, tuple)):
            self._infer_batch_kind(args, ident, verb=verb)
            return
        if not isinstance(ident, str):
            return
        # Address sigil (``¶`` draft chunk, ``§`` paper citation) is
        # self-identifying — route it before the colon logic. The draft
        # skill documents ``get(id='¶handle')`` with no ``kind=``.
        if ident[:1] in _SIGIL_KIND:
            self._infer_sigil_kind(args, ident)
            return
        # Leading slash → path view (/recent etc.). Don't extract.
        if ident.startswith("/"):
            return
        if ":" not in ident:
            # a draft chunk handle ``dc<id>`` (optionally with a
            # ``-B+A`` reading window) routes to the draft handler, which
            # parses the window. Drafts have no slug, so the generic
            # chunk-handle path below can't rewrite them to ``slug~ord``.
            if self._maybe_route_draft_chunk(args, ident):
                return
            # Same for a plan chunk handle ``pe<id>`` — its own grammar.
            if self._maybe_route_plan_chunk(args, ident):
                return
            # Universal handles relative navigation: ``pc10+1`` / ``pc10-2..3`` /
            # ``pc10^`` resolves against current structure to a per-kind
            # chunk selector. Try this before the absolute-handle path
            # (a relative handle is not a well-formed absolute one).
            if self._maybe_infer_kind_from_relative(args, ident):
                return
            # a universal handle (``<2-char code><decimal id>``)
            # self-identifies — resolve it to (kind, public_id) before the
            # bare-slug fallback, so it isn't mis-read as a slug.
            if self._maybe_infer_kind_from_handle(args, ident, verb=verb):
                return
            # No ``kind:`` prefix and no sigil — try resolving a bare
            # slug (optionally with a ``~selector`` / ``/view``) to its
            # owning kind, so ``get(id='wu22c~312')`` self-identifies as
            # the paper that owns ``wu22c``.
            self._infer_slug_kind(args, ident)
            return
        # Only honour a colon that comes before any selector / view
        # path separator. ``markdown:notes/a.md`` is fine; ``foo/bar:x``
        # is not — the colon there isn't a kind prefix.
        for sep in ("/", "?"):
            if sep in ident and ident.find(sep) < ident.find(":"):
                return
        prefix, _, rest = ident.partition(":")
        prefix = prefix.strip()
        if not prefix or not rest:
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if prefix not in live_kinds:
            # Not a recognised kind prefix — leave the value alone.
            # Better to surface a "no such kind" error downstream than
            # eat a legitimate identifier that happens to contain ":".
            return

        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != prefix:
            raise BadInput(
                f"id={ident!r} prefix kind={prefix!r} conflicts with "
                f"kind={existing_kind!r}",
                next=(
                    f"drop one: either pass id={rest!r} with "
                    f"kind={prefix!r}, or pass id={ident!r} without kind="
                ),
            )
        args["kind"] = prefix
        args["id"] = rest
