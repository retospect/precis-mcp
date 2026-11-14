"""The private-kind registry generalises the learning-only private route.

A second registered private kind gets learning's privacy boundary — the
classifier, the credential allowlists, the audit redaction and the private
dispatch — from one registry entry, with no per-kind literals in the gates.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import Mock

import pytest

from precis import private_kinds
from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import Denied
from precis.learning_privacy import is_learning_call
from precis.oauth.store import OAuthIdentity
from precis.private_kinds import PrivateKind, private_kind_of
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.tools.core import _log_tool_call, _personal_call_allowed
from precis.user_context import user_scope


class _JournalHandler(Handler):
    """A stub private kind; only its KindSpec reaches the privacy gates."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="journal",
        title="Private journal",
        description="Owner-scoped journal entries.",
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
        supports_chunk_selectors=False,
        placement="system",
        modes=(),
        edit_modes=("replace",),
    )
    ARGS: ClassVar[dict[str, ArgsSpec]] = {"get": ArgsSpec({"days"})}

    def get(
        self,
        *,
        id: str | int | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        return Response(body="private journal body")


_JOURNAL = PrivateKind(
    kind="journal",
    help_skill="precis-journal-help",
    oauth_scope="journal",
    handler=f"{_JournalHandler.__module__}:{_JournalHandler.__qualname__}",
    next_hint="get(kind='journal', id='/recent')",
)


@pytest.fixture
def journal_kind(monkeypatch: pytest.MonkeyPatch) -> PrivateKind:
    monkeypatch.setitem(private_kinds.PRIVATE_KINDS, "journal", _JOURNAL)
    return _JOURNAL


def _runtime(monkeypatch: pytest.MonkeyPatch) -> PrecisRuntime:
    runtime = PrecisRuntime(config=PrecisConfig(), hub=Hub())
    monkeypatch.setattr("precis.user_context.require_user", lambda _store: 7)
    return runtime


def _oauth(scopes: tuple[str, ...]) -> OAuthIdentity:
    return OAuthIdentity(
        user_id=7,
        grant_id=3,
        access_token_id=9,
        client_id="test-client",
        scopes=scopes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_registered_kind_classifies_in_all_spellings(
    journal_kind: PrivateKind,
) -> None:
    """me663455: private_kind_of must recognize a second registered kind in the
    explicit, combined-kind and canonical-prefix spellings so its payloads take
    the private route exactly as learning's do."""
    for payload in (
        {"kind": "journal"},
        {"kind": "paper,journal"},
        {"kind": " JOURNAL "},
        {"id": "journal:jo1"},
        {"id": ["journal:jo1"]},
    ):
        assert private_kind_of(payload) == "journal"


def test_public_kinds_stay_unclassified(journal_kind: PrivateKind) -> None:
    """me663455: registry membership must not widen classification — paper,
    memory and kindless payloads still return None and keep public dispatch."""
    assert private_kind_of({"kind": "paper", "id": "pa1"}) is None
    assert private_kind_of({"kind": "memory"}) is None
    assert private_kind_of({}) is None


def test_personal_token_reaches_registered_kind_and_help(
    journal_kind: PrivateKind,
) -> None:
    """me663455: a personal MCP token is allowed any registered private kind's
    calls and that kind's exact help-skill read, and refused every other
    kind — the boundary follows the registry, not a learning literal."""
    with user_scope(7, 11):
        assert _personal_call_allowed("get", {"kind": "journal"})
        assert _personal_call_allowed("put", {"id": "journal:jo1"})
        assert _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-journal-help"}
        )
        assert _personal_call_allowed("get", {"kind": "profile", "id": "me"})
        assert not _personal_call_allowed("get", {"kind": "paper", "id": "pa1"})
        assert not _personal_call_allowed("get", {"kind": "memory"})
        assert not _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-overview"}
        )


def test_oauth_scope_gates_registered_kind(journal_kind: PrivateKind) -> None:
    """me663455: an OAuth connection without mcp:full reaches a private kind
    only when that kind's own oauth_scope is granted — a learning-scoped grant
    must not open journal, and a journal grant must not open learning."""
    with user_scope(7, oauth=_oauth(("learning",))):
        assert not _personal_call_allowed("get", {"kind": "journal"})
        assert not _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-journal-help"}
        )
        assert _personal_call_allowed("get", {"kind": "learning"})
    with user_scope(7, oauth=_oauth(("journal",))):
        assert _personal_call_allowed("get", {"kind": "journal"})
        assert _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-journal-help"}
        )
        assert _personal_call_allowed("get", {"kind": "profile", "id": "me"})
        assert _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-oauth-help"}
        )
        assert not _personal_call_allowed("get", {"kind": "learning"})
        assert not _personal_call_allowed(
            "get", {"kind": "skill", "id": "precis-learning-help"}
        )
        assert not _personal_call_allowed("get", {"kind": "paper", "id": "pa1"})


