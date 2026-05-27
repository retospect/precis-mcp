"""Tests for :mod:`precis.default_tags` + the dispatch wiring.

Phase 5 of the cold-start token budget design
(``docs/design/mcp-cold-start-token-budget.md``). Covers:

- :func:`precis.default_tags.parse` parsing semantics.
- :func:`precis.default_tags.merge` set-union with order preservation.
- :func:`precis.default_tags.suggest_missing` for the ``tag`` verb.
- :func:`precis.default_tags.apply_to_put_args` mutation contract.
- ``KindSpec.note_like`` is set on the curated note-like kinds.
- :meth:`PrecisRuntime._apply_default_tags_policy` applies the merge
  on ``put`` for note-like kinds, emits a hint, and is a no-op for
  non-note-like kinds and for verbs other than ``put`` / ``tag``.
- ``tag`` verb gets a suggestion hint without mutation.
- End-to-end: ``PRECIS_DEFAULT_TAGS`` layered with the ``workspace``
  auto-tag on prose-file kinds (markdown / plaintext / tex) — OQ-17
  from the design / ADR 0013. Both layers must land on the resulting
  ref: ``workspace`` for file-rooted-ness, defaults for session
  context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis import default_tags
from precis.hints import Hint
from precis.protocol import Handler, KindSpec
from precis.response import Response

# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------


def test_parse_handles_none() -> None:
    assert default_tags.parse(None) == ()


def test_parse_handles_empty_string() -> None:
    assert default_tags.parse("") == ()


def test_parse_single_tag() -> None:
    assert default_tags.parse("fbproj") == ("fbproj",)


def test_parse_multiple_preserves_order() -> None:
    """Operator-stated order survives parsing — used for stable
    rendering in the merge / suggestion-hint messages.
    """
    assert default_tags.parse("fbproj,2026-q2,team-research") == (
        "fbproj",
        "2026-q2",
        "team-research",
    )


def test_parse_tolerates_whitespace() -> None:
    assert default_tags.parse(" fbproj ,  2026-q2 ") == ("fbproj", "2026-q2")


def test_parse_drops_empty_entries() -> None:
    assert default_tags.parse("fbproj,,team-research,") == (
        "fbproj",
        "team-research",
    )


def test_parse_dedupes_first_occurrence_wins() -> None:
    assert default_tags.parse("fbproj,team,fbproj") == ("fbproj", "team")


# ---------------------------------------------------------------------------
# merge()
# ---------------------------------------------------------------------------


def test_merge_no_defaults_returns_explicit_copy() -> None:
    explicit = ["a", "b"]
    out = default_tags.merge(explicit, ())
    assert out == ["a", "b"]
    # Returned list is a copy — not the caller's original.
    out.append("c")
    assert explicit == ["a", "b"]


def test_merge_no_explicit_returns_defaults_in_order() -> None:
    assert default_tags.merge(None, ("x", "y", "z")) == ["x", "y", "z"]


def test_merge_unions_preserving_explicit_first() -> None:
    """Explicit-first ordering: the agent's stated order survives;
    defaults fill in only the gaps."""
    out = default_tags.merge(["mine", "yours"], ("ours", "mine", "shared"))
    assert out == ["mine", "yours", "ours", "shared"]


def test_merge_dedupes_within_defaults() -> None:
    """Defensive: even if defaults somehow contains a duplicate,
    merge() filters it (parse() already dedupes, but merge can be
    called directly)."""
    out = default_tags.merge(["a"], ("b", "c", "b"))
    assert out == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# suggest_missing()
# ---------------------------------------------------------------------------


def test_suggest_missing_with_no_defaults() -> None:
    assert default_tags.suggest_missing(["a"], ()) == ()


def test_suggest_missing_no_explicit_returns_all_defaults() -> None:
    assert default_tags.suggest_missing(None, ("x", "y")) == ("x", "y")


def test_suggest_missing_filters_present_tags() -> None:
    assert default_tags.suggest_missing(
        ["fbproj", "other"], ("fbproj", "2026-q2", "team")
    ) == ("2026-q2", "team")


def test_suggest_missing_returns_empty_when_all_present() -> None:
    assert default_tags.suggest_missing(["a", "b"], ("a", "b")) == ()


# ---------------------------------------------------------------------------
# apply_to_put_args()
# ---------------------------------------------------------------------------


def test_apply_to_put_args_no_defaults_is_noop() -> None:
    args: dict[str, Any] = {"tags": ["a"]}
    added = default_tags.apply_to_put_args(args, ())
    assert added == ()
    assert args == {"tags": ["a"]}


def test_apply_to_put_args_appends_missing_defaults() -> None:
    args: dict[str, Any] = {"tags": ["mine"]}
    added = default_tags.apply_to_put_args(args, ("fbproj", "2026-q2"))
    assert args["tags"] == ["mine", "fbproj", "2026-q2"]
    assert added == ("fbproj", "2026-q2")


def test_apply_to_put_args_handles_missing_tags_key() -> None:
    """``put`` payload may omit the tags key entirely; apply must
    create it from the merge."""
    args: dict[str, Any] = {"kind": "memory", "text": "..."}
    added = default_tags.apply_to_put_args(args, ("fbproj",))
    assert args["tags"] == ["fbproj"]
    assert added == ("fbproj",)


def test_apply_to_put_args_handles_none_tags() -> None:
    """Equivalent to missing tags key; apply normalises both."""
    args: dict[str, Any] = {"tags": None}
    added = default_tags.apply_to_put_args(args, ("fbproj",))
    assert args["tags"] == ["fbproj"]
    assert added == ("fbproj",)


def test_apply_to_put_args_returns_empty_when_already_present() -> None:
    args: dict[str, Any] = {"tags": ["fbproj", "2026-q2"]}
    added = default_tags.apply_to_put_args(args, ("fbproj", "2026-q2"))
    assert added == ()
    # ``tags`` is left as-is; the merge would have produced the same
    # list, but we don't bother re-assigning.
    assert args["tags"] == ["fbproj", "2026-q2"]


# ---------------------------------------------------------------------------
# KindSpec.note_like flip-list
# ---------------------------------------------------------------------------


def test_note_like_kinds_are_flipped() -> None:
    """Phase 5 step 2 audit: every note-like handler has
    ``note_like=True`` on its ``KindSpec``. Catches an unintentional
    revert.
    """
    from precis.handlers.conversation import ConversationHandler
    from precis.handlers.flashcard import FlashcardHandler
    from precis.handlers.gripe import GripeHandler
    from precis.handlers.markdown import MarkdownHandler
    from precis.handlers.memory import MemoryHandler
    from precis.handlers.plaintext import PlaintextHandler
    from precis.handlers.quest import QuestHandler
    from precis.handlers.tex import TexHandler
    from precis.handlers.todo import TodoHandler

    note_like_handlers: list[type[Handler]] = [
        MemoryHandler,
        TodoHandler,
        GripeHandler,
        FlashcardHandler,
        QuestHandler,
        ConversationHandler,
        MarkdownHandler,
        PlaintextHandler,
        TexHandler,
    ]
    for cls in note_like_handlers:
        assert cls.spec.note_like is True, (
            f"{cls.__name__} is in the note-like flip-list but spec.note_like is False"
        )


def test_non_note_like_kinds_remain_default() -> None:
    """Symmetric guard: ingested / cache / generator kinds keep
    ``note_like=False`` so PRECIS_DEFAULT_TAGS doesn't pollute their
    refs.
    """
    from precis.handlers.calc import CalcHandler
    from precis.handlers.math import MathHandler
    from precis.handlers.oracle import OracleHandler
    from precis.handlers.paper import PaperHandler
    from precis.handlers.patent import PatentHandler
    from precis.handlers.skill import SkillHandler
    from precis.handlers.web import WebHandler
    from precis.handlers.youtube import YouTubeHandler

    not_note_like: list[type[Handler]] = [
        PaperHandler,
        PatentHandler,
        WebHandler,
        YouTubeHandler,
        MathHandler,
        OracleHandler,
        SkillHandler,
        CalcHandler,
    ]
    for cls in not_note_like:
        assert cls.spec.note_like is False, (
            f"{cls.__name__} is NOT in the note-like flip-list but "
            f"spec.note_like is True"
        )


# ---------------------------------------------------------------------------
# Runtime dispatch wiring: _apply_default_tags_policy
# ---------------------------------------------------------------------------


class _NoteLikeHandler(Handler):
    spec = KindSpec(
        kind="testnote",
        title="Test note kind",
        description="for tests",
        supports_put=True,
        supports_tag=True,
        note_like=True,
    )

    def put(self, **_kw: Any) -> Response:  # type: ignore[override]
        return Response(body="ok")

    def tag(self, **_kw: Any) -> Response:  # type: ignore[override]
        return Response(body="ok")


class _NotNoteLikeHandler(Handler):
    spec = KindSpec(
        kind="testcache",
        title="Test cache kind",
        description="for tests",
        supports_put=True,
        supports_tag=True,
        note_like=False,
    )

    def put(self, **_kw: Any) -> Response:  # type: ignore[override]
        return Response(body="ok")


def _runtime_with_defaults(defaults: tuple[str, ...]):
    from precis.config import PrecisConfig
    from precis.dispatch import Hub
    from precis.runtime import PrecisRuntime

    return PrecisRuntime(
        config=PrecisConfig(),
        hub=Hub(),
        default_tags_resolved=defaults,
    )


def _captured_hints(rt) -> list[Hint]:
    """Drain the runtime's hint bus inside a synthetic request scope.

    The dispatch hook calls ``hub.emit_hint``, which is a no-op
    outside a request scope. Tests that exercise the hook use this
    helper to open a scope, run the policy, and harvest the hints.
    """
    return list(rt.hub.hints._collected if hasattr(rt.hub.hints, "_collected") else [])


def test_policy_noop_when_defaults_empty() -> None:
    rt = _runtime_with_defaults(())
    handler = _NoteLikeHandler()
    args: dict[str, Any] = {"tags": ["mine"]}
    rt._apply_default_tags_policy(handler, "put", args)
    assert args == {"tags": ["mine"]}


def test_policy_noop_for_non_note_like_kind() -> None:
    rt = _runtime_with_defaults(("fbproj",))
    handler = _NotNoteLikeHandler()
    args: dict[str, Any] = {"tags": ["explicit"]}
    rt._apply_default_tags_policy(handler, "put", args)
    assert args == {"tags": ["explicit"]}


def test_policy_noop_for_non_put_non_tag_verbs() -> None:
    """get / search / edit / delete / link don't carry session
    tags via this dispatch hook."""
    rt = _runtime_with_defaults(("fbproj",))
    handler = _NoteLikeHandler()
    for verb in ("get", "search", "edit", "delete", "link"):
        args: dict[str, Any] = {"tags": ["mine"]}
        rt._apply_default_tags_policy(handler, verb, args)
        assert args == {"tags": ["mine"]}, f"verb={verb} mutated args"


def test_policy_merges_on_put_for_note_like_kind() -> None:
    rt = _runtime_with_defaults(("fbproj", "2026-q2"))
    handler = _NoteLikeHandler()
    args: dict[str, Any] = {"tags": ["mine"]}
    with rt.hints.request():
        rt._apply_default_tags_policy(handler, "put", args)
        hints = rt.hints.collect()
    assert args["tags"] == ["mine", "fbproj", "2026-q2"]
    assert any(h.topic == "default_tags.merged" for h in hints), (
        f"expected default_tags.merged hint, got topics: {[h.topic for h in hints]}"
    )


def test_policy_emits_no_hint_when_all_present_on_put() -> None:
    """If the caller already supplies every default, the merge is
    a no-op and no hint should fire (no signal worth surfacing)."""
    rt = _runtime_with_defaults(("fbproj",))
    handler = _NoteLikeHandler()
    args: dict[str, Any] = {"tags": ["fbproj", "other"]}
    with rt.hints.request():
        rt._apply_default_tags_policy(handler, "put", args)
        hints = rt.hints.collect()
    assert all(h.topic != "default_tags.merged" for h in hints)


def test_policy_suggests_missing_on_tag() -> None:
    rt = _runtime_with_defaults(("fbproj", "2026-q2"))
    handler = _NoteLikeHandler()
    args: dict[str, Any] = {"add": ["fbproj", "ad-hoc"]}
    with rt.hints.request():
        rt._apply_default_tags_policy(handler, "tag", args)
        hints = rt.hints.collect()
    # Args NOT mutated — tag is operator-explicit.
    assert args["add"] == ["fbproj", "ad-hoc"]
    suggested = [h for h in hints if h.topic == "default_tags.suggested"]
    assert len(suggested) == 1
    assert "2026-q2" in suggested[0].text
    assert "fbproj" not in suggested[0].text  # already present


def test_policy_no_tag_hint_when_all_present() -> None:
    rt = _runtime_with_defaults(("fbproj",))
    handler = _NoteLikeHandler()
    args: dict[str, Any] = {"add": ["fbproj", "extra"]}
    with rt.hints.request():
        rt._apply_default_tags_policy(handler, "tag", args)
        hints = rt.hints.collect()
    assert all(h.topic != "default_tags.suggested" for h in hints)


# ---------------------------------------------------------------------------
# build_runtime: default_tags_resolved is populated from config
# ---------------------------------------------------------------------------


def test_build_runtime_resolves_default_tags_from_config(
    monkeypatch,
) -> None:
    """End-to-end: PRECIS_DEFAULT_TAGS env var → PrecisConfig field
    → parsed tuple on PrecisRuntime."""
    from precis.config import PrecisConfig
    from precis.runtime import build_runtime

    config = PrecisConfig(default_tags="fbproj,2026-q2")
    monkeypatch.setattr(
        "precis.dispatch.boot",
        lambda **_kw: MagicMock(kinds=set()),
    )
    rt = build_runtime(config=config)
    assert rt.default_tags_resolved == ("fbproj", "2026-q2")


def test_build_runtime_default_tags_default_empty(monkeypatch) -> None:
    """No env var → empty tuple → policy is a no-op."""
    from precis.config import PrecisConfig
    from precis.runtime import build_runtime

    config = PrecisConfig(default_tags=None)
    monkeypatch.setattr(
        "precis.dispatch.boot",
        lambda **_kw: MagicMock(kinds=set()),
    )
    rt = build_runtime(config=config)
    assert rt.default_tags_resolved == ()


# ---------------------------------------------------------------------------
# OQ-17: PRECIS_DEFAULT_TAGS × workspace auto-tag layering on prose-file kinds
# ---------------------------------------------------------------------------
#
# Background (ADR 0013, OQ-17 in OPEN-ITEMS.md): the prose-file handlers
# (markdown / plaintext / tex) auto-stamp every ingested ref with the
# ``workspace`` flag tag — useful so an agent can scope
# ``search(tags=['workspace'])`` to file-rooted content. Phase 5 added the
# ``PRECIS_DEFAULT_TAGS`` env var, which the runtime layers into every
# ``put`` on note-like kinds (markdown / plaintext / tex are note-like).
# The design's tentative position was that the two layers cooperate:
# ``workspace`` identifies file-rooted-ness, defaults identify session
# context; both true simultaneously is the right semantics.
#
# This section pins the layering end-to-end through ``runtime.dispatch``
# so the runtime's ``_apply_default_tags_policy`` hook actually fires
# (the unit tests above stub the handler — they don't catch the case
# where the handler's ``put`` silently drops the merged ``tags=``).


_PROSE_FIXTURES: list[tuple[str, str, str]] = [
    ("plaintext", "notes.txt", "para one.\n\npara two.\n"),
    ("markdown", "notes.md", "# Heading\n\npara one.\n\npara two.\n"),
    ("tex", "notes.tex", "\\section{intro}\n\nbody paragraph.\n"),
]


def _register_prose_handlers(hub: Any, root: Path) -> None:
    """Construct + register the three prose-file handlers against ``hub``.

    Mirrors what :func:`precis.dispatch.boot` does for the file-handler
    trio, minus the gating bookkeeping. Calling ``_register_with``
    directly is the supported way to wire a handler into a hand-built
    hub for tests (see ``protocol.Handler._register_with``).
    """
    from precis.handlers.markdown import MarkdownHandler
    from precis.handlers.plaintext import PlaintextHandler
    from precis.handlers.tex import TexHandler

    for cls in (PlaintextHandler, MarkdownHandler, TexHandler):
        handler = cls(hub=hub, root=root)
        handler._register_with(hub)


def _tag_string_set(tags: list[Any]) -> set[str]:
    """Flatten ``store.tags_for`` rows into a canonical string set.

    Open / flag namespaces collapse to their bare value (``fbproj`` /
    ``workspace``); closed-prefix tags use ``prefix:value`` so any
    closed-axis defaults remain distinguishable. Matches the agent-
    facing rendering used everywhere else in the codebase.
    """
    out: set[str] = set()
    for tag in tags:
        if tag.namespace == "closed":
            out.add(f"{tag.prefix}:{tag.value}")
        else:
            out.add(tag.value)
    return out


@pytest.mark.parametrize(("kind", "filename", "body"), _PROSE_FIXTURES)
@pytest.mark.parametrize(
    ("default_tags_env", "expected_defaults"),
    [
        (None, ()),
        ("fbproj", ("fbproj",)),
        ("fbproj,scratch", ("fbproj", "scratch")),
    ],
)
def test_default_tags_layer_with_workspace_on_prose_handlers(
    store: Any,
    tmp_path: Path,
    kind: str,
    filename: str,
    body: str,
    default_tags_env: str | None,
    expected_defaults: tuple[str, ...],
) -> None:
    """``workspace`` and ``PRECIS_DEFAULT_TAGS`` layer on every prose-file
    ``put``. ADR 0013 OQ-17.

    Pins the design's tentative contract: a fresh markdown / plaintext /
    tex ref carries the auto-stamped ``workspace`` flag plus every
    operator-stated default. Regression guard against a future refactor
    that drops one layer (handler signature drift, dispatch-hook
    re-routing, ``note_like`` flag flip, etc.).
    """
    from precis import default_tags as _dt
    from precis.config import PrecisConfig
    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.runtime import PrecisRuntime

    root = tmp_path / "root"
    root.mkdir()

    hub = Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
    _register_prose_handlers(hub, root)

    rt = PrecisRuntime(
        config=PrecisConfig(),
        hub=hub,
        default_tags_resolved=_dt.parse(default_tags_env),
    )

    slug = filename.rsplit(".", 1)[0]
    out = rt.dispatch(
        "put",
        {"kind": kind, "id": slug, "text": body, "mode": "create"},
    )
    assert f"created {kind} '{slug}'" in out, (
        f"put({kind=}) failed: {out!r}"
    )

    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None, f"{kind}: ref not found after put"

    tag_strs = _tag_string_set(store.tags_for(ref.id))

    assert "workspace" in tag_strs, (
        f"{kind}: workspace flag missing after put with "
        f"PRECIS_DEFAULT_TAGS={default_tags_env!r}; got {sorted(tag_strs)}"
    )
    for default in expected_defaults:
        assert default in tag_strs, (
            f"{kind}: default tag {default!r} missing after put with "
            f"PRECIS_DEFAULT_TAGS={default_tags_env!r}; got {sorted(tag_strs)}"
        )
