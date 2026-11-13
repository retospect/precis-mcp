"""Skill-example drift check (gr657066 — gr657041 slice 3; gr657045 is
the instance that motivated it).

Every shipped skill teaches the seven-verb surface through worked calls
(``put(kind='memory', text=…)`` in fenced blocks and inline code). When
the verbs move — an arg renamed, a param refused, a kind losing a verb —
the example rots silently: the structural lint
(``test_skills_examples.py``) checks kind/axis pairing and python
aliases, not the call surface itself. This test is the deploy-gate half
of the gripe: it emulates the dispatcher's own argument gates against
every literal ``verb(kind='<kind>', …)`` call in every shipped skill and
fails on the calls a real dispatch would refuse.

Per call it checks, against the live ``boot`` registry:

- ``kind=`` names a registered kind — wildcard (``'*'``/``'all'``/…),
  the ``'source'`` cross-kind alias and comma-lists take the multi-kind
  path (search-only); env-gated kinds that did not boot are skipped;
- the kind's ``KindSpec`` supports the verb;
- the literal ``mode=`` on put/edit is in the spec's declared mode
  vocabulary (``_validate_mode``);
- every supplied top-level kwarg survives the strictness gate —
  ``_handler_accepted_kwargs`` plus the wrapper-defaulted set plus the
  params dispatch consumes before the gate (``reason=`` on put/edit) or
  resolves on the search pre-pass (``under=``/``uncited=``/``sort=`` …);
- every literal ``args={…}`` key is a declared ``ARGS_KEYS[verb]`` key,
  a real handler kwarg the flatten path promotes, a dispatcher-consumed
  search facet, or a documented near-miss alias the gate never sees
  (``parent``, ``title``, ``retired``, ``ring``, ``context``,
  ``trust``/``writer``).

Routing is emulated the way ``_call_handler`` does: ``get`` with
``extent=`` or an eye-view literal takes the eye path (id/view/q/ring
only), ``get`` with ``q=`` on a ``supports_find_in_ref`` kind takes the
find path (id/view/q/context only). A ``search`` carrying a literal
``view=`` skips the args-key gate — view intercepts (count/stubs/flow/
meta/…) own their extras grammar. Non-literal ``kind=``/routing values
are skipped or judged leniently — this is a linter for drift, not a
proof of correctness.

The second half — :func:`test_skill_examples_execute_on_the_live_surface`,
``nightly``-marked — is the gripe's live check: it *runs* every
fully-literal example through the real MCP surface (slim verbs +
``tools.core`` mounted on ``runtime_with_store``) and fails on what the
call *sends* that the surface refuses — TypeErrors at the verb, the
``does not accept``/``not accepted by``/``shadows``/``unknown view``/
bad-mode refusals — plus ``# rejected`` calls that silently stopped
refusing and ``# →`` output annotations on
deterministic kinds (calc). Store/environment outcomes (NotFound,
Denied, Upstream, Internal, env-gated kinds, placeholder refs) are
skipped, not failed. Broader output drift stays with the advisory
skill evals (``scripts/skill-eval``). A finding names
``<file>:<line>`` — fix the skill, or (for a deliberate
counter-example) mark the line ``# rejected`` so
``_strip_negative_lines`` drops it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from typing import Any, cast

import pytest

from precis.dispatch import Hub, boot
from precis.handlers._eye import is_eye_view
from precis.protocol import TOLERATES_EXTRA_KWARGS_ATTR, Handler
from precis.runtime import PrecisRuntime
from precis.runtime._shared import (
    CROSS_KIND_ALIASES,
    CROSS_KIND_SOURCE_ALIAS,
    CROSS_KIND_SOURCE_KINDS,
)
from precis.runtime.dispatch import (
    _EDIT_MODE_WRAPPER_DEFAULT,
    _WRAPPER_DEFAULTED_KWARGS,
    CORE_PARAMS,
    _handler_accepted_kwargs,
)
from precis.store import Store
from precis.utils.handle_registry import is_known_kind
from tests._skill_roots import skill_files
from tests.test_skills_examples import (
    _NEGATIVE_LINE_MARKERS,
    _is_aspirational,
    _strip_negative_lines,
)

_VERBS = ("get", "search", "put", "edit", "delete", "tag", "link")

#: ``verb(`` call opener inside example text.
_CALL_RE = re.compile(
    r"\b(" + "|".join(_VERBS) + r")\s*\(",
)

#: Fenced code blocks of every language (calls hide in unlabelled and
#: ```text fences too, not just ```python) and inline code spans.
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")

#: Params the dispatcher consumes before the strictness gate ever sees
#: them, per verb (``reason=`` folds into the revision log on put/edit).
#: ``kind`` is popped at dispatch; ``args`` is the extras carrier.
_CONSUMED: dict[str, frozenset[str]] = {
    "put": frozenset({"reason"}),
    "edit": frozenset({"reason"}),
}

#: ``search``'s dispatcher-level params — read and popped by the
#: pre-handler passes (``_resolve_under``, ``_hoist_source_facet_args``,
#: ``_hoist_spray_args``, ``_resolve_canon``, the folder/``sort=``/
#: ``since=``/``until=`` source-search routes and the memory batch),
#: legal at the top level or inside ``args=`` on any kind.
_SEARCH_DISPATCH_KEYS = frozenset(
    {
        "under",
        "axis",
        "depth",
        "uncited",
        "cited",
        "hubbed",
        "canon",
        "angle",
        "like",
        "n",
        "top_k",
        "sort",
        "since",
        "until",
        "folder",
    }
)

#: args= keys the dispatcher pops or aliases before the extras gate:
#: ``parent``/``title`` are near-miss spellings folded onto real kwargs,
#: ``retired`` spells view='retired' on get, ``ring`` rides the eye
#: extras, ``context`` the find-in-ref extras, ``trust``/``writer`` are
#: the write-side provenance payloads, and ``batch`` is the memory
#: search batch (``dispatch_memory_batch`` — kind='memory' only, kept in
#: the shared set here: a batch on another kind refuses inside the
#: batch dispatcher before the extras gate would see it anyway).
_ARGS_ALIASES = frozenset(
    {
        "parent",
        "title",
        "retired",
        "ring",
        "context",
        "trust",
        "writer",
        "batch",
    }
)

#: Top-level kwargs on the get-eye path (extent rides to Handler.eye
#: with id/q/ring; everything else is a refused stray, gr524663).
_EYE_TOP = frozenset({"kind", "id", "view", "q", "args", "extent", "ring"})

#: Top-level kwargs on the get find-in-ref path (gr486840: id/view/q
#: only; ``context`` is the single args= key).
_FIND_TOP = frozenset({"kind", "id", "view", "q", "args"})

#: Kinds a shipped skill teaches as *proposed* — precis-quantum-help's
#: last section describes the unbuilt quantum surface the skill itself
#: declares nonexistent ("precis has no QPU access today"). Entries
#: leave this set when the kind ships (or the skill is rewritten).
_UNSHIPPED_KINDS = frozenset({"quantum"})

_KIND_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

_NONLITERAL = object()


def _literal(node: ast.AST) -> Any:
    """``ast.literal_eval`` or the ``_NONLITERAL`` sentinel."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return _NONLITERAL


