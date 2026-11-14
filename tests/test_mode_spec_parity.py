"""``KindSpec.modes`` / ``edit_modes`` parity with what handlers actually
do (gr292913, sentinel semantics fixed under gr343755).

Before gr292913's fix, ``KindSpec.modes`` existed (declared for exactly
four file/cache kinds) but was read nowhere — every ``put``/``edit`` mode
rejection hand-rolled its own message, so the declaration and the real
per-kind behaviour could silently drift apart. ``KindSpec.edit_modes``
didn't exist at all: ``edit``'s ``mode=`` vocabulary is a *different*
vocabulary from ``put``'s (region-rewrite ops vs. create/import), and the
two verbs' generic MCP tool descriptions each advertise one static
cross-kind string even though per-kind handlers accept wildly different
(or no) modes — exactly the gap agents kept tripping on.

gr343755 fixed a second, subtler ambiguity: both fields defaulted to the
shared ``()`` sentinel, which conflated "``mode=`` isn't a concept here at
all" with "``mode=`` is a recognised parameter that rejects every value"
(``message``'s put is the latter — it distinguishes the two on purpose so
a caller reaching for either shape gets the same, unambiguous "not
accepted" error). The default is now ``None`` ("no concept"); ``()``
is reserved for a handler that genuinely branches on ``mode=`` — checks
it, and rejects it unconditionally.

``_EXPECTED_MODES`` below is the hand-audited ground truth: every kind
whose ``put``/``edit`` method *branches on* a ``mode`` parameter
(dispatches differently per value, checks for a specific one, or
explicitly rejects any value) gets an entry; every other kind is expected
to report ``None`` — either ``mode=`` isn't a parameter on that verb at
all, or it's silently swallowed by the handler's ``**_kw`` catch-all with
no branching (neither of which is "declares a vocabulary" in the sense
this field records).
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from inspect import signature
from typing import Any

import pytest

from precis.dispatch import Hub, boot
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools import core as tools_core


def test_memory_reads_filters_are_declared_on_handler_and_tool() -> None:
    """Inventory args stay visible to strict MCP schemas (gr563563)."""
    from precis.handlers.memory import MemoryHandler
    from precis.tools.core import search

    for verb in (MemoryHandler.search, search):
        parameters = signature(verb).parameters
        for name in ("space", "min_chars", "min_reads"):
            assert name in parameters
            assert parameters[name].default is None


def test_memory_process_arguments_reach_the_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        assert verb == "put"
        captured.update(payload)
        return "queued"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    for verb in (MemoryHandler.put, tools_core.put):
        parameters = signature(verb).parameters
        for name in ("stages", "action", "wait_s"):
            assert name in parameters
            assert parameters[name].default is None
    tools_core.put(
        kind="memory",
        id=42,
        mode="process",
        stages=["embeddings"],
        action="promote",
        wait_s=0,
    )
    assert captured["mode"] == "process"
    assert captured["stages"] == ["embeddings"]
    assert captured["action"] == "promote"
    assert captured["wait_s"] == 0


@pytest.mark.parametrize("via_slim", [False, True])
def test_memory_process_survives_runtime_defaults_and_args(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    via_slim: bool,
) -> None:
    from precis.tools.mcp_slim import make_slim_verb
    from tests.conftest import id_of

    runtime = runtime_with_store
    handler = MemoryHandler(hub=runtime.hub)
    ref_id = id_of(
        handler.put(text="Processing fixture body", title="Parity memory").body
    )
    runtime.default_tags_resolved = ("processing-session-default",)
    monkeypatch.setattr(tools_core, "_runtime", runtime)
    verb = make_slim_verb("put", tools_core.put) if via_slim else tools_core.put
    result = verb(
        kind="memory",
        id=ref_id,
        mode="process",
        args={"stages": ["embeddings"], "action": "promote", "wait_s": 0},
    )
    assert isinstance(result, str), result
    assert "[error:" not in result
    assert "embeddings: queued" in result
    with runtime.live_store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM ref_tags rt JOIN tags t USING(tag_id) "
                "WHERE rt.ref_id=%s AND t.value='processing-session-default'",
                (ref_id,),
            ).fetchone()
            is None
        )


# (kind, verb) -> the exact value that kind's KindSpec should declare.
# Absence from this table means "expect None" (no mode= concept) for
# that (kind, verb) pair. An entry of `()` means "recognises mode= but
# rejects every value" — a deliberately different state from absence.
_EXPECTED_MODES: dict[tuple[str, str], tuple[str, ...] | None] = {
    # Private learning appends check-ins without a mode; only settings
    # replacement is exposed by edit. The common mode gate enforces both.
    ("learning", "put"): (),
    ("learning", "edit"): ("replace",),
    # -- put: file kinds, creation-only -------------------------------
    ("markdown", "put"): ("create",),
    ("plaintext", "put"): ("create",),
    ("tex", "put"): ("create",),
    ("python", "put"): ("create",),
    # -- put: paid-cache kinds, free web-UI import --------------------
    ("websearch", "put"): ("import",),
    ("perplexity-reasoning", "put"): ("import",),
    ("perplexity-research", "put"): ("import",),
    # -- put: gripe's mode= is a redundant-but-unambiguous confirmation of
    #    what id= already selects — 'create' (no id=) or 'comment' (id=<n>);
    #    a mismatch is BadInput (gr338441) -----------------------------
    ("gripe", "put"): ("create", "comment"),
    # -- put: pathway frames the reaction network without ML compute;
    #    'recompute_kinetics' mints autocatpath_kinetics for a stored
    #    config (aggregate_job) ------------------------------------------
    ("pathway", "put"): (
        "preview",
        "revalidate",
        "recompute_kinetics",
        "verify",
        "measure_modes",
        "export",
    ),
    # -- put: structure's recognised modes are the deliberate-rerun
    #    door (put(id=<slug-or-handle>, mode='rerun')) onto the quest
    #    compute lane (gr499904), the CI-NEB edge ('neb'), the NVT
    #    Langevin MD run ('md', struct_md job) and the audited harvest
    #    trust-stamp correction ('trust', gr592893) — every other value
    #    is rejected -----------------------------------------------
    ("structure", "put"): ("rerun", "neb", "md", "trust"),
    # -- put: job's recognised modes are the retry surface
    #    (put(id=N, mode='retry')), the queued-pin move (mode='retarget')
    #    and the audited claim.* override door (mode='setting', gr556919);
    #    the audited wall/deadline extension door (mode='extend',
    #    td626662); every other value is rejected — a real vocabulary,
    #    not the reject-all `()` shape (gr343755) -------------------------
    ("job", "put"): ("retry", "retarget", "setting", "wake", "extend"),
    # -- fleet: put is the collector report only; edit rewrites the
    #    coordinator-owned fields (assigned/slice/note) -----------------
    ("fleet", "put"): ("report",),
    ("fleet", "edit"): ("replace",),
    # -- put: message recognises mode= but rejects every value outright —
    #    messages are immutable once queued, so there's no create/import
    #    axis at all. This is the canonical `()` (not None) example the
    #    gr343755 sentinel split exists for ---------------------------
    ("message", "put"): (),
    ("memory", "put"): ("process",),
    # -- put: paper's sole mode queues an SI discovery+fetch for an
    #    existing paper (put(id=<slug>, mode='fetch-si')); any other value
    #    is BadInput (si-attachments build 1) -------------------------
    ("paper", "put"): ("fetch-si",),
    # -- put: measure's sole mode is the hand-run extraction-guard
    #    recheck (put(mode='recheck', args={'papers': […]}) — a dry-run
    #    lister over stored table_recipe rows; audited: the handler
    #    refuses any other mode and writes nothing under it (gr506762
    #    subset-4) -------------------------------------------------
    ("measure", "put"): ("recheck",),
    # -- put: machine's sole recognised mode is the fleet-census sync
    #    (put(mode='sync') upserts one ref per host_heartbeat row);
    #    every other value is BadInput and no-mode-with-id= is the
    #    manual one-ref upsert (gr549585) -----------------------------
    ("machine", "put"): ("sync",),
    # -- edit: file kinds, the region-rewrite grammar -------------------
    ("markdown", "edit"): ("find-replace", "append", "insert", "replace"),
    ("plaintext", "edit"): ("find-replace", "append", "insert", "replace"),
    ("tex", "edit"): ("find-replace", "append", "insert", "replace"),
    ("python", "edit"): ("find-replace", "append", "insert", "replace"),
    # -- edit: numeric-ref kinds that only accept a full-body rewrite ---
    # todo 'gate' (gr597921): fill a pending sha-in-prod gate's args.sha —
    # the lander's post-land write for a slice landed by hand.
    ("todo", "edit"): ("replace", "find-replace", "insert", "delete", "gate"),
    ("memory", "edit"): ("replace", "find-replace", "insert", "append", "delete"),
    ("quest", "edit"): ("replace", "pause", "resume"),
    # finding body edits: whole-body replace or find-replace (gr509620)
    ("finding", "edit"): ("replace", "find-replace"),
    # checklist's edit() selector is op=, a distinct kwarg from mode=
    # (edit() has no mode= parameter at all) — no entry here, expect
    # None (gr343755: a stray edit_modes=_EDIT_OPS previously conflated
    # the op= vocabulary with the mode= field this test audits).
}


def test_paper_retire_force_is_an_edit_arg_not_a_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.paper import PaperHandler
    from precis.tools import core

    captured: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        assert verb == "edit"
        captured.update(payload)
        return "captured"

    monkeypatch.setattr(core, "_dispatch", capture)
    assert (
        core.edit(
            kind="paper",
            id="pa1",
            args={"retire": True, "force": True, "reason": "unwanted upload"},
        )
        == "captured"
    )
    assert "force" not in captured
    assert captured.get("reason") is None
    assert captured["__extras__"] == {
        "retire": True,
        "force": True,
        "reason": "unwanted upload",
    }
    assert "force" in PaperHandler.ARGS_KEYS["edit"]
    assert PaperHandler.spec.edit_modes is None


def test_pres_reextract_args_reach_edit_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.handlers.presentation import PresentationHandler

    captured: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        assert verb == "edit"
        captured.update(payload)
        return "captured"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    assert (
        tools_core.edit(kind="pres", id="pr1", args={"reextract": True}) == "captured"
    )
    assert captured["__extras__"] == {"reextract": True}
    assert "args" in signature(tools_core.edit).parameters
    assert "args" in signature(PresentationHandler.edit).parameters
    assert PresentationHandler.ARGS_KEYS["edit"] == frozenset({"reextract"})
    assert PresentationHandler.spec.edit_modes is None


@pytest.fixture
def full_hub(store: Store) -> Iterator[Hub]:
    """Every store-backed handler registered — the real dispatch registry,
    not a hand-picked subset (mirrors ``test_mcp_verb_kwarg_parity.py``)."""
    yield boot(store=store)


def test_declared_modes_match_the_audited_table(full_hub: Hub) -> None:
    """Every registered kind's ``spec.modes`` / ``spec.edit_modes`` equals
    the hand-audited expectation exactly — no silent drift either way."""
    mismatches: list[str] = []
    for kind in sorted(full_hub.handlers):
        spec = full_hub.handlers[kind].spec
        for verb, declared in (("put", spec.modes), ("edit", spec.edit_modes)):
            expected = _EXPECTED_MODES.get((kind, verb))
            if declared != expected:
                mismatches.append(
                    f"{kind}.{verb}: declared={declared!r} expected={expected!r}"
                )
    assert not mismatches, (
        "KindSpec modes/edit_modes drifted from the audited table — either "
        "a handler's mode-branching changed without updating its KindSpec "
        "declaration, or _EXPECTED_MODES here is stale:\n" + "\n".join(mismatches)
    )


def test_declared_set_only_for_branching_handlers(full_hub: Hub) -> None:
    """A non-``None`` declared value (``()`` or a real vocabulary) only
    ever appears where ``_EXPECTED_MODES`` says a handler branches on
    ``mode=`` — every other kind stays at the ``None`` default
    (self-check: catches a stray ``modes=``/``edit_modes=`` added to a
    kind without also auditing + recording its behaviour here)."""
    stray: list[str] = []
    for kind in sorted(full_hub.handlers):
        spec = full_hub.handlers[kind].spec
        for verb, declared in (("put", spec.modes), ("edit", spec.edit_modes)):
            if declared is not None and (kind, verb) not in _EXPECTED_MODES:
                stray.append(f"{kind}.{verb}: declared={declared!r}, not audited")
    assert not stray, (
        "non-None modes/edit_modes with no _EXPECTED_MODES entry — audit "
        "the handler's put()/edit() and add it to the table above:\n" + "\n".join(stray)
    )


def test_todo_memory_edit_replace_only_and_gripe_put_create_comment(
    full_hub: Hub,
) -> None:
    """The three kinds gr292913 named explicitly, pinned individually so a
    regression here fails with a sharp, single-kind message.

    gripe's row changed under gr338441: it used to declare no accepted
    put mode= at all; now 'create'/'comment' are accepted as an
    optional, id-presence-must-agree confirmation (see
    ``GripeHandler._validate_put_mode``)."""
    todo = full_hub.handlers["todo"].spec
    memory = full_hub.handlers["memory"].spec
    gripe = full_hub.handlers["gripe"].spec
    assert todo.edit_modes == ("replace", "find-replace", "insert", "delete", "gate")
    assert memory.edit_modes == (
        "replace",
        "find-replace",
        "insert",
        "append",
        "delete",
    )
    assert gripe.modes == ("create", "comment")
    # gripe's edit verb is title-only (Reto ruling 2026-10-19) — it takes
    # no mode=, so edit_modes stays the None ("no concept") default, not
    # the reject-all `()` (gr343755).
    assert gripe.supports_edit is True
    assert gripe.edit_modes is None


def test_job_retry_and_message_reject_all_are_distinct_sentinels(
    full_hub: Hub,
) -> None:
    """gr343755's canonical pair: job genuinely has one mode (a real,
    non-empty vocabulary); message recognises mode= but rejects every
    value (the `()` sentinel) — neither collapses into "no concept"
    (``None``), and the two must stay visibly distinct from each
    other."""
    job = full_hub.handlers["job"].spec
    message = full_hub.handlers["message"].spec
    assert job.modes == ("retry", "retarget", "setting", "wake", "extend")
    assert message.modes == ()
    assert job.modes is not None
    assert message.modes is not None
    assert job.modes != message.modes


def test_expected_modes_table_kinds_are_still_registered(full_hub: Hub) -> None:
    """Self-check: the audit table isn't vacuous — at least the always-on
    numeric-ref kinds it names are live. The file kinds (markdown/
    plaintext/tex/python) are env-gated on ``PRECIS_ROOT`` (see
    ``KindSpec.requires_env``) and legitimately absent unless a test sets
    it — the two parity checks above already only ever walk what's
    actually registered, so their absence here doesn't weaken this
    guard, just narrows the "still current" self-check to what's
    unconditionally live."""
    always_on = {"todo", "memory", "quest", "gripe", "job", "message"}
    missing = sorted(always_on - set(full_hub.handlers))
    assert not missing, f"audited kind(s) no longer registered: {missing}"


def test_memory_delete_options_are_declared_on_tool_and_handler() -> None:
    """Retirement kwargs must survive strict MCP schemas and the extras gate."""
    for name in ("dry_run", "superseded_by", "force"):
        for function in (tools_core.delete, MemoryHandler.delete):
            parameter = inspect.signature(function).parameters[name]
            assert parameter.kind != inspect.Parameter.VAR_KEYWORD


@pytest.mark.parametrize("via_args", [False, True])
def test_memory_delete_options_reach_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch, via_args: bool
) -> None:
    seen: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        seen["verb"] = verb
        seen["payload"] = payload
        return "captured"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    options: dict[str, Any] = {
        "dry_run": True,
        "superseded_by": "td123",
        "force": True,
    }
    if via_args:
        result = tools_core.delete(kind="memory", id=42, args=options)
    else:
        result = tools_core.delete(kind="memory", id=42, **options)
    assert result == "captured"
    assert seen["verb"] == "delete"
    payload = seen["payload"]
    forwarded = payload["__extras__"] if via_args else payload
    for name, value in options.items():
        assert forwarded[name] == value


@pytest.mark.parametrize("via_args", [False, True])
def test_memory_delete_options_pass_runtime_validation(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch, via_args: bool
) -> None:
    """The real dispatcher must accept options, not merely package them."""
    runtime = runtime_with_store
    store = runtime.live_store
    old = store.insert_ref(kind="memory", slug=None, title="Retirement parity old")
    successor = store.insert_ref(
        kind="memory", slug=None, title="Retirement parity successor"
    )
    monkeypatch.setattr(tools_core, "_runtime", runtime)
    options: dict[str, Any] = {
        "dry_run": True,
        "superseded_by": f"me{successor.id}",
        "force": True,
    }

    if via_args:
        result = tools_core.delete(kind="memory", id=old.id, args=options)
    else:
        result = tools_core.delete(kind="memory", id=old.id, **options)

    assert isinstance(result, str), result
    assert "[error:" not in result
    assert "dry" in result.lower()
    assert f"me{successor.id}" in result
    ref = store.fetch_refs_by_ids([old.id])[old.id]
    assert ref.retired_at is None
    assert "superseded_by" not in ref.meta


def test_kinetics_summary_views_and_args_are_declared() -> None:
    from precis.handlers.quest import _QUEST_CONCRETE_VIEWS, QuestHandler
    from precis_pathway.handler import PathwayHandler

    assert "kinetics-summary" in PathwayHandler.spec.views
    assert "kinetics-summary" in _QUEST_CONCRETE_VIEWS
    for handler in (PathwayHandler, QuestHandler):
        assert {"axis", "page_size", "offset"} <= handler.ARGS["get"].keys
    # gr592637: 'class' stays declared only on the pathway side, where
    # kinetics-summary still accepts the legacy axis=value selector; quest
    # dropped it — the lens 'where' dict is the one spelling there.
    assert "class" in PathwayHandler.ARGS["get"].keys
    assert "class" not in QuestHandler.ARGS["get"].keys


@pytest.mark.parametrize("in_args", [False, True])
def test_paper_stub_authors_reach_dispatch_without_a_new_mode(
    full_hub: Hub, monkeypatch: pytest.MonkeyPatch, in_args: bool
) -> None:
    seen: dict[str, Any] = {}

    def capture(verb: str, payload: dict[str, Any], ctx: object = None) -> str:
        assert verb == "put"
        seen.update(payload)
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", capture)
    authors: list[str | dict[str, Any]] = ["Doe, Jane"]
    if in_args:
        assert (
            tools_core.put(kind="paper", title="A book", args={"authors": authors})
            == "ok"
        )
        assert seen["__extras__"]["authors"] == authors
    else:
        assert tools_core.put(kind="paper", title="A book", authors=authors) == "ok"
        assert seen["authors"] == authors
    assert seen["mode"] is None
    paper = full_hub.handlers["paper"]
    assert "authors" in paper.ARGS_KEYS["put"]
    assert paper.spec.modes == ("fetch-si",)


def test_pathway_objective_is_a_declared_read_view() -> None:
    """gr559655: the stored-point read must pass declared-view validation."""
    from precis_pathway.handler import PathwayHandler

    assert "objective" in PathwayHandler.spec.views
    assert "objective" not in (PathwayHandler.spec.modes or ())
