"""Phase 12a — QuestHandler read surface.

The handler reuses ``acatome_quest_mcp.db.DB`` + models.  Tests bypass the
live PG pool by injecting a fake DB via :func:`_set_db_for_testing`.  Both
the handler and the fake are fully synchronous after the April 2026
``psycopg3`` rewrite.

Covers:

1.  Bare list + single-id read surface (full UUID + short prefix)
2.  Registry views (``/recent``, ``/queued``, ``/needs-user``, ``/failed``,
    ``/ingesting``, ``/agent/<id>``)
3.  Sub-selector views (``<id>/candidates``, ``<id>/misconceptions``)
4.  ``/help`` wired to skill body
5.  Error shapes (ID_MALFORMED, ID_NOT_FOUND, ID_AMBIGUOUS, VIEW_UNKNOWN,
    PARAM_INVALID on kwarg typos)
6.  Registration in the plugin registry
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

pytest.importorskip("acatome_quest_mcp", reason="precis-mcp[quest] not installed")

from acatome_quest_mcp.misconceptions import Misconception, MisconceptionCode
from acatome_quest_mcp.models import (
    Candidate,
    PaperRef,
    PaperRequest,
    RequestStatus,
    ResolvedRef,
)

from precis.handlers.quest import QuestHandler
from precis.protocol import ErrorCode, PrecisError

# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------


class FakeDB:
    """Sync-behaving mock that returns PaperRequest fixtures.

    The handler expects ``.get(uid)``, ``.find(...)``, and ``.pool`` (used
    only for the short-prefix lookup path — we patch
    ``_db_find_by_prefix`` directly in those tests).
    """

    def __init__(self, requests: list[PaperRequest]) -> None:
        self.requests = requests
        self.schema = "papers"
        self.pool = None  # not used when we override _db_find_by_prefix

    def get(self, uid: UUID) -> PaperRequest | None:
        for r in self.requests:
            if r.id == uid:
                return r
        return None

    def find(
        self,
        *,
        status=None,
        created_by=None,
        has_misconception=None,
        source_document=None,
        limit=100,
    ) -> list[PaperRequest]:
        out = list(self.requests)
        if status is not None:
            s = status.value if hasattr(status, "value") else str(status)
            out = [r for r in out if r.status.value == s]
        if created_by is not None:
            out = [r for r in out if r.created_by == created_by]
        if has_misconception is True:
            out = [r for r in out if r.misconceptions]
        elif has_misconception is False:
            out = [r for r in out if not r.misconceptions]
        if source_document is not None:
            out = [
                r for r in out if (r.source or {}).get("document") == source_document
            ]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[:limit]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_request(
    *,
    uid: UUID,
    status: RequestStatus,
    title: str = "Test Paper",
    authors: list[str] | None = None,
    year: int = 2024,
    doi: str | None = None,
    created_by: str | None = None,
    candidates: list[Candidate] | None = None,
    misconceptions: list[Misconception] | None = None,
    created_at: datetime | None = None,
) -> PaperRequest:
    ts = created_at or datetime.now(UTC)
    return PaperRequest(
        id=uid,
        created_at=ts,
        updated_at=ts,
        created_by=created_by,
        source={},
        input=PaperRef(doi=doi, title=title, authors=authors or []),
        resolved=ResolvedRef(
            doi=doi,
            title=title,
            authors=authors or [],
            year=year,
            score=1.0 if doi else 0.3,
            source="crossref",
        ),
        candidates=candidates or [],
        status=status,
        misconceptions=misconceptions or [],
        attempts=[],
        priority=0,
        not_before=ts,
    )


_UID_QUEUED = UUID("11111111-1111-1111-1111-111111111111")
_UID_NEEDS_USER = UUID("22222222-2222-2222-2222-222222222222")
_UID_FAILED = UUID("33333333-3333-3333-3333-333333333333")
_UID_INGESTING = UUID("44444444-4444-4444-4444-444444444444")
_UID_INGESTED = UUID("55555555-5555-5555-5555-555555555555")
_UID_ASA_1 = UUID("66666666-6666-6666-6666-666666666666")
_UID_ASA_2 = UUID("77777777-7777-7777-7777-777777777777")


def _candidate(doi: str, title: str, score: float = 0.7) -> Candidate:
    return Candidate(
        ref=ResolvedRef(
            doi=doi,
            title=title,
            authors=["Smith"],
            year=2024,
            score=score,
            source="crossref",
        ),
        reason="title-match",
    )


@pytest.fixture
def fake_db() -> FakeDB:
    reqs = [
        _mk_request(
            uid=_UID_QUEUED,
            status=RequestStatus.QUEUED,
            title="Queued paper",
            doi="10.1021/jacs.q",
        ),
        _mk_request(
            uid=_UID_NEEDS_USER,
            status=RequestStatus.NEEDS_USER,
            title="Ambiguous title",
            candidates=[
                _candidate("10.x/alpha", "Alpha paper"),
                _candidate("10.x/beta", "Beta paper"),
            ],
        ),
        _mk_request(
            uid=_UID_FAILED,
            status=RequestStatus.FAILED,
            title="Could not fetch",
            doi="10.x/fail",
        ),
        _mk_request(
            uid=_UID_INGESTING,
            status=RequestStatus.INGESTING,
            title="Landing soon",
            doi="10.x/in",
        ),
        _mk_request(
            uid=_UID_INGESTED,
            status=RequestStatus.INGESTED,
            title="Already here",
            doi="10.x/done",
            misconceptions=[
                Misconception.of(
                    MisconceptionCode.RETRACTED,
                    evidence="Retraction Watch, 2024",
                ),
            ],
        ),
        _mk_request(
            uid=_UID_ASA_1,
            status=RequestStatus.QUEUED,
            title="Asa first",
            doi="10.x/a1",
            created_by="asa",
        ),
        _mk_request(
            uid=_UID_ASA_2,
            status=RequestStatus.NEEDS_USER,
            title="Asa second",
            created_by="asa",
        ),
    ]
    return FakeDB(reqs)


@pytest.fixture
def handler(fake_db, monkeypatch) -> QuestHandler:
    h = QuestHandler(db=fake_db)

    # Patch _db_find_by_prefix to use FakeDB.requests directly.
    def fake_prefix(prefix: str):
        pref = prefix.lower()
        return [r for r in fake_db.requests if str(r.id).startswith(pref)]

    monkeypatch.setattr(h, "_db_find_by_prefix", fake_prefix)
    return h


# ---------------------------------------------------------------------------
# Thin wrapper for read()
# ---------------------------------------------------------------------------


def _read(
    h: QuestHandler,
    path: str = "",
    *,
    selector: str | None = None,
    view: str | None = None,
    query: str = "",
) -> str:
    return h.read(
        path=path,
        selector=selector,
        view=view,
        subview=None,
        query=query,
        summarize=False,
        depth=0,
        page=1,
    )


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestBareAndSingleId:
    def test_bare_call_returns_recent(self, handler):
        out = _read(handler)
        assert "Recent quests" in out
        assert "quest:11111111" in out  # queued
        assert "quest:55555555" in out  # ingested

    def test_single_id_full_uuid(self, handler):
        out = _read(handler, path=str(_UID_QUEUED))
        assert "quest:11111111" in out
        assert "Queued paper" in out
        assert "queued" in out

    def test_single_id_short_prefix(self, handler):
        out = _read(handler, path="22222222")
        assert "Ambiguous title" in out
        assert "needs_user" in out
        assert "2 candidates" in out

    def test_single_id_unknown_full_uuid(self, handler):
        missing = UUID("99999999-9999-9999-9999-999999999999")
        with pytest.raises(PrecisError) as exc_info:
            _read(handler, path=str(missing))
        assert exc_info.value.code is ErrorCode.ID_NOT_FOUND

    def test_single_id_unknown_prefix(self, handler):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler, path="abcdef12")
        assert exc_info.value.code is ErrorCode.ID_NOT_FOUND

    def test_single_id_malformed(self, handler):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler, path="not-a-uuid-zzz")
        assert exc_info.value.code is ErrorCode.ID_MALFORMED


class TestRegistryViews:
    def test_recent_view(self, handler):
        out = _read(handler, path="/recent")
        assert "Recent quests" in out
        # All 7 fixture requests should appear.
        for short_id in (
            "11111111",
            "22222222",
            "33333333",
            "44444444",
            "55555555",
            "66666666",
            "77777777",
        ):
            assert f"quest:{short_id}" in out

    def test_queued_view(self, handler):
        out = _read(handler, path="/queued")
        assert "Queued quests" in out
        assert "quest:11111111" in out  # queued
        assert "quest:66666666" in out  # queued (asa)
        assert "quest:33333333" not in out  # failed

    def test_needs_user_view(self, handler):
        out = _read(handler, path="/needs-user")
        assert "awaiting user input" in out
        assert "quest:22222222" in out
        assert "quest:77777777" in out
        # Non-needs_user statuses not shown.
        assert "quest:11111111" not in out
        # Helpful next-steps included when backlog is non-empty.
        assert "skill:quest-disambiguate" in out

    def test_needs_user_empty(self, handler, fake_db):
        fake_db.requests = [
            r for r in fake_db.requests if r.status != RequestStatus.NEEDS_USER
        ]
        out = _read(handler, path="/needs-user")
        assert "empty" in out.lower()
        # No skill pointer when nothing needs attention.
        assert "skill:quest-disambiguate" not in out

    def test_failed_view_merges_failed_and_extract_failed(self, handler):
        out = _read(handler, path="/failed")
        assert "Failed quests" in out
        assert "quest:33333333" in out

    def test_ingesting_view(self, handler):
        out = _read(handler, path="/ingesting")
        assert "In-flight quests" in out
        assert "quest:44444444" in out
        # Only fetching + ingesting, not queued.
        assert "quest:11111111" not in out

    def test_agent_view(self, handler):
        out = _read(handler, path="/agent/asa")
        assert "asa" in out
        assert "quest:66666666" in out
        assert "quest:77777777" in out
        # Requests from other creators absent.
        assert "quest:11111111" not in out

    def test_agent_view_missing_name(self, handler):
        """Bare /agent without an id should raise PARAM_INVALID."""
        with pytest.raises(PrecisError) as exc_info:
            _read(handler, path="/agent")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_unknown_view_raises(self, handler):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler, path="/wibble")
        assert exc_info.value.code is ErrorCode.VIEW_UNKNOWN


class TestSubSelectorViews:
    def test_candidates_via_path_suffix(self, handler):
        out = _read(handler, path="22222222/candidates")
        assert "2 candidates" in out
        assert "[0] Alpha paper" in out
        assert "[1] Beta paper" in out
        assert "mode='confirm'" in out

    def test_candidates_empty_when_none_present(self, handler):
        out = _read(handler, path="11111111/candidates")  # queued, no candidates
        assert "no candidates" in out

    def test_misconceptions_view(self, handler):
        out = _read(handler, path="55555555/misconceptions")
        assert "1 misconceptions" in out
        assert "retracted" in out
        assert "Retraction Watch" in out
        assert "skill:quest-disambiguate" in out

    def test_misconceptions_empty(self, handler):
        out = _read(handler, path="11111111/misconceptions")
        assert "no misconceptions" in out


class TestHelpView:
    def test_help_renders_find_paper_skill(self, handler):
        out = _read(handler, path="help", view="help")
        assert "skill:find-paper" in out
        assert "three-step loop" in out.lower()


class TestSearch:
    def test_search_via_query_returns_matches(self, handler):
        out = _read(handler, query="queued")
        assert "Queued paper" in out

    def test_search_case_insensitive(self, handler):
        out = _read(handler, query="QUEUED")
        assert "Queued paper" in out

    def test_search_no_matches(self, handler):
        out = _read(handler, query="no-such-title")
        assert "0" in out or "empty" in out.lower()

    def test_search_verb(self, handler):
        out = handler.search(query="queued", top_k=5)
        assert "Queued paper" in out


class TestOnboarding:
    def test_quest_declares_find_paper(self):
        assert QuestHandler.onboarding_skill == "find-paper"

    def test_views_dict_shape(self):
        # Dict, not set — for the enricher's options lookup.
        assert isinstance(QuestHandler.views, dict)
        assert "help" in QuestHandler.views
        assert "recent" in QuestHandler.views


class TestRegistration:
    def test_quest_kind_registered(self):
        from precis.registry import KINDS, _discover

        _discover()
        assert "quest" in KINDS

    def test_quest_scheme_resolvable(self):
        from precis.registry import _reset_instance_cache, resolve

        # Cache may hold an instance populated from a prior test's DB;
        # reset so we build a pristine QuestHandler() here.
        _reset_instance_cache()
        try:
            handler = resolve("quest", path="/recent")
            assert isinstance(handler, QuestHandler)
        finally:
            _reset_instance_cache()

    def test_resolve_returns_cached_instance(self):
        """resolve() memoises handler instances so state + warm pools are reused."""
        from precis.registry import _reset_instance_cache, resolve

        _reset_instance_cache()
        try:
            first = resolve("quest", path="/recent")
            second = resolve("quest", path="/queued")
            assert first is second
        finally:
            _reset_instance_cache()

    def test_find_paper_skill_bundled(self):
        """The find-paper skill ships with the package — verify discoverable."""
        from precis.handlers.skill import SkillHandler

        sh = SkillHandler()
        sh._ensure_fresh()
        assert "find-paper" in sh._index
        assert "quest" in sh._index["find-paper"].applies_to