def _call_node(text: str, start: int) -> tuple[ast.Call, int] | None:
    """Parse the balanced call whose ``(`` sits at ``start`` in ``text``.

    Scans with string/comment awareness so a ``)`` inside a quoted arg
    or trailing comment doesn't end the fragment early, then hands the
    fragment to ``ast.parse``. Returns ``(node, end)`` where ``end`` is
    the offset just past the closing ``)`` *in ``text``* — the node's
    own ``end_col_offset`` is relative to the re-parsed single fragment,
    so for a multiline call it points at the wrong ``text`` offset and
    any trailing ``# rejected``/``# →`` comment is lost (review
    td658273). ``None`` for anything that isn't a syntactically
    complete call (placeholders like ``id=<this todo>`` fail the parse
    and are skipped — the gate only judges calls it can read literally).
    """
    depth = 0
    i = start
    quote: str | None = None
    while i < len(text):
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                quote = None
            else:
                i += 1
            continue
        if text.startswith("'''", i) or text.startswith('"""', i):
            quote = text[i : i + 3]
            i += 3
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                try:
                    node = ast.parse(f"_v{text[start : i + 1]}", mode="eval").body
                except SyntaxError:
                    return None
                if isinstance(node, ast.Call):
                    return node, i + 1
                return None
        i += 1
    return None


