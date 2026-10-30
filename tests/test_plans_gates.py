"""Dark-factory step 3 (td542093): plan gate evaluators and the release
record that feeds ``sha-in-prod`` / ``tests-green``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest

from precis import settings as psettings
from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.plans import gates
from precis.plans.gates import GateResult, evaluate
from precis.store import Store
from tests._script import load_script
from tests.conftest import id_of
from tests.test_round_gate import REPO_ROOT

HEAD = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"
LANDED = ["1111111aaaaa", "2222222bbbbb", "3333333ccccc"]


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def release() -> ModuleType:
    return load_script("release_script_gates", REPO_ROOT / "scripts" / "release")


def _todo(handler: TodoHandler, text: str = "Ask something.") -> int:
    return id_of(handler.put(text=text, meta={"llm_tier": None}).body)


def _record(store: Store, head: str = HEAD, landed: list[str] | None = None) -> None:
    rec = {
        "head": head,
        "deployed_at": "2026-10-30T12:00:00Z",
        "landed": LANDED if landed is None else landed,
    }
    psettings.set_setting(gates.PROD_HEAD_KEY, json.dumps(rec), store=store)


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


# ── not-before ────────────────────────────────────────────────────


def test_not_before_open_when_past(store: Store) -> None:
    res = evaluate(
        store, {"kind": "not-before", "args": {"ts": "2020-01-01T00:00:00Z"}}, todo_id=1
    )
    assert res.open and "past" in res.why


def test_not_before_closed_when_future(store: Store) -> None:
    res = evaluate(
        store, {"kind": "not-before", "args": {"ts": "2999-01-01T00:00:00Z"}}, todo_id=1
    )
    assert not res.open and "waiting until 2999" in res.why


def test_not_before_boundary_uses_injected_clock(store: Store) -> None:
    gate = {"kind": "not-before", "args": {"ts": "2026-10-30T12:00:00+00:00"}}
    before = datetime(2026, 10, 30, 11, 59, 59, tzinfo=UTC)
    at = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    assert not evaluate(store, gate, todo_id=1, now=before).open
    assert evaluate(store, gate, todo_id=1, now=at).open


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"ts": ""},
        {"ts": 12345},
        {"ts": "not a date"},
        {"ts": "2020-01-01T00:00:00"},  # naive: UTC would be a guess
    ],
)
def test_not_before_bad_args_close(store: Store, args: dict[str, Any]) -> None:
    res = evaluate(store, {"kind": "not-before", "args": args}, todo_id=1)
    assert res == GateResult(False, res.why) and res.why.startswith("not-before")


# ── answer ────────────────────────────────────────────────────────


def test_answer_closed_for_open_ask(store: Store, handler: TodoHandler) -> None:
    tid = _todo(handler)
    res = evaluate(store, {"kind": "answer"}, todo_id=tid)
    assert not res.open and f"td{tid}" in res.why


def test_answer_open_when_done(store: Store, handler: TodoHandler) -> None:
    tid = _todo(handler)
    handler.tag(id=tid, add=["STATUS:done"])
    res = evaluate(store, {"kind": "answer", "args": {}}, todo_id=tid)
    assert res.open and "STATUS:done" in res.why


def test_answer_open_on_answer_comment(store: Store, handler: TodoHandler) -> None:
    tid = _todo(handler)
    handler.put(id=tid, text="thinking about it")
    assert not evaluate(store, {"kind": "answer"}, todo_id=tid).open
    handler.put(id=tid, text="  Answer: go with option b")
    res = evaluate(store, {"kind": "answer"}, todo_id=tid)
    assert res.open and "newest comment" in res.why


def test_answer_only_newest_comment_counts(store: Store, handler: TodoHandler) -> None:
    tid = _todo(handler)
    handler.put(id=tid, text="answer: yes")
    handler.put(id=tid, text="actually, wait")
    assert not evaluate(store, {"kind": "answer"}, todo_id=tid).open


def test_answer_missing_todo_closes(store: Store) -> None:
    res = evaluate(store, {"kind": "answer"}, todo_id=987654321)
    assert not res.open and "not found" in res.why


# ── sha-in-prod / tests-green ─────────────────────────────────────


def test_sha_in_prod_closed_without_record(store: Store) -> None:
    res = evaluate(
        store, {"kind": "sha-in-prod", "args": {"sha": "abcdef1"}}, todo_id=1
    )
    assert not res.open and "no prod head recorded" in res.why


@pytest.mark.parametrize(
    "sha", [HEAD[:7], HEAD[:12], HEAD, HEAD.upper(), "1111111", "2222222bbbbb"]
)
def test_sha_in_prod_open_for_head_or_landed(store: Store, sha: str) -> None:
    _record(store)
    res = evaluate(store, {"kind": "sha-in-prod", "args": {"sha": sha}}, todo_id=1)
    assert res.open, res.why


def test_sha_in_prod_long_sha_matches_abbreviated_landed(store: Store) -> None:
    _record(store)
    full = "3333333ccccc" + "0" * 28
    res = evaluate(store, {"kind": "sha-in-prod", "args": {"sha": full}}, todo_id=1)
    assert res.open and "landed in prod" in res.why


def test_sha_in_prod_closed_for_unknown_sha(store: Store) -> None:
    _record(store)
    res = evaluate(
        store, {"kind": "sha-in-prod", "args": {"sha": "9999999"}}, todo_id=1
    )
    assert not res.open and "not in prod yet" in res.why and "ab12cd34e" in res.why


@pytest.mark.parametrize(
    "sha", [None, "", "abc123", "main", "zzzzzzz", 1234567, "a" * 41]
)
def test_sha_in_prod_bad_args_close(store: Store, sha: object) -> None:
    _record(store)
    res = evaluate(store, {"kind": "sha-in-prod", "args": {"sha": sha}}, todo_id=1)
    assert not res.open and res.why.startswith("sha-in-prod")


def test_sha_in_prod_garbled_record_closes(store: Store) -> None:
    psettings.set_setting(gates.PROD_HEAD_KEY, "{not json", store=store)
    res = evaluate(store, {"kind": "sha-in-prod", "args": {"sha": HEAD[:9]}}, todo_id=1)
    assert not res.open and "no prod head recorded" in res.why


def test_tests_green_range_end_in_prod(store: Store) -> None:
    _record(store)
    gate = {"kind": "tests-green", "args": {"range": f"9999999..{HEAD[:9]}"}}
    res = evaluate(store, gate, todo_id=1)
    assert res.open, res.why
    single = {"kind": "tests-green", "args": {"range": "2222222"}}
    assert evaluate(store, single, todo_id=1).open


def test_tests_green_closed_when_end_not_deployed(store: Store) -> None:
    _record(store)
    gate = {"kind": "tests-green", "args": {"range": f"{HEAD[:9]}..9999999"}}
    res = evaluate(store, gate, todo_id=1)
    assert not res.open and "no green deploy covers it" in res.why


@pytest.mark.parametrize("rng", [None, "", "a..", "..main", "HEAD~3..HEAD", 7])
def test_tests_green_bad_args_close(store: Store, rng: object) -> None:
    _record(store)
    res = evaluate(store, {"kind": "tests-green", "args": {"range": rng}}, todo_id=1)
    assert not res.open and res.why.startswith("tests-green")


# ── dispatch ──────────────────────────────────────────────────────


def test_unknown_kind_closes(store: Store) -> None:
    res = evaluate(store, {"kind": "moon-phase"}, todo_id=1)
    assert not res.open and res.why.startswith("unknown gate kind 'moon-phase'")


@pytest.mark.parametrize(
    "gate", [{}, {"kind": None}, {"kind": 5}, "not-before", None, []]
)
def test_malformed_gate_closes(store: Store, gate: Any) -> None:
    res = evaluate(store, gate, todo_id=1)
    assert not res.open and res.why


def test_non_dict_args_close(store: Store) -> None:
    res = evaluate(store, {"kind": "sha-in-prod", "args": "abcdef1"}, todo_id=1)
    assert not res.open and "args must be a dict" in res.why


def test_flat_spec_without_args_key(store: Store) -> None:
    _record(store)
    assert evaluate(store, {"kind": "sha-in-prod", "sha": HEAD[:8]}, todo_id=1).open


def test_store_fault_closes_instead_of_raising() -> None:
    class Boom:
        def get_ref(self, **_: object) -> None:
            raise RuntimeError("db down")

    broken: Any = Boom()
    res = evaluate(broken, {"kind": "answer"}, todo_id=1)
    assert not res.open and "RuntimeError" in res.why


# ── the release record writer ─────────────────────────────────────


class FakeProd:
    """A fake ``scripts/prod-precis`` transport backed by the dev DB's
    settings layer, speaking the ``precis settings get|set`` output format."""

    def __init__(self, store: Store, *, fail_set: bool = False) -> None:
        self.store = store
        self.fail_set = fail_set
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        assert args[:2] in (["settings", "get"], ["settings", "set"])
        key = args[2]
        if args[1] == "get":
            value, layer = psettings.resolve(key, store=self.store)
            return 0, f"{key} = {value} (layer={layer})\n", ""
        if self.fail_set:
            return 1, "", "refused: https://example.invalid/x boom"
        psettings.set_setting(key, args[3], store=self.store)
        return 0, f"settings: set {key} = {args[3]} (db)\n", ""


def test_prod_head_key_matches_registry(release: ModuleType) -> None:
    assert release.PROD_HEAD_KEY == gates.PROD_HEAD_KEY
    assert gates.PROD_HEAD_KEY in psettings.REGISTRY


def test_record_prod_head_writes_the_record_gates_read(
    store: Store, release: ModuleType
) -> None:
    prod = FakeProd(store)
    now = datetime(2026, 10, 30, 12, 34, 56, tzinfo=UTC)
    line = release.record_prod_head(
        "oldoldold", HEAD, transport=prod, now=now, landed=LANDED
    )
    assert line == f"prod head recorded: {HEAD[:9]} (+3 landed)"
    assert [c[1] for c in prod.calls] == ["get", "set"]
    rec = gates.read_prod_head(store)
    assert rec == {
        "head": HEAD,
        "deployed_at": "2026-10-30T12:34:56Z",
        "landed": LANDED,
    }
    gate = {"kind": "sha-in-prod", "args": {"sha": "2222222"}}
    assert evaluate(store, gate, todo_id=1).open


def test_record_accumulates_across_deploys(store: Store, release: ModuleType) -> None:
    prod = FakeProd(store)
    release.record_prod_head("p0", HEAD, transport=prod, landed=LANDED)
    head2 = "fe" * 20
    release.record_prod_head(HEAD, head2, transport=prod, landed=["4444444ddddd"])
    rec = gates.read_prod_head(store)
    assert rec is not None and rec["head"] == head2
    # this deploy's shas first, then the previous head and its landed set
    assert rec["landed"] == ["4444444ddddd", HEAD[:12], *LANDED]
    assert evaluate(
        store, {"kind": "sha-in-prod", "args": {"sha": "1111111"}}, todo_id=1
    ).open


def test_record_caps_landed(store: Store, release: ModuleType) -> None:
    many = [f"{i:012x}" for i in range(release.MAX_LANDED + 50)]
    rec = release.prod_head_record(HEAD, many, None, datetime.now(UTC))
    assert len(rec["landed"]) == release.MAX_LANDED
    assert rec["landed"][0] == many[0]


def test_record_dedupes_and_survives_unreadable_previous(
    store: Store, release: ModuleType
) -> None:
    prev = {"head": HEAD, "landed": [LANDED[0], LANDED[0]]}
    rec = release.prod_head_record(HEAD, [LANDED[0]], prev, datetime.now(UTC))
    assert rec["landed"] == [LANDED[0], HEAD[:12]]
    assert release.parse_prod_head("garbage") is None
    assert release.parse_prod_head("release.prod_head = None (layer=default)") is None
    assert release.parse_prod_head('release.prod_head = {"head":"x"} (layer=db)') == {
        "head": "x"
    }


def test_record_reports_failures_without_raising(
    store: Store, release: ModuleType
) -> None:
    failing = FakeProd(store, fail_set=True)
    line = release.record_prod_head("p", HEAD, transport=failing, landed=LANDED)
    assert line.startswith("prod head not recorded (exit 1): refused: <url>")
    assert gates.read_prod_head(store) is None

    def boom(args: list[str]) -> tuple[int, str, str]:
        raise OSError("no wrapper")

    assert (
        release.record_prod_head("p", HEAD, transport=boom, landed=LANDED)
        == "prod head not recorded: OSError"
    )
    assert release.record_prod_head("p", "", transport=boom).endswith("no head sha")


def test_landed_shas_abbreviates_the_range(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        seen.append(args)
        return HEAD + "\n" + "c" * 40

    monkeypatch.setattr(release, "git", fake_git)
    assert release.landed_shas("aaa", "bbb") == [HEAD[:12], "c" * 12]
    assert seen[0][-1] == "aaa..bbb"
    assert release.landed_shas("", "bbb") == [HEAD[:12], "c" * 12]
    assert seen[1][-1] == "bbb"


def test_real_transport_skipped_without_prod_dsn(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GRIPE_NOTE", "0")
    assert release.record_prod_head("p", HEAD, landed=LANDED) == (
        "prod head not recorded: PRECIS_GRIPE_NOTE=0"
    )
