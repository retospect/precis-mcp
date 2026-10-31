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
from precis.plans.gates import GateVerdict, evaluate
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
    assert res == GateVerdict(False, res.why) and res.why.startswith("not-before")


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


CAND = "cafe0123456789abcdef0123456789abcdef0123"


def _verdicts(store: Store, *rows: dict[str, Any]) -> None:
    psettings.set_setting(gates.CI_VERDICTS_KEY, json.dumps(list(rows)), store=store)


def _row(
    verdict: str, at: str, sha: str = CAND, run_id: str = "777", **extra: Any
) -> dict[str, Any]:
    return {
        "sha": sha[:12],
        "range": f"{HEAD[:12]}..{sha[:12]}",
        "verdict": verdict,
        "run_id": run_id,
        "at": at,
        **extra,
    }


def _tg(rng: str) -> dict[str, Any]:
    return {"kind": "tests-green", "args": {"range": rng}}


def test_tests_green_opens_on_a_green_verdict_before_deploy(store: Store) -> None:
    _record(store)  # prod is at HEAD; CAND has not deployed
    _verdicts(store, _row("green", "2026-10-31T01:00:00Z"))
    res = evaluate(store, _tg(f"{HEAD[:9]}..{CAND[:9]}"), todo_id=1)
    assert res.open and "CI green on cafe01234" in res.why and "run 777" in res.why


def test_tests_green_closes_on_a_red_verdict_with_the_reason(store: Store) -> None:
    _verdicts(store, _row("red", "2026-10-31T01:00:00Z", run_id="888"))
    res = evaluate(store, _tg(CAND[:9]), todo_id=1)
    assert not res.open and "CI red on cafe01234" in res.why and "run 888" in res.why


def test_tests_green_red_verdict_beats_a_deployed_head(store: Store) -> None:
    """A recorded red on the end wins over the prod-head derivation."""
    _record(store)
    _verdicts(store, _row("red", "2026-10-31T01:00:00Z", sha=HEAD))
    assert not evaluate(store, _tg(HEAD[:9]), todo_id=1).open


def test_tests_green_newer_red_beats_older_green(store: Store) -> None:
    _verdicts(
        store,
        _row("green", "2026-10-31T01:00:00Z"),
        _row("red", "2026-10-31T02:00:00Z"),
    )
    assert not evaluate(store, _tg(CAND[:9]), todo_id=1).open


def test_tests_green_newer_green_beats_older_red(store: Store) -> None:
    # list order is not time order: the `at` stamp decides
    _verdicts(
        store,
        _row("green", "2026-10-31T02:00:00Z"),
        _row("red", "2026-10-31T01:00:00Z"),
    )
    assert evaluate(store, _tg(CAND[:9]), todo_id=1).open


def test_tests_green_matches_the_recorded_range(store: Store) -> None:
    # the gate names an end the row's sha does not prefix, but the whole range
    row = _row("green", "2026-10-31T01:00:00Z")
    row["sha"] = "0" * 12
    row["range"] = f"{HEAD[:9]}..{CAND[:9]}"
    _verdicts(store, row)
    assert evaluate(store, _tg(f"{HEAD[:9]}..{CAND[:9]}"), todo_id=1).open


def test_tests_green_other_sha_verdict_does_not_apply(store: Store) -> None:
    _record(store)
    _verdicts(store, _row("green", "2026-10-31T01:00:00Z", sha="d" * 40))
    res = evaluate(store, _tg(CAND[:9]), todo_id=1)
    assert not res.open and "no green deploy covers it" in res.why


def test_tests_green_falls_back_to_prod_head_without_a_verdict(store: Store) -> None:
    _record(store)
    assert evaluate(store, _tg(HEAD[:9]), todo_id=1).open
    other = "9" * 12
    _verdicts(store, _row("green", "2026-10-31T01:00:00Z", sha="d" * 40))
    assert evaluate(store, _tg(HEAD[:9]), todo_id=1).open
    assert not evaluate(store, _tg(other), todo_id=1).open


@pytest.mark.parametrize(
    "raw", ["{not json", '{"sha":"x"}', '[1,"x",{"sha":"cafe0123","verdict":"?"}]']
)
def test_tests_green_garbled_verdicts_fall_back(store: Store, raw: str) -> None:
    _record(store)
    psettings.set_setting(gates.CI_VERDICTS_KEY, raw, store=store)
    assert gates.read_ci_verdicts(store) == []
    assert evaluate(store, _tg(HEAD[:9]), todo_id=1).open
    assert not evaluate(store, _tg(CAND[:9]), todo_id=1).open