def _iter_calls(text: str) -> Iterator[tuple[str, ast.Call, int]]:
    """Yield ``(verb, call node, line)`` for every parseable verb call
    in the text — fenced blocks (negative-marked lines stripped) and
    inline code spans alike."""
    for m in _FENCE_RE.finditer(text):
        block = _strip_negative_lines(m.group(1))
        for cm in _CALL_RE.finditer(block):
            parsed = _call_node(block, cm.end() - 1)
            if parsed is not None:
                node, _end = parsed
                line = text[: m.start()].count("\n") + block[: cm.start()].count("\n")
                yield cm.group(1), node, line + 1
    for m in _INLINE_RE.finditer(text):
        for cm in _CALL_RE.finditer(m.group(1)):
            parsed = _call_node(m.group(1), cm.end() - 1)
            if parsed is not None:
                node, _end = parsed
                yield cm.group(1), node, text[: m.start()].count("\n") + 1


def _kwargs(call: ast.Call) -> dict[str, ast.AST] | None:
    """The call's keyword args, or ``None`` when it isn't the plain
    ``verb(**kwargs)`` shape this linter judges (positional args or a
    ``**`` unpacking can't be emulated faithfully — skipped)."""
    if call.args or not isinstance(call.func, ast.Name):
        return None
    out: dict[str, ast.AST] = {}
    for kw in call.keywords:
        if kw.arg is None:  # ** unpacking
            return None
        out[kw.arg] = kw.value
    return out


def _args_literal_keys(call: ast.Call) -> tuple[set[str], bool]:
    """The literal keys of an ``args={…}`` dict literal; ``(set(), False)``
    when args= is absent or non-literal (the caller skips the key gate)."""
    kw = _kwargs(call)
    if kw is None or "args" not in kw:
        return set(), False
    node = kw["args"]
    if not isinstance(node, ast.Dict):
        return set(), False
    keys: set[str] = set()
    for k in node.keys:
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            return set(), False
        keys.add(k.value)
    return keys, True


def _kind_parts(kind: str) -> list[str] | None:
    """``kind=`` → the kinds to attribute the call to: ``None`` for a
    placeholder (``<kind>``, ``id='…'``, …), a one-element list for a
    single kind, ``[]`` for the all-kinds wildcard."""
    k = kind.strip().lower()
    if k in CROSS_KIND_ALIASES:
        return []
    # Split before validating: a comma-list (``'memory,todo'``) fails
    # the single-name pattern — splitting first keeps it a fan-out,
    # not a silent skip.
    parts = [p.strip() for p in k.split(",")]
    if not all(p and _KIND_NAME_RE.fullmatch(p) for p in parts):
        # Anything with non-name characters (``<kind>``, ``<any>``,
        # ``x:y``) is a placeholder the corpus uses to teach shape.
        return None
    if k == CROSS_KIND_SOURCE_ALIAS:
        return list(CROSS_KIND_SOURCE_KINDS)
    return parts