def test_oauth_denied_text_names_registry_not_input(
    journal_kind: PrivateKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me663455: the personal-scope Denied is built from the registry and never
    echoes caller input, so a private payload's fields cannot leak through the
    refusal text."""
    from precis.tools.core import _require_personal_scope

    with user_scope(7, 11):
        with pytest.raises(Denied) as caught:
            _require_personal_scope("get", {"kind": "paper", "id": "SECRET"})
    assert "learning" in str(caught.value) and "journal" in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in (caught.value.next or "")


def test_audit_sample_redacts_registered_kind(
    journal_kind: PrivateKind, caplog: pytest.LogCaptureFixture
) -> None:
    """me663455: the shared audit line for a call to a registered private kind
    must contain only the kind name — ids, queries and free text stay out of
    the sample on either the success or error path."""
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="get",
            payload={"kind": "journal", "id": "journal:jo1", "q": "private query"},
            duration_ms=1.0,
            error=False,
        )
    args = caplog.records[-1].args
    assert isinstance(args, tuple)
    assert args[-1] == {"kind": "journal"}


def test_registered_kind_dispatches_privately_never_shared_ledger(
    journal_kind: PrivateKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me663455: a registered private kind routes through dispatch_private and
    never the shared dispatch — no tool-call ledger row, no search-feedback
    fold — with the handler resolved under its own kind name."""
    runtime = _runtime(monkeypatch)
    record = Mock(side_effect=AssertionError("private content entered shared ledger"))
    monkeypatch.setattr(runtime, "_record_tool_call", record)
    public = Mock(side_effect=AssertionError("private kind reached public dispatch"))
    resolved = Mock(return_value=object())
    monkeypatch.setattr(runtime, "_dispatch_status", public)
    monkeypatch.setattr(runtime, "_resolve_handler", resolved)
    monkeypatch.setattr(runtime, "_validate_mode", lambda *_a: None)
    monkeypatch.setattr(
        runtime,
        "_invoke_handler",
        lambda *_a, **_k: Response(body="private route"),
    )
    body, error = runtime.dispatch_with_status(
        "get", {"kind": "journal", "id": "jo1"}, fold=True
    )
    assert not error and body == "private route"
    public.assert_not_called()
    record.assert_not_called()
    assert resolved.call_args.args[0] == "journal"


def test_registered_kind_refusals_carry_its_hint(
    journal_kind: PrivateKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me663455: private-route refusals name the registered kind and its own
    next hint, never a leftover learning literal."""
    runtime = _runtime(monkeypatch)
    owner = Mock(return_value=7)
    resolve = Mock(side_effect=AssertionError("handler reached for unsupported verb"))
    monkeypatch.setattr("precis.user_context.require_user", owner)
    monkeypatch.setattr(runtime, "_resolve_handler", resolve)
    body, error = runtime.dispatch_with_status(
        "search", {"kind": "journal", "q": "private"}
    )
    assert error and "[error:Unsupported]" in body
    assert "journal does not support search" in body
    assert _JOURNAL.next_hint in body and "learning" not in body
    owner.assert_not_called()
    resolve.assert_not_called()


def test_learning_is_the_sole_registered_private_kind() -> None:
    """me663455: the registry holds exactly learning today — the refactor adds
    no second kind and is_learning_call must agree with private_kind_of on
    every spelling tests/test_learning_dispatch.py exercises."""
    assert set(private_kinds.PRIVATE_KINDS) == {"learning"}
    entry = private_kinds.PRIVATE_KINDS["learning"]
    assert entry.help_skill == "precis-learning-help"
    assert entry.oauth_scope == "learning"
    assert entry.handler == "precis.handlers.learning:LearningHandler"
    for payload in (
        {"kind": "learning"},
        {"kind": "paper,learning"},
        {"kind": " LEARNING "},
        {"id": "learning:/activity"},
        {"id": ["learning:le1"]},
        {"kind": "paper", "id": "pa1"},
        {"kind": "memory"},
        {},
    ):
        assert is_learning_call(payload) == (private_kind_of(payload) == "learning")