@pytest.mark.parametrize("rng", [None, "", "a..", "..main", "HEAD~3..HEAD", 7])
def test_tests_green_bad_args_close(store: Store, rng: object) -> None:
    _record(store)
    res = evaluate(store, {"kind": "tests-green", "args": {"range": rng}}, todo_id=1)
    assert not res.open and res.why.startswith("tests-green")


# ── drc-clean ─────────────────────────────────────────────────────


def _pcb(store: Store, slug: str = "gate-board") -> int:
    """A two-part design with a board row (U1, R1; nets VCC/GND)."""
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {"refdes": "U1", "label": "MCU", "pins": [{"name": "1"}, {"name": "2"}]},
            {"refdes": "R1", "label": "R", "pins": [{"name": "1"}, {"name": "2"}]},
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}],
        connections=[
            {"net": "VCC", "refdes": "U1", "pin": "1"},
            {"net": "VCC", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "2"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    )
    store.pcb_ensure_board(int(ref.id))
    return int(ref.id)


def _review_run(store: Store, ref_id: int, *severities: str) -> str:
    """Record a DRC run the way pcb_review's check leg does
    (check_run.run(record=True) -> check_run.record_run -> the store write),
    one finding per severity."""
    from precis.pcb import check_run
    from precis.pcb.drc import DrcFinding

    findings = [
        DrcFinding(rule=f"rule{i}", severity=sev, where=f"U1.{i}", detail="x")
        for i, sev in enumerate(severities)
    ]
    board_id = store.pcb_ensure_board(ref_id)
    ran = check_run.record_run(
        store, board_id, findings, False, "routed", record=True, claimed_phase=None
    )
    assert ran.run_id is not None
    return ran.run_id


def _drc(board: object) -> dict[str, Any]:
    return {"kind": "drc-clean", "args": {"board": board}}


def test_drc_clean_closed_with_no_run(store: Store) -> None:
    ref_id = _pcb(store)
    res = evaluate(store, _drc(f"pb{ref_id}"), todo_id=1)
    assert res == GateVerdict(False, f"no DRC run yet on pb{ref_id}")


def test_drc_clean_closed_with_the_error_count(store: Store) -> None:
    ref_id = _pcb(store)
    run = _review_run(store, ref_id, "error", "error", "warn")
    res = evaluate(store, _drc(f"pb{ref_id}"), todo_id=1)
    assert not res.open
    assert res.why == f"2 DRC error(s) on pb{ref_id} (run {run[:12]})"


def test_drc_clean_open_when_newest_run_has_no_error(store: Store) -> None:
    ref_id = _pcb(store)
    _review_run(store, ref_id, "error")
    run = _review_run(store, ref_id, "warn")
    res = evaluate(store, _drc(f"pb{ref_id}"), todo_id=1)
    assert res.open, res.why
    assert res.why == f"DRC clean on pb{ref_id} (run {run[:12]}, 1 warning(s))"
    # A clean run (zero findings) is a verdict too, and the newest wins.
    _review_run(store, ref_id, "error")
    assert not evaluate(store, _drc(f"pb{ref_id}"), todo_id=1).open
    _review_run(store, ref_id)
    assert evaluate(store, _drc(f"pb{ref_id}"), todo_id=1).open


def test_drc_clean_closed_when_the_design_changed_since(store: Store) -> None:
    ref_id = _pcb(store)
    run = _review_run(store, ref_id)
    assert evaluate(store, _drc(f"pb{ref_id}"), todo_id=1).open
    assert store.pcb_set_placement(ref_id, {"U1": (5.0, 7.0)}) == 1
    res = evaluate(store, _drc(f"pb{ref_id}"), todo_id=1)
    assert not res.open
    assert (
        res.why == f"pb{ref_id} changed since DRC run {run[:12]}; the check must re-run"
    )


@pytest.mark.parametrize("spell", ["slug", "pcb:slug", "handle", "id"])
def test_drc_clean_resolves_board_spellings(store: Store, spell: str) -> None:
    ref_id = _pcb(store, slug="spelled")
    _review_run(store, ref_id)
    board = {
        "slug": "spelled",
        "pcb:slug": "pcb:spelled",
        "handle": f"PB{ref_id}",
        "id": str(ref_id),
    }[spell]
    res = evaluate(store, _drc(board), todo_id=1)
    assert res.open, res.why
    assert f"pb{ref_id}" in res.why


@pytest.mark.parametrize("board", [None, "", "  ", 7, "pcb:"])
def test_drc_clean_bad_args_close(store: Store, board: object) -> None:
    res = evaluate(store, _drc(board), todo_id=1)
    assert not res.open and res.why.startswith("drc-clean")


def test_drc_clean_unknown_board_closes(store: Store) -> None:
    res = evaluate(store, _drc("no-such-board"), todo_id=1)
    assert not res.open
    assert res.why.startswith("drc-clean:") and "not found" in res.why


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
    assert release.record_ci_verdict("a..b", "b", "green") == (
        "ci verdict not recorded: PRECIS_GRIPE_NOTE=0"
    )


# ── the CI verdict writer ─────────────────────────────────────────

NOW = datetime(2026, 10, 31, 3, 4, 5, tzinfo=UTC)


def test_ci_verdicts_key_matches_registry(release: ModuleType) -> None:
    assert release.CI_VERDICTS_KEY == gates.CI_VERDICTS_KEY
    assert gates.CI_VERDICTS_KEY in psettings.REGISTRY
    assert psettings.REGISTRY[gates.CI_VERDICTS_KEY].env_var is None


def test_record_ci_verdict_writes_the_row_gates_read(
    store: Store, release: ModuleType
) -> None:
    prod = FakeProd(store)
    rng = f"{HEAD[:12]}..{CAND[:12]}"
    line = release.record_ci_verdict(
        CAND, rng, "green", "4242", transport=prod, now=NOW
    )
    assert line == f"ci verdict recorded: {CAND[:9]} green"
    assert [c[1] for c in prod.calls] == ["get", "set"]
    assert gates.read_ci_verdicts(store) == [
        {
            "sha": CAND[:12],
            "range": rng,
            "verdict": "green",
            "run_id": "4242",
            "at": "2026-10-31T03:04:05Z",
        }
    ]
    assert evaluate(store, _tg(f"{HEAD[:9]}..{CAND[:9]}"), todo_id=1).open


def test_record_ci_verdict_red_closes_the_gate(
    store: Store, release: ModuleType
) -> None:
    prod = FakeProd(store)
    release.record_ci_verdict(
        CAND, f"{HEAD[:12]}..{CAND[:12]}", "red", "9", transport=prod
    )
    res = evaluate(store, _tg(CAND[:9]), todo_id=1)
    assert not res.open and "CI red" in res.why and "run 9" in res.why


def test_record_ci_verdict_dedupes_by_sha_keeping_newest(
    store: Store, release: ModuleType
) -> None:
    prod = FakeProd(store)
    other = "e" * 40
    release.record_ci_verdict(CAND, "r1", "red", "1", transport=prod, now=NOW)
    release.record_ci_verdict(other, "r2", "green", "2", transport=prod, now=NOW)
    release.record_ci_verdict(CAND, "r3", "green", "3", transport=prod, now=NOW)
    rows = gates.read_ci_verdicts(store)
    assert [(r["sha"], r["verdict"], r["run_id"]) for r in rows] == [
        (other[:12], "green", "2"),
        (CAND[:12], "green", "3"),
    ]


def test_record_ci_verdict_caps_the_list(release: ModuleType) -> None:
    prev = [
        {"sha": f"{i:012x}", "verdict": "green"} for i in range(release.MAX_CI_VERDICTS)
    ]
    rows = release.ci_verdict_rows(CAND, "r", "red", "5", prev, NOW)
    assert len(rows) == release.MAX_CI_VERDICTS
    assert rows[-1]["sha"] == CAND[:12]
    assert rows[0]["sha"] == f"{1:012x}"  # the oldest fell off


def test_record_ci_verdict_survives_unreadable_previous(
    store: Store, release: ModuleType
) -> None:
    psettings.set_setting(gates.CI_VERDICTS_KEY, "{garbage", store=store)
    assert release.parse_ci_verdicts("garbage") == []
    assert release.parse_ci_verdicts("release.ci_verdicts = [] (layer=default)") == []
    assert release.parse_ci_verdicts('release.ci_verdicts = {"a":1} (layer=db)') == []
    release.record_ci_verdict(CAND, "r", "green", "1", transport=FakeProd(store))
    assert [r["sha"] for r in gates.read_ci_verdicts(store)] == [CAND[:12]]


def test_record_ci_verdict_reports_failures_without_raising(
    store: Store, release: ModuleType
) -> None:
    failing = FakeProd(store, fail_set=True)
    line = release.record_ci_verdict(CAND, "r", "green", "1", transport=failing)
    assert line.startswith("ci verdict not recorded (exit 1): refused: <url>")
    assert gates.read_ci_verdicts(store) == []

    def boom(args: list[str]) -> tuple[int, str, str]:
        raise OSError("no wrapper")

    assert (
        release.record_ci_verdict(CAND, "r", "red", transport=boom)
        == "ci verdict not recorded: OSError"
    )
    assert release.record_ci_verdict("", "r", "red", transport=boom).endswith("no sha")
    assert "not green|red" in release.record_ci_verdict(CAND, "r", "?", transport=boom)