def _check_single(
    hub: Hub, verb: str, kind: str, call: ast.Call, kw: dict[str, ast.AST]
) -> list[str]:
    """The gate emulation for one call against one kind."""
    if not is_known_kind(kind):
        if kind in _UNSHIPPED_KINDS:
            return []
        return [f"{verb}(kind={kind!r}): unknown kind"]
    handler = hub.handlers.get(kind)
    if handler is None:
        return []  # env-gated kind didn't boot — the corpus can still name it
    if not handler.spec.supports(verb):
        return [f"{verb}(kind={kind!r}): kind does not support {verb}"]
    cls = cast("type[Handler]", type(handler))
    method = getattr(handler, verb)
    if getattr(method, TOLERATES_EXTRA_KWARGS_ATTR, False):
        return []  # the kind opted out of the strictness gate entirely
    accepted = set(_handler_accepted_kwargs(cls, verb))
    args_keys, args_is_literal = _args_literal_keys(call)
    supplied = set(kw) - {"kind", "args"}
    findings: list[str] = []

    # ── routing ─────────────────────────────────────────────────
    view_val = _literal(kw["view"]) if "view" in kw else None
    extent_val = _literal(kw["extent"]) if "extent" in kw else None
    eye = verb == "get" and (
        (extent_val is not None)
        or (isinstance(view_val, str) and is_eye_view(view_val))
    )
    find = (
        verb == "get"
        and not eye
        and kw.get("q") is not None
        and _literal(kw["q"]) is not None
        and getattr(handler, "supports_find_in_ref", False)
    )

    if eye:
        strays = supplied - _EYE_TOP
        if strays:
            findings.append(
                f"{verb}(kind={kind!r}): extent=/eye doesn't combine "
                f"with {sorted(strays)} (gr524663: id/view/q only, ring "
                "via args=)"
            )
        if args_is_literal:
            bad = args_keys - {"ring"}
            if bad:
                findings.append(
                    f"{verb}(kind={kind!r}): args= keys {sorted(bad)} "
                    "are refused on the eye path (only 'ring')"
                )
        return findings
    if find:
        strays = supplied - _FIND_TOP
        if strays:
            findings.append(
                f"{verb}(kind={kind!r}): q= (find-in-ref) doesn't combine "
                f"with {sorted(strays)} — drop them, or drop q="
            )
        if args_is_literal:
            bad = args_keys - {"context"}
            if bad:
                findings.append(
                    f"{verb}(kind={kind!r}): args= keys {sorted(bad)} "
                    "are refused on find-in-ref (only 'context')"
                )
        return findings

    # ── mode vocabulary (put/edit) ──────────────────────────────
    if verb in ("put", "edit") and "mode" in kw:
        mode_val = _literal(kw["mode"])
        if isinstance(mode_val, str) and mode_val != _EDIT_MODE_WRAPPER_DEFAULT:
            modes = handler.spec.edit_modes if verb == "edit" else handler.spec.modes
            if modes is not None and mode_val not in modes:
                findings.append(
                    f"{verb}(kind={kind!r}): mode={mode_val!r} not in "
                    f"its modes {list(modes)}"
                )

    # ── top-level strictness gate ───────────────────────────────
    # ``CORE_PARAMS[verb]`` names the wire-declared params the verb
    # consumes itself (search's ``tags``/``scope``/``status`` facet
    # filters, edit's find/replace payload keys) — they never reach the
    # kind handler, so they can't refuse them (td658272: the comma
    # fan-out made ``search(kind='a,b', tags=[...])`` flag on every
    # member kind).
    allowed = (
        accepted
        | set(CORE_PARAMS.get(verb, ()))
        | set(_WRAPPER_DEFAULTED_KWARGS.get(verb, ()))
        | set(_CONSUMED.get(verb, ()))
        | (_SEARCH_DISPATCH_KEYS if verb == "search" else frozenset())
    )
    unknown = sorted(k for k in supplied if k not in allowed)
    if unknown:
        findings.append(
            f"{verb}(kind={kind!r}) does not accept {unknown} "
            f"(its handler kwargs: {sorted(accepted)})"
        )

    # ── args= extras gate ───────────────────────────────────────
    # A literal view= skips this: the view intercepts (count, stubs,
    # flow, meta, …) own their extras grammar per kind.
    if (
        args_is_literal
        and args_keys
        and not (verb == "search" and isinstance(view_val, str))
    ):
        dispatch_keys = _SEARCH_DISPATCH_KEYS if verb == "search" else frozenset()
        consumed = set(_CONSUMED.get(verb, ()))
        if "args" in accepted:
            declared = (getattr(cls, "ARGS_KEYS", None) or {}).get(verb)
            if declared is not None:
                bad = (
                    args_keys
                    - set(declared)
                    - accepted
                    - _ARGS_ALIASES
                    - dispatch_keys
                    - consumed
                )
            else:
                bad = set()  # whole-dict passthrough, no key registry
        else:
            # Flatten path: every key must be a real kwarg, a dispatcher
            # key, a hoisted consumed key or a popped alias; ``args``
            # itself is never promoted.
            bad = (
                args_keys
                - accepted
                - _ARGS_ALIASES
                - dispatch_keys
                - consumed
                - {"args"}
            )
        if bad:
            findings.append(
                f"{verb}(kind={kind!r}): args= keys {sorted(bad)} not accepted"
            )
    return findings


