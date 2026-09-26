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

from collections.abc import Iterator

import pytest

from precis.dispatch import Hub, boot
from precis.store import Store

# (kind, verb) -> the exact value that kind's KindSpec should declare.
# Absence from this table means "expect None" (no mode= concept) for
# that (kind, verb) pair. An entry of `()` means "recognises mode= but
# rejects every value" — a deliberately different state from absence.
_EXPECTED_MODES: dict[tuple[str, str], tuple[str, ...] | None] = {
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
    # -- put: pathway frames the reaction network without ML compute ----
    ("pathway", "put"): ("preview",),
    # -- put: job's sole recognised mode is the retry surface
    #    (put(id=N, mode='retry')); every other value is rejected — a
    #    real one-element vocabulary, not the reject-all `()` shape
    #    (gr343755) -----------------------------------------------------
    ("job", "put"): ("retry",),
    # -- put: message recognises mode= but rejects every value outright —
    #    messages are immutable once queued, so there's no create/import
    #    axis at all. This is the canonical `()` (not None) example the
    #    gr343755 sentinel split exists for ---------------------------
    ("message", "put"): (),
    # -- edit: file kinds, the region-rewrite grammar -------------------
    ("markdown", "edit"): ("find-replace", "append", "insert", "replace"),
    ("plaintext", "edit"): ("find-replace", "append", "insert", "replace"),
    ("tex", "edit"): ("find-replace", "append", "insert", "replace"),
    ("python", "edit"): ("find-replace", "append", "insert", "replace"),
    # -- edit: numeric-ref kinds that only accept a full-body rewrite ---
    ("todo", "edit"): ("replace",),
    ("memory", "edit"): ("replace",),
    ("quest", "edit"): ("replace",),
    # checklist's edit() selector is op=, a distinct kwarg from mode=
    # (edit() has no mode= parameter at all) — no entry here, expect
    # None (gr343755: a stray edit_modes=_EDIT_OPS previously conflated
    # the op= vocabulary with the mode= field this test audits).
}


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
    assert todo.edit_modes == ("replace",)
    assert memory.edit_modes == ("replace",)
    assert gripe.modes == ("create", "comment")
    # gripe doesn't support edit at all — edit_modes stays the None
    # ("no concept") default, not the reject-all `()` (gr343755).
    assert gripe.supports_edit is False
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
    assert job.modes == ("retry",)
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