def _check_call(hub: Hub, verb: str, call: ast.Call) -> list[str]:
    """Resolve ``kind=`` and run the gate per named kind."""
    kw = _kwargs(call)
    if kw is None:
        return []
    kind_val = _literal(kw.get("kind", ast.Constant(value=None)))
    if not isinstance(kind_val, str):
        return []  # non-literal kind — cannot attribute the call
    parts = _kind_parts(kind_val)
    if parts is None:
        return []  # placeholder kind — teaches shape, not a real call
    if parts == []:
        # Wildcard fan-out: no single handler to attribute; verify only
        # that the verb is search (multi-kind is search-only).
        return (
            []
            if verb == "search"
            else [f"{verb}(kind={kind_val!r}): multi-kind is search-only"]
        )
    if len(parts) > 1:
        if verb != "search":
            return [f"{verb}(kind={kind_val!r}): a comma-list only works on search"]
        bad_kinds = [
            p for p in parts if not is_known_kind(p) and p not in _UNSHIPPED_KINDS
        ]
        if bad_kinds:
            return [f"{verb}(kind={kind_val!r}): unknown kind(s) {bad_kinds}"]
        # Per-kind emulation on every member of the fan-out — a call
        # refused by any listed kind is drift the reader hits.
        findings: list[str] = []
        for kind in parts:
            findings.extend(_check_single(hub, verb, kind, call, kw))
        return findings
    return _check_single(hub, verb, parts[0], call, kw)


@pytest.fixture
def full_hub(store: Store) -> Iterator[Hub]:
    """The real dispatch registry (:func:`precis.dispatch.boot`) —
    kinds gated on an optional dependency that isn't installed simply
    don't register, same as production; the guard walks what's live."""
    yield boot(store=store)


def test_kind_parts_splits_comma_lists() -> None:
    """A comma-list kind (``'memory,todo'``) is a fan-out, not a
    placeholder — splitting before the single-name check keeps those
    examples checked instead of silently skipped (review td658272)."""
    assert _kind_parts("memory,todo") == ["memory", "todo"]
    assert _kind_parts(" memory , todo ") == ["memory", "todo"]
    assert _kind_parts("memory") == ["memory"]
    assert _kind_parts("*") == []
    assert _kind_parts("<kind>") is None


def test_iter_calls_live_reads_multiline_tails() -> None:
    """``# rejected``/``# →`` tails must be found when the call itself
    spans lines: the live pass reads the tail from the scan-verified end
    of the call, not from the parsed fragment's ``end_col_offset`` (which
    is relative to the re-parse and lands mid-call on multiline input —
    review td658273)."""
    text = (
        "```python\n"
        "put(kind='memory',\n"
        "    text='x')  # rejected\n"
        "put(kind='calc', text='2+2'\n"
        "    )  # → 4\n"
        "```"
    )
    calls = list(_iter_calls_live(text))
    assert [(v, neg, exp) for v, _n, _l, neg, exp in calls] == [
        ("put", True, None),
        ("put", False, "4"),
    ]


def test_skill_examples_survive_the_verb_gates(full_hub: Hub) -> None:
    """Every literal ``verb(kind='K', …)`` example in a shipped skill
    must name args the live dispatcher accepts."""
    findings: list[str] = []
    for path in skill_files():
        text = path.read_text(encoding="utf-8")
        if _is_aspirational(text):
            continue
        for verb, call, line in _iter_calls(text):
            for f in _check_call(full_hub, verb, call):
                rendered = verb + ast.unparse(call)[len("_v") :]
                findings.append(f"{path.name}:{line}: {f} — `{rendered[:120]}`")
    assert not findings, (
        "skill examples drifted from the live verb surface "
        f"({len(findings)} finding(s); mark deliberate counter-examples "
        "`# rejected`):\n" + "\n".join(findings)
    )


# ---------------------------------------------------------------------------
# Live-dispatch pass (nightly)
#
# The gate emulation above approximates the dispatcher's argument rules;
# this pass *runs* every fully-literal example through the real MCP
# surface — the slim verbs for put/edit/search (the args= fold, the
# top-level promotion, the deprecation note) and the registered verb
# functions for get/delete/tag/link, mounted on ``runtime_with_store``.
# It catches the classes a signature emulator cannot see: a
# wrapper-defaulted kwarg a handler refuses (``verdict='approved'``
# landing on ``fleet.edit``), a param the handler accepts but the wire
# verb never declared (``note=`` on ``structure.link``), and an
# argument that type-checks nowhere (a TypeError before dispatch). It
# also checks ``# rejected``-marked calls still refuse and ``# →``
# output annotations on the deterministic ``calc`` surface.
#
# The slim verb's deprecated-top-level fold is *not* flagged: the
# corpus's canonical spelling is the top-level one — the fold exists
# so slim-schema clients keep working while docs lead with it.
#
# Classification is deliberately asymmetric. A call that *sends*
# something the surface refuses — an unaccepted kwarg, a shadowing
# args= key, a mode outside the vocabulary, an unknown view, a verb the
# kind doesn't take — is drift and fails. A call refused for what it
# *lacks* or *names* — a required param the shorthand citation never
# claimed to supply, a placeholder ref, a not-found handle, an
# env-gated kind or dependency (Denied/Unsupported/Upstream/Internal)
# — is environment, not drift, and is skipped.
# ---------------------------------------------------------------------------

#: ``# →``/``# ->`` trailing-output annotation on an example line.
_ARROW_RE = re.compile(r"#\s*(?:→|->)\s*(?P<exp>\S.*)$")

#: A value that marks the example as shape-teaching, not runnable:
#: ``<slug>``, ``…`` and ``...`` inside a literal (or a bare ``...``/
#: non-dict ``args=`` shape like ``{...}``) — the call isn't executed.
_PLACEHOLDER_RE = re.compile(r"<[^>\n]*>|…|\.\.\.")

#: Error-envelope messages that mean the *call's own spelling* is
#: refused — the arg-name/refused-param drift the gripe names.
#: Value-level refusals (requires/missing/invalid/not-a/not-found) and
#: env envelopes (Denied/Unsupported/Upstream/Internal) pass.
_LIVE_DRIFT_RES = (
    re.compile(r"does not accept"),
    re.compile(r"not accepted by"),
    re.compile(r"shadows the explicit kwargs"),
    re.compile(r"is not supported with"),
    re.compile(r"is not supported for kind="),
    re.compile(r"\bdoes not support (get|search|put|edit|delete|tag|link)\b"),
    re.compile(r"only supports mode="),
    re.compile(r"mode= is not accepted on"),
    re.compile(r"unknown (?:list )?view"),
)

#: Kinds whose ``# →`` annotations are literal, deterministic output —
#: compared against the first line of the live result. Other kinds'
#: arrows are prose expectations (``# → memory id=501``), not text.
_OUTPUT_CHECKED_KINDS = frozenset({"calc"})


def _call_tail(text: str, endpos: int) -> str:
    """The comment tail after a call: the rest of its closing line plus
    a comment-only next line (``# rejected``/``# →`` markers live in
    both spellings)."""
    rest = text[endpos:]
    nl = rest.find("\n")
    if nl == -1:
        return rest
    tail = rest[:nl]
    nxt = rest[nl + 1 :]
    nl2 = nxt.find("\n")
    second = nxt if nl2 == -1 else nxt[:nl2]
    if second.lstrip().startswith("#"):
        return tail + " " + second
    return tail


def _negative_tail(tail: str) -> bool:
    """Whether a call's trailing comment marks it expected-to-fail:
    the ``# rejected``-family markers, or a doc-rendered
    ``# [error:…]`` envelope line right after the call."""
    lowered = tail.lower()
    if any(mk in lowered for mk in _NEGATIVE_LINE_MARKERS):
        return True
    return re.search(r"#\s*\[error:", tail) is not None


def _iter_calls_live(
    text: str,
) -> Iterator[tuple[str, ast.Call, int, bool, str | None]]:
    """Yield ``(verb, call, line, negative, expect)`` for every
    parseable call. Unlike :func:`_iter_calls`, negative-marked calls
    are *kept* (flagged ``negative=True``) so the live pass can assert
    they still refuse, and the ``# →`` tail is returned for the
    deterministic output check."""
    for m in _FENCE_RE.finditer(text):
        block = m.group(1)
        base = text[: m.start()].count("\n")
        for cm in _CALL_RE.finditer(block):
            parsed = _call_node(block, cm.end() - 1)
            if parsed is None:
                continue
            node, end = parsed
            tail = _call_tail(block, end - 1)
            arrow = _ARROW_RE.search(tail)
            yield (
                cm.group(1),
                node,
                base + block[: cm.start()].count("\n") + 1,
                _negative_tail(tail),
                arrow.group("exp").strip() if arrow else None,
            )
    for m in _INLINE_RE.finditer(text):
        span = m.group(1)
        for cm in _CALL_RE.finditer(span):
            parsed = _call_node(span, cm.end() - 1)
            if parsed is None:
                continue
            node, end = parsed
            tail = _call_tail(span, end - 1)
            arrow = _ARROW_RE.search(tail)
            yield (
                cm.group(1),
                node,
                text[: m.start()].count("\n") + 1,
                _negative_tail(tail),
                arrow.group("exp").strip() if arrow else None,
            )


def _has_placeholder(value: Any) -> bool:
    """Whether a literal value carries an unsubstituted placeholder —
    ``<slug>``, ``…``, ``...``, a bare ``Ellipsis`` — meaning the
    example teaches shape, not a call to run."""
    if value is _NONLITERAL or value is Ellipsis:
        return True
    if isinstance(value, str):
        return bool(_PLACEHOLDER_RE.search(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_has_placeholder(v) for v in value)
    if isinstance(value, dict):
        return any(_has_placeholder(k) or _has_placeholder(v) for k, v in value.items())
    return False


def _result_text(result: Any) -> tuple[str, bool]:
    """``(text, is_error)`` for both verb return shapes: the rendered
    string on success, the ``CallToolResult`` on error."""
    if isinstance(result, str):
        return result, False
    content = getattr(result, "content", None) or []
    text = "\n".join(getattr(p, "text", "") for p in content)
    return text, bool(getattr(result, "isError", False))


def _expected_token(expect: str) -> str:
    """The checkable field of a ``# →`` annotation — text before the
    trailing ``(…)`` gloss (``# → sqrt(2)   (exact, not 1.4142…)``)."""
    return re.split(r"\s{2,}\(", expect)[0].strip()


def _output_matches(got_line: str, want: str) -> bool:
    """Whether the live first line honours a ``# →`` annotation — an
    exact or ``=``-suffix match, plus float rounding for shortened
    numeric annotations (``0.997495`` annotating
    ``… = 0.997494986604054``)."""
    tail = got_line.rsplit("=", 1)[-1].strip()
    if want in (got_line, tail):
        return True
    try:
        want_f = float(want)
        got_f = float(tail)
    except ValueError:
        return False
    decimals = len(want.partition(".")[2])
    if decimals and "e" not in want.lower():
        return format(got_f, f".{decimals}f") == want
    return got_f == want_f


@pytest.mark.nightly
def test_skill_examples_execute_on_the_live_surface(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Run every fully-literal skill example through the real MCP
    surface and fail on the calls a live dispatch refuses for *what
    they send*: wrong arg names, refused params, invalid modes —
    plus ``# rejected`` calls that no longer
    refuse and ``# →`` output annotations that drifted (calc).

    Env-dependent outcomes are skipped, not failed: placeholder refs,
    NotFound/Denied/Upstream/Internal envelopes, env-gated kinds that
    didn't boot, and value-level refusals (``requires``/``missing``/
    ``invalid``) on shorthand citations. Too heavy for the ship gate —
    the nightly run owns it, matching the gripe's nightly-or-gate ask.
    """
    from precis.tools import TOOL_REGISTRY
    from precis.tools import core as tools_core
    from precis.tools.mcp_slim import make_slim_verb

    surface = {
        v: (
            make_slim_verb(v, TOOL_REGISTRY[v]["func"])
            if v in ("put", "edit", "search")
            else TOOL_REGISTRY[v]["func"]
        )
        for v in _VERBS
    }
    booted = set(runtime_with_store.hub.handlers)
    findings: list[str] = []
    tools_core._runtime = runtime_with_store
    try:
        for path in skill_files():
            text = path.read_text(encoding="utf-8")
            if _is_aspirational(text):
                continue
            for verb, call, line, negative, expect in _iter_calls_live(text):
                kw = _kwargs(call)
                if kw is None:
                    continue
                vals: dict[str, Any] = {}
                for name, node in kw.items():
                    v = _literal(node)
                    if v is _NONLITERAL:
                        break
                    vals[name] = v
                else:
                    if any(_has_placeholder(v) for v in vals.values()):
                        continue
                    if "args" in vals and not isinstance(vals["args"], dict):
                        continue
                    kind_val = vals.get("kind")
                    parts = _kind_parts(kind_val) if isinstance(kind_val, str) else None
                    if parts is not None and parts and not (set(parts) & booted):
                        continue  # env-gated kind that didn't boot
                    rendered = verb + ast.unparse(call)[len("_v") :]
                    loc = f"{path.name}:{line}"
                    try:
                        body, is_error = _result_text(surface[verb](**vals))
                    except Exception as exc:
                        if negative:
                            continue
                        findings.append(
                            f"{loc}: raised {type(exc).__name__} "
                            f"({str(exc)[:100]}) before dispatch — "
                            f"`{rendered[:120]}`"
                        )
                        continue
                    if negative:
                        if not is_error:
                            findings.append(
                                f"{loc}: marked negative but the live "
                                f"call succeeded — `{rendered[:120]}`"
                            )
                        continue
                    if is_error:
                        head = (body.splitlines() or [""])[0]
                        if expect is not None and parts == list(_OUTPUT_CHECKED_KINDS):
                            # A deterministic example promised a literal
                            # ``# →`` result: an error envelope — even a
                            # benign NotFound/Denied one — means the
                            # promised output was not produced (review
                            # td658273: this used to skip silently).
                            findings.append(
                                f"{loc}: promised output {expect!r} but "
                                f"the call errored — {head[:110]} — "
                                f"`{rendered[:120]}`"
                            )
                        elif any(p.search(body) for p in _LIVE_DRIFT_RES):
                            findings.append(f"{loc}: {head[:110]} — `{rendered[:120]}`")
                        continue
                    if expect is not None and parts == list(_OUTPUT_CHECKED_KINDS):
                        got = (body.splitlines() or [""])[0].strip()
                        want = _expected_token(expect)
                        if want and not _output_matches(got, want):
                            findings.append(
                                f"{loc}: output drift — expected "
                                f"{want!r}, got {got[:80]!r} — "
                                f"`{rendered[:120]}`"
                            )
    finally:
        tools_core._runtime = None
    assert not findings, (
        "skill examples drifted from the live dispatch surface "
        f"({len(findings)} finding(s)):\n" + "\n".join(findings)
    )
